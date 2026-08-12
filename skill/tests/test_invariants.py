"""Exhaustive safety invariants, swept over the whole reachable input space.

Why this file exists, stated plainly: across five rounds of independent review,
every round after the first found a Critical defect *introduced by the previous
round's fix*. Each round I wrote an ad-hoc exhaustive probe, confirmed the fix,
and threw the probe away — so the next change was only checked against the
example-based tests, which is exactly how the same class kept coming back one
seam over.

The example tests in `test_routing.py` and `test_defects.py` document specific
scenarios and specific past defects. This file does something different: it
enumerates the input space and asserts the safety properties over all of it, so
a change cannot satisfy the examples while breaking the property somewhere the
examples do not look.

Every invariant here was learned from a real defect that shipped past a green
suite. The comment on each says which.

Run:  python3 -m pytest skill/tests/test_invariants.py -q
"""

import itertools
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))

from route_task import (  # noqa: E402
    ROLES,
    TASK_CLASSES,
    Task,
    ValidationError,
    load_config,
    route,
)

CFG = load_config()
MODEL_IDS = sorted(m["id"] for m in CFG["models"].values())
FAMILY_OF = {m["id"]: m["family"] for m in CFG["models"].values()}
LOCAL_FAMILY = {"claude_code": "claude", "codex": "openai"}
TIER_OF = {m["id"]: m["capability_tier"] for m in CFG["models"].values()}
NOMINAL_TIER = {role: TIER_OF[CFG["models"][key]["id"]]
                for role, key in CFG["role_bindings"]["default"].items()}
BAND_FLOOR = {
    band: min((NOMINAL_TIER[r] for r in (spec.get("reviewers") or spec.get("candidates") or [])
               if r in NOMINAL_TIER), default=0)
    for band, spec in CFG["review"].items()
}

# Dimension corners plus a midpoint. Bands are determined by the weighted sum,
# so the corners cover every band and the midpoint catches boundary handling.
DIMENSIONS = [(0, 0, 0, 0), (1, 1, 1, 1), (2, 2, 2, 0), (3, 3, 3, 3), (0, 3, 0, 0), (3, 0, 3, 2)]

# Flag sets chosen to reach every distinct code path: none, each override
# family, the disagreement route (which binds a judge at ANY band — the blind
# spot that produced round 5's Critical), and the degraded binding.
FLAG_SETS = [
    [],
    ["auth_sensitive"],
    ["financial_sensitive"],
    ["migration", "data_integrity_sensitive"],
    ["unknown_root_cause"],
    ["review_disagreement"],
    ["review_disagreement", "bridge_down"],
    ["long_horizon", "review_disagreement"],
    ["bridge_down"],
    ["auth_sensitive", "bridge_down"],
    ["production_hotfix", "concurrency_sensitive"],
]

# Scarcity is what forces roles onto shared models. Without it the degenerate
# bindings are unreachable and every seat invariant passes vacuously.
SCARCITY = [
    [],
    [MODEL_IDS[0]],
    MODEL_IDS[:2],
    MODEL_IDS[1:3],
    MODEL_IDS[2:5],
    MODEL_IDS[:3],
]

RUNTIMES = ["claude_code", "codex"]

# Round 6: without this dimension `excluded_prior_failures` was empty on all
# 8,712 routes, so the invariant that a failed model is never re-emitted
# intersected with the empty set every single time. Reverting round 2's fix
# left all twenty invariants green. Both spellings are swept — a role alias,
# which the router resolves through the *current* binding, and a concrete id,
# which it does not — because they take different paths through the exclusion.
PRIOR = [
    [],
    ["senior_engineer"],
    ["worker_fast", "worker_balanced"],
    [MODEL_IDS[2]],
    [MODEL_IDS[0], MODEL_IDS[4]],
]


_WITHHELD = 0


def _routes():
    """Every reachable route. Terminal ones are yielded too — several
    invariants are specifically about what a terminal route must withhold."""
    for task_class, dims, flags, runtime, scarce, prior in itertools.product(
        TASK_CLASSES, DIMENSIONS, FLAG_SETS, RUNTIMES, SCARCITY, PRIOR
    ):
        c, u, b, rev = dims
        try:
            yield route(Task(
                task_class=task_class, complexity=c, uncertainty=u,
                blast_radius=b, reversibility=rev, flags=list(flags),
                runtime=runtime, unavailable_models=list(scarce),
                prior_models=list(prior),
            ), CFG)
        except ValidationError:
            # Every candidate withheld: failing closed is a correct outcome.
            global _WITHHELD
            _WITHHELD += 1
            continue


ALL = None


def routes():
    global ALL
    if ALL is None:
        ALL = list(_routes())
    return ALL


def parties(out):
    return [m for m in [out["selected_model"], *out["review"]["reviewer_models"]] if m]


def test_the_sweep_is_large_and_reaches_the_interesting_states():
    """Guards every invariant below. A sweep that never reaches a degraded
    binding, a substitution, a terminal, or a seated judge proves nothing about
    them — and that is precisely how earlier invariant tests passed while the
    invariants were false."""
    all_routes = routes()
    assert len(all_routes) > 5000, f"sweep collapsed to {len(all_routes)} routes"

    reached = {
        "terminal": sum(1 for o in all_routes if o["terminal"]),
        "substitution": sum(1 for o in all_routes if o["review"]["self_review_avoided"]),
        "judge_seated": sum(1 for o in all_routes if o["review"]["judge_model"]),
        "judge_unavailable": sum(1 for o in all_routes if o["review"]["judge_unavailable"]),
        "compromised": sum(1 for o in all_routes if o["review"]["independence_compromised"]),
        "fallback": sum(1 for o in all_routes if o["fallbacks_applied"]),
        "compensation": sum(1 for o in all_routes if o["fallback_compensations_applied"]),
        "bridge_down": sum(1 for o in all_routes if "bridge down" in o["rationale"]),
        # Round 6 added the three below. Each names the precondition of an
        # invariant that was passing on zero routes.
        "prior_exclusion": sum(1 for o in all_routes if o["excluded_prior_failures"]),
        "depth_reduced": sum(1 for o in all_routes if o["review"]["review_depth_reduced"]),
        "fallback_note": sum(1 for o in all_routes
                             if any("->" in n for n in o["fallbacks_applied"])),
    }
    missing = [k for k, v in reached.items() if v == 0]
    assert not missing, f"the sweep never reached: {missing}"


def test_every_swept_dimension_actually_varies():
    """The generalisation of the round-6 failure.

    Counting reached *output* states was not enough: the prior-failure
    dimension existed in the invariant's filter but never in the sweep's
    input, so the state it guarded was unreachable by construction and the
    guard above had nothing to notice. A dimension that contributes one
    distinct value is a dimension that is not being tested, whatever the
    output counts say — so the sweep asserts its own inputs vary."""
    dimensions = {
        "TASK_CLASSES": TASK_CLASSES, "DIMENSIONS": DIMENSIONS,
        "FLAG_SETS": FLAG_SETS, "RUNTIMES": RUNTIMES,
        "SCARCITY": SCARCITY, "PRIOR": PRIOR,
    }
    flat = [(k, len({tuple(v) if isinstance(v, (list, tuple)) else v for v in vals}))
            for k, vals in dimensions.items()]
    collapsed = [k for k, n in flat if n < 2]
    assert not collapsed, f"swept dimension(s) contribute a single value: {collapsed}"

    # And each must reach the router: a dimension varied in the list but
    # dropped on the way into Task() is the same defect one layer down.
    assert len(routes()) == len(list(itertools.product(
        TASK_CLASSES, DIMENSIONS, FLAG_SETS, RUNTIMES, SCARCITY, PRIOR
    ))) - _WITHHELD, "sweep size does not match the product of its dimensions"


# ---------------------------------------------------------------------------
# Seat invariants — five rounds of Criticals live here
# ---------------------------------------------------------------------------

def test_no_reviewer_holds_the_implementer_model_where_independence_is_required():
    """Rounds 3 and 4. Collision was judged on role labels while a degraded
    binding maps several roles to one model."""
    for out in routes():
        rv = out["review"]
        if out["terminal"] or not rv["independence_required"]:
            continue
        models = [m for m in rv["reviewer_models"] if m]
        assert out["selected_model"] not in models, (
            f"{out['task_class']}/{rv['band']}: implementer {out['selected_model']} "
            f"is also a reviewer"
        )


def test_no_two_reviewers_share_a_model_where_independence_is_required():
    for out in routes():
        rv = out["review"]
        if out["terminal"] or not rv["independence_required"]:
            continue
        models = [m for m in rv["reviewer_models"] if m]
        assert len(models) == len(set(models)), (
            f"{out['task_class']}/{rv['band']}: duplicate reviewer models {models}"
        )


def test_the_judge_is_never_a_party_at_any_band():
    """Round 5. The judge is bound by the disagreement path at ANY band, but
    seat allocation sat behind `if review["independent"]` and LOW declares
    independence false — so LOW + disagreement skipped it entirely. LOW's
    worker-reviews-itself is documented design; the judge is not covered by
    that exemption."""
    for out in routes():
        rv = out["review"]
        if out["terminal"] or not rv["judge_model"]:
            continue
        assert rv["judge_model"] not in parties(out), (
            f"{out['task_class']}/{rv['band']}: judge {rv['judge_model']} is also a party"
        )


def test_a_seated_judge_is_never_weaker_than_any_party_it_adjudicates():
    """Round 6. The previous spelling compared ROLE ORDINALS, and under
    scarcity a role holds whatever model is left: `worker_fast` on the frontier
    model, `worker_balanced` on a mid one. Twenty-seven routes seated a tier-1
    judge over a tier-3 implementer and its reviewer, and the role comparison
    called it well ordered. The implementer counts as a party — it is one side
    of any dispute about its own work."""
    for out in routes():
        rv = out["review"]
        if out["terminal"] or not rv["judge_model"]:
            continue
        involved = parties(out)
        assert involved, "a judge was seated with no party to adjudicate"
        assert TIER_OF[rv["judge_model"]] >= max(TIER_OF[m] for m in involved), (
            f"{out['task_class']}/{rv['band']}: judge {rv['judge_model']} "
            f"(tier {TIER_OF[rv['judge_model']]}) is outranked by {involved}"
        )


def test_a_review_below_its_band_floor_is_disclosed_and_gated():
    """Round 6. Fallbacks and de-confliction both re-seat reviewers without
    consulting the band, so a HIGH review could be staffed at tier 0 and emitted
    as if the band had been met. The router cannot restore the depth; what it
    must not do is stay quiet about spending it."""
    for out in routes():
        rv = out["review"]
        if out["terminal"]:
            continue
        floor = BAND_FLOOR[rv["band"]]
        under = [m for m in rv["reviewer_models"] if m and TIER_OF[m] < floor]
        if not under:
            assert not rv["review_depth_reduced"], "reported a shortfall that is not there"
            continue
        assert rv["review_depth_reduced"], (
            f"{rv['band']} staffed at {under} (floor tier {floor}) without disclosure")
        assert out["requires_human_confirmation"], (
            f"{rv['band']} staffed below its floor and still dispatchable")


# ---------------------------------------------------------------------------
# Executability — a route must be runnable exactly as written
# ---------------------------------------------------------------------------

def test_no_route_emits_a_model_the_caller_withheld():
    """Round 1's Critical. Five of ten fallback paths substituted the same
    model back and recorded a downgrade that never happened."""
    for out in routes():
        blocked = set(out["unavailable_models"])
        emitted = set(parties(out)) | {out["review"]["judge_model"]} - {None}
        assert not (emitted & blocked), f"emitted withheld model(s) {emitted & blocked}"


def test_no_route_emits_a_model_that_already_failed():
    """Round 2. Role-tier escalation moved the label while a degraded binding
    kept the same model behind it."""
    for out in routes():
        failed = set(out["excluded_prior_failures"])
        emitted = set(parties(out)) | {out["review"]["judge_model"]} - {None}
        assert not (emitted & failed)


def test_bridge_down_never_names_an_unreachable_family():
    """Round 2. A model on the far side of a downed bridge cannot be invoked,
    so naming it produces a route that cannot be executed."""
    for out in routes():
        if "bridge down" not in out["rationale"]:
            continue
        runtime = "codex" if "codex" in out["rationale"] else None
        # The rationale names the degraded binding; derive the runtime from it.
        local = "openai" if "openai_only" in out["rationale"] else "claude"
        emitted = [m for m in parties(out) + [out["review"]["judge_model"]] if m]
        foreign = {m for m in emitted if FAMILY_OF[m] != local}
        assert not foreign, f"bridge down but emitted {sorted(foreign)} (local={local})"


def test_a_terminal_route_withholds_every_execution_binding():
    """Rounds 3 and 4. Nulling only the worker left a consumer able to dispatch
    the reviewers from a route whose own rationale said not to."""
    for out in routes():
        if not out["terminal"]:
            continue
        rv = out["review"]
        assert out["selected_model"] is None
        assert out["selected_effort"] is None
        assert rv["reviewer_models"] == []
        assert rv["judge_model"] is None
        assert rv["effort"] is None


# ---------------------------------------------------------------------------
# Honest reporting — "a recorded change must be a real change"
# ---------------------------------------------------------------------------

def test_every_recorded_fallback_actually_changed_the_model():
    for out in routes():
        for note in out["fallbacks_applied"]:
            if "->" not in note:
                continue
            before, after = (s.strip() for s in note.split("->", 1))
            # Round 6: the previous spelling compared "X unavailable" against
            # "X" and so could never fail. Ten paths emitting round 1's exact
            # Critical — a fallback that lands on the model it replaced — stayed
            # green. The marker word has to come off before the comparison.
            before = before.split(":")[-1].strip().removesuffix(" unavailable").strip()
            assert before and before != after, f"no-op fallback recorded: {note}"


def test_every_recorded_substitution_changed_who_sits_in_the_seat():
    """Round 6, twice over. The name promised seats and the body compared role
    labels, so a substitution that landed on the same model read as a change;
    and nothing checked the record against the final roster, so the judge retry
    could re-seat the very reviewer a record named and leave the rationale
    asserting a reviewer who is not on the route."""
    for out in routes():
        rv = out["review"]
        if out["terminal"]:
            continue
        seated = dict(zip(rv["reviewers"], rv["reviewer_models"]))
        for sub in rv["self_review_avoided"]:
            assert sub["replaced"] != sub["with"]
            assert sub["with"] in rv["reviewers"], (
                f"rationale names reviewer {sub['with']} but the seats hold "
                f"{rv['reviewers']}")
            assert seated.get(sub["with"]) != out["selected_model"] or not rv[
                "independence_required"], (
                f"substitution landed back on the implementer model "
                f"{out['selected_model']}")


def test_a_compensation_is_recorded_only_when_it_was_applied():
    """Round 3. `raise_effort_to_MAX_and_add_second_review` reported itself
    applied while doing only the first half."""
    for out in routes():
        if "raise_effort_to_MAX_and_add_second_review" not in out["fallback_compensations_applied"]:
            continue
        if out["terminal"]:
            continue
        assert out["selected_effort"] == CFG["effort_levels"][-1]
        assert out["review"]["independence_required"] is True


def test_independence_is_never_reported_as_enforced_without_evidence():
    """Round 2. The policy's requirement was being reported as if it were an
    observation of what happened."""
    for out in routes():
        rv = out["review"]
        if rv["review_independence"] != "enforced":
            continue
        pytest.fail("no route in this sweep supplies evidence, so none may claim enforced")


# ---------------------------------------------------------------------------
# Gates — a control that discloses but does not stop is not a control
# ---------------------------------------------------------------------------

def test_every_critical_review_requires_a_human():
    """Round 4. The router cannot verify an isolation receipt's provenance, so
    it never treats one as proof."""
    for out in routes():
        if out["review"]["band"] == "CRITICAL":
            assert out["requires_human_confirmation"] is True


def test_compromised_independence_is_terminal():
    for out in routes():
        if out["review"]["independence_compromised"]:
            assert out["terminal"] is not None
            assert out["requires_human_confirmation"] is True


def test_an_unavailable_judge_gates_even_though_it_does_not_stop():
    for out in routes():
        if out["review"]["judge_unavailable"] and not out["terminal"]:
            assert out["requires_human_confirmation"] is True
            assert out["review"]["judge"] is None


def test_a_terminal_route_always_requires_a_human():
    for out in routes():
        if out["terminal"]:
            assert out["requires_human_confirmation"] is True


# ---------------------------------------------------------------------------
# Structural invariants that predate the review loop
# ---------------------------------------------------------------------------

def test_a_critical_domain_flag_always_reaches_high_review_and_worker():
    critical_flags = set(CFG["flags"]["critical_domain"])
    bands = sorted(CFG["router"]["bands"], key=lambda b: CFG["router"]["bands"][b]["ordinal"])
    for out in routes():
        if not (set(out["critical_flags"]) & critical_flags):
            continue
        assert bands.index(out["review"]["band"]) >= bands.index("HIGH")
        if out["selected_role"]:
            assert ROLES.index(out["selected_role"]) >= ROLES.index("worker_balanced")


def test_review_depth_is_never_weaker_than_its_band_requires():
    """The property that makes every invariant above checkable: no
    worker-selection branch can weaken a review.

    Stated precisely, because the absolute form is not true: a review may be
    *stronger* than its band's baseline — a fallback compensation can add a
    second independent review to a LOW-band route, which is the whole point of
    the compensation. What must never happen is a review weaker than the band
    asked for, because that is the direction in which the worker's identity
    could quietly influence how hard its own output is checked.
    """
    raised_without_reason = []
    for out in routes():
        rv = out["review"]
        baseline = CFG["review"][rv["band"]]
        want_independent = baseline.get("independent", False)
        want_checks = set(baseline.get("required_checks", []))

        assert rv["independence_required"] >= want_independent, (
            f"{out['task_class']}/{rv['band']}: independence downgraded below the band"
        )
        assert want_checks <= set(rv["required_checks"]), (
            f"{out['task_class']}/{rv['band']}: required checks dropped"
        )
        # Anything above the baseline must have a recorded cause, or it is a
        # silent policy change rather than a disclosed compensation.
        if rv["independence_required"] and not want_independent:
            if not out["fallback_compensations_applied"]:
                raised_without_reason.append((out["task_class"], rv["band"]))
    assert not raised_without_reason, (
        f"review strengthened above its band with nothing recorded: "
        f"{sorted(set(raised_without_reason))[:5]}"
    )


def test_the_rationale_always_names_the_band_and_any_fallback():
    for out in routes():
        assert out["risk_band"] in out["rationale"]
        if out["fallbacks_applied"]:
            assert "Fallback" in out["rationale"]
