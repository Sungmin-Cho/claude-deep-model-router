#!/usr/bin/env python3
"""Deterministic half of the model router.

Classification is a judgment call and stays with the model: this script never
decides a task's class, its dimension scores, or its flags. It takes those as
input and computes everything downstream — score, band, overrides, worker,
effort, review policy, and the concrete model bindings — the same way every
time.

That split is the whole point. The subjective step is where routers go wrong in
interesting ways; the deterministic step is where they go wrong in boring,
testable ways, so it belongs in code with tests around it.

Usage:
    route_task.py --class DEBUGGING --complexity 2 --uncertainty 3 \\
                  --blast-radius 2 --reversibility 1 \\
                  --flags auth_sensitive,unknown_root_cause

    route_task.py --json '{"task_class": "ARCHITECTURE", "complexity": 3, ...}'

Requires PyYAML to read config/model-routing.yaml — there is deliberately no
embedded copy of the policy, so the config cannot drift from the code.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "model-routing.yaml"

BANDS = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
EFFORTS = ["MINIMAL", "LOW", "MEDIUM", "HIGH", "VERY_HIGH", "MAX"]
ROLES = [
    "worker_fast",
    "worker_balanced",
    "senior_engineer",
    "reasoning_specialist",
    "principal_architect",
]
TASK_CLASSES = [
    "MECHANICAL", "IMPLEMENTATION", "DEBUGGING", "REFACTORING", "ARCHITECTURE",
    "INVESTIGATION", "MIGRATION", "REVIEW", "TESTING", "DOCUMENTATION", "OPERATIONS",
]
CRITICAL_DOMAIN_FLAGS = (
    "security_sensitive", "auth_sensitive",
    "financial_sensitive", "data_integrity_sensitive",
)


def load_config(path: Path = CONFIG_PATH) -> dict:
    try:
        import yaml
    except ImportError:
        sys.exit(
            "route_task.py needs PyYAML to read the policy config.\n"
            "  pip install pyyaml\n"
            "The policy is not duplicated in this script on purpose — one source of truth."
        )
    with open(path) as f:
        return yaml.safe_load(f)


# --------------------------------------------------------------------------
# Input
# --------------------------------------------------------------------------

@dataclass
class Task:
    """Everything the model must decide before the deterministic part runs."""

    task_class: str
    complexity: int
    uncertainty: int
    blast_radius: int
    reversibility: int
    reasoning_centric: bool = False
    flags: list[str] = field(default_factory=list)
    prior_failures: int = 0
    prior_models: list[str] = field(default_factory=list)
    implementation_role: str | None = None   # for REVIEW tasks
    runtime: str = "claude_code"             # claude_code | codex
    unavailable_roles: list[str] = field(default_factory=list)

    def __post_init__(self):
        if self.task_class not in TASK_CLASSES:
            raise ValueError(
                f"unknown task_class {self.task_class!r}; expected one of {TASK_CLASSES}"
            )
        for name in ("complexity", "uncertainty", "blast_radius", "reversibility"):
            v = getattr(self, name)
            if not isinstance(v, int) or not 0 <= v <= 3:
                raise ValueError(f"{name} must be an integer 0..3, got {v!r}")
        if self.prior_failures < 0:
            raise ValueError("prior_failures must be >= 0")

    def has(self, flag: str) -> bool:
        return flag in self.flags

    @property
    def critical_flags(self) -> list[str]:
        return [f for f in CRITICAL_DOMAIN_FLAGS if f in self.flags]


# --------------------------------------------------------------------------
# Ordered-enum helpers
# --------------------------------------------------------------------------

def band_max(a: str, b: str) -> str:
    return BANDS[max(BANDS.index(a), BANDS.index(b))]


def effort_max(a: str, b: str) -> str:
    return EFFORTS[max(EFFORTS.index(a), EFFORTS.index(b))]


def role_max(a: str, b: str) -> str:
    return ROLES[max(ROLES.index(a), ROLES.index(b))]


def role_above(role: str) -> str:
    i = ROLES.index(role)
    return ROLES[min(i + 1, len(ROLES) - 1)]


# --------------------------------------------------------------------------
# Stage 2 — score
# --------------------------------------------------------------------------

def score(task: Task, cfg: dict) -> int:
    w = cfg["router"]["score_weights"]
    return (
        task.complexity * w["complexity"]
        + task.uncertainty * w["uncertainty"]
        + task.blast_radius * w["blast_radius"]
        + task.reversibility * w["reversibility"]
    )


def band_from_score(value: int, cfg: dict) -> str:
    for name in BANDS:
        b = cfg["router"]["bands"][name]
        if b["min"] <= value <= b["max"]:
            return name
    raise ValueError(f"score {value} falls outside every band — check score_weights")


# --------------------------------------------------------------------------
# Stage 3 — overrides. Unconditional, never returns early.
# --------------------------------------------------------------------------

def apply_overrides(task: Task, band: str) -> tuple[str, list[str]]:
    applied: list[str] = []
    crit = task.critical_flags

    if crit:
        band = band_max(band, "HIGH")
        applied.append("critical_domain")
    if crit and task.reversibility >= 2:
        band = "CRITICAL"
        applied.append("critical_irreversible")
    if task.has("migration") and task.has("data_integrity_sensitive"):
        band = "CRITICAL"
        applied.append("migration_data_integrity")
    if task.has("production_hotfix"):
        band = band_max(band, "HIGH")
        applied.append("production_hotfix")
    if task.has("public_api_change"):
        band = band_max(band, "MEDIUM")
        applied.append("public_api_change")

    return band, applied


# --------------------------------------------------------------------------
# Stage 4 — worker. Class dispatch, but no early return from the pipeline.
# --------------------------------------------------------------------------

def select_worker(task: Task, band: str, cfg: dict) -> tuple[str, list[str]]:
    notes: list[str] = []
    cell = cfg["worker_selection"][task.task_class][band]
    if cell == "by_reasoning_centric":
        worker = "reasoning_specialist" if task.reasoning_centric else "senior_engineer"
        notes.append(f"reasoning_centric={task.reasoning_centric} selected {worker}")
    else:
        worker = cell

    # Class-specific promotions, applied after the table.
    if task.task_class == "ARCHITECTURE" and (task.uncertainty == 3 or task.has("long_horizon")):
        if worker != "principal_architect":
            worker = "principal_architect"
            notes.append("architecture promotion: uncertainty==3 or long_horizon")

    if task.task_class == "DEBUGGING" and task.has("unknown_root_cause") and task.prior_failures >= 2:
        target = "reasoning_specialist" if task.reasoning_centric else "senior_engineer"
        if ROLES.index(target) > ROLES.index(worker):
            worker = target
            notes.append("debugging promotion: unknown root cause after 2+ failures")

    if task.task_class == "INVESTIGATION" and task.has("unknown_root_cause"):
        if ROLES.index("worker_balanced") > ROLES.index(worker):
            worker = "worker_balanced"
            notes.append("investigation promotion: unknown root cause")

    # Invariant: a critical-domain flag floors the worker at worker_balanced.
    if task.critical_flags:
        promoted = role_max(worker, "worker_balanced")
        if promoted != worker:
            notes.append("critical-domain floor raised worker to worker_balanced")
            worker = promoted

    # Never retry the tier that already failed.
    if task.prior_failures >= 1 and task.prior_models:
        failed = [m for m in task.prior_models if m in ROLES]
        if failed:
            highest_failed = max(failed, key=ROLES.index)
            promoted = role_max(worker, role_above(highest_failed))
            if promoted != worker:
                notes.append(f"escalated above failed tier {highest_failed}")
                worker = promoted

    return worker, notes


# --------------------------------------------------------------------------
# Stage 5 — effort
# --------------------------------------------------------------------------

def select_effort(task: Task, band: str, cfg: dict) -> tuple[str, list[str]]:
    notes: list[str] = []
    table = cfg["effort_by_work"]

    if task.task_class in ("MECHANICAL",):
        effort = table["formatting_rename"]
    elif task.task_class == "DOCUMENTATION":
        effort = table["boilerplate"]
    elif task.task_class in ("DEBUGGING", "INVESTIGATION"):
        effort = table["unknown_root_cause"] if task.has("unknown_root_cause") else table["debugging"]
    elif task.task_class == "REFACTORING":
        effort = table["multi_system_refactoring"] if task.has("cross_service_change") else table["refactoring"]
    elif task.task_class in ("ARCHITECTURE", "MIGRATION"):
        effort = table["complex_architecture"] if band == "CRITICAL" else table["architecture"]
    elif task.task_class == "REVIEW":
        effort = table["adversarial_review"] if band == "CRITICAL" else table["standard_review"]
    else:  # IMPLEMENTATION, TESTING, OPERATIONS
        effort = table["multi_file_feature"] if task.complexity >= 2 else table["straightforward_impl"]

    floors = cfg["effort_floors"]
    if band == "HIGH":
        raised = effort_max(effort, floors["band_HIGH"])
        if raised != effort:
            notes.append(f"band HIGH floored effort at {floors['band_HIGH']}")
            effort = raised
    if band == "CRITICAL":
        raised = effort_max(effort, floors["band_CRITICAL"])
        if raised != effort:
            notes.append(f"band CRITICAL floored effort at {floors['band_CRITICAL']}")
            effort = raised
    if task.critical_flags:
        raised = effort_max(effort, floors["any_critical_domain"])
        if raised != effort:
            notes.append("critical-domain flag floored effort at HIGH")
            effort = raised

    return effort, notes


# --------------------------------------------------------------------------
# Stage 6 — review. Depends on BAND ONLY.
# --------------------------------------------------------------------------

def select_review(band: str, worker: str, cfg: dict) -> dict:
    spec = dict(cfg["review"][band])

    if band == "MEDIUM":
        preferred = spec["preferred_by_implementer"].get(worker)
        candidates = spec["candidates"]
        chosen = preferred if preferred in candidates else candidates[0]
        spec = {
            "reviewers": [chosen],
            "effort": spec["effort"],
            "independent": spec["independent"],
            "prefer_cross_family": spec["prefer_cross_family"],
        }

    spec.setdefault("required_checks", [])
    spec["band"] = band
    return spec


# --------------------------------------------------------------------------
# Stage 7 — resolve roles to concrete models, with fallbacks
# --------------------------------------------------------------------------

def resolve(roles: list[str], task: Task, cfg: dict) -> tuple[dict[str, str], list[str]]:
    binding_name = "default"
    if "bridge_down" in task.flags:
        binding_name = "claude_only" if task.runtime == "claude_code" else "openai_only"

    binding = cfg["role_bindings"][binding_name]
    fallbacks: list[str] = []
    resolved: dict[str, str] = {}

    for role in roles:
        key = binding.get(role)
        if role in task.unavailable_roles or key is None:
            degraded = cfg["role_bindings"][
                "claude_only" if task.runtime == "claude_code" else "openai_only"
            ].get(role)
            if degraded is None:
                degraded = binding.get("worker_balanced")
            fallbacks.append(f"{role}: unavailable -> {degraded}")
            key = degraded
        resolved[role] = cfg["models"][key]["id"]

    if binding_name != "default":
        fallbacks.append(f"binding degraded to {binding_name} (cross-provider bridge down)")

    return resolved, fallbacks


def families(resolved: dict[str, str], cfg: dict) -> dict[str, str]:
    by_id = {m["id"]: m["family"] for m in cfg["models"].values()}
    return {role: by_id[model_id] for role, model_id in resolved.items()}


# --------------------------------------------------------------------------
# Stage 8 — emit
# --------------------------------------------------------------------------

def route(task: Task, cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()

    risk_score = score(task, cfg)
    band = band_from_score(risk_score, cfg)
    band, overrides = apply_overrides(task, band)

    worker, worker_notes = select_worker(task, band, cfg)
    effort, effort_notes = select_effort(task, band, cfg)
    review = select_review(band, worker, cfg)

    def roles_for(rev: dict) -> list[str]:
        needed = [worker] + list(rev["reviewers"])
        if band == "CRITICAL":
            needed.append(cfg["review"]["disagreement"]["default_judge"])
        return list(dict.fromkeys(needed))

    # Resolution runs twice on purpose. The first pass exists only to learn
    # which fallbacks apply, because routing confidence depends on them; the
    # confidence may then raise the review band, which changes the reviewer
    # set. Resolving once up front would emit reviewer roles that were never
    # checked for availability — the exact thing invariant I4 forbids.
    _, fallbacks = resolve(roles_for(review), task, cfg)

    confidence = routing_confidence(task, band, fallbacks)
    review_band = review["band"]
    if confidence < cfg["router"]["confidence"]["extra_review_below"] and review_band != "CRITICAL":
        review_band = BANDS[BANDS.index(review_band) + 1]
        review = select_review(review_band, worker, cfg)
        overrides.append(f"low_routing_confidence_raised_review_to_{review_band}")

    resolved, fallbacks = resolve(roles_for(review), task, cfg)
    fams = families(resolved, cfg)

    reviewer_families = {fams[r] for r in review["reviewers"] if r in fams}
    cross_family = len(reviewer_families) > 1 or (
        len(review["reviewers"]) == 1
        and review["reviewers"][0] in fams
        and worker in fams
        and fams[review["reviewers"][0]] != fams[worker]
    )

    effort_key = "claude_code" if task.runtime == "claude_code" else "codex"
    native_effort = cfg["effort_map"][effort_key][effort]

    return {
        "risk_score": risk_score,
        "risk_band": band,
        "band_overrides_applied": overrides,
        "critical_flags": task.critical_flags,
        "selected_role": worker,
        "selected_model": resolved.get(worker),
        "selected_effort": effort,
        "selected_effort_native": native_effort,
        "review": {
            "band": review["band"],
            "reviewers": review["reviewers"],
            "reviewer_models": [resolved.get(r) for r in review["reviewers"]],
            "effort": review["effort"],
            "independent": review["independent"],
            "required_checks": review.get("required_checks", []),
            "judge": cfg["review"]["disagreement"]["default_judge"] if band == "CRITICAL" else None,
        },
        "cross_family_review": cross_family,
        "fallbacks_applied": fallbacks,
        "routing_confidence": confidence,
        "notes": worker_notes + effort_notes,
        "rationale": explain(task, risk_score, band, overrides, worker, effort, review, fallbacks),
    }


def routing_confidence(task: Task, band: str, fallbacks: list[str]) -> float:
    """A rough, deliberately conservative self-assessment.

    Confidence drops where the inputs themselves are shaky — maximum uncertainty,
    repeated prior failures, degraded bindings — because those are exactly the
    situations where a confidently-wrong route costs the most.
    """
    c = 0.95
    if task.uncertainty == 3:
        c -= 0.20
    elif task.uncertainty == 2:
        c -= 0.08
    if task.prior_failures >= 2:
        c -= 0.15
    elif task.prior_failures == 1:
        c -= 0.05
    if fallbacks:
        c -= 0.10
    if task.has("unknown_root_cause"):
        c -= 0.10
    return round(max(0.0, min(1.0, c)), 2)


def explain(task, risk_score, band, overrides, worker, effort, review, fallbacks) -> str:
    parts = [
        f"{task.task_class} scored {risk_score}/18 "
        f"(c={task.complexity} u={task.uncertainty} b={task.blast_radius} r={task.reversibility}) "
        f"-> band {band}."
    ]
    if overrides:
        parts.append(f"Overrides applied: {', '.join(overrides)}.")
    if task.critical_flags:
        parts.append(f"Critical-domain flags: {', '.join(task.critical_flags)}.")
    parts.append(f"Worker {worker} at {effort} effort.")
    parts.append(
        f"Review band {review['band']}: {', '.join(review['reviewers'])} "
        f"at {review['effort']}, independent={review['independent']}."
    )
    if review.get("required_checks"):
        parts.append(f"Required checks: {', '.join(review['required_checks'])}.")
    if fallbacks:
        parts.append(f"Fallbacks: {'; '.join(fallbacks)}.")
    else:
        parts.append("No fallbacks applied.")
    return " ".join(parts)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--json", help="full task as a JSON object; overrides the flags below")
    p.add_argument("--class", dest="task_class", choices=TASK_CLASSES)
    p.add_argument("--complexity", type=int)
    p.add_argument("--uncertainty", type=int)
    p.add_argument("--blast-radius", type=int)
    p.add_argument("--reversibility", type=int)
    p.add_argument("--reasoning-centric", action="store_true")
    p.add_argument("--flags", default="", help="comma-separated flag names")
    p.add_argument("--prior-failures", type=int, default=0)
    p.add_argument("--prior-models", default="", help="comma-separated role aliases that already failed")
    p.add_argument("--runtime", default="claude_code", choices=["claude_code", "codex"])
    p.add_argument("--unavailable", default="", help="comma-separated roles to treat as unavailable")
    p.add_argument("--format", default="text", choices=["text", "json"])
    args = p.parse_args(argv)

    def split(s: str) -> list[str]:
        return [x.strip() for x in s.split(",") if x.strip()]

    if args.json:
        payload = json.loads(args.json)
        task = Task(**payload)
    else:
        missing = [n for n in ("task_class", "complexity", "uncertainty", "blast_radius", "reversibility")
                   if getattr(args, n) is None]
        if missing:
            p.error(f"missing required arguments: {', '.join('--' + m.replace('_', '-') for m in missing)}")
        task = Task(
            task_class=args.task_class,
            complexity=args.complexity,
            uncertainty=args.uncertainty,
            blast_radius=args.blast_radius,
            reversibility=args.reversibility,
            reasoning_centric=args.reasoning_centric,
            flags=split(args.flags),
            prior_failures=args.prior_failures,
            prior_models=split(args.prior_models),
            runtime=args.runtime,
            unavailable_roles=split(args.unavailable),
        )

    result = route(task)

    if args.format == "json":
        print(json.dumps(result, indent=2))
        return 0

    r = result
    print(f"risk_score:  {r['risk_score']}")
    print(f"risk_band:   {r['risk_band']}")
    print(f"overrides:   {r['band_overrides_applied'] or '(none)'}")
    print(f"worker:      {r['selected_role']}  ->  {r['selected_model']}")
    print(f"effort:      {r['selected_effort']}  (native: {r['selected_effort_native']})")
    print("review:")
    print(f"  band:        {r['review']['band']}")
    print(f"  reviewers:   {', '.join(r['review']['reviewers'])}")
    print(f"  models:      {', '.join(m for m in r['review']['reviewer_models'] if m)}")
    print(f"  effort:      {r['review']['effort']}")
    print(f"  independent: {r['review']['independent']}")
    if r["review"]["required_checks"]:
        print(f"  checks:      {', '.join(r['review']['required_checks'])}")
    if r["review"]["judge"]:
        print(f"  judge:       {r['review']['judge']}")
    print(f"cross_family_review: {r['cross_family_review']}")
    print(f"fallbacks:   {r['fallbacks_applied'] or '(none)'}")
    print(f"confidence:  {r['routing_confidence']}")
    if r["notes"]:
        print("notes:")
        for n in r["notes"]:
            print(f"  - {n}")
    print()
    print(r["rationale"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
