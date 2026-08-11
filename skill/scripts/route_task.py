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

The taxonomy (bands, roles, task classes, flags, efforts) and the override
rules are read from config/model-routing.yaml rather than restated here. A
constant duplicated in code is a second source of truth that drifts silently,
and the config's claim to be authoritative has to be true to be useful.

Usage:
    route_task.py --class DEBUGGING --complexity 2 --uncertainty 3 \\
                  --blast-radius 2 --reversibility 1 \\
                  --flags auth_sensitive,unknown_root_cause

    route_task.py --json '{"task_class": "ARCHITECTURE", "complexity": 3, ...}'

Exit status is nonzero when the route reaches a terminal state — retry budget
exhausted, or routing confidence below the escalation floor. Those are normal
outcomes that need a human, not routes to execute.

Requires PyYAML to read the config. There is deliberately no embedded copy of
the policy.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "model-routing.yaml"


class ValidationError(ValueError):
    """Raised for any malformed routing input, from the CLI or the API."""


def load_config(path: Path = CONFIG_PATH) -> dict:
    try:
        import yaml
    except ImportError:  # pragma: no cover - environment-dependent
        sys.exit(
            "route_task.py needs PyYAML to read the policy config.\n"
            "  pip install pyyaml\n"
            "The policy is not duplicated in this script on purpose — one source of truth."
        )
    with open(path) as f:
        return yaml.safe_load(f)


_CFG = load_config()

# --------------------------------------------------------------------------
# Taxonomy, derived from the config so there is exactly one source of truth.
# --------------------------------------------------------------------------

BANDS: list[str] = sorted(_CFG["router"]["bands"], key=lambda b: _CFG["router"]["bands"][b]["ordinal"])
EFFORTS: list[str] = list(_CFG["effort_levels"])
ROLES: list[str] = list(_CFG["role_tiers"])
TASK_CLASSES: list[str] = list(_CFG["worker_selection"])
CRITICAL_DOMAIN_FLAGS: tuple[str, ...] = tuple(_CFG["flags"]["critical_domain"])
KNOWN_FLAGS: frozenset[str] = frozenset(f for group in _CFG["flags"].values() for f in group)
RUNTIMES: frozenset[str] = frozenset(_CFG["effort_map"])
MODEL_KEYS: frozenset[str] = frozenset(_CFG["models"])
MODEL_IDS: frozenset[str] = frozenset(m["id"] for m in _CFG["models"].values())
MODEL_ID_TO_KEY: dict[str, str] = {m["id"]: k for k, m in _CFG["models"].items()}


# --------------------------------------------------------------------------
# Input
# --------------------------------------------------------------------------

@dataclass
class Task:
    """Everything the model must decide before the deterministic part runs.

    Validation is strict and loud. Silently ignoring an unknown flag is how a
    classifier ends up believing it asked for a protection it never got.
    """

    task_class: str
    complexity: int
    uncertainty: int
    blast_radius: int
    reversibility: int
    reasoning_centric: bool = False
    flags: list[str] = field(default_factory=list)
    prior_failures: int = 0
    prior_models: list[str] = field(default_factory=list)
    implementation_role: str | None = None      # for REVIEW tasks
    runtime: str = "claude_code"
    unavailable_roles: list[str] = field(default_factory=list)
    unavailable_models: list[str] = field(default_factory=list)
    isolation_available: bool | None = None     # None = not established

    def __post_init__(self):
        self._require_choice("task_class", self.task_class, TASK_CLASSES)
        for name in ("complexity", "uncertainty", "blast_radius", "reversibility"):
            self._require_score(name, getattr(self, name))
        self._require_bool("reasoning_centric", self.reasoning_centric)
        self._require_int("prior_failures", self.prior_failures, minimum=0)
        self._require_choice("runtime", self.runtime, sorted(RUNTIMES))

        self._require_str_list("flags", self.flags, KNOWN_FLAGS, "flag")
        self._require_str_list("unavailable_roles", self.unavailable_roles, frozenset(ROLES), "role")
        self._require_str_list("unavailable_models", self.unavailable_models, MODEL_IDS, "model id")
        # prior_models accepts either a role alias or a concrete model id,
        # because selected_model is emitted as an id and feeding it back to the
        # next round has to work.
        self._require_str_list(
            "prior_models", self.prior_models, frozenset(ROLES) | MODEL_IDS, "role or model id"
        )

        if self.implementation_role is not None:
            self._require_choice("implementation_role", self.implementation_role, ROLES)
        if self.isolation_available is not None:
            self._require_bool("isolation_available", self.isolation_available)

    # -- validators ------------------------------------------------------

    @staticmethod
    def _require_choice(name, value, allowed):
        if value not in allowed:
            raise ValidationError(f"{name}: {value!r} is not one of {sorted(allowed)}")

    @staticmethod
    def _require_int(name, value, minimum=None):
        # bool is a subclass of int; True as a dimension score is a type error.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(f"{name}: expected an integer, got {type(value).__name__}")
        if minimum is not None and value < minimum:
            raise ValidationError(f"{name}: must be >= {minimum}, got {value}")

    @classmethod
    def _require_score(cls, name, value):
        cls._require_int(name, value)
        if not 0 <= value <= 3:
            raise ValidationError(f"{name}: must be between 0 and 3, got {value}")

    @staticmethod
    def _require_bool(name, value):
        if not isinstance(value, bool):
            raise ValidationError(
                f"{name}: expected a boolean, got {type(value).__name__} {value!r}"
            )

    @staticmethod
    def _require_str_list(name, value, allowed, label):
        if isinstance(value, str) or not isinstance(value, (list, tuple)):
            raise ValidationError(f"{name}: expected a list, got {type(value).__name__}")
        for item in value:
            if not isinstance(item, str):
                raise ValidationError(f"{name}: expected strings, got {type(item).__name__}")
            if item not in allowed:
                raise ValidationError(f"{name}: unknown {label} {item!r}")

    # -- convenience -----------------------------------------------------

    def has(self, flag: str) -> bool:
        return flag in self.flags

    @property
    def critical_flags(self) -> list[str]:
        return [f for f in CRITICAL_DOMAIN_FLAGS if f in self.flags]

    @property
    def failed_roles(self) -> list[str]:
        """Prior failures normalized to role aliases."""
        out = []
        for item in self.prior_models:
            if item in ROLES:
                out.append(item)
            else:
                key = MODEL_ID_TO_KEY[item]
                out.extend(r for r, k in _CFG["role_bindings"]["default"].items()
                           if k == key and r in ROLES)
        return out


# --------------------------------------------------------------------------
# Ordered-enum helpers
# --------------------------------------------------------------------------

def band_max(a: str, b: str) -> str:
    return BANDS[max(BANDS.index(a), BANDS.index(b))]


def effort_max(a: str, b: str) -> str:
    return EFFORTS[max(EFFORTS.index(a), EFFORTS.index(b))]


def effort_up(effort: str, steps: int = 1) -> str:
    return EFFORTS[min(EFFORTS.index(effort) + steps, len(EFFORTS) - 1)]


def role_max(a: str, b: str) -> str:
    return ROLES[max(ROLES.index(a), ROLES.index(b))]


def role_above(role: str, steps: int = 1) -> str:
    return ROLES[min(ROLES.index(role) + steps, len(ROLES) - 1)]


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
    raise ValidationError(f"score {value} falls outside every band — check score_weights")


# --------------------------------------------------------------------------
# Stage 3 — overrides, evaluated from config. Unconditional, never early-returns.
# --------------------------------------------------------------------------

def _predicate(node: Any, task: Task, cfg: dict) -> bool:
    if not isinstance(node, dict) or len(node) != 1:
        raise ValidationError(f"malformed override predicate: {node!r}")
    (op, arg), = node.items()
    if op == "flag":
        return task.has(arg)
    if op == "any_flag_in":
        return any(task.has(f) for f in cfg["flags"][arg])
    if op == "dimension_at_least":
        (dim, threshold), = arg.items()
        return getattr(task, dim) >= threshold
    if op == "all":
        return all(_predicate(sub, task, cfg) for sub in arg)
    if op == "any":
        return any(_predicate(sub, task, cfg) for sub in arg)
    raise ValidationError(f"unknown override operator: {op!r}")


def apply_overrides(task: Task, band: str, cfg: dict) -> tuple[str, list[str], str | None]:
    applied: list[str] = []
    route_path: str | None = None

    for entry in cfg["overrides"]:
        if not _predicate(entry["when"], task, cfg):
            continue
        effect = entry["effect"]
        changed = False
        if "band_at_least" in effect:
            new = band_max(band, effect["band_at_least"])
            changed = new != band
            band = new
        if "band_exactly" in effect:
            changed = changed or band != effect["band_exactly"]
            band = effect["band_exactly"]
        if "route" in effect:
            route_path = effect["route"]
            changed = True
        # Record the override whenever its predicate held, even if the band was
        # already high enough — the rationale should show what fired, not only
        # what moved the number.
        applied.append(entry["name"])

    return band, applied, route_path


# --------------------------------------------------------------------------
# Stage 4 — worker
# --------------------------------------------------------------------------

def select_worker(task: Task, band: str, cfg: dict) -> tuple[str, list[str]]:
    notes: list[str] = []
    cell = cfg["worker_selection"][task.task_class][band]
    if cell == "by_reasoning_centric":
        worker = "reasoning_specialist" if task.reasoning_centric else "senior_engineer"
        notes.append(f"reasoning_centric={task.reasoning_centric} selected {worker}")
    else:
        worker = cell

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

    if task.critical_flags:
        promoted = role_max(worker, cfg["router"]["floors"]["critical_domain_worker"])
        if promoted != worker:
            notes.append("critical-domain floor raised worker to worker_balanced")
            worker = promoted

    # Never hand the task back to a tier that already failed. When the caller
    # reported failures but named no model, escalate anyway — the retry rule is
    # the loop-prevention control, and letting it lapse on a missing optional
    # field defeats it exactly when it matters.
    if task.prior_failures >= 1:
        failed = task.failed_roles
        if failed:
            highest = max(failed, key=ROLES.index)
            promoted = role_max(worker, role_above(highest))
            if promoted != worker:
                notes.append(f"escalated above failed tier {highest}")
                worker = promoted
        else:
            promoted = role_above(worker, task.prior_failures)
            if promoted != worker:
                notes.append(
                    f"escalated above failed tier (unnamed) after {task.prior_failures} failure(s)"
                )
                worker = promoted

    return worker, notes


# --------------------------------------------------------------------------
# Stage 5 — effort
# --------------------------------------------------------------------------

def select_effort(task: Task, band: str, cfg: dict) -> tuple[str, list[str]]:
    notes: list[str] = []
    table = cfg["effort_by_work"]

    if task.task_class == "MECHANICAL":
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
    else:
        effort = table["multi_file_feature"] if task.complexity >= 2 else table["straightforward_impl"]

    floors = cfg["effort_floors"]
    for condition, floor, why in (
        (band == "HIGH", floors["band_HIGH"], "band HIGH"),
        (band == "CRITICAL", floors["band_CRITICAL"], "band CRITICAL"),
        (bool(task.critical_flags), floors["any_critical_domain"], "critical-domain flag"),
    ):
        if condition:
            raised = effort_max(effort, floor)
            if raised != effort:
                notes.append(f"{why} floored effort at {floor}")
                effort = raised

    return effort, notes


# --------------------------------------------------------------------------
# Stage 6 — review, by band alone
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
# Stage 7 — resolve roles to concrete models
# --------------------------------------------------------------------------

def _blocked_models(task: Task, cfg: dict, binding: dict) -> set[str]:
    """Every model id the caller told us not to emit."""
    blocked = set(task.unavailable_models)
    for role in task.unavailable_roles:
        key = binding.get(role)
        if key:
            blocked.add(cfg["models"][key]["id"])
    return blocked


def _candidates_for(role: str, task: Task, cfg: dict, binding: dict) -> list[str]:
    """Ordered registry keys to try for a role, best first."""
    ordered: list[str] = []
    primary = binding.get(role)
    if primary:
        ordered.append(primary)
    ordered.extend(cfg["fallbacks"].get(task.runtime, {}).get(role, []))
    # Degraded single-provider binding, then the role-tier ladder upward and
    # downward. Upward first: a stronger substitute is a safer degradation than
    # a weaker one.
    degraded_name = "claude_only" if task.runtime == "claude_code" else "openai_only"
    degraded = cfg["role_bindings"][degraded_name].get(role)
    if degraded:
        ordered.append(degraded)
    index = ROLES.index(role)
    for other in ROLES[index + 1:] + ROLES[:index][::-1]:
        key = binding.get(other)
        if key:
            ordered.append(key)
    seen: set[str] = set()
    return [k for k in ordered if not (k in seen or seen.add(k))]


def resolve(roles: list[str], task: Task, cfg: dict) -> tuple[dict[str, str], list[str], set[str]]:
    binding_name = "default"
    fallbacks: list[str] = []
    if task.has("bridge_down"):
        binding_name = "claude_only" if task.runtime == "claude_code" else "openai_only"
        fallbacks.append(f"binding degraded to {binding_name} (cross-provider bridge down)")

    binding = cfg["role_bindings"][binding_name]
    blocked = _blocked_models(task, cfg, cfg["role_bindings"]["default"]) | _blocked_models(task, cfg, binding)
    resolved: dict[str, str] = {}

    for role in roles:
        primary_key = binding.get(role)
        primary_id = cfg["models"][primary_key]["id"] if primary_key else None
        chosen_key = None
        for key in _candidates_for(role, task, cfg, binding):
            model_id = cfg["models"][key]["id"]
            if model_id in blocked:
                continue
            chosen_key = key
            break
        if chosen_key is None:
            raise ValidationError(
                f"no available model for role {role!r}: every candidate is unavailable. "
                "This is an operational failure, not a route."
            )
        chosen_id = cfg["models"][chosen_key]["id"]
        resolved[role] = chosen_id
        # Only record a fallback when the emitted model actually changed. A
        # recorded no-op reads as a managed degradation when none occurred.
        if primary_id is not None and chosen_id != primary_id:
            fallbacks.append(f"{role}: {primary_id} unavailable -> {chosen_id}")

    return resolved, fallbacks, blocked


def families(resolved: dict[str, str], cfg: dict) -> dict[str, str]:
    by_id = {m["id"]: m["family"] for m in cfg["models"].values()}
    return {role: by_id[model_id] for role, model_id in resolved.items()}


# --------------------------------------------------------------------------
# Independence
# --------------------------------------------------------------------------

def independence(review: dict, task: Task) -> str:
    """Separate the policy requirement from what was actually enforced.

    `independent` is what the band asks for. `review_independence` is what the
    runtime can prove it got. Reporting the first as if it were the second is
    the single most damaging thing this router can do, because it converts a
    safety control into a false assurance — so an unestablished isolation
    capability reports `degraded`, never `enforced`.
    """
    if not review["independent"]:
        return "not_applicable"
    if task.isolation_available is True:
        return "enforced"
    return "degraded"


# --------------------------------------------------------------------------
# Confidence
# --------------------------------------------------------------------------

def routing_confidence(task: Task, band: str, fallbacks: list[str]) -> float:
    """A deliberately conservative self-assessment.

    Confidence drops where the inputs themselves are shaky — maximum
    uncertainty, repeated prior failures, degraded bindings — because those are
    the situations where a confidently wrong route costs the most.
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


# --------------------------------------------------------------------------
# Stage 8 — emit
# --------------------------------------------------------------------------

def route(task: Task, cfg: dict | None = None) -> dict:
    cfg = cfg or _CFG

    risk_score = score(task, cfg)
    band = band_from_score(risk_score, cfg)
    band, overrides, route_path = apply_overrides(task, band, cfg)

    worker, worker_notes = select_worker(task, band, cfg)
    effort, effort_notes = select_effort(task, band, cfg)
    review = select_review(band, worker, cfg)

    disagreement = cfg["review"]["disagreement"]

    def roles_for(rev: dict) -> list[str]:
        needed = [worker] + list(rev["reviewers"])
        # The judge follows the REVIEW band, not the risk band. A review
        # promoted to CRITICAL by low confidence needs adjudication just as
        # much as one that scored there.
        if rev["band"] == "CRITICAL" or route_path == "disagreement":
            needed.append(disagreement["default_judge"])
        return list(dict.fromkeys(needed))

    # Resolution runs twice on purpose. The first pass exists only to learn
    # which fallbacks apply, because routing confidence depends on them; the
    # confidence may then raise the review band, which changes the reviewer
    # set. Resolving once up front would emit reviewer roles that were never
    # checked for availability.
    _, probe_fallbacks, _ = resolve(roles_for(review), task, cfg)

    confidence = routing_confidence(task, band, probe_fallbacks)
    if confidence < cfg["router"]["confidence"]["extra_review_below"] and review["band"] != "CRITICAL":
        promoted = BANDS[BANDS.index(review["band"]) + 1]
        review = select_review(promoted, worker, cfg)
        overrides.append(f"low_routing_confidence_raised_review_to_{promoted}")

    resolved, fallbacks, blocked = resolve(roles_for(review), task, cfg)
    fams = families(resolved, cfg)

    reviewer_families = {fams[r] for r in review["reviewers"] if r in fams}
    cross_family = len(reviewer_families) > 1 or (
        len(review["reviewers"]) == 1
        and review["reviewers"][0] in fams
        and worker in fams
        and fams[review["reviewers"][0]] != fams[worker]
    )

    review_independence = independence(review, task)
    judge = disagreement["default_judge"] if (
        review["band"] == "CRITICAL" or route_path == "disagreement"
    ) else None

    # Terminal states. These are normal outcomes that need a human, not routes
    # to execute — so no executable model is emitted for them.
    terminal = None
    retry_cap = cfg["retry"]["max_total_implementation_attempts"]
    if task.prior_failures >= retry_cap:
        terminal = "HUMAN_REQUIRED"
    elif confidence < cfg["router"]["confidence"]["escalate_below"]:
        terminal = "ESCALATE_ROUTING"

    requires_human = bool(
        terminal
        or (review["band"] == "CRITICAL" and review_independence == "degraded")
    )

    effort_key = task.runtime
    native_effort = cfg["effort_map"][effort_key][effort]

    result = {
        "task_class": task.task_class,
        "complexity": task.complexity,
        "uncertainty": task.uncertainty,
        "blast_radius": task.blast_radius,
        "reversibility": task.reversibility,
        "reasoning_centric": task.reasoning_centric,
        "risk_score": risk_score,
        "risk_band": band,
        "band_overrides_applied": overrides,
        "critical_flags": task.critical_flags,
        "route_path": route_path,
        "terminal": terminal,
        "selected_role": None if terminal else worker,
        "selected_model": None if terminal else resolved.get(worker),
        "selected_effort": None if terminal else effort,
        "selected_effort_native": None if terminal else native_effort,
        "review": {
            "band": review["band"],
            "reviewers": review["reviewers"],
            "reviewer_models": [resolved.get(r) for r in review["reviewers"]],
            "effort": review["effort"],
            "independence_required": review["independent"],
            "review_independence": review_independence,
            "required_checks": review.get("required_checks", []),
            "judge": judge,
            "judge_model": resolved.get(judge) if judge else None,
        },
        "cross_family_review": cross_family,
        "fallbacks_applied": fallbacks,
        "unavailable_models": blocked,
        "escalation_count": task.prior_failures,
        "retry_count": task.prior_failures,
        "routing_confidence": confidence,
        "requires_human_confirmation": requires_human,
        "notes": worker_notes + effort_notes,
    }
    result["rationale"] = explain(task, result)
    return result


def explain(task: Task, r: dict) -> str:
    parts = [
        f"{task.task_class} scored {r['risk_score']}/18 "
        f"(c={task.complexity} u={task.uncertainty} b={task.blast_radius} r={task.reversibility}) "
        f"-> band {r['risk_band']}."
    ]
    if r["band_overrides_applied"]:
        parts.append(f"Overrides applied: {', '.join(r['band_overrides_applied'])}.")
    if r["critical_flags"]:
        parts.append(f"Critical-domain flags: {', '.join(r['critical_flags'])}.")
    if r["terminal"]:
        parts.append(
            f"TERMINAL: {r['terminal']} — no executable route emitted; "
            f"routing confidence {r['routing_confidence']} after {task.prior_failures} prior failure(s). "
            "Surface to a human with what was tried, what evidence accumulated, "
            "and the blocking uncertainty."
        )
    else:
        parts.append(f"Worker {r['selected_role']} at {r['selected_effort']} effort.")
    rv = r["review"]
    parts.append(
        f"Review band {rv['band']}: {', '.join(rv['reviewers'])} at {rv['effort']}, "
        f"independence_required={rv['independence_required']}, "
        f"review_independence={rv['review_independence']}."
    )
    if rv["judge"]:
        parts.append(f"Judge: {rv['judge']}.")
    if rv["required_checks"]:
        parts.append(f"Required checks: {', '.join(rv['required_checks'])}.")
    if r["fallbacks_applied"]:
        parts.append(f"Fallbacks: {'; '.join(r['fallbacks_applied'])}.")
    else:
        parts.append("No fallbacks applied.")
    if not r["cross_family_review"] and rv["independence_required"]:
        parts.append("cross_family_review=false — reviewers share a family; weigh the second verdict accordingly.")
    if r["requires_human_confirmation"]:
        parts.append("Requires human confirmation before proceeding.")
    return " ".join(parts)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _split(value: str) -> list[str]:
    return [x.strip() for x in value.split(",") if x.strip()]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--json", help="full task as a JSON object; overrides the flags below")
    p.add_argument("--class", dest="task_class", choices=TASK_CLASSES)
    p.add_argument("--complexity", type=int)
    p.add_argument("--uncertainty", type=int)
    p.add_argument("--blast-radius", type=int)
    p.add_argument("--reversibility", type=int)
    p.add_argument("--reasoning-centric", action="store_true")
    p.add_argument("--flags", default="",
                   help=f"comma-separated; known flags: {', '.join(sorted(KNOWN_FLAGS))}")
    p.add_argument("--prior-failures", type=int, default=0)
    p.add_argument("--prior-models", default="",
                   help="comma-separated role aliases or model ids that already failed")
    p.add_argument("--runtime", default="claude_code", choices=sorted(RUNTIMES))
    p.add_argument("--unavailable", default="", help="comma-separated roles to treat as unavailable")
    p.add_argument("--unavailable-models", default="",
                   help="comma-separated concrete model ids to treat as unavailable")
    p.add_argument("--isolation", choices=["available", "unavailable"],
                   help="whether reviewer context isolation was confirmed this session")
    p.add_argument("--format", default="text", choices=["text", "json"])
    return p


def main(argv: list[str] | None = None) -> int:
    p = build_parser()
    args = p.parse_args(argv)

    try:
        if args.json:
            try:
                payload = json.loads(args.json)
            except json.JSONDecodeError as exc:
                raise ValidationError(f"--json is not valid JSON: {exc}") from None
            if not isinstance(payload, dict):
                raise ValidationError("--json must be a JSON object")
            unknown = set(payload) - set(Task.__dataclass_fields__)
            if unknown:
                raise ValidationError(f"--json has unknown field(s): {', '.join(sorted(unknown))}")
            task = Task(**payload)
        else:
            missing = [n for n in ("task_class", "complexity", "uncertainty",
                                   "blast_radius", "reversibility")
                       if getattr(args, n) is None]
            if missing:
                p.error("missing required arguments: "
                        + ", ".join("--" + m.replace("_", "-") for m in missing))
            isolation = None
            if args.isolation is not None:
                isolation = args.isolation == "available"
            task = Task(
                task_class=args.task_class,
                complexity=args.complexity,
                uncertainty=args.uncertainty,
                blast_radius=args.blast_radius,
                reversibility=args.reversibility,
                reasoning_centric=args.reasoning_centric,
                flags=_split(args.flags),
                prior_failures=args.prior_failures,
                prior_models=_split(args.prior_models),
                runtime=args.runtime,
                unavailable_roles=_split(args.unavailable),
                unavailable_models=_split(args.unavailable_models),
                isolation_available=isolation,
            )
        result = route(task)
    except ValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        print(json.dumps(result, indent=2, default=sorted))
    else:
        _print_text(result)

    return 1 if result["terminal"] else 0


def _print_text(r: dict) -> None:
    print(f"risk_score:  {r['risk_score']}")
    print(f"risk_band:   {r['risk_band']}")
    print(f"overrides:   {r['band_overrides_applied'] or '(none)'}")
    if r["terminal"]:
        print(f"TERMINAL:    {r['terminal']}  — no executable route emitted")
    else:
        print(f"worker:      {r['selected_role']}  ->  {r['selected_model']}")
        print(f"effort:      {r['selected_effort']}  (native: {r['selected_effort_native']})")
    rv = r["review"]
    print("review:")
    print(f"  band:            {rv['band']}")
    print(f"  reviewers:       {', '.join(rv['reviewers'])}")
    print(f"  models:          {', '.join(m for m in rv['reviewer_models'] if m)}")
    print(f"  effort:          {rv['effort']}")
    print(f"  required:        independent={rv['independence_required']}")
    print(f"  actual:          {rv['review_independence']}")
    if rv["required_checks"]:
        print(f"  checks:          {', '.join(rv['required_checks'])}")
    if rv["judge"]:
        print(f"  judge:           {rv['judge']} -> {rv['judge_model']}")
    print(f"cross_family_review: {r['cross_family_review']}")
    print(f"fallbacks:   {r['fallbacks_applied'] or '(none)'}")
    print(f"confidence:  {r['routing_confidence']}")
    if r["requires_human_confirmation"]:
        print("human:       CONFIRMATION REQUIRED")
    if r["notes"]:
        print("notes:")
        for n in r["notes"]:
            print(f"  - {n}")
    print()
    print(r["rationale"])


if __name__ == "__main__":
    raise SystemExit(main())
