"""Validation scenarios and structural invariants for the deterministic router.

The four cases marked REGRESSION are the ones the previous policy version got
wrong: it dispatched on task class with early returns and only checked
critical-domain flags afterwards, so debugging an auth bug and designing a
payments architecture silently skipped mandatory dual review. Those two cases
plus the exact-boundary and band-ambiguity cases are the reason this file
exists — if a future edit reintroduces class-dispatch-before-override, they
fail loudly.

Note on scenario dimension scores: the spec's scenario table names expected
routes without fixing the four dimension scores, so each test picks scores a
careful classifier would plausibly assign for that description. Where the
resulting review band differs from the spec's loose parenthetical, the test
says so rather than bending the policy to match.

Run:  python3 -m pytest skill/tests/ -q
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from route_task import (  # noqa: E402
    BANDS,
    ROLES,
    TASK_CLASSES,
    Task,
    load_config,
    route,
)

CFG = load_config()


def r(**kw):
    """Route a task, defaulting every dimension to 0."""
    kw.setdefault("complexity", 0)
    kw.setdefault("uncertainty", 0)
    kw.setdefault("blast_radius", 0)
    kw.setdefault("reversibility", 0)
    return route(Task(**kw), CFG)


def at_least(role, floor):
    return ROLES.index(role) >= ROLES.index(floor)


def band_at_least(band, floor):
    return BANDS.index(band) >= BANDS.index(floor)


# ---------------------------------------------------------------------------
# Scenario 1-6, 12-13: ordinary routing
# ---------------------------------------------------------------------------

def test_s1_mechanical_rename_stays_cheap():
    """A rename across 12 files is a large workload with no uncertainty."""
    out = r(task_class="MECHANICAL")
    assert out["risk_band"] == "LOW"
    assert out["selected_role"] == "worker_fast"
    assert out["selected_effort"] == "LOW"
    assert out["review"]["band"] == "LOW"
    assert out["review"]["independence_required"] is False


def test_s2_ordinary_feature_with_clear_spec():
    out = r(task_class="IMPLEMENTATION", complexity=1, uncertainty=1, blast_radius=1)
    assert out["risk_band"] == "MEDIUM"
    assert out["selected_role"] == "worker_fast"
    assert out["selected_effort"] == "MEDIUM"
    assert out["review"]["band"] == "MEDIUM"
    assert len(out["review"]["reviewers"]) == 1


def test_s3_complex_multi_file_feature():
    out = r(task_class="IMPLEMENTATION", complexity=2, uncertainty=2, blast_radius=1)
    assert out["risk_band"] == "HIGH"
    assert out["selected_role"] == "worker_balanced"
    assert out["selected_effort"] == "HIGH"
    # Review is HIGH, not MEDIUM: review depth follows the band alone, and this
    # scores 8. That independence is what makes the invariants below checkable.
    assert out["review"]["band"] == "HIGH"


def test_s4_failed_worker_escalates_rather_than_retrying():
    # Round 12: `--prior-models` takes concrete model ids now. A role alias does
    # not say which model held that seat on the attempt that failed, and five
    # rounds of trying to infer it produced five different defects, so the
    # router asks instead. The scenario is unchanged — a failed worker escalates.
    tier = {m["id"]: m["capability_tier"] for m in CFG["models"].values()}
    failed = CFG["models"][CFG["role_bindings"]["default"]["worker_fast"]]["id"]
    out = r(
        task_class="IMPLEMENTATION",
        complexity=1, uncertainty=1, blast_radius=1,
        prior_failures=1,
        prior_models=[failed],
    )
    # On the model, not the label. Round 8: excluding the failed model already
    # moves `worker_fast` onto `claude-sonnet-5` — the very model
    # `worker_balanced` would have supplied — so the role-label assertion failed
    # on a route that satisfies this test's own intent exactly. What §S4
    # requires is that the retry does not re-run what failed and is stronger.
    assert out["selected_model"] != failed
    assert tier[out["selected_model"]] > tier[failed]
    assert any("capability tier" in n for n in out["notes"]), out["notes"]


def test_s5_hard_logical_verification_routes_to_reasoning():
    out = r(
        task_class="INVESTIGATION",
        complexity=2, uncertainty=2, blast_radius=2, reversibility=0,
        reasoning_centric=True,
    )
    assert out["selected_role"] == "reasoning_specialist"
    assert band_at_least(out["review"]["band"], "MEDIUM")


def test_s6_difficult_code_centric_debugging():
    out = r(
        task_class="DEBUGGING",
        complexity=2, uncertainty=2, blast_radius=1, reversibility=0,
        reasoning_centric=False,
    )
    assert at_least(out["selected_role"], "worker_balanced")
    assert out["selected_role"] != "reasoning_specialist"


def test_s12_large_repetitive_generation_is_not_frontier_work():
    """Volume is not difficulty. 20 similar components stay on the cheap worker."""
    out = r(task_class="IMPLEMENTATION", complexity=1)
    assert out["risk_band"] == "LOW"
    assert out["selected_role"] == "worker_fast"
    assert out["review"]["band"] == "LOW"


def test_s13_high_uncertainty_tiny_diff_still_escalates():
    """Five lines, but nobody knows which five. Size is not the signal."""
    out = r(task_class="DEBUGGING", complexity=0, uncertainty=3, blast_radius=1)
    assert out["risk_score"] == 8
    assert out["risk_band"] == "HIGH"
    assert at_least(out["selected_role"], "worker_balanced")


# ---------------------------------------------------------------------------
# REGRESSION cases — the ones the previous policy got wrong
# ---------------------------------------------------------------------------

def test_s7_regression_auth_change_forces_dual_independent_review():
    """An auth change is small and well-understood, and still must not ship
    on a single lightweight review."""
    out = r(task_class="IMPLEMENTATION", complexity=1, uncertainty=0,
            blast_radius=1, reversibility=0, flags=["auth_sensitive"])
    assert band_at_least(out["risk_band"], "HIGH")
    assert at_least(out["selected_role"], "worker_balanced")
    assert band_at_least(out["review"]["band"], "HIGH")
    assert len(out["review"]["reviewers"]) == 2
    assert out["review"]["independence_required"] is True


def test_s8_regression_auth_bug_unknown_root_cause():
    """The exact case the previous version dropped: DEBUGGING dispatched early
    and never reached the critical-flag override."""
    out = r(task_class="DEBUGGING", complexity=2, uncertainty=2,
            blast_radius=1, reversibility=0,
            flags=["auth_sensitive", "unknown_root_cause"])
    assert band_at_least(out["risk_band"], "HIGH")
    assert at_least(out["selected_role"], "worker_balanced")
    assert band_at_least(out["review"]["band"], "HIGH")
    assert len(out["review"]["reviewers"]) == 2
    assert out["review"]["independence_required"] is True
    assert "critical_domain" in out["band_overrides_applied"]


def test_s14_regression_score_exactly_10_is_unambiguously_high():
    """The previous version compared `>= 10` in one place and `high_risk_max: 10`
    in another, so a score of exactly 10 banded differently depending on which
    branch you arrived through."""
    seen = set()
    for task_class in TASK_CLASSES:
        out = r(task_class=task_class, complexity=2, uncertainty=2,
                blast_radius=2, reversibility=0)
        assert out["risk_score"] == 10
        seen.add(out["risk_band"])
    assert seen == {"HIGH"}, f"score 10 banded inconsistently across classes: {seen}"


def test_s15_regression_payments_architecture():
    """ARCHITECTURE also dispatched early in the previous version, so a payments
    design skipped the critical-domain override entirely."""
    out = r(task_class="ARCHITECTURE", complexity=3, uncertainty=3,
            blast_radius=3, reversibility=2,
            flags=["financial_sensitive"])
    assert out["risk_band"] == "CRITICAL"
    assert out["selected_role"] == "principal_architect"
    assert out["review"]["band"] == "CRITICAL"
    assert len(out["review"]["reviewers"]) == 2
    assert out["review"]["independence_required"] is True
    assert set(out["review"]["required_checks"]) == {
        "security", "edge_cases", "rollback", "test_adequacy", "specification_compliance",
    }


# ---------------------------------------------------------------------------
# Scenario 9-11, 16-18
# ---------------------------------------------------------------------------

def test_s9_migration_touching_user_save_data():
    out = r(task_class="MIGRATION", complexity=3, uncertainty=2,
            blast_radius=3, reversibility=3,
            flags=["migration", "data_integrity_sensitive"])
    assert out["risk_band"] == "CRITICAL"
    assert out["selected_role"] == "principal_architect"
    assert "rollback" in out["review"]["required_checks"]
    assert "test_adequacy" in out["review"]["required_checks"]
    assert "migration_data_integrity" in out["band_overrides_applied"]


def test_s10_new_subsystem_with_maximum_uncertainty():
    out = r(task_class="ARCHITECTURE", complexity=2, uncertainty=3,
            blast_radius=1, reversibility=1)
    assert out["selected_role"] == "principal_architect"
    assert band_at_least(out["review"]["band"], "HIGH")


def test_s11_disagreement_judge_is_configured_and_bound():
    """Judge selection is policy, not runtime — assert the binding exists and
    that a CRITICAL route actually names it."""
    d = CFG["review"]["disagreement"]
    assert d["default_judge"] == "principal_architect"
    assert d["resolution"]["PASS+FAIL"] == "judge"
    assert d["resolution"]["FAIL+PASS"] == "judge"
    assert d["resolution"]["FAIL+FAIL"] == "reimplement"

    out = r(task_class="ARCHITECTURE", complexity=3, uncertainty=3,
            blast_radius=3, reversibility=2, flags=["financial_sensitive"])
    # The architect is the implementer here, so it cannot also judge its own
    # work; with no higher tier free, adjudication goes to a human.
    assert out["review"]["judge_unavailable"] is True
    assert out["requires_human_confirmation"] is True


def test_s16_isolation_unavailable_is_a_disclosed_degradation():
    """The router must never claim independence it did not structurally get.
    Degradation is a runtime observation, so the policy's job is to make the
    degraded state representable and to require human confirmation."""
    assert CFG["human_in_the_loop"]["on_any_critical_review"] == "require_human_confirmation"
    assert CFG["human_in_the_loop"]["on_independence_unachievable"] == "terminal"
    out = r(task_class="IMPLEMENTATION", complexity=3, uncertainty=3,
            blast_radius=3, reversibility=2, flags=["security_sensitive"])
    assert out["review"]["independence_required"] is True  # what the policy asks for
    # ...and the runtime must downgrade the *claim*, not the requirement.


def test_s17_unresolvable_model_degrades_instead_of_failing():
    out = r(task_class="INVESTIGATION", complexity=2, uncertainty=2,
            blast_radius=2, unavailable_roles=["reasoning_specialist"])
    assert out["selected_model"], "a route was still emitted"
    assert out["fallbacks_applied"], "the fallback was recorded"
    assert any("reasoning_specialist" in f for f in out["fallbacks_applied"])


def test_s18_both_runtimes_agree_on_role_effort_and_review_band():
    """Parity: given identical inputs and capability, only the resolved model
    ids may differ between runtimes."""
    kw = dict(task_class="DEBUGGING", complexity=2, uncertainty=3,
              blast_radius=2, reversibility=1, flags=["auth_sensitive"])
    a = r(runtime="claude_code", **kw)
    b = r(runtime="codex", **kw)
    assert a["selected_role"] == b["selected_role"]
    assert a["selected_effort"] == b["selected_effort"]
    assert a["review"]["band"] == b["review"]["band"]
    assert a["review"]["reviewers"] == b["review"]["reviewers"]
    assert a["risk_band"] == b["risk_band"]
    # The native effort spelling is the one thing allowed to differ; it is
    # asserted concretely in test_defects.py::test_d9_parity_holds_...


# ---------------------------------------------------------------------------
# Structural invariants — asserted over the whole input space, not one example
# ---------------------------------------------------------------------------

ALL_DIMS = [(c, u, b, rev)
            for c in (0, 3) for u in (0, 3) for b in (0, 3) for rev in (0, 3)]


def test_i1_every_task_reaches_review_selection():
    for task_class in TASK_CLASSES:
        for c, u, b, rev in ALL_DIMS:
            out = r(task_class=task_class, complexity=c, uncertainty=u,
                    blast_radius=b, reversibility=rev)
            assert out["review"]["reviewers"], f"{task_class} {c}{u}{b}{rev} got no reviewer"
            assert out["review"]["band"] in BANDS


@pytest.mark.parametrize("flag", [
    "security_sensitive", "auth_sensitive",
    "financial_sensitive", "data_integrity_sensitive",
])
def test_i2_critical_flag_implies_review_band_at_least_high(flag):
    """Verified for every task class, including the two the previous version
    let slip through."""
    for task_class in TASK_CLASSES:
        out = r(task_class=task_class, flags=[flag])
        assert band_at_least(out["review"]["band"], "HIGH"), (
            f"{task_class} with {flag} produced review band {out['review']['band']}"
        )
        assert out["review"]["independence_required"] is True


@pytest.mark.parametrize("flag", [
    "security_sensitive", "auth_sensitive",
    "financial_sensitive", "data_integrity_sensitive",
])
def test_i3_critical_flag_implies_worker_at_least_balanced(flag):
    for task_class in TASK_CLASSES:
        out = r(task_class=task_class, flags=[flag])
        assert at_least(out["selected_role"], "worker_balanced"), (
            f"{task_class} with {flag} routed to {out['selected_role']}"
        )


def test_i4_emitted_routes_name_only_known_models():
    known = {m["id"] for m in CFG["models"].values()}
    for task_class in TASK_CLASSES:
        for c, u, b, rev in ALL_DIMS:
            out = r(task_class=task_class, complexity=c, uncertainty=u,
                    blast_radius=b, reversibility=rev)
            assert out["selected_model"] in known
            for m in out["review"]["reviewer_models"]:
                assert m in known


def test_i5_rationale_names_band_flags_and_fallbacks():
    out = r(task_class="MIGRATION", complexity=3, uncertainty=2,
            blast_radius=3, reversibility=3,
            flags=["migration", "data_integrity_sensitive"],
            unavailable_roles=["principal_architect"])
    text = out["rationale"]
    assert out["risk_band"] in text
    assert "data_integrity_sensitive" in text
    assert "Fallbacks:" in text
    assert "migration_data_integrity" in text


# ---------------------------------------------------------------------------
# Policy consistency — the config must not contradict itself
# ---------------------------------------------------------------------------

def test_bands_are_contiguous_and_exhaustive():
    ranges = [CFG["router"]["bands"][b] for b in BANDS]
    assert ranges[0]["min"] == 0
    assert ranges[-1]["max"] == 18
    for lower, upper in zip(ranges, ranges[1:]):
        assert upper["min"] == lower["max"] + 1, "bands must be contiguous, no gap or overlap"


def test_every_task_class_has_a_row_for_every_band():
    for task_class in TASK_CLASSES:
        row = CFG["worker_selection"][task_class]
        assert set(row) == set(BANDS), f"{task_class} is missing a band"


def test_every_role_resolves_in_every_binding():
    for name, binding in CFG["role_bindings"].items():
        for role in ROLES:
            assert role in binding, f"binding {name} has no entry for {role}"
            assert binding[role] in CFG["models"], f"{name}.{role} names an unknown model"


def test_effort_maps_cover_every_conceptual_level():
    for runtime, mapping in CFG["effort_map"].items():
        assert set(mapping) == set(CFG["effort_levels"]), f"{runtime} effort map is incomplete"


def test_review_policy_exists_for_every_band():
    for band in BANDS:
        assert band in CFG["review"], f"no review policy for band {band}"


def test_high_and_critical_reviews_are_cross_family_by_construction():
    """Dual review is only worth its cost if the two reviewers fail differently.
    Bind both to one family and the second review is near-redundant."""
    binding = CFG["role_bindings"]["default"]
    fams = {
        role: CFG["models"][binding[role]]["family"]
        for role in ("senior_engineer", "reasoning_specialist")
    }
    assert len(set(fams.values())) == 2, f"HIGH/CRITICAL reviewers share a family: {fams}"


def test_ledger_does_not_overclaim():
    """Any entry marked plainly `verified` must carry evidence, and the two
    known-unproven items must not be marked verified."""
    entries = {e["item"]: e for e in CFG["verification_ledger"]["entries"]}
    for item, e in entries.items():
        assert e.get("evidence"), f"{item} claims a status with no evidence"
    codex_isolation = next(k for k in entries if "Codex native subagent" in k)
    assert entries[codex_isolation]["status"] != "verified"
    worker_fast = next(k for k in entries if "worker_fast binding" in k)
    assert entries[worker_fast]["status"] == "price_verified_quality_unverified"
