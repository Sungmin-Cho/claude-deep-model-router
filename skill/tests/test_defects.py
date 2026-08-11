"""Regression tests for defects found in round 1 of adversarial review.

Every test here failed before the corresponding fix. They exist because the
original suite passed 36/36 while five of ten fallback paths silently emitted
a model that had just been declared unavailable — green tests that never
entered the defective branch.

The lesson encoded here: an invariant test must exercise the condition the
invariant is about. `test_i4` asserted "routes name only available models"
without ever marking a model unavailable, so it could not have failed.

Run:  python3 -m pytest skill/tests/ -q
"""

import itertools
import json
import subprocess
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))

from route_task import (  # noqa: E402
    BANDS,
    ROLES,
    TASK_CLASSES,
    Task,
    ValidationError,
    load_config,
    route,
)

CFG = load_config()


def r(**kw):
    kw.setdefault("complexity", 0)
    kw.setdefault("uncertainty", 0)
    kw.setdefault("blast_radius", 0)
    kw.setdefault("reversibility", 0)
    return route(Task(**kw), CFG)


def cli(*args):
    proc = subprocess.run(
        [sys.executable, str(SKILL / "scripts" / "route_task.py"), *args],
        capture_output=True, text=True,
    )
    return proc


# ---------------------------------------------------------------------------
# D1 — fallback must actually fall back  (both reviewers, independently)
# ---------------------------------------------------------------------------

def test_d1_no_fallback_returns_the_model_it_just_declared_unavailable():
    """The原 defect: the degraded binding for a role often names the same model
    as the default binding, so the 'fallback' was a no-op that still recorded a
    downgrade in fallbacks_applied."""
    for runtime, role in itertools.product(("claude_code", "codex"), ROLES):
        out = r(task_class="IMPLEMENTATION", complexity=2, uncertainty=2,
                blast_radius=2, runtime=runtime, unavailable_roles=[role])
        emitted = {out["selected_model"], *(m for m in out["review"]["reviewer_models"] if m)}
        blocked = out["unavailable_models"]
        assert not (emitted & blocked), (
            f"{runtime}/{role}: emitted a model that was unavailable "
            f"({emitted & blocked}); fallbacks={out['fallbacks_applied']}"
        )


def test_d1_fallback_note_is_only_recorded_when_the_model_actually_changed():
    """A recorded fallback that changed nothing is worse than no record — it
    reads as a managed degradation when none happened."""
    for runtime, role in itertools.product(("claude_code", "codex"), ROLES):
        out = r(task_class="IMPLEMENTATION", complexity=2, uncertainty=2,
                blast_radius=2, runtime=runtime, unavailable_roles=[role])
        for note in out["fallbacks_applied"]:
            if "->" not in note:
                continue
            before, after = (s.strip() for s in note.split("->", 1))
            assert before.split(":")[-1].strip() != after, f"no-op fallback recorded: {note}"


def test_d1_unavailable_accepts_model_ids_too():
    ids = {m["id"] for m in CFG["models"].values()}
    for model_id in ids:
        out = r(task_class="IMPLEMENTATION", complexity=2, uncertainty=2,
                blast_radius=2, unavailable_models=[model_id])
        emitted = {out["selected_model"], *(m for m in out["review"]["reviewer_models"] if m)}
        assert model_id not in emitted


def test_d1_multiple_simultaneous_unavailabilities():
    out = r(task_class="ARCHITECTURE", complexity=3, uncertainty=3, blast_radius=3,
            reversibility=2, flags=["financial_sensitive"],
            unavailable_roles=["principal_architect", "reasoning_specialist"])
    emitted = {out["selected_model"], *(m for m in out["review"]["reviewer_models"] if m)}
    assert not (emitted & out["unavailable_models"])
    assert out["fallbacks_applied"]


# ---------------------------------------------------------------------------
# D2 — terminal control-loop gates must actually stop
# ---------------------------------------------------------------------------

def test_d2_retry_exhaustion_is_terminal_not_a_route():
    cap = CFG["retry"]["max_total_implementation_attempts"]
    out = r(task_class="DEBUGGING", complexity=2, uncertainty=1, blast_radius=1,
            prior_failures=cap, prior_models=["worker_fast"])
    assert out["terminal"] == "HUMAN_REQUIRED"
    assert out["selected_model"] is None, "an exhausted task must not get an executable route"


def test_d2_low_routing_confidence_is_terminal_not_a_route():
    out = r(task_class="DEBUGGING", complexity=3, uncertainty=3, blast_radius=1,
            flags=["unknown_root_cause"], prior_failures=2, prior_models=["worker_fast"])
    assert out["routing_confidence"] < CFG["router"]["confidence"]["escalate_below"]
    assert out["terminal"] == "ESCALATE_ROUTING"
    assert out["selected_model"] is None


def test_d2_cli_exits_nonzero_on_a_terminal_state():
    cap = str(CFG["retry"]["max_total_implementation_attempts"])
    p = cli("--class", "DEBUGGING", "--complexity", "2", "--uncertainty", "1",
            "--blast-radius", "1", "--reversibility", "0",
            "--prior-failures", cap, "--prior-models", "worker_fast")
    assert p.returncode != 0, "a terminal state must not exit 0"
    assert "HUMAN_REQUIRED" in p.stdout + p.stderr


# ---------------------------------------------------------------------------
# D3 — judge follows the review band, not the risk band
# ---------------------------------------------------------------------------

def test_d3_confidence_promoted_critical_review_gets_a_judge():
    out = r(task_class="DEBUGGING", complexity=2, uncertainty=2, blast_radius=1,
            flags=["auth_sensitive", "unknown_root_cause"])
    assert out["review"]["band"] == "CRITICAL"
    assert out["risk_band"] != "CRITICAL", "this is the promoted case, not a natural CRITICAL"
    assert out["review"]["judge"] is not None, "a CRITICAL review without a judge is half a control"
    assert out["review"]["judge_model"] is not None, "the judge must also be resolved for availability"


def test_d3_every_critical_review_has_a_resolved_judge():
    for task_class in TASK_CLASSES:
        out = r(task_class=task_class, complexity=3, uncertainty=3,
                blast_radius=3, reversibility=3)
        if out["review"]["band"] == "CRITICAL":
            assert out["review"]["judge_model"] in {m["id"] for m in CFG["models"].values()}


# ---------------------------------------------------------------------------
# D4 — independence: policy requirement vs observed enforcement
# ---------------------------------------------------------------------------

def test_d4_route_emits_the_review_independence_enum():
    out = r(task_class="IMPLEMENTATION", complexity=1)
    assert out["review"]["review_independence"] in {"enforced", "degraded", "not_applicable"}


def test_d4_unknown_isolation_is_never_reported_as_enforced():
    """The most damaging thing this skill can produce is a claim of independence
    it did not structurally get. Absent evidence, it must not claim it."""
    out = r(task_class="IMPLEMENTATION", complexity=2, uncertainty=2, blast_radius=2,
            isolation_available=None)
    assert out["review"]["independence_required"] is True
    assert out["review"]["review_independence"] == "degraded"


def test_d4_isolation_confirmed_yields_enforced():
    out = r(task_class="IMPLEMENTATION", complexity=2, uncertainty=2, blast_radius=2,
            isolation_available=True)
    assert out["review"]["review_independence"] == "enforced"


def test_d4_degraded_critical_requires_human_confirmation():
    out = r(task_class="ARCHITECTURE", complexity=3, uncertainty=3, blast_radius=3,
            reversibility=2, flags=["financial_sensitive"], isolation_available=False)
    assert out["review"]["band"] == "CRITICAL"
    assert out["review"]["review_independence"] == "degraded"
    assert out["requires_human_confirmation"] is True


def test_d4_low_band_review_is_not_applicable_rather_than_degraded():
    out = r(task_class="MECHANICAL")
    assert out["review"]["independence_required"] is False
    assert out["review"]["review_independence"] == "not_applicable"


# ---------------------------------------------------------------------------
# D5 — prior-failure escalation must not silently no-op
# ---------------------------------------------------------------------------

def test_d5_prior_failures_without_prior_models_still_escalates():
    plain = r(task_class="IMPLEMENTATION", complexity=1, uncertainty=1, blast_radius=1)
    out = r(task_class="IMPLEMENTATION", complexity=1, uncertainty=1, blast_radius=1,
            prior_failures=1)
    assert ROLES.index(out["selected_role"]) > ROLES.index(plain["selected_role"]), (
        "a reported failure with no named model must still escalate — the "
        "retry rule is the loop-prevention control"
    )


def test_d5_prior_models_accepts_concrete_model_ids():
    """selected_model is emitted as a model id, so feeding it back must work."""
    out = r(task_class="IMPLEMENTATION", complexity=1, uncertainty=1, blast_radius=1,
            prior_failures=1, prior_models=["gpt-5.6-luna"])
    assert ROLES.index(out["selected_role"]) >= ROLES.index("worker_balanced")
    assert any("escalated above" in n for n in out["notes"])


def test_d5_unknown_prior_model_is_rejected_not_ignored():
    with pytest.raises(ValidationError):
        r(task_class="IMPLEMENTATION", complexity=1, prior_failures=1,
          prior_models=["not-a-real-model"])


# ---------------------------------------------------------------------------
# D6 — config is the single source of truth
# ---------------------------------------------------------------------------

def test_d6_taxonomy_is_derived_from_config_not_duplicated():
    assert BANDS == sorted(CFG["router"]["bands"], key=lambda b: CFG["router"]["bands"][b]["ordinal"])
    assert ROLES == CFG["role_tiers"]
    assert set(TASK_CLASSES) == set(CFG["worker_selection"])


def test_d6_every_configured_override_has_a_consumer():
    """A rule declared in config and consumed nowhere is a promise the system
    does not keep."""
    exercised = set()
    for entry in CFG["overrides"]:
        name = entry["name"]
        probe = _probe_for_override(entry)
        if probe is None:
            continue
        out = r(**probe)
        if name in out["band_overrides_applied"] or out.get("route_path") == entry.get("effect", {}).get("route"):
            exercised.add(name)
    declared = {e["name"] for e in CFG["overrides"]}
    assert declared - exercised == set(), f"overrides with no observable effect: {declared - exercised}"


def _probe_for_override(entry):
    """Build a minimal task that should trigger exactly this override."""
    flags = _flags_in(entry["when"])
    if not flags:
        return None
    task = dict(task_class="MECHANICAL", complexity=0, uncertainty=0,
                blast_radius=0, reversibility=0, flags=sorted(flags))
    if "reversibility" in json.dumps(entry["when"]):
        task["reversibility"] = 3
    return task


def _flags_in(node):
    out = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "flag":
                out.add(value)
            elif key == "any_flag_in":
                out |= set(CFG["flags"][value])
            else:
                out |= _flags_in(value)
    elif isinstance(node, list):
        for item in node:
            out |= _flags_in(item)
    return out


def test_d6_review_disagreement_routes_to_the_disagreement_path():
    out = r(task_class="MECHANICAL", flags=["review_disagreement"])
    assert out["route_path"] == "disagreement"
    assert out["review"]["judge"] is not None


def test_d6_every_declared_flag_has_an_effect_or_a_documented_role():
    """Elevating flags must elevate something.

    The probe starts from the lowest possible task so a band floor is
    observable — measuring against an already-MEDIUM baseline would hide a
    MEDIUM floor and report a working rule as inert. A flag whose effect is
    genuinely conditional must say so in `conditional_flags`, which is what
    separates a deliberate combination rule from one nobody wired up.
    """
    conditional = CFG.get("conditional_flags", {})
    inert = []
    base = r(task_class="MECHANICAL")
    for flag in CFG["flags"]["elevating"]:
        probe = [flag] + list(conditional.get(flag, {}).get("requires", []))
        with_flag = r(task_class="MECHANICAL", flags=probe)
        changed = (
            flag in with_flag["band_overrides_applied"]
            or with_flag["risk_band"] != base["risk_band"]
            or with_flag["selected_role"] != base["selected_role"]
            or with_flag["selected_effort"] != base["selected_effort"]
            or with_flag["review"]["band"] != base["review"]["band"]
            or with_flag["routing_confidence"] != base["routing_confidence"]
            or with_flag.get("route_path") != base.get("route_path")
        )
        if not changed:
            inert.append(flag)
    assert inert == [], f"elevating flags with no observable effect: {inert}"


def test_d6_conditional_flags_actually_fire_in_combination():
    for flag, spec in CFG.get("conditional_flags", {}).items():
        alone = r(task_class="MECHANICAL", flags=[flag])
        combined = r(task_class="MECHANICAL", flags=[flag, *spec["requires"]])
        assert combined["risk_band"] != alone["risk_band"] or combined["band_overrides_applied"], (
            f"{flag} declares a conditional effect that does not fire with {spec['requires']}"
        )


# ---------------------------------------------------------------------------
# D7 — input validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    {"task_class": "IMPLEMENTATION", "complexity": 1, "uncertainty": 1,
     "blast_radius": 1, "reversibility": 0, "reasoning_centric": "false"},
    {"task_class": "IMPLEMENTATION", "complexity": 1, "uncertainty": 1,
     "blast_radius": 1, "reversibility": 0, "runtime": "not-a-runtime"},
    {"task_class": "IMPLEMENTATION", "complexity": True, "uncertainty": 1,
     "blast_radius": 1, "reversibility": 0},
    {"task_class": "IMPLEMENTATION", "complexity": 1, "uncertainty": 1,
     "blast_radius": 1, "reversibility": 0, "flags": ["not_a_known_flag"]},
    {"task_class": "IMPLEMENTATION", "complexity": 1, "uncertainty": 1,
     "blast_radius": 1, "reversibility": 0, "unavailable_roles": ["not_a_role"]},
    {"task_class": "IMPLEMENTATION", "complexity": 1, "uncertainty": 1,
     "blast_radius": 1, "reversibility": 0, "flags": "auth_sensitive"},
])
def test_d7_invalid_input_is_rejected(payload):
    with pytest.raises(ValidationError):
        route(Task(**payload), CFG)


def test_d7_cli_rejects_bad_json_cleanly_without_a_traceback():
    p = cli("--json", '{"task_class":"IMPLEMENTATION","complexity":1,"uncertainty":1,'
                      '"blast_radius":1,"reversibility":0,"runtime":"not-a-runtime"}')
    assert p.returncode != 0
    assert "Traceback" not in p.stderr, "a bad input should not surface a raw traceback"
    assert "runtime" in (p.stdout + p.stderr)


def test_d7_bridge_down_is_a_declared_flag_not_a_magic_string():
    known = set().union(*(set(v) for v in CFG["flags"].values()))
    assert "bridge_down" in known, "the only correct way to select a degraded binding must be discoverable"


# ---------------------------------------------------------------------------
# D8 — documentation must not drift from the registry
# ---------------------------------------------------------------------------

def test_d8_model_ids_appear_only_in_the_registry():
    """The contract says identifiers live in config and nowhere else. Copies in
    prose are guaranteed to drift after the next registry change."""
    ids = {m["id"] for m in CFG["models"].values()}
    offenders = {}
    for path in sorted((SKILL / "references").glob("*.md")):
        text = path.read_text()
        hits = sorted(i for i in ids if i in text)
        if hits:
            offenders[path.name] = hits
    assert offenders == {}, f"model ids duplicated outside the registry: {offenders}"


def test_d8_skill_md_does_not_hardcode_model_ids():
    ids = {m["id"] for m in CFG["models"].values()}
    text = (SKILL / "SKILL.md").read_text()
    assert not [i for i in ids if i in text]


# ---------------------------------------------------------------------------
# D9 — no tautological assertions
# ---------------------------------------------------------------------------

def test_d9_no_test_file_contains_a_tautological_assert():
    """`assert X or True` can never fail. It reads as coverage and is not."""
    for path in sorted((SKILL / "tests").glob("*.py")):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("assert ") and stripped.endswith(" or True"):
                pytest.fail(f"{path.name}:{lineno} tautological assertion: {stripped}")


def test_d9_parity_holds_including_the_native_effort_spelling():
    """The original parity test ended in `or True`, so the native-effort claim
    in its own docstring was never checked."""
    kw = dict(task_class="DEBUGGING", complexity=2, uncertainty=3,
              blast_radius=2, reversibility=1, flags=["auth_sensitive"])
    a = r(runtime="claude_code", **kw)
    b = r(runtime="codex", **kw)
    assert a["selected_role"] == b["selected_role"]
    assert a["selected_effort"] == b["selected_effort"]
    assert a["review"]["band"] == b["review"]["band"]
    claude_map = CFG["effort_map"]["claude_code"]
    codex_map = CFG["effort_map"]["codex"]
    assert a["selected_effort_native"] == claude_map[a["selected_effort"]]
    assert b["selected_effort_native"] == codex_map[b["selected_effort"]]
