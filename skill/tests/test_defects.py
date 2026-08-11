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

def emitted_models(out):
    """Every concrete model the route hands a consumer."""
    return {m for m in [
        out["selected_model"], out["review"]["judge_model"],
        *out["review"]["reviewer_models"],
    ] if m}


# Routes chosen so that between them every role actually reaches the resolver.
# The earlier version used one fixed route where worker_fast and
# principal_architect were never resolved, so four of ten (role x runtime)
# combinations were asserted about without ever being exercised.
ROLE_FORCING_ROUTES = {
    "worker_fast": dict(task_class="MECHANICAL"),
    "worker_balanced": dict(task_class="IMPLEMENTATION", complexity=2, uncertainty=2, blast_radius=1),
    "senior_engineer": dict(task_class="IMPLEMENTATION", complexity=3, uncertainty=1,
                            blast_radius=3, reversibility=2),
    "reasoning_specialist": dict(task_class="INVESTIGATION", complexity=2, uncertainty=2,
                                 blast_radius=2, reasoning_centric=True),
    "principal_architect": dict(task_class="ARCHITECTURE", complexity=3, uncertainty=3,
                                blast_radius=3, reversibility=2, flags=["financial_sensitive"]),
}


def test_d1_every_role_is_actually_resolved_by_its_forcing_route():
    """Guards the guard: if a route stops reaching its role, the fallback tests
    below would pass vacuously without anyone noticing."""
    for role, probe in ROLE_FORCING_ROUTES.items():
        out = r(**probe)
        touched = {out["selected_role"], *out["review"]["reviewers"], out["review"]["judge"]}
        assert role in touched, f"{role} is never resolved by its probe route"


def test_d1_no_fallback_returns_the_model_it_just_declared_unavailable():
    """The original defect: the degraded binding for a role often names the same
    model as the default binding, so the 'fallback' was a no-op that still
    recorded a downgrade in fallbacks_applied."""
    for runtime, (role, probe) in itertools.product(("claude_code", "codex"),
                                                    ROLE_FORCING_ROUTES.items()):
        out = r(**probe, runtime=runtime, unavailable_roles=[role])
        blocked = set(out["unavailable_models"])
        leak = emitted_models(out) & blocked
        assert not leak, (
            f"{runtime}/{role}: emitted an unavailable model ({leak}); "
            f"fallbacks={out['fallbacks_applied']}"
        )


def test_d1_fallback_note_is_only_recorded_when_the_model_actually_changed():
    """A recorded fallback that changed nothing is worse than no record — it
    reads as a managed degradation when none happened."""
    saw_a_real_fallback = False
    for runtime, (role, probe) in itertools.product(("claude_code", "codex"),
                                                    ROLE_FORCING_ROUTES.items()):
        out = r(**probe, runtime=runtime, unavailable_roles=[role])
        for note in out["fallbacks_applied"]:
            if "->" not in note:
                continue
            saw_a_real_fallback = True
            before, after = (s.strip() for s in note.split("->", 1))
            assert before.split(":")[-1].strip() != after, f"no-op fallback recorded: {note}"
    assert saw_a_real_fallback, "the probe set never triggered a fallback — assertion was vacuous"


def test_d1_bridge_down_never_crosses_the_provider_boundary():
    """When the bridge is down the other family is unreachable by definition,
    so naming a model from it produces a route that cannot be executed."""
    families = {m["id"]: m["family"] for m in CFG["models"].values()}
    local = {"claude_code": "claude", "codex": "openai"}
    for runtime, (role, probe) in itertools.product(("claude_code", "codex"),
                                                    ROLE_FORCING_ROUTES.items()):
        for unavailable in ([], [role]):
            kw = dict(probe)
            kw["flags"] = list(kw.get("flags", [])) + ["bridge_down"]
            out = r(**kw, runtime=runtime, unavailable_roles=unavailable)
            foreign = {m for m in emitted_models(out) if families[m] != local[runtime]}
            assert not foreign, (
                f"{runtime}/bridge_down/{role} unavailable={unavailable}: "
                f"emitted unreachable-family model(s) {sorted(foreign)}"
            )


def test_d1_a_failed_model_is_never_re_emitted_under_a_new_role():
    """Role-tier escalation alone is not enough: in a degraded binding the top
    roles collapse onto one model, so 'escalating' re-ran what just failed
    while recording a promotion that never happened."""
    for runtime in ("claude_code", "codex"):
        for prior in ("worker_balanced", "senior_engineer", "reasoning_specialist"):
            out = r(task_class="IMPLEMENTATION", complexity=2, uncertainty=1, blast_radius=1,
                    runtime=runtime, flags=["bridge_down"],
                    prior_failures=1, prior_models=[prior])
            if out["terminal"]:
                continue          # failing closed is an acceptable outcome
            failed = set(out["excluded_prior_failures"])
            leak = emitted_models(out) & failed
            assert not leak, f"{runtime}/{prior}: re-emitted the failed model {leak}"


def test_d1_unavailable_accepts_model_ids_too():
    ids = {m["id"] for m in CFG["models"].values()}
    for model_id in ids:
        out = r(task_class="IMPLEMENTATION", complexity=2, uncertainty=2,
                blast_radius=2, unavailable_models=[model_id])
        assert model_id not in emitted_models(out)


def test_d1_multiple_simultaneous_unavailabilities():
    out = r(task_class="ARCHITECTURE", complexity=3, uncertainty=3, blast_radius=3,
            reversibility=2, flags=["financial_sensitive"],
            unavailable_roles=["principal_architect", "reasoning_specialist"])
    assert not (emitted_models(out) & set(out["unavailable_models"]))
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


def test_d3_every_critical_review_either_seats_a_judge_or_says_it_cannot():
    """A judge must be able to adjudicate the reviewers it sits above. When the
    implementer is already the architect there is no higher authority in the
    ladder, so the honest outcome is to say so and hand adjudication to a
    human — not to seat one of the disputing parties as its own judge."""
    known = {m["id"] for m in CFG["models"].values()}
    for task_class in TASK_CLASSES:
        out = r(task_class=task_class, complexity=3, uncertainty=3,
                blast_radius=3, reversibility=3)
        rv = out["review"]
        if rv["band"] != "CRITICAL" or out["terminal"]:
            continue
        if rv["judge_unavailable"]:
            assert rv["judge"] is None
            assert out["requires_human_confirmation"] is True
            assert "human must resolve" in out["rationale"]
        else:
            assert rv["judge_model"] in known


def test_d11_no_seat_is_held_twice():
    """Worker, every reviewer, and the judge are distinct models. The judge was
    allocated after de-confliction ran and never checked against it, so an
    adjudicator could be the same model as a reviewer whose disagreement it was
    brought in to settle."""
    checked = 0
    for out in _sweep():
        rv = out["review"]
        if out["terminal"] or not rv["independence_required"]:
            continue
        seats = [out["selected_model"], *rv["reviewer_models"], rv["judge_model"]]
        filled = [x for x in seats if x]
        checked += 1
        assert len(filled) == len(set(filled)), (
            f"{out['task_class']}/{rv['band']}: a model holds more than one seat: {seats}"
        )
    assert checked, "the sweep produced no dispatchable independent route"


def test_d11_judge_is_never_weaker_than_the_reviewers_it_adjudicates():
    for out in _sweep():
        rv = out["review"]
        if out["terminal"] or not rv["judge"]:
            continue
        floor = max((ROLES.index(x) for x in rv["reviewers"]), default=0)
        assert ROLES.index(rv["judge"]) >= floor, (
            f"judge {rv['judge']} sits below reviewers {rv['reviewers']}"
        )


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


def test_d4_capability_attestation_alone_is_only_planned_not_enforced():
    """A route is computed before any reviewer runs, so the caller saying
    isolation is *possible* cannot establish that it *happened*. Treating the
    attestation as proof let the flag alone clear the CRITICAL human gate."""
    out = r(task_class="IMPLEMENTATION", complexity=2, uncertainty=2, blast_radius=2,
            isolation_available=True)
    assert out["review"]["review_independence"] == "planned"


def test_d4_only_post_dispatch_evidence_yields_enforced():
    out = r(task_class="IMPLEMENTATION", complexity=2, uncertainty=2, blast_radius=2,
            isolation_available=True,
            isolation_evidence=["session-a1b2", "session-c3d4"])
    assert out["review"]["review_independence"] == "enforced"


def test_d4_evidence_must_be_distinct_per_reviewer():
    """Two reviewers in the same session is precisely the leak the requirement
    exists to prevent, so a repeated identifier must not count."""
    out = r(task_class="IMPLEMENTATION", complexity=2, uncertainty=2, blast_radius=2,
            isolation_available=True, isolation_evidence=["same", "same"])
    assert out["review"]["review_independence"] != "enforced"


def test_d4_confirmed_absence_is_distinguished_from_no_evidence():
    """`unavailable` is positive evidence that isolation cannot be achieved;
    `degraded` is the absence of evidence either way. Collapsing them makes a
    confirmed gap indistinguishable from an unchecked one."""
    unknown = r(task_class="IMPLEMENTATION", complexity=2, uncertainty=2, blast_radius=2)
    confirmed_absent = r(task_class="IMPLEMENTATION", complexity=2, uncertainty=2,
                         blast_radius=2, isolation_available=False)
    assert unknown["review"]["review_independence"] == "degraded"
    assert confirmed_absent["review"]["review_independence"] == "unavailable"


@pytest.mark.parametrize("isolation,evidence", [
    (None, []), (False, []), (True, []), (True, ["only-one"]),
])
def test_d4_critical_without_enforced_independence_requires_a_human(isolation, evidence):
    out = r(task_class="ARCHITECTURE", complexity=3, uncertainty=3, blast_radius=3,
            reversibility=2, flags=["financial_sensitive"],
            isolation_available=isolation, isolation_evidence=evidence)
    assert out["review"]["band"] == "CRITICAL"
    assert out["review"]["review_independence"] != "enforced"
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


# ---------------------------------------------------------------------------
# D10 — an implementer must never review its own MODEL
# ---------------------------------------------------------------------------
#
# The first version of this check compared role labels, and the sweep never set
# runtime or flags — so it only ever ran the default binding, where the role
# check already held. Under a degraded single-provider binding several roles
# resolve to one model, and there the worker, both "independent" reviewers and
# the judge were all the same model while the route reported a substitution and
# cleared the human gate.
#
# Roles are labels. Models are what actually runs. The invariant is about
# models, so the test is too — and it sweeps the bindings where roles collapse.

DIMENSION_SWEEP = [(0, 0, 0, 0), (2, 2, 2, 0), (3, 2, 3, 1), (3, 3, 3, 3), (2, 2, 2, 2)]
BINDING_SWEEP = [
    dict(runtime="claude_code", flags=[]),
    dict(runtime="claude_code", flags=["bridge_down"]),
    dict(runtime="codex", flags=[]),
    dict(runtime="codex", flags=["bridge_down"]),
]

# Scarcity is what forces roles onto shared models, and the previous sweep
# never created any: it varied runtime and bridge_down but never marked a model
# unavailable, so every collision the code could actually produce lived outside
# the sweep. Withholding models is the cheapest way to reach that state.
_MODEL_IDS = sorted(m["id"] for m in CFG["models"].values())
SCARCITY_SWEEP = [
    [],
    [_MODEL_IDS[0]],
    _MODEL_IDS[:2],
    _MODEL_IDS[:3],
    _MODEL_IDS[1:4],
    _MODEL_IDS[2:5],
]


def _sweep():
    for task_class in TASK_CLASSES:
        for c, u, b, rev in DIMENSION_SWEEP:
            for reasoning in (False, True):
                for binding in BINDING_SWEEP:
                    for scarce in SCARCITY_SWEEP:
                        try:
                            yield r(task_class=task_class, complexity=c, uncertainty=u,
                                    blast_radius=b, reversibility=rev,
                                    reasoning_centric=reasoning,
                                    unavailable_models=list(scarce), **binding)
                        except ValidationError:
                            # Every candidate withheld — failing closed is a
                            # correct outcome, not a route to inspect.
                            continue


def test_d10_sweep_actually_reaches_the_conditions_it_asserts_about():
    """Guards the guard, and it has to guard three separate things.

    Each round of review found the same failure: an invariant test that never
    entered the branch where the invariant could break. A sweep that produces
    no substitutions, no compromised routes, and no compensations proves
    nothing about any of them.
    """
    substituted = compromised = compensated = 0
    for out in _sweep():
        if out["review"].get("self_review_avoided"):
            substituted += 1
        if out["review"].get("independence_compromised"):
            compromised += 1
        if out["fallback_compensations_applied"]:
            compensated += 1
    assert substituted, "the sweep never needed a reviewer substitution"
    assert compromised, "the sweep never reached an unresolvable collision"
    assert compensated, "the sweep never exercised a fallback compensation"


def test_d10_worker_model_is_never_one_of_its_own_reviewers():
    offenders = []
    for out in _sweep():
        rv = out["review"]
        if not rv["independence_required"] or out["terminal"]:
            continue
        if out["selected_model"] and out["selected_model"] in rv["reviewer_models"]:
            offenders.append((out["task_class"], rv["band"], out["selected_model"]))
    assert offenders == [], f"worker model also reviews its own output in: {sorted(set(offenders))[:5]}"


def test_d10_two_reviewers_are_never_the_same_model():
    offenders = []
    for out in _sweep():
        rv = out["review"]
        if not rv["independence_required"] or out["terminal"]:
            continue
        models = [m for m in rv["reviewer_models"] if m]
        if len(models) != len(set(models)):
            offenders.append((out["task_class"], rv["band"], tuple(models)))
    assert offenders == [], f"duplicate reviewer models in: {sorted(set(offenders))[:5]}"


def test_d10_unresolvable_collision_gates_rather_than_merely_disclosing():
    """Disclosure is not a control. A route whose reviewers could not be given
    distinct models must stop, not ship with a boolean set and hope the
    consumer reads it."""
    seen = 0
    for out in _sweep():
        rv = out["review"]
        if not rv.get("independence_compromised"):
            continue
        seen += 1
        assert rv["review_independence"] == "unavailable"
        assert out["requires_human_confirmation"] is True
        assert "could not be established" in out["rationale"]
    assert seen, "no compromised route in the sweep — the assertion was vacuous"


def test_d10_compensation_path_cannot_bypass_deconfliction():
    """The second-review compensation appends a reviewer and flips
    `independent` to true after the band's own de-confliction has run. Checking
    the invariant mid-pipeline protected only the paths that existed when the
    check was written; this asserts the post-condition at the emit boundary."""
    checked = 0
    for out in _sweep():
        rv = out["review"]
        if not out["fallback_compensations_applied"] or not rv["independence_required"]:
            continue
        if out["terminal"] or rv.get("independence_compromised"):
            continue
        checked += 1
        models = [m for m in rv["reviewer_models"] if m]
        assert out["selected_model"] not in models
        assert len(models) == len(set(models))
    assert checked, "no compensated independent route in the sweep"


def test_d10_every_recorded_substitution_actually_changed_the_model():
    """The defect this whole file exists to prevent, applied to the newest
    code: a recorded avoidance that avoided nothing."""
    seen = 0
    for out in _sweep():
        for sub in (out["review"].get("self_review_avoided") or []):
            seen += 1
            assert sub["replaced"] != sub["with"]
            assert sub["with"] in ROLES
            assert sub["reason"]
    assert seen, "no substitution in the sweep — the assertion was vacuous"


def test_d10_substitute_is_never_weaker_than_the_implementer_where_one_exists():
    """Restores a guard that was dropped when D10 was rewritten. A reviewer
    below the implementer's tier cannot supply the check the implementer could
    not perform on itself — but scarcity can leave no stronger option, and
    taking a weaker distinct reviewer beats taking the implementer itself. The
    assertion is therefore about disclosure, not prohibition."""
    for out in _sweep():
        rv = out["review"]
        for sub in (rv.get("self_review_avoided") or []):
            if ROLES.index(sub["with"]) < ROLES.index(out["selected_role"] or sub["with"]):
                assert out["cross_family_review"] is not None
                assert "Reviewer slot substituted" in out["rationale"]


def test_d10_self_review_avoided_is_always_a_list():
    """Documented as a list; it used to emit null when nothing was substituted,
    so a consumer iterating it as documented hit a TypeError."""
    for out in _sweep():
        assert isinstance(out["review"]["self_review_avoided"], list)


def test_d10_substitution_is_disclosed_in_the_rationale():
    for out in _sweep():
        if out["review"].get("self_review_avoided"):
            assert "Reviewer slot substituted" in out["rationale"]
            return
    pytest.fail("no substitution occurred in the sweep — the assertion was vacuous")


def test_d10_low_band_self_review_is_intentional_not_a_bug():
    """LOW explicitly does not ask for independence, so worker_fast reviewing
    worker_fast there is the documented design, not the defect above."""
    out = r(task_class="MECHANICAL")
    assert out["review"]["independence_required"] is False
    assert out["selected_role"] in out["review"]["reviewers"]
