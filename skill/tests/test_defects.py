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
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))

from route_task import (  # noqa: E402
    BANDS,
    ROLES,
    TASK_CLASSES,
    Policy,
    Resolver,
    Task,
    ValidationError,
    load_config,
    route,
)

CFG = load_config()


def _task(**kw):
    kw.setdefault("complexity", 0)
    kw.setdefault("uncertainty", 0)
    kw.setdefault("blast_radius", 0)
    kw.setdefault("reversibility", 0)
    return Task(**kw)


def r(**kw):
    return route(_task(**kw), CFG)


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
    # Round 11 moved this probe. The original (`DEBUGGING 3/3/1`, two failures,
    # one named alias) now hits the retry ceiling FIRST — the reconstruction
    # correctly places attempt 2 at the top tier, so `HUMAN_REQUIRED` fires
    # before confidence is consulted. That is the right answer for that input
    # and not what this test is about, so the probe was moved to one where
    # confidence is the binding constraint. The subject is unchanged: a route
    # below the escalation floor is terminal, not an executable route.
    out = r(task_class="MECHANICAL", complexity=0, uncertainty=2, blast_radius=0,
            flags=["unknown_root_cause", "bridge_down"], prior_failures=2,
            prior_models=["gpt-5.6-luna", "claude-sonnet-5"])
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


def test_d11_judge_is_never_weaker_than_any_party_it_adjudicates():
    for out in _sweep():
        rv = out["review"]
        if out["terminal"] or not rv["judge"]:
            continue
        # Round 7. This was the pre-round-6 spelling, left asserting the very
        # property round 6 disproved: role ordinals are not capability. It gave
        # false assurance (a reader sees judge strength covered twice) and it
        # opposed the corrected invariant — the moment scarcity produced an
        # ordinal/tier inversion, a CORRECT seating would have failed here.
        tier = {m["id"]: m["capability_tier"] for m in CFG["models"].values()}
        parties = [m for m in [out["selected_model"], *rv["reviewer_models"]] if m]
        assert tier[rv["judge_model"]] >= max(tier[m] for m in parties), (
            f"judge {rv['judge_model']} is outranked by a party it adjudicates: {parties}"
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


def _sweep(with_task=False):
    for task_class in TASK_CLASSES:
        for c, u, b, rev in DIMENSION_SWEEP:
            for reasoning in (False, True):
                for binding in BINDING_SWEEP:
                    for scarce in SCARCITY_SWEEP:
                        try:
                            kwargs = dict(task_class=task_class, complexity=c, uncertainty=u,
                                          blast_radius=b, reversibility=rev,
                                          reasoning_centric=reasoning,
                                          unavailable_models=list(scarce), **binding)
                            out = r(**kwargs)
                            yield (_task(**kwargs), out) if with_task else out
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
    # Both sweeps. `_sweep()` never sets a flag, so it never takes the
    # disagreement route, so it never seats a judge — and the judge retry is
    # exactly where round 6 found a substitution record outliving the seat it
    # described. A regression test that cannot reach the path is the failure
    # mode this whole file is named for.
    for out in itertools.chain(_sweep(), _disagreement_sweep()):
        rv = out["review"]
        for sub in (rv.get("self_review_avoided") or []):
            seen += 1
            assert sub["with"] in rv["reviewers"], (
                f"record names reviewer {sub['with']}, seats hold {rv['reviewers']}")
            assert sub["replaced"] != sub["with"]
            assert sub["with"] in ROLES
            assert sub["reason"]
            # Role labels are not models. Under a degraded binding several
            # roles collapse onto one id, so a substitution between two
            # aliases can rename the seat without changing who sits in it —
            # which is the exact defect this file is named after. The seat the
            # substitute now holds must differ from the implementer's.
            substitute_model = _peek_model(out, sub["with"])
            if substitute_model is not None and out["selected_model"] is not None:
                assert substitute_model != out["selected_model"], (
                    f"{out['task_class']}/{rv['band']}: substituted "
                    f"{sub['replaced']} -> {sub['with']} but both resolve to "
                    f"{substitute_model}"
                )
    assert seen, "no substitution in the sweep — the assertion was vacuous"


def test_d10_a_below_tier_substitute_is_only_taken_when_nothing_better_is_free():
    """Scarcity can leave no reviewer at or above the implementer's tier, and a
    weaker distinct reviewer still beats the implementer reviewing itself. What
    must not happen is taking a weaker one while a stronger one sits unused.

    The previous version of this test asserted `cross_family_review is not
    None` (always true, it is a bool) and that the rationale contains a string
    `explain()` emits unconditionally on that branch — the same shape as the
    `or True` tautology `test_d9` forbids, so the invariant in its own name was
    never checked.

    Round 6 found the replacement still could not fail. It asked "was a
    stronger role free?" through `_peek_model`, which only knows roles that
    appear in the emitted route — so an *unseated* candidate, the only kind
    that could have been free, returned `None` and was skipped. 1,829 of the
    checks were skipped that way and the 1,512 that ran merely rediscovered
    models already in `used`. The question is about roles the route did not
    take, so it has to be asked of the resolver, not of the answer.
    """
    checked = free_seen = 0
    for task, out in _sweep(with_task=True):
        rv = out["review"]
        if out["terminal"] or not out["selected_role"]:
            continue
        # Only substituted seats are in scope. A band that configures a
        # cheaper reviewer than the implementer (LOW does, by design) is not a
        # substitution and asserting over it tests the policy, not the code.
        seated = dict(zip(rv["reviewers"], rv["reviewer_models"]))
        tier = {m["id"]: m["capability_tier"] for m in CFG["models"].values()}
        resolver = Resolver(task, Policy.of(CFG))
        worker_tier = tier[out["selected_model"]]
        weak = [seated[sub["with"]] for sub in rv["self_review_avoided"]
                if seated.get(sub["with"]) and tier[seated[sub["with"]]] < worker_tier]
        if not weak:
            continue
        checked += 1
        used = {out["selected_model"], *(m for m in rv["reviewer_models"] if m),
                rv["judge_model"]} - {None}
        for role in ROLES:
            if role in rv["reviewers"]:
                continue
            model = resolver.peek(role)          # asks the resolver, not the route
            if model is None or model in used:
                # A candidate whose model is already held could not have been
                # taken anyway. Counting it was counting the rediscoveries this
                # test was rewritten to stop counting.
                continue
            free_seen += 1
            assert model in used or tier[model] < worker_tier, (
                f"{out['task_class']}/{rv['band']}: reviewed at {weak} while "
                f"{role} -> {model} (tier {tier[model]}) sat unused"
            )
    assert checked, "no below-tier substitution in the sweep — the assertion was vacuous"
    assert free_seen, "no unseated candidate was ever resolved — the check was skipped"


# ---------------------------------------------------------------------------
# D13 — round 7: what the round-6 fixes broke, and what verified nothing
# ---------------------------------------------------------------------------

def test_d13_skill_md_stays_inside_its_line_budget():
    """The handoff sets 500 lines for the entry point, and every round adds
    contract to it. Left unenforced the budget is a comment, and the file that
    both runtimes load on every invocation grows without anyone deciding to."""
    lines = (SKILL / "SKILL.md").read_text().splitlines()
    assert len(lines) <= 500, (
        f"SKILL.md is {len(lines)} lines against a 500-line budget; move detail "
        f"into references/ rather than raising the number")


def test_d13_the_judge_retry_never_seats_the_implementer_as_its_own_reviewer():
    """Round 6 relaxed the retry at LOW on the reasoning that LOW permits
    self-review. LOW's exemption is about the reviewer the BAND CONFIGURED
    landing on the implementer, not a licence for the router to move it there
    to free a model for the judge. With the relaxation in place this exact
    route traded a distinct, stronger reviewer for the implementer itself,
    recorded nothing (LOW's depth floor is 0, so the shortfall gate cannot fire
    either), and flipped requires_human_confirmation from true to false."""
    out = r(task_class="MIGRATION", flags=["review_disagreement"], runtime="claude_code",
            unavailable_models=["gpt-5.6-luna", "claude-haiku-4-5-20251001",
                                "claude-sonnet-5", "claude-fable-5", "gpt-5.6-sol"])
    rv = out["review"]
    assert rv["band"] == "LOW" and out["route_path"] == "disagreement", "probe drifted"
    assert out["selected_model"] not in rv["reviewer_models"], (
        f"implementer {out['selected_model']} was re-seated as its own reviewer "
        f"to free a judge: reviewers={rv['reviewer_models']} judge={rv['judge_model']}")
    assert rv["judge_unavailable"] and out["requires_human_confirmation"], (
        "with two models and three seats there is no independent judge; saying "
        "otherwise buys the seat with the review")


def test_d13_a_substitution_record_that_outlives_its_seat_is_raised_not_dropped():
    """Round 7. The emit boundary used to filter records down to those matching
    the final roster, which made every assertion about that match true by
    construction — the round-6 defect shape, reintroduced by the round-6 fix.
    Two reviewers proved it independently: `_restate` -> `[]` left 114 green.

    So `_restate` is tested directly, and the boundary raises instead of
    correcting."""
    from route_task import _restate
    rec = [{"replaced": "reasoning_specialist", "with": "principal_architect", "reason": "x"}]
    # untouched: the record does not point at the re-seated role
    assert _restate(rec, "senior_engineer", "worker_balanced") == rec
    # re-pointed: the seat it named now holds someone else
    assert _restate(rec, "principal_architect", "worker_balanced") == [
        {"replaced": "reasoning_specialist", "with": "worker_balanced", "reason": "x"}]
    # dropped: the displaced role came back, so nothing was substituted
    assert _restate(rec, "principal_architect", "reasoning_specialist") == []
    assert _restate(None, "a", "b") == []


def test_d13_a_retry_escalates_to_a_stronger_model_not_merely_a_higher_role():
    """Round 7. Moving the role up one is not an escalation; moving to a
    stronger MODEL is. Under scarcity the next role along resolved to a peer at
    the same capability_tier, so the retry re-ran at the strength that had just
    failed while the note claimed a promotion and a tier-3 model sat unused.
    The invariant sweep missed it because it varied `prior_models` but left
    `prior_failures` at zero, so the branch was never entered.

    Round 8 found that the escape hatch below — `if terminal: return` — was
    excusing the very defect the test names. `_promote_above` measured its floor
    with `resolver.peek(failed)`, which returns the failed model's REPLACEMENT
    because the failure has already been excluded, so the floor was inflated to
    a tier nothing had run at and the simplest documented retry
    (`--prior-models senior_engineer`) terminated with "no model is stronger"
    while an untried tier-3 model sat free. Terminal is now only accepted when
    an independent check confirms nothing stronger was reachable.
    """
    tier = {m["id"]: m["capability_tier"] for m in CFG["models"].values()}
    ids = sorted(tier)
    # Every spelling of "what failed": a concrete id, a role alias, an alias
    # outside `role_tiers` (which `failed_roles` used to drop entirely), and
    # nothing at all.
    shapes = [
        ("concrete", lambda first: {"prior_models": [first]}),
        ("alias senior_engineer", lambda _: {"prior_models": ["senior_engineer"]}),
        ("alias reasoning_specialist", lambda _: {"prior_models": ["reasoning_specialist"]}),
        ("concrete worker_balanced_alt model", lambda _: {"prior_models": ["gpt-5.6-terra"]}),
        ("unnamed", lambda _: {}),
    ]
    for scarce in ([], ["claude-haiku-4-5-20251001", "claude-sonnet-5", "gpt-5.6-luna"],
                   ["claude-fable-5"], ["claude-fable-5", "gpt-5.6-sol"]):
        first = r(task_class="MECHANICAL", complexity=1, uncertainty=1, blast_radius=1,
                  unavailable_models=list(scarce))
        if not first["selected_model"]:
            continue
        for name, build in shapes:
            kw = build(first["selected_model"])
            second = r(task_class="MECHANICAL", complexity=1, uncertainty=1, blast_radius=1,
                       unavailable_models=list(scarce), prior_failures=1, **kw)
            # What tier did the previous attempt actually run at?
            named = kw.get("prior_models")
            if named:
                ran = max(tier[m] if m in tier else
                          tier[CFG["models"][CFG["role_bindings"]["default"][m]]["id"]]
                          for m in named)
            else:
                ran = tier[first["selected_model"]]
            reachable = [m for m in ids if m not in scarce and tier[m] > ran]
            if second["terminal"]:
                assert not reachable, (
                    f"{name}/{scarce}: declared exhaustion above tier {ran} while "
                    f"{reachable} were free — {second['notes']}")
                continue
            assert tier[second["selected_model"]] > ran, (
                f"{name}/{scarce}: previous attempt ran at tier {ran}; retry chose "
                f"{second['selected_model']} (tier {tier[second['selected_model']]}) "
                f"and recorded {second['notes']}")


# Config entries the code deliberately does not read, each with the reason.
# Anything NOT listed here must have a consumer: round 9 found eight keys that
# had none, two of which were a second copy of a number the router reads from
# somewhere else — the drift the config header forbids.
DOCUMENTED_BUT_UNREAD = {
    "version": "schema marker for humans and diffs",
    "conditional_flags": "documents which flag pairs are deliberate, for the "
                         "inert-flag test; the effect itself lives in `overrides`",
    "worker_promotions": "prose description of promotions implemented in "
                         "select_worker; kept as the human-readable statement",
    "worker_balanced_selection": "documents that worker_balanced_alt is a "
                                 "same-family fallback, which `fallbacks` implements",
    "verification_ledger": "provenance of the identifiers, read by people",
    "router.default_worker": "states the policy's starting point; the table in "
                             "worker_selection is what executes",
    "router.default_orchestrator": "guidance for the calling agent, not the router",
    "router.default_orchestrator_effort": "guidance for the calling agent",
    # Round 10, from the instrumented reader below. Each of these is read by
    # nobody across 34,848 routes; the reason is what separates "documented on
    # purpose" from "forgotten".
    "retry.same_model_same_effort": "budget for the CALLING agent's loop; one "
                                    "route() call cannot count attempts",
    "retry.same_model_higher_effort": "same",
    "retry.stronger_model": "same",
    "retry.max_review_rounds": "the caller runs the review loop, not the router",
    "retry.max_judge_invocations": "same",
    "retry.require_new_evidence_on_same_tier": "states the rule the router "
                                               "enforces by refusing same-tier retries",
    "review.disagreement.resolution": "verdict-combination table the caller "
                                      "applies after reviews return",
    "review.disagreement.code_local_dispute_judge": "REAL GAP, recorded not "
        "excused: the router always seats `default_judge` and never inspects "
        "the dispute's kind, so these two never bind. Wiring them needs a "
        "dispute-kind input the schema does not have yet.",
    "review.disagreement.formal_reasoning_dispute_judge": "see above",
    "review.disagreement.formal_reasoning_dispute_effort": "see above",
    "review.MEDIUM.preferred_by_implementer.reasoning_specialist": "read for "
        "the implementers that reach MEDIUM; these two do not in any probe",
    "review.MEDIUM.preferred_by_implementer.worker_balanced_alt": "see above",
    "effort_by_work": "per-work-kind guidance for the caller; the router's own "
                      "effort comes from effort_by_work entries that map to a band",
    "effort_map.claude_code.MINIMAL": "vocabulary completeness — no band or "
                                      "floor selects MINIMAL",
    "effort_map.codex.MINIMAL": "same",
    "models": "ids/families/tiers are read; price_per_mtok and verified are "
              "cost and provenance documentation for people",
    "transports": "how the CALLER invokes each model; the router names models, "
                  "it does not dispatch them",
}


class _Recording(Mapping):
    """A config that remembers which paths were actually read.

    A string scan of the source cannot answer this question: `cfg["effort_map"]
    [task.runtime][effort]` reads a leaf whose name appears nowhere, and
    `cfg["flags"].values()` reads every child of a key without naming one. Round
    10 tried the scan both ways — any-segment matching excused whole subtrees,
    leaf matching invented orphans — so the reads are instrumented instead.

    A `Mapping` rather than a `dict` subclass on purpose: `dict(spec)` copies a
    dict subclass's storage directly without going through `__getitem__`, so the
    router's ordinary `spec = dict(spec)` would have slipped every key in that
    subtree past the recorder.
    """

    def __init__(self, data, path=(), seen=None):
        self._data, self._path = data, path
        self._seen = seen if seen is not None else set()

    def __getitem__(self, key):
        value = self._data[key]
        self._seen.add(self._path + (str(key),))
        if isinstance(value, dict):
            return _Recording(value, self._path + (str(key),), self._seen)
        return value

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)

    def get(self, key, default=None):
        if key in self._data:
            return self[key]
        self._seen.add(self._path + (str(key),))
        return default


def test_d14_every_config_rule_has_a_consumer_or_a_recorded_reason():
    """The config's own rule: "a rule declared and consumed nowhere is a promise
    the system does not keep." Round 9 found eight keys breaking it. Round 10
    found the test that was supposed to hold the line matching ANY segment of a
    path, so `"router"` appearing once in the source excused every key beneath
    it — including the two removed in round 9, which could be re-added with the
    test still green. It now runs the router over a spread of inputs and asserts
    on what was actually read."""
    seen: set = set()
    cfg = _Recording(load_config(), (), seen)
    # Every task class and every band, because a key read only on
    # `MIGRATION/CRITICAL` is not inert — it is uncovered, and conflating the
    # two would turn this test into a coverage complaint. The retry budget is
    # exercised past its own ceiling for the same reason.
    probes = [
        dict(task_class=c, complexity=x, uncertainty=u, blast_radius=b, reversibility=r,
             flags=list(f), runtime=rt, prior_failures=pf,
             prior_models=list(pm), unavailable_models=list(um), isolation_available=iso)
        for c in TASK_CLASSES
        for x, u, b, r in ((0, 0, 0, 0), (1, 1, 1, 1), (2, 2, 2, 0), (3, 3, 3, 3))
        for f in ([], ["auth_sensitive"], ["review_disagreement"], ["bridge_down"],
                  ["long_horizon"], ["migration", "data_integrity_sensitive"],
                  ["unknown_root_cause"], ["production_hotfix"], ["public_api_change"],
                  ["concurrency_sensitive"], ["auth_sensitive", "review_disagreement"])
        for rt in ("claude_code", "codex")
        for pf, pm in ((0, []), (1, ["senior_engineer"]), (2, []), (5, []))
        for um in ([], ["claude-fable-5"], ["claude-opus-5", "gpt-5.6-sol"])
        for iso in (None, True, False)
    ]
    for kw in probes:
        try:
            route(Task(**kw), cfg)
        except (ValidationError, Exception):
            continue

    def paths(node, prefix=()):
        if isinstance(node, dict):
            for k, v in node.items():
                yield from paths(v, prefix + (str(k),))
        else:
            yield prefix

    def excused(path):
        return any(".".join(path[:n]) in DOCUMENTED_BUT_UNREAD
                   for n in range(1, len(path) + 1))

    def unread(snapshot):
        """Leaves the router never read. One predicate, used by the assertion
        and by the canary below, so weakening it fails the canary."""
        return {".".join(path) for path in paths(snapshot)
                if not excused(path) and path not in seen}

    orphans = sorted(unread(load_config()))
    assert not orphans, (
        f"the router never read {orphans} across {len(probes)} routes; either wire "
        f"them up or add them to DOCUMENTED_BUT_UNREAD with a reason")

    # A canary, because the guard could not otherwise detect its own weakening:
    # with the allowlist complete the orphan set is empty under a correct rule
    # AND under the any-segment rule this replaced, so nothing distinguished
    # them. An unread key planted under an ancestor that IS read must surface —
    # that ancestor is precisely what the old spelling used as an excuse.
    planted = dict(load_config())
    planted["router"] = {**planted["router"],
                         "floors": {**planted["router"]["floors"], "__canary__": "unread"}}
    canary = unread(planted)
    assert canary == {"router.floors.__canary__"}, (
        f"the guard did not surface a planted unread key under a read ancestor; "
        f"it reported {sorted(canary)}")


def test_d14_every_declared_compensation_can_actually_be_emitted():
    """Round 9. `fallback_compensations.cross_family_reviewer_lost` was declared
    and `_compensations()` had no branch that could emit it, so a compensation
    name that is misspelled — or added to the config and never wired — goes
    inert in silence while reading as active policy."""
    src = (SKILL / "scripts" / "route_task.py").read_text()
    declared = set(CFG["fallback_compensations"])
    emittable = {name for name in declared if f'"{name}"' in src or f"'{name}'" in src}
    assert declared == emittable, (
        f"declared but unreachable compensations: {sorted(declared - emittable)}")

    # Round 10: the check above reads the KEYS, and the router dispatches on the
    # VALUES. Renaming an effect left `_compensations()` emitting it, `route()`
    # matching no branch, the compensation reported as applied, and 130 tests
    # green — an inert compensation reading as active, which is the exact defect
    # the config comment above this block claims the test prevents.
    from route_task import UnknownCompensationError
    effects = set(CFG["fallback_compensations"].values())
    unimplemented = {e for e in effects if f'"{e}"' not in src}
    assert not unimplemented, f"declared effects nothing implements: {sorted(unimplemented)}"

    altered = {**CFG, "fallback_compensations": {
        **CFG["fallback_compensations"], "principal_architect_to_senior": "typo_effect"}}
    with pytest.raises(UnknownCompensationError):
        route(_task(task_class="ARCHITECTURE", complexity=3, uncertainty=3, blast_radius=3,
                    reversibility=2, unavailable_roles=["principal_architect"]), altered)


def test_d14_consecutive_retries_climb_until_they_genuinely_run_out():
    """Round 9. Every earlier retry test asserted ONE route, and the defect
    lived between routes: with nothing named, the floor was recomputed from the
    base route every call, so `prior_failures` 2, 3 and 4 all re-dispatched the
    model attempt 2 had just run while the note claimed an escalation and the
    route stayed dispatchable at exit 0. A single-route assertion cannot see
    that. This one chains the attempts, which is the only shape that can."""
    tier = {m["id"]: m["capability_tier"] for m in CFG["models"].values()}
    ids = sorted(tier)
    # Round 10: `MECHANICAL` and `IMPLEMENTATION` at these scores route
    # identically, so the class loop was one iteration wearing two names. The
    # class that matters is one whose BASE route depends on `prior_failures` —
    # DEBUGGING promotes at pf>=2 — because that is where the reconstruction
    # counted the same promotion twice and declared exhaustion at a tier it had
    # invented, with two untried models free.
    for scarce in ([], ["claude-fable-5"], ["gpt-5.6-luna", "claude-haiku-4-5-20251001"],
                   ["claude-fable-5", "gpt-5.6-sol"]):
        for task_class, extra in (("MECHANICAL", {}), ("IMPLEMENTATION", {}),
                                  ("DEBUGGING", {"flags": ["unknown_root_cause"]}),
                                  ("INVESTIGATION", {"flags": ["unknown_root_cause"]})):
            seen, last = [], None
            for pf in range(0, 5):
                out = r(task_class=task_class, complexity=1, uncertainty=1, blast_radius=1,
                        prior_failures=pf, unavailable_models=list(scarce), **extra)
                if out["terminal"]:
                    # Exhaustion is only honest when nothing stronger is free
                    # AND unused: a model this chain has already burned is not
                    # a model the next attempt could have used.
                    left = [m for m in ids
                            if m not in scarce and m not in seen and tier[m] > (last or -1)]
                    assert not left, (
                        f"{task_class}/{scarce}/pf={pf}: terminal at tier {last} while "
                        f"{left} were reachable and untried — {out['notes']}")
                    break
                now = tier[out["selected_model"]]
                # The contract, in the order it binds: never re-run a model;
                # climb while there is somewhere to climb to; and once the
                # ladder is spent, hold at the strongest thing reachable and
                # ask a human rather than silently repeating or regressing.
                assert out["selected_model"] not in seen, (
                    f"{task_class}/{scarce}: re-ran {out['selected_model']} — "
                    f"{out['notes']}")
                assert last is None or now > last, (
                    f"{task_class}/{scarce}: attempt {pf + 1} runs "
                    f"{out['selected_model']} (tier {now}) after tier {last} — "
                    f"{out['notes']}")
                # From the first retry on, the floor came from a reconstruction
                # rather than a model id the caller observed run, so the route
                # is disclosed and gated.
                if pf >= 1:
                    assert out["retry_history_inferred"], "inference not disclosed"
                    assert out["requires_human_confirmation"], (
                        f"{task_class}/{scarce}: an inferred retry was emitted as "
                        f"dispatchable")
                else:
                    assert not out["retry_history_inferred"]
                seen.append(out["selected_model"])
                last = now


def test_d14_a_role_alias_that_ran_a_fallback_is_not_read_as_its_nominal_model():
    """Round 9. `_failed_tier` read the alias through the binding, but if the
    binding's model was already withheld the role fell back and ran something
    else — usually stronger. The floor came out too low and the retry re-emitted
    the exact model that had just failed, recorded as an escalation."""
    tier = {m["id"]: m["capability_tier"] for m in CFG["models"].values()}
    scarce = ["gpt-5.6-luna", "claude-haiku-4-5-20251001"]
    first = r(task_class="MECHANICAL", unavailable_models=list(scarce))
    assert first["selected_role"] == "worker_fast"
    assert first["selected_model"] == "claude-sonnet-5", "probe drifted"
    second = r(task_class="MECHANICAL", unavailable_models=list(scarce),
               prior_failures=1, prior_models=["worker_fast"])
    assert second["selected_model"] != first["selected_model"], (
        f"re-ran {first['selected_model']} after it failed: {second['notes']}")
    assert tier[second["selected_model"]] > tier[first["selected_model"]]


def test_d15_the_reconstruction_base_is_the_pf_zero_route():
    """Round 11. The base used to be captured by POSITION — a variable assigned
    partway down the promotion chain, with a comment claiming everything above
    it was independent of `prior_failures`. Two promotions below it were also
    independent, so the base was a role the task never started from and a pf=1
    retry re-dispatched what pf=0 had just run. A position in a function is not
    a property and cannot be asserted; this can."""
    from route_task import _base_worker
    checked = 0
    for task_class in TASK_CLASSES:
        for dims in ((0, 0, 0, 0), (1, 1, 1, 1), (2, 2, 2, 0), (3, 3, 3, 3)):
            for flags in ([], ["unknown_root_cause"], ["auth_sensitive"], ["long_horizon"]):
                for pf in (1, 2, 3):
                    kw = dict(task_class=task_class, complexity=dims[0], uncertainty=dims[1],
                              blast_radius=dims[2], reversibility=dims[3], flags=list(flags))
                    zero = r(**kw)
                    if not zero["selected_role"]:
                        continue
                    task = _task(**kw, prior_failures=pf)
                    band = zero["review"]["band"] if False else None
                    from route_task import Policy, Resolver, score, band_from_score, apply_overrides
                    policy = Policy.of(CFG)
                    b = band_from_score(score(task, CFG), policy)
                    b = apply_overrides(task, b, policy)[0]
                    base, _ = _base_worker(task, b, policy, Resolver(task, policy))
                    assert base == zero["selected_role"], (
                        f"{task_class}/{dims}/{flags}/pf={pf}: reconstruction starts from "
                        f"{base}, but the pf=0 route runs {zero['selected_role']}")
                    checked += 1
                    del band
    assert checked > 100, f"only {checked} combinations reached the assertion"


def test_d15_a_partial_history_is_not_treated_as_a_complete_one():
    """Round 11. `prior_models` was read as exact whenever its entries were
    concrete ids, without checking that they account for every failure. Two
    failures with one named model kept the floor at that one model's tier, so
    the retry re-emitted the model attempt 2 had just run — ungated, exit 0."""
    tier = {m["id"]: m["capability_tier"] for m in CFG["models"].values()}
    first = r(task_class="MECHANICAL")
    second = r(task_class="MECHANICAL", prior_failures=1,
               prior_models=[first["selected_model"]])
    assert not second["retry_history_inferred"], "complete history must not be gated"
    stale = r(task_class="MECHANICAL", prior_failures=2,
              prior_models=[first["selected_model"]])
    assert stale["selected_model"] != second["selected_model"], (
        f"re-emitted {second['selected_model']} after it failed: {stale['notes']}")
    assert tier[stale["selected_model"]] > tier[second["selected_model"]]
    assert stale["retry_history_inferred"] and stale["requires_human_confirmation"], (
        "an incomplete history was presented as verified")
    # And the other direction: more models than failures is a contradiction the
    # router should not silently absorb.
    complete = r(task_class="MECHANICAL", prior_failures=2,
                 prior_models=[first["selected_model"], second["selected_model"]])
    assert not complete["retry_history_inferred"], "complete history was gated anyway"


def test_d15_every_human_control_action_is_validated_and_load_bearing():
    """Round 11. The five action keys were compared against string literals with
    no vocabulary check, so a one-character typo deleted the control silently.
    Each is checked twice here: an unknown value must fail at construction, and
    a DIFFERENT valid value must actually change behaviour — otherwise the key
    is read but inert, which is the shape that has cost three rounds."""
    from route_task import Policy, ConfigError
    keys = ("on_retry_exhaustion", "on_any_critical_review",
            "on_independence_unachievable", "on_judge_unavailable",
            "on_review_depth_reduced")
    for key in keys:
        for bad in ("require_human_confirmaton", "", None, 1, "TERMINAL"):
            altered = {**CFG, "human_in_the_loop": {**CFG["human_in_the_loop"], key: bad}}
            with pytest.raises(ConfigError):
                Policy(altered)

    # Load-bearing: flip the CRITICAL gate to notify-only and the route must
    # stop requiring confirmation.
    # A route where the CRITICAL gate is the ONLY trigger — otherwise flipping
    # it changes nothing and the test would pass on an inert control.
    critical = dict(task_class="MECHANICAL", complexity=0, uncertainty=3,
                    blast_radius=0, reversibility=0, flags=["auth_sensitive"])
    assert r(**critical)["requires_human_confirmation"]
    relaxed = {**CFG, "human_in_the_loop": {**CFG["human_in_the_loop"],
                                            "on_any_critical_review": "notify_human"}}
    out = route(_task(**critical), relaxed)
    assert not out["requires_human_confirmation"], (
        "on_any_critical_review is read but changes nothing — an inert control")


def test_d15_every_reference_skill_md_names_actually_exists():
    """Round 11, and self-inflicted: chasing the 500-line budget I stripped the
    `references/` prefix from all six links in SKILL.md, so every path an agent
    is told to open resolved to nothing. A line budget is not worth a broken
    artifact, and nothing was checking."""
    text = (SKILL / "SKILL.md").read_text()
    # EVERY backticked .md token, not just the ones already carrying the
    # prefix: the defect was a prefix being dropped, so a pattern that only
    # matches prefixed paths cannot see it.
    named = set(re.findall(r"`([A-Za-z0-9_./-]+\.md)`", text))
    assert len(named) >= 6, f"SKILL.md names only {sorted(named)}"
    broken = sorted(n for n in named if n != "SKILL.md" and not (SKILL / n).is_file())
    assert not broken, (
        f"SKILL.md tells an agent to open {broken}, which do not resolve relative "
        f"to it; the files live under references/")
    for doc in sorted((SKILL / "references").glob("*.md")):
        assert f"references/{doc.name}" in text, f"{doc.name} is never pointed at"

    # Fences must balance, or a section renders as literal code — this round's
    # new control-loop.md text was swallowed by an unclosed ```yaml block.
    for doc in sorted((SKILL / "references").glob("*.md")) + [SKILL / "SKILL.md"]:
        fences = sum(1 for line in doc.read_text().splitlines() if line.startswith("```"))
        assert fences % 2 == 0, f"{doc.name} has {fences} code fences — one is unclosed"


def test_d14_an_alias_is_resolved_through_both_withholding_channels():
    """Round 10. The caller can withhold by model id OR by role, and
    `_failed_tier` read only the first — so behind `--unavailable <role>`, which
    the config header calls the documented route, the alias resolved to a model
    that had been withheld and never ran. The floor came out at that model's
    tier and the retry escalated straight back onto the model that had actually
    failed, while `excluded_prior_failures` named one that never ran.

    The round-9 test used the model channel only, which is exactly the input the
    round-9 implementation handled, so it could not see this."""
    first = r(task_class="REFACTORING", complexity=2,
              unavailable_roles=["senior_engineer", "reasoning_specialist"])
    held = first["selected_model"]
    nominal = CFG["models"][CFG["role_bindings"]["default"]["senior_engineer"]]["id"]

    retry = r(task_class="REFACTORING", complexity=2,
              unavailable_roles=["senior_engineer", "reasoning_specialist"],
              prior_failures=1, prior_models=["senior_engineer"])
    # What `senior_engineer` HELD, not what the binding nominally names.
    ladder = Resolver(_task(task_class="REFACTORING", complexity=2,
                            unavailable_roles=["senior_engineer", "reasoning_specialist"]),
                      Policy.of(CFG))
    actually_ran = ladder.held_by("senior_engineer")
    assert actually_ran != nominal, "probe drifted: the alias no longer falls back"

    assert actually_ran in retry["excluded_prior_failures"], (
        f"the model the alias held ({actually_ran}) was not excluded; "
        f"excluded={retry['excluded_prior_failures']}")
    assert nominal not in retry["excluded_prior_failures"], (
        f"{nominal} was withheld and never ran, yet is reported as already-failed")
    assert actually_ran not in emitted_models(retry), (
        f"re-emitted {actually_ran} after it failed: {retry['notes']}")
    del first, held


def test_d14_a_bonus_review_that_cannot_be_isolated_does_not_kill_the_task():
    """Round 9. The isolation terminal keyed off `review["independent"]`, which
    the architect-downgrade compensation sets at ANY band. A LOW route whose
    *bonus* second review could not be isolated emitted nothing at all — a
    compensation punishing the caller for its own best effort. The terminal
    belongs to the band's own requirement."""
    out = r(task_class="ARCHITECTURE", flags=["long_horizon"],
            unavailable_models=["claude-fable-5"], isolation_available=False)
    assert out["review"]["band"] == "LOW", "probe drifted"
    assert not CFG["review"]["LOW"].get("independent"), "LOW now asks for independence"
    assert out["fallback_compensations_applied"], "probe no longer reaches the compensation"
    assert out["terminal"] is None, (
        "a bonus review's isolation gap terminated a band that never asked for it")
    assert out["selected_model"]


def test_d14_the_human_gate_exit_status_must_survive_posix_truncation():
    """Round 9. The guard rejected 0/1/2 and nothing else, so 256 passed it and
    exited 0 — a human-gated route reporting success, the single hazard the
    value exists to remove. It also raised after the route had been printed,
    outside main()'s handler, landing on exit 1 for a route it had just emitted
    as executable."""
    from route_task import Policy, ConfigError
    cfg = load_config()
    for bad in (256, 512, -256, 0, 1, 2, True, "3", 3.0):
        altered = {**cfg, "human_in_the_loop": {**cfg["human_in_the_loop"],
                                                "human_gate_exit_status": bad}}
        with pytest.raises(ConfigError):
            Policy(altered)
    for good in (3, 7, 255):
        altered = {**cfg, "human_in_the_loop": {**cfg["human_in_the_loop"],
                                                "human_gate_exit_status": good}}
        assert Policy(altered).human_gate_exit_status == good


def test_d13_a_confirmed_isolation_gap_is_terminal_where_the_band_requires_it():
    """Round 8. The policy declares `on_independence_unachievable: terminal`,
    and the five-state contract goes to the trouble of separating `unavailable`
    (a confirmed gap) from `degraded` (an unchecked one). The router answered
    both the same way — an ordinary route at exit 0 — which also falsified the
    exhaustiveness claim the exit contract had just made."""
    out = r(task_class="IMPLEMENTATION", complexity=2, uncertainty=2, blast_radius=2,
            isolation_available=False)
    assert out["review"]["independence_required"], "probe drifted"
    assert out["review"]["review_independence"] == "unavailable"
    assert out["terminal"] == "INDEPENDENCE_UNAVAILABLE"
    assert out["requires_human_confirmation"]
    # And the band that does not ask for independence is untouched by this.
    low = r(task_class="MECHANICAL", isolation_available=False)
    assert not low["review"]["independence_required"] and not low["terminal"]


def test_d13_the_human_gate_exit_status_comes_from_the_config():
    """Round 8. The key was declared in `human_in_the_loop` and read nowhere;
    the CLI hard-coded 3. The config's own rule is that a rule consumed nowhere
    is a promise the system does not keep."""
    import tempfile, yaml, os
    cfg = load_config()
    assert cfg["human_in_the_loop"]["human_gate_exit_status"] == 3
    probe = ["--class", "IMPLEMENTATION", "--complexity", "0", "--uncertainty", "0",
             "--blast-radius", "0", "--reversibility", "0",
             "--flags", "auth_sensitive,bridge_down", "--runtime", "codex"]
    assert cli(*probe).returncode == 3
    # Change the policy and the CLI must follow it, or the key is decoration.
    altered = dict(cfg)
    altered["human_in_the_loop"] = {**cfg["human_in_the_loop"], "human_gate_exit_status": 7}
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "model-routing.yaml"
        path.write_text(yaml.safe_dump(altered))
        proc = subprocess.run(
            [sys.executable, "-c",
             "import sys;sys.path.insert(0,%r);import route_task as rt;"
             "from pathlib import Path;"
             "rt._DEFAULT_CFG=rt.load_config(Path(%r));sys.exit(rt.main(sys.argv[1:]))"
             % (str(SKILL / "scripts"), str(path)), *probe],
            capture_output=True, text=True)
    assert proc.returncode == 7, (
        f"config said 7, CLI returned {proc.returncode}: the key is not read")


def _peek_model(out, role):
    """The model a role holds *as a reviewer* in this route.

    Round 6: the previous version fell through to the judge seat, so when the
    judge retry re-seated a reviewer this returned the displaced role's model
    from the judge and the stale substitution record looked consistent. A
    substitution record is about a reviewer slot; reading it out of the judge
    seat answers a different question than the one being asked.
    """
    rv = out["review"]
    for name, model in zip(rv["reviewers"], rv["reviewer_models"]):
        if name == role:
            return model
    return None


# ---------------------------------------------------------------------------
# D12 — the disagreement path is a seat allocator too
# ---------------------------------------------------------------------------
#
# `route_path == "disagreement"` binds a judge at ANY band, but seat allocation
# used to sit behind `if review["independent"]`, and LOW declares
# `independent: false`. The code's blind spot and the test suite's blind spot
# were the same one: the sweep never set `review_disagreement` either.

DISAGREEMENT_SWEEP = [
    ["review_disagreement"],
    ["review_disagreement", "bridge_down"],
    ["review_disagreement", "long_horizon"],
    ["review_disagreement", "auth_sensitive"],
]


def _disagreement_sweep():
    for task_class in TASK_CLASSES:
        for c, u, b, rev in DIMENSION_SWEEP:
            for flags in DISAGREEMENT_SWEEP:
                for runtime in ("claude_code", "codex"):
                    for scarce in SCARCITY_SWEEP:
                        try:
                            yield r(task_class=task_class, complexity=c, uncertainty=u,
                                    blast_radius=b, reversibility=rev, flags=list(flags),
                                    runtime=runtime, unavailable_models=list(scarce))
                        except ValidationError:
                            continue


def test_d12_sweep_reaches_low_band_disagreement_routes():
    low = [o for o in _disagreement_sweep()
           if not o["terminal"] and o["review"]["band"] == "LOW" and o["review"]["judge"]]
    assert low, "the sweep never produced a LOW-band route with a judge"


def test_d12_the_judge_is_never_a_party_at_any_band():
    """An adjudicator brought in to settle a dispute must not be one of the
    parties. LOW's worker-reviews-itself is documented design; the judge seat
    is not covered by that exemption."""
    checked = 0
    for out in _disagreement_sweep():
        rv = out["review"]
        if out["terminal"] or not rv["judge_model"]:
            continue
        checked += 1
        parties = {out["selected_model"], *(m for m in rv["reviewer_models"] if m)}
        assert rv["judge_model"] not in parties, (
            f"{out['task_class']}/{rv['band']}: judge {rv['judge_model']} is also a party"
        )
    assert checked, "no seated judge in the disagreement sweep"


def test_d12_judge_unavailable_gates_and_explains():
    seen = 0
    for out in _disagreement_sweep():
        rv = out["review"]
        if out["terminal"] or not rv["judge_unavailable"]:
            continue
        seen += 1
        assert rv["judge"] is None and rv["judge_model"] is None
        assert out["requires_human_confirmation"] is True
        assert "human must resolve" in out["rationale"]
    assert seen, "no judge_unavailable route in the sweep — the assertion was vacuous"


def test_d12_judge_reclaims_a_tier_a_reviewer_did_not_need():
    """Allocation is greedy — worker, then reviewers, then judge — and the
    reviewer step maximises tier, so the judge could be told nothing adequate
    was free while a model sat unused behind a reviewer that did not need it.
    This is the exact route that exhibited it; a stop nobody can act on is as
    unhelpful as a missing one."""
    out = r(task_class="MECHANICAL", flags=["review_disagreement"],
            unavailable_models=["claude-fable-5"])
    rv = out["review"]
    assert rv["judge_unavailable"] is False
    assert rv["judge_model"] is not None
    parties = {out["selected_model"], *(m for m in rv["reviewer_models"] if m)}
    assert rv["judge_model"] not in parties
