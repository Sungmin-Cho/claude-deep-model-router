#!/usr/bin/env python3
"""Deterministic half of the model router.

Classification is a judgment call and stays with the model: this script never
decides a task's class, its dimension scores, or its flags. It takes those as
input and computes everything downstream — score, band, overrides, worker,
effort, review policy, and the concrete model bindings — the same way every
time.

Two properties are load-bearing and were each broken once before:

1. **The emitted route must be executable as written.** Every model named has
   been checked against what the caller said is unavailable, against the
   provider boundary when the bridge is down, and against what already failed.
2. **A recorded change must be a real change.** A fallback is recorded only
   when the model actually differs; an escalation only when the model actually
   moves. Recording a degradation or a promotion that did not happen is worse
   than recording nothing, because it reads as a managed decision.

The taxonomy and the override rules are read from the config rather than
restated here — a constant duplicated in code is a second source of truth that
drifts silently.

Usage:
    route_task.py --class DEBUGGING --complexity 2 --uncertainty 3 \\
                  --blast-radius 2 --reversibility 1 \\
                  --flags auth_sensitive,unknown_root_cause

Exit status is 1 for a terminal state (retry budget spent, or confidence below
the escalation floor), 2 for invalid input, 0 otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "model-routing.yaml"

MAX_PROMOTION_PASSES = 4   # bounded fixed point; the band ladder is only 4 deep


class ValidationError(ValueError):
    """Raised for any malformed routing input, from the CLI or the API."""


class ConfigError(RuntimeError):
    """Raised when the policy config cannot be read or is internally invalid."""


def load_config(path: Path = CONFIG_PATH) -> dict:
    """Read the policy. Raises rather than exiting, so importing this module
    can never terminate the interpreter."""
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise ConfigError(
            "route_task.py needs PyYAML to read the policy config (pip install pyyaml). "
            "The policy is not duplicated in this script on purpose — one source of truth."
        ) from exc
    try:
        with open(path) as f:
            return yaml.safe_load(f)
    except OSError as exc:
        raise ConfigError(f"cannot read policy config at {path}: {exc}") from exc


# --------------------------------------------------------------------------
# Policy — the taxonomy derived from one config, so `route(task, cfg)` honours
# the config it was given all the way down to input validation.
# --------------------------------------------------------------------------

class Policy:
    """Everything derivable from a config, computed once per config."""

    _cache: dict[int, "Policy"] = {}

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.bands: list[str] = sorted(cfg["router"]["bands"], key=lambda b: cfg["router"]["bands"][b]["ordinal"])
        self.efforts: list[str] = list(cfg["effort_levels"])
        self.roles: list[str] = list(cfg["role_tiers"])
        self.task_classes: list[str] = list(cfg["worker_selection"])
        self.critical_domain_flags: tuple[str, ...] = tuple(cfg["flags"]["critical_domain"])
        self.known_flags: frozenset[str] = frozenset(f for g in cfg["flags"].values() for f in g)
        self.runtimes: frozenset[str] = frozenset(cfg["effort_map"])
        self.model_ids: frozenset[str] = frozenset(m["id"] for m in cfg["models"].values())
        self.id_to_key: dict[str, str] = {m["id"]: k for k, m in cfg["models"].items()}
        self.family_of: dict[str, str] = {m["id"]: m["family"] for m in cfg["models"].values()}
        # Which family survives when the cross-provider bridge is down.
        self.local_family: dict[str, str] = {
            runtime: cfg["models"][cfg["role_bindings"][binding]["worker_fast"]]["family"]
            for runtime, binding in (("claude_code", "claude_only"), ("codex", "openai_only"))
        }

    @classmethod
    def of(cls, cfg: dict) -> "Policy":
        key = id(cfg)
        if key not in cls._cache:
            cls._cache[key] = cls(cfg)
        return cls._cache[key]

    # ordered-enum helpers, bound to this policy's vocabulary
    def band_max(self, a, b): return self.bands[max(self.bands.index(a), self.bands.index(b))]
    def effort_max(self, a, b): return self.efforts[max(self.efforts.index(a), self.efforts.index(b))]
    def effort_up(self, e, n=1): return self.efforts[min(self.efforts.index(e) + n, len(self.efforts) - 1)]
    def role_max(self, a, b): return self.roles[max(self.roles.index(a), self.roles.index(b))]
    def role_above(self, r, n=1): return self.roles[min(self.roles.index(r) + n, len(self.roles) - 1)]
    def at_ceiling(self, r): return self.roles.index(r) == len(self.roles) - 1


_DEFAULT_CFG: dict | None = None


def default_config() -> dict:
    """Lazy so a missing or broken config surfaces at call time, not import."""
    global _DEFAULT_CFG
    if _DEFAULT_CFG is None:
        _DEFAULT_CFG = load_config()
    return _DEFAULT_CFG


def _default_policy() -> Policy:
    return Policy.of(default_config())


# Module-level vocabulary, kept for callers that import these names. They
# describe the default config; `route(task, cfg)` uses the cfg it was handed.
def __getattr__(name: str):
    p = _default_policy()
    mapping = {
        "BANDS": p.bands, "EFFORTS": p.efforts, "ROLES": p.roles,
        "TASK_CLASSES": p.task_classes,
        "CRITICAL_DOMAIN_FLAGS": p.critical_domain_flags,
        "KNOWN_FLAGS": p.known_flags, "MODEL_IDS": p.model_ids,
        "RUNTIMES": p.runtimes,
    }
    if name in mapping:
        return mapping[name]
    raise AttributeError(name)


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
    implementation_role: str | None = None
    runtime: str = "claude_code"
    unavailable_roles: list[str] = field(default_factory=list)
    unavailable_models: list[str] = field(default_factory=list)
    # Caller's attestation that reviewer isolation *can* be achieved. This is a
    # capability claim, not proof that it happened — see `isolation_evidence`.
    isolation_available: bool | None = None
    # Post-dispatch proof: one distinct session/process identifier per
    # reviewer. Only this can raise independence to `enforced`.
    isolation_evidence: list[str] = field(default_factory=list)
    # Set by route(); not part of the caller's input contract.
    _policy: Any = field(default=None, repr=False, compare=False)

    def validate(self, policy: Policy) -> None:
        self._require_choice("task_class", self.task_class, policy.task_classes)
        for name in ("complexity", "uncertainty", "blast_radius", "reversibility"):
            self._require_score(name, getattr(self, name))
        self._require_bool("reasoning_centric", self.reasoning_centric)
        self._require_int("prior_failures", self.prior_failures, minimum=0)
        self._require_choice("runtime", self.runtime, sorted(policy.runtimes))
        self._require_str_list("flags", self.flags, policy.known_flags, "flag")
        self._require_str_list("unavailable_roles", self.unavailable_roles,
                               frozenset(policy.roles), "role")
        self._require_str_list("unavailable_models", self.unavailable_models,
                               policy.model_ids, "model id")
        # prior_models accepts a role alias or a concrete model id, because
        # selected_model is emitted as an id and feeding it back must work.
        self._require_str_list("prior_models", self.prior_models,
                               frozenset(policy.roles) | policy.model_ids, "role or model id")
        if self.implementation_role is not None:
            self._require_choice("implementation_role", self.implementation_role, policy.roles)
        if self.isolation_available is not None:
            self._require_bool("isolation_available", self.isolation_available)
        # This field is the only input that can reach `enforced`, so it gets
        # the same strictness as everything else. It previously skipped the
        # shared validator, and a list of integers was enough to report an
        # independence the router had no basis for.
        if isinstance(self.isolation_evidence, str) or not isinstance(self.isolation_evidence, (list, tuple)):
            raise ValidationError("isolation_evidence: expected a list of session identifiers")
        for item in self.isolation_evidence:
            if not isinstance(item, str) or not item.strip():
                raise ValidationError(
                    f"isolation_evidence: expected non-empty strings, got {item!r}"
                )
        self._policy = policy

    # -- validators ------------------------------------------------------

    @staticmethod
    def _require_choice(name, value, allowed):
        if value not in allowed:
            raise ValidationError(f"{name}: {value!r} is not one of {sorted(allowed)}")

    @staticmethod
    def _require_int(name, value, minimum=None):
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
            raise ValidationError(f"{name}: expected a boolean, got {type(value).__name__} {value!r}")

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

    def critical_flags(self, policy: Policy) -> list[str]:
        return [f for f in policy.critical_domain_flags if f in self.flags]

    def failed_roles(self, policy: Policy, binding: dict) -> list[str]:
        """Prior failures normalized to role aliases, resolved against the
        binding actually in force — mapping through the default binding would
        misread a degraded-binding model as a different (usually higher) role."""
        out = []
        for item in self.prior_models:
            if item in policy.roles:
                out.append(item)
            else:
                key = policy.id_to_key[item]
                out.extend(r for r, k in binding.items() if k == key and r in policy.roles)
        return out

    def failed_model_ids(self, policy: Policy, binding: dict) -> set[str]:
        """Concrete models that already failed, whether named as a role or an id.

        A role alias is ambiguous across bindings: `senior_engineer` under a
        degraded binding is a different model from `senior_engineer` under the
        default one, so resolving it through only the *current* binding let a
        model that had just failed come back under a different label after the
        bridge recovered. Every binding that could have produced the alias is
        excluded instead — over-exclusion costs a cheaper model, while
        under-exclusion re-runs a known failure.
        """
        return self.failed_and_ambiguous(policy, binding)[0] | self.failed_and_ambiguous(policy, binding)[1]

    def failed_and_ambiguous(self, policy: Policy, binding: dict) -> tuple[set[str], set[str]]:
        """(models that demonstrably failed, models excluded only as ambiguous).

        Excluding every binding's reading of a role alias is the safe choice —
        under-exclusion re-runs a known failure. But reporting all of them as
        "already failed" states something untrue about models that never ran,
        which is the reporting-layer version of the defect this module is
        built to avoid. The two sets are kept apart so the caution stays and
        the claim stays accurate.
        """
        failed: set[str] = set()
        ambiguous: set[str] = set()
        for item in self.prior_models:
            if item in policy.model_ids:
                failed.add(item)
                continue
            actual = binding.get(item)
            if actual:
                failed.add(policy.cfg["models"][actual]["id"])
            for candidate_binding in policy.cfg["role_bindings"].values():
                if (key := candidate_binding.get(item)):
                    ambiguous.add(policy.cfg["models"][key]["id"])
        return failed, ambiguous - failed


# --------------------------------------------------------------------------
# Stage 2 — score
# --------------------------------------------------------------------------

def score(task: Task, cfg: dict) -> int:
    w = cfg["router"]["score_weights"]
    return (task.complexity * w["complexity"] + task.uncertainty * w["uncertainty"]
            + task.blast_radius * w["blast_radius"] + task.reversibility * w["reversibility"])


def band_from_score(value: int, policy: Policy) -> str:
    for name in policy.bands:
        b = policy.cfg["router"]["bands"][name]
        if b["min"] <= value <= b["max"]:
            return name
    raise ConfigError(f"score {value} falls outside every band — check score_weights")


# --------------------------------------------------------------------------
# Stage 3 — overrides, evaluated from config
# --------------------------------------------------------------------------

KNOWN_EFFECT_KEYS = frozenset({"band_at_least", "band_exactly", "route"})


def _predicate(node: Any, task: Task, cfg: dict) -> bool:
    if not isinstance(node, dict) or len(node) != 1:
        raise ConfigError(f"malformed override predicate: {node!r}")
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
    raise ConfigError(f"unknown override operator: {op!r}")


def apply_overrides(task: Task, band: str, policy: Policy) -> tuple[str, list[str], list[str], str | None]:
    """Returns (band, fired_overrides, redundant_overrides, route_path).

    An unknown effect key raises rather than being skipped. The asymmetry the
    other way round — strict about operators, lax about effects — let a single
    typo turn a safety rule into decoration that still reported itself as
    applied, and no test could tell the difference.

    `redundant` means the override fired and asked for a band the task had
    already reached by another rule. That is expected and healthy: several
    rules independently agreeing on CRITICAL is redundancy by design, not a
    rule that failed to work. It is reported separately only so the rationale
    can show which rule actually moved the number.
    """
    cfg = policy.cfg
    applied: list[str] = []
    redundant: list[str] = []
    route_path: str | None = None

    for entry in cfg["overrides"]:
        effect = entry["effect"]
        unknown = set(effect) - KNOWN_EFFECT_KEYS
        if unknown:
            raise ConfigError(
                f"override {entry['name']!r} has unknown effect key(s) {sorted(unknown)}; "
                f"known keys are {sorted(KNOWN_EFFECT_KEYS)}"
            )
        if not _predicate(entry["when"], task, cfg):
            continue

        changed = False
        if "band_at_least" in effect:
            new = policy.band_max(band, effect["band_at_least"])
            changed |= new != band
            band = new
        if "band_exactly" in effect:
            changed |= band != effect["band_exactly"]
            band = effect["band_exactly"]
        if "route" in effect:
            route_path = effect["route"]
            changed = True

        applied.append(entry["name"])
        if not changed:
            redundant.append(entry["name"])

    return band, applied, redundant, route_path


# --------------------------------------------------------------------------
# Stage 4 — worker
# --------------------------------------------------------------------------

def select_worker(task: Task, band: str, policy: Policy, binding: dict) -> tuple[str, list[str], bool]:
    """Returns (worker, notes, ceiling_exhausted)."""
    cfg = policy.cfg
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
        if policy.roles.index(target) > policy.roles.index(worker):
            worker = target
            notes.append("debugging promotion: unknown root cause after 2+ failures")

    if task.task_class == "INVESTIGATION" and task.has("unknown_root_cause"):
        if policy.roles.index("worker_balanced") > policy.roles.index(worker):
            worker = "worker_balanced"
            notes.append("investigation promotion: unknown root cause")

    if task.critical_flags(policy):
        promoted = policy.role_max(worker, cfg["router"]["floors"]["critical_domain_worker"])
        if promoted != worker:
            notes.append("critical-domain floor raised worker to worker_balanced")
            worker = promoted

    ceiling_exhausted = False
    if task.prior_failures >= 1:
        failed = task.failed_roles(policy, binding)
        if failed:
            highest = max(failed, key=policy.roles.index)
            if policy.at_ceiling(highest):
                # There is no tier above the ceiling. Silently clamping here
                # produced an "escalation" that re-ran the same model.
                ceiling_exhausted = True
                notes.append(f"retry ladder exhausted: {highest} is the top tier")
            promoted = policy.role_max(worker, policy.role_above(highest))
            if promoted != worker:
                notes.append(f"escalated above failed tier {highest}")
                worker = promoted
        else:
            if policy.at_ceiling(worker):
                ceiling_exhausted = True
                notes.append("retry ladder exhausted: already at the top tier")
            promoted = policy.role_above(worker, task.prior_failures)
            if promoted != worker:
                notes.append(f"escalated above failed tier (unnamed) after "
                             f"{task.prior_failures} failure(s)")
                worker = promoted

    return worker, notes, ceiling_exhausted


# --------------------------------------------------------------------------
# Stage 5 — effort
# --------------------------------------------------------------------------

def select_effort(task: Task, band: str, policy: Policy) -> tuple[str, list[str]]:
    cfg = policy.cfg
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
        (bool(task.critical_flags(policy)), floors["any_critical_domain"], "critical-domain flag"),
    ):
        if condition:
            raised = policy.effort_max(effort, floor)
            if raised != effort:
                notes.append(f"{why} floored effort at {floor}")
                effort = raised
    return effort, notes


# --------------------------------------------------------------------------
# Stage 6 — review, by band alone
# --------------------------------------------------------------------------

def select_review(band: str, worker: str, policy: Policy, resolver: "Resolver") -> dict:
    """Review depth follows the band. Which concrete reviewer fills a MEDIUM
    slot additionally considers availability and family, because picking a
    reviewer that then falls back to the implementer's own family throws away
    the diversity that is the point of the slot."""
    cfg = policy.cfg
    spec = dict(cfg["review"][band])

    if band == "MEDIUM":
        worker_family = resolver.family_for_role(worker)
        preferred = spec["preferred_by_implementer"].get(worker)
        ordered = [c for c in ([preferred] if preferred else []) + list(spec["candidates"]) if c]
        seen, ranked = set(), []
        for c in ordered:
            if c not in seen:
                seen.add(c)
                ranked.append(c)
        chosen = None
        for candidate in ranked:                       # first cross-family and available
            model = resolver.peek(candidate)
            if model and policy.family_of[model] != worker_family:
                chosen = candidate
                break
        if chosen is None:
            for candidate in ranked:                   # then merely available
                if resolver.peek(candidate):
                    chosen = candidate
                    break
        spec = {
            "reviewers": [chosen or ranked[0]],
            "effort": spec["effort"],
            "independent": spec["independent"],
            "prefer_cross_family": spec["prefer_cross_family"],
        }

    spec.setdefault("required_checks", [])
    spec["band"] = band

    # An implementer must never be one of its own independent reviewers. At
    # HIGH and CRITICAL the reviewer pair is fixed, so any task whose worker is
    # already `senior_engineer` or `reasoning_specialist` was being reviewed by
    # itself — the exact arrangement dual review exists to prevent, in the most
    # common high-risk routes. Substitute the colliding slot, preferring a
    # replacement from a family the other reviewer does not already cover.
    if spec["independent"]:
        spec = _deconflict(spec, worker, policy, resolver)
    return spec


def _deconflict(spec: dict, worker: str, policy: Policy, resolver: "Resolver") -> dict:
    """Ensure no reviewer resolves to the implementer's model, or to another
    reviewer's.

    The collision test is on the **resolved model**, not the role label. Roles
    are not distinct models: a degraded single-provider binding maps several
    roles onto one id, so a role-level check reported a substitution while the
    same model kept every seat — implementer, both "independent" reviewers, and
    the judge. Recording an avoidance that avoided nothing is the same defect
    this module removed from the fallback and escalation paths, and it is worse
    here because it clears a safety gate.

    When no substitution can break the collision, the caller is told
    (`independence_compromised`) rather than being handed a route that looks
    independent.
    """
    worker_model = resolver.peek(worker)
    worker_tier = policy.roles.index(worker)
    reviewers = list(spec["reviewers"])
    substitutions: list[dict] = []
    taken = {worker_model} if worker_model else set()
    compromised = False

    for index, role in enumerate(reviewers):
        model = resolver.peek(role)
        if model is not None and model not in taken:
            taken.add(model)
            continue

        other_models = {resolver.peek(x) for i, x in enumerate(reviewers) if i != index}
        pool = []
        for candidate in policy.roles:
            if candidate in reviewers:
                continue
            candidate_model = resolver.peek(candidate)
            if candidate_model is None or candidate_model in taken:
                continue
            pool.append((candidate, candidate_model))

        # Strength first, then a family the other reviewer does not cover. A
        # reviewer below the implementer's tier cannot supply the check the
        # implementer could not perform on itself; a lost family difference is
        # a real but lesser cost, and cross_family_review discloses it.
        other_family = next((policy.family_of[m] for m in other_models if m), None)
        pick = max(
            pool,
            key=lambda pair: (policy.roles.index(pair[0]) >= worker_tier,
                              policy.family_of[pair[1]] != other_family,
                              policy.roles.index(pair[0])),
            default=None,
        )
        if pick is None:
            compromised = True
            continue
        substitutions.append({"replaced": role, "with": pick[0],
                              "reason": "would have shared a model with the implementer"
                                        if model == worker_model else
                                        "would have duplicated another reviewer"})
        reviewers[index] = pick[0]
        taken.add(pick[1])

    if substitutions or compromised:
        spec = dict(spec)
        spec["reviewers"] = reviewers
        spec["self_review_avoided"] = substitutions
        spec["independence_compromised"] = compromised
    return spec


def _extra_reviewer(review: dict, worker: str, policy: "Policy", resolver: "Resolver") -> str | None:
    """A reviewer whose model is not already in use by the worker or a peer."""
    taken = {resolver.peek(worker)} | {resolver.peek(x) for x in review["reviewers"]}
    pool = [x for x in policy.roles
            if x not in review["reviewers"] and x != worker and resolver.peek(x) not in taken]
    return max(pool, key=policy.roles.index, default=None)


# --------------------------------------------------------------------------
# Stage 7 — resolution
# --------------------------------------------------------------------------

class Resolver:
    """Turns role aliases into concrete models, honouring every constraint the
    caller supplied and the provider boundary implied by the runtime state."""

    def __init__(self, task: Task, policy: Policy):
        self.task = task
        self.policy = policy
        cfg = policy.cfg

        self.bridge_down = task.has("bridge_down")
        self.binding_name = "default"
        self.notes: list[str] = []
        if self.bridge_down:
            self.binding_name = "claude_only" if task.runtime == "claude_code" else "openai_only"
            self.notes.append(f"binding degraded to {self.binding_name} (cross-provider bridge down)")
        self.binding = cfg["role_bindings"][self.binding_name]

        # When the bridge is down the opposite family is unreachable by
        # definition — a fallback that crosses it names a model that cannot be
        # invoked, which is the one thing a route must never do.
        self.allowed_family = policy.local_family[task.runtime] if self.bridge_down else None

        blocked = set(task.unavailable_models)
        for role in task.unavailable_roles:
            for b in (cfg["role_bindings"]["default"], self.binding):
                key = b.get(role)
                if key:
                    blocked.add(cfg["models"][key]["id"])
        # A tier that already failed must not be re-emitted under a new label.
        # Role-level escalation alone is not enough: in a degraded binding the
        # top roles collapse onto one model, so "escalating" changed nothing.
        self.failed, self.ambiguous = task.failed_and_ambiguous(policy, self.binding)
        self.blocked = blocked
        self.unusable = blocked | self.failed | self.ambiguous

    def _candidates(self, role: str) -> list[str]:
        cfg = self.policy.cfg
        ordered: list[str] = []
        if (primary := self.binding.get(role)):
            ordered.append(primary)
        ordered.extend(cfg["fallbacks"].get(self.task.runtime, {}).get(role, []))
        degraded_name = "claude_only" if self.task.runtime == "claude_code" else "openai_only"
        if (d := cfg["role_bindings"][degraded_name].get(role)):
            ordered.append(d)
        index = self.policy.roles.index(role)
        for other in self.policy.roles[index + 1:] + self.policy.roles[:index][::-1]:
            if (k := self.binding.get(other)):
                ordered.append(k)
        seen: set[str] = set()
        out = []
        for k in ordered:
            if k in seen:
                continue
            seen.add(k)
            if self.allowed_family and cfg["models"][k]["family"] != self.allowed_family:
                continue
            out.append(k)
        return out

    def peek(self, role: str) -> str | None:
        """The model this role would resolve to, or None if nothing is usable."""
        cfg = self.policy.cfg
        for key in self._candidates(role):
            model_id = cfg["models"][key]["id"]
            if model_id not in self.unusable:
                return model_id
        return None

    def family_for_role(self, role: str) -> str | None:
        model = self.peek(role)
        return self.policy.family_of[model] if model else None

    def resolve(self, roles: list[str]) -> tuple[dict[str, str], list[str], list[str]]:
        """Returns (role -> model id, fallback notes, compensation notes)."""
        cfg = self.policy.cfg
        resolved: dict[str, str] = {}
        fallbacks = list(self.notes)
        compensations: list[str] = []
        comp_cfg = cfg.get("fallback_compensations", {})

        for role in roles:
            primary_key = self.binding.get(role)
            primary_id = cfg["models"][primary_key]["id"] if primary_key else None
            chosen_id = self.peek(role)
            if chosen_id is None:
                raise ValidationError(
                    f"no usable model for role {role!r}: every candidate is unavailable, "
                    f"already failed, or on the unreachable side of a downed bridge. "
                    "This is an operational failure, not a route."
                )
            resolved[role] = chosen_id
            if primary_id is not None and chosen_id != primary_id:
                fallbacks.append(f"{role}: {primary_id} unavailable -> {chosen_id}")
                compensations.extend(self._compensations(role, primary_id, chosen_id, comp_cfg))
        return resolved, fallbacks, compensations

    def _compensations(self, role, primary_id, chosen_id, comp_cfg) -> list[str]:
        """Configured compensations for a downgrade. Declared and never applied,
        these were policy that existed only as a comment."""
        out = []
        fam = self.policy.family_of
        if role == "principal_architect" and (rule := comp_cfg.get("principal_architect_to_senior")):
            out.append(rule)
        if role == "reasoning_specialist" and fam[chosen_id] == fam.get(
                self.peek("senior_engineer") or chosen_id):
            if (rule := comp_cfg.get("reasoning_specialist_to_same_family")):
                out.append(rule)
        return out


# --------------------------------------------------------------------------
# Independence
# --------------------------------------------------------------------------

def independence(review: dict, task: Task) -> str:
    """Separate what the policy asks for from what was actually established.

    Three states, not two. `unavailable` is positive evidence that isolation
    cannot be achieved; `degraded` is the absence of evidence either way.
    Collapsing them makes a confirmed gap indistinguishable from an unchecked
    one, and the config's own ledger records a case of exactly that.

    `enforced` requires post-dispatch proof — one distinct session per
    reviewer. A caller's capability attestation alone yields `planned`, because
    a route computed before any reviewer runs cannot know what happened.
    """
    if not review["independent"]:
        return "not_applicable"
    # A reviewer set the router could not de-conflict is not independent, no
    # matter what the caller attests.
    if review.get("independence_compromised"):
        return "unavailable"
    if task.isolation_available is False:
        return "unavailable"
    distinct = len({e.strip() for e in task.isolation_evidence if e.strip()})
    # One identifier per reviewer — no more, and no hidden extra minimum. The
    # old `and distinct >= 2` made a single-reviewer MEDIUM band unable to
    # reach `enforced` even when the caller did exactly what both documents
    # instruct, with no note explaining the refusal.
    if review["reviewers"] and distinct >= len(review["reviewers"]):
        return "enforced"
    if task.isolation_available is True:
        return "planned"
    return "degraded"


# --------------------------------------------------------------------------
# Confidence
# --------------------------------------------------------------------------

def routing_confidence(task: Task, fallbacks: list[str]) -> float:
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
    cfg = cfg if cfg is not None else default_config()
    policy = Policy.of(cfg)
    task.validate(policy)

    resolver = Resolver(task, policy)

    risk_score = score(task, cfg)
    band = band_from_score(risk_score, policy)
    band, overrides, redundant_overrides, route_path = apply_overrides(task, band, policy)

    worker, worker_notes, ceiling_exhausted = select_worker(task, band, policy, resolver.binding)
    effort, effort_notes = select_effort(task, band, policy)
    disagreement = cfg["review"]["disagreement"]

    def roles_for(rev):
        needed = [worker] + list(rev["reviewers"])
        # The judge follows the REVIEW band, not the risk band — a review
        # promoted by low confidence needs adjudication just as much.
        if rev["band"] == "CRITICAL" or route_path == "disagreement":
            needed.append(disagreement["default_judge"])
        return list(dict.fromkeys(needed))

    # Bounded fixed point. Confidence depends on the fallbacks, the fallbacks
    # depend on which roles are needed, and which roles are needed depends on
    # the review band — which confidence can raise. Computing confidence once
    # from a preliminary role set let a route whose *final* fallbacks pushed it
    # below the escalation floor still emit as executable.
    review_band = band
    promoted_once = False
    for _ in range(MAX_PROMOTION_PASSES):
        review = select_review(review_band, worker, policy, resolver)
        resolved, fallbacks, compensations = resolver.resolve(roles_for(review))
        confidence = routing_confidence(task, fallbacks)
        threshold = cfg["router"]["confidence"]["extra_review_below"]
        # The policy is "raise the review band ONE level" — the loop exists so
        # the terminal decision sees the final confidence, not to change how
        # far the promotion goes. Confidence is very nearly invariant in the
        # review band, so a loop that re-promotes on every pass walks to
        # CRITICAL every time; that regression put a CRITICAL human gate on
        # routine documentation work, and a gate that fires on everything
        # trains people to wave it through.
        if confidence < threshold and review_band != "CRITICAL" and not promoted_once:
            promoted = policy.bands[policy.bands.index(review_band) + 1]
            overrides.append(f"low_routing_confidence_raised_review_to_{promoted}")
            review_band = promoted
            promoted_once = True
            continue
        break
    else:  # pragma: no cover - the band ladder is shorter than the pass budget
        raise ConfigError("review band promotion failed to reach a fixed point")

    applied_compensations: list[str] = []
    for note in compensations:
        if note == "raise_effort_one_level":
            effort = policy.effort_up(effort)
            effort_notes.append("compensation: fallback lost family diversity, effort +1")
            applied_compensations.append(note)
        elif note == "raise_effort_to_MAX_and_add_second_review":
            effort = policy.efforts[-1]
            effort_notes.append("compensation: architect downgraded, effort raised to MAX")
            # The name promises two things. Recording it while doing one is the
            # same false report this module exists to avoid, so the extra
            # reviewer is actually added — and if none can be resolved, the
            # compensation is not claimed.
            extra = _extra_reviewer(review, worker, policy, resolver)
            if extra:
                review = dict(review)
                review["reviewers"] = list(review["reviewers"]) + [extra]
                review["independent"] = True
                resolved, fallbacks, _ = resolver.resolve(roles_for(review))
                applied_compensations.append(note)
                effort_notes.append(f"compensation: added a second independent review ({extra})")
            else:
                effort_notes.append(
                    "compensation NOT fully applied: no additional reviewer could be resolved"
                )
    compensations = applied_compensations

    # Final de-confliction, at the emit boundary rather than mid-pipeline.
    #
    # This invariant has now been broken four times, each in a different place,
    # because it was being enforced at one point that later code could route
    # around: the compensation path above appends a reviewer and flips
    # `independent` to true, so a LOW-band review that never went through
    # de-confliction was promoted to "independent" with the worker's own model
    # sitting in a reviewer slot. Checking in the middle protects only the
    # paths that existed when the check was written. Checking here protects
    # every path, including ones added later, because nothing runs after it.
    judge_role = disagreement["default_judge"] if (
        review["band"] == "CRITICAL" or route_path == "disagreement") else None

    if review["independent"]:
        review = _deconflict(review, worker, policy, resolver)

        # The judge is a seat like any other. It was allocated after
        # de-confliction ran and never checked against it, so an adjudicator
        # could be the same model as one of the reviewers whose disagreement it
        # was brought in to settle — self-adjudication, which is the same
        # failure as self-review one level up. All seats are allocated together.
        if judge_role:
            taken = {resolver.peek(worker)} | {resolver.peek(x) for x in review["reviewers"]}
            if resolver.peek(judge_role) in taken:
                # A judge must be able to adjudicate the reviewers it is
                # settling between, so a replacement below their tier is not a
                # judge — it is a third opinion with less standing than the
                # disagreement it is resolving.
                floor = max((policy.roles.index(x) for x in review["reviewers"]), default=0)
                pool = [x for x in policy.roles
                        if policy.roles.index(x) >= floor
                        and resolver.peek(x) and resolver.peek(x) not in taken]
                replacement = max(pool, key=policy.roles.index, default=None)
                review = dict(review)
                if replacement:
                    review["judge_override"] = replacement
                    judge_role = replacement
                else:
                    # No independent adjudicator exists here. Say so and hand
                    # the adjudication to a human rather than seating a judge
                    # that is one of the parties.
                    review["judge_unavailable"] = True
                    judge_role = None

        resolved, fallbacks, _ = resolver.resolve(
            list(dict.fromkeys([worker] + list(review["reviewers"]) + ([judge_role] if judge_role else [])))
        )
        # Post-condition, asserted rather than assumed: every seat — the
        # implementer, each reviewer, and the judge — holds a distinct model.
        seat_roles = [worker] + list(review["reviewers"]) + ([judge_role] if judge_role else [])
        seat_models = [resolved.get(x) for x in seat_roles]
        filled = [m for m in seat_models if m]
        if len(filled) != len(set(filled)):
            review = dict(review)
            review["independence_compromised"] = True

    fams = {r: policy.family_of[m] for r, m in resolved.items()}
    reviewer_families = {fams[r] for r in review["reviewers"] if r in fams}
    cross_family = len(reviewer_families) > 1 or (
        len(review["reviewers"]) == 1 and review["reviewers"][0] in fams and worker in fams
        and fams[review["reviewers"][0]] != fams[worker]
    )

    review_independence = independence(review, task)
    judge = review.get("judge_override") or judge_role

    terminal = None
    if review.get("independence_compromised"):
        # The band asked for independent review and no assignment of distinct
        # models could provide it. Emitting a dispatchable route here would
        # hand back reviewers that are the implementer wearing another label,
        # with a boolean alongside saying so — disclosure standing in for a
        # control. There is no safe route to emit, so none is.
        terminal = "INDEPENDENCE_UNAVAILABLE"
    elif task.prior_failures >= cfg["retry"]["max_total_implementation_attempts"]:
        terminal = "HUMAN_REQUIRED"
    elif ceiling_exhausted:
        terminal = "HUMAN_REQUIRED"
    elif confidence < cfg["router"]["confidence"]["escalate_below"]:
        terminal = "ESCALATE_ROUTING"

    # The router cannot verify where an isolation receipt came from. It is a
    # caller-supplied string; nothing here binds it to an actual dispatch, to
    # this route, or to a particular reviewer. Letting it clear the CRITICAL
    # gate would make the strongest control in the policy forgeable by typing.
    # So CRITICAL always asks a human, and `enforced` reports what the caller
    # claims without acting on it as proof.
    requires_human = bool(
        terminal
        or review["band"] == "CRITICAL"
        # Disclosure is not a control. A route whose reviewers could not be
        # given distinct models is one where the implementer reviews itself;
        # emitting it as dispatchable and hoping the consumer reads a boolean
        # is exactly the false-assurance shape this policy exists to avoid.
        or review.get("independence_compromised")
        or review.get("judge_unavailable")
    )

    # A terminal route emits no execution bindings at all. Nulling only the
    # worker left a consumer able to dispatch the reviewers from a route the
    # rationale said must not be executed.
    executable = terminal is None
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
        "band_overrides_redundant": redundant_overrides,
        "critical_flags": task.critical_flags(policy),
        "route_path": route_path,
        "terminal": terminal,
        "selected_role": worker if executable else None,
        "selected_model": resolved.get(worker) if executable else None,
        "selected_effort": effort if executable else None,
        "selected_effort_native": cfg["effort_map"][task.runtime][effort] if executable else None,
        "review": {
            "band": review["band"],
            "reviewers": review["reviewers"],
            "reviewer_models": [resolved.get(r) for r in review["reviewers"]] if executable else [],
            "effort": review["effort"] if executable else None,
            "independence_required": review["independent"],
            "review_independence": review_independence,
            "required_checks": review.get("required_checks", []),
            "judge": judge,
            "judge_model": (resolved.get(judge) if judge else None) if executable else None,
            "self_review_avoided": review.get("self_review_avoided") or [],
            "independence_compromised": bool(review.get("independence_compromised")),
            "judge_unavailable": bool(review.get("judge_unavailable")),
        },
        "cross_family_review": cross_family,
        "fallbacks_applied": (
            fallbacks if executable
            else [f.split(":")[0] + ": binding withheld (terminal route)"
                  if ":" in f and "->" in f else f for f in fallbacks]
        ),
        "fallback_compensations_applied": compensations,
        "unavailable_models": sorted(resolver.blocked),
        "excluded_prior_failures": sorted(resolver.failed),
        "excluded_as_ambiguous_alias": sorted(resolver.ambiguous),
        "escalation_count": task.prior_failures,
        "retry_count": task.prior_failures,
        "routing_confidence": confidence,
        "requires_human_confirmation": requires_human,
        "notes": worker_notes + effort_notes,
    }
    result["rationale"] = explain(task, result)
    return result


def explain(task: Task, r: dict) -> str:
    parts = [f"{task.task_class} scored {r['risk_score']}/18 "
             f"(c={task.complexity} u={task.uncertainty} b={task.blast_radius} r={task.reversibility}) "
             f"-> band {r['risk_band']}."]
    if r["band_overrides_applied"]:
        parts.append(f"Overrides applied: {', '.join(r['band_overrides_applied'])}.")
    if r["band_overrides_redundant"]:
        parts.append(f"Overrides that fired but were already satisfied: "
                     f"{', '.join(r['band_overrides_redundant'])}.")
    if r["critical_flags"]:
        parts.append(f"Critical-domain flags: {', '.join(r['critical_flags'])}.")
    if r["terminal"]:
        parts.append(
            f"TERMINAL: {r['terminal']} — no executable bindings emitted; routing confidence "
            f"{r['routing_confidence']} after {task.prior_failures} prior failure(s). Surface to a "
            "human with what was tried, what evidence accumulated, and the blocking uncertainty."
        )
    else:
        parts.append(f"Worker {r['selected_role']} at {r['selected_effort']} effort.")
    rv = r["review"]
    parts.append(f"Review band {rv['band']}: {', '.join(rv['reviewers'])}, "
                 f"independence_required={rv['independence_required']}, "
                 f"review_independence={rv['review_independence']}.")
    for sub in (rv.get("self_review_avoided") or []):
        parts.append(f"Reviewer slot substituted: {sub['replaced']} -> {sub['with']} "
                     f"({sub['reason']}).")
    if rv.get("independence_compromised"):
        parts.append("Independence could not be established: no distinct model was available "
                     "for every seat.")
    if rv.get("judge_unavailable"):
        parts.append("No independent adjudicator is available at or above the reviewers' tier; "
                     "a human must resolve any disagreement.")
    if rv["judge"]:
        parts.append(f"Judge: {rv['judge']}.")
    if rv["required_checks"]:
        parts.append(f"Required checks: {', '.join(rv['required_checks'])}.")
    if r["fallbacks_applied"]:
        parts.append(f"Fallbacks: {'; '.join(r['fallbacks_applied'])}.")
    else:
        parts.append("No fallbacks applied.")
    if r["fallback_compensations_applied"]:
        parts.append(f"Compensations: {', '.join(r['fallback_compensations_applied'])}.")
    if r["excluded_prior_failures"]:
        parts.append(f"Excluded as already-failed: {', '.join(r['excluded_prior_failures'])}.")
    if r["excluded_as_ambiguous_alias"]:
        parts.append("Also withheld because a role alias is ambiguous across bindings "
                     f"(these did not run): {', '.join(r['excluded_as_ambiguous_alias'])}.")
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


def build_parser(policy: Policy) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--json", help="full task as a JSON object; overrides the flags below")
    p.add_argument("--class", dest="task_class", choices=policy.task_classes)
    p.add_argument("--complexity", type=int)
    p.add_argument("--uncertainty", type=int)
    p.add_argument("--blast-radius", type=int)
    p.add_argument("--reversibility", type=int)
    p.add_argument("--reasoning-centric", action="store_true")
    p.add_argument("--flags", default="",
                   help=f"comma-separated; known: {', '.join(sorted(policy.known_flags))}")
    p.add_argument("--prior-failures", type=int, default=0)
    p.add_argument("--prior-models", default="",
                   help="comma-separated role aliases or model ids that already failed")
    p.add_argument("--runtime", default="claude_code", choices=sorted(policy.runtimes))
    p.add_argument("--unavailable", default="", help="comma-separated unavailable roles")
    p.add_argument("--unavailable-models", default="", help="comma-separated unavailable model ids")
    p.add_argument("--isolation", choices=["available", "unavailable"],
                   help="whether reviewer context isolation can be achieved this session")
    p.add_argument("--isolation-evidence", default="",
                   help="comma-separated distinct session ids, one per dispatched reviewer")
    p.add_argument("--format", default="text", choices=["text", "json"])
    return p


REQUIRED_JSON_FIELDS = ("task_class", "complexity", "uncertainty", "blast_radius", "reversibility")
PUBLIC_FIELDS = {f.name for f in fields(Task) if not f.name.startswith("_")}


def main(argv: list[str] | None = None) -> int:
    try:
        policy = _default_policy()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    p = build_parser(policy)
    args = p.parse_args(argv)

    try:
        if args.json:
            try:
                payload = json.loads(args.json)
            except json.JSONDecodeError as exc:
                raise ValidationError(f"--json is not valid JSON: {exc}") from None
            if not isinstance(payload, dict):
                raise ValidationError("--json must be a JSON object")
            if (unknown := set(payload) - PUBLIC_FIELDS):
                raise ValidationError(f"--json has unknown field(s): {', '.join(sorted(unknown))}")
            if (missing := [f for f in REQUIRED_JSON_FIELDS if f not in payload]):
                raise ValidationError(f"--json is missing required field(s): {', '.join(missing)}")
            try:
                task = Task(**payload)
            except TypeError as exc:
                raise ValidationError(f"--json field types are invalid: {exc}") from None
        else:
            if (missing := [n for n in REQUIRED_JSON_FIELDS if getattr(args, n) is None]):
                p.error("missing required arguments: "
                        + ", ".join("--" + m.replace("_", "-") for m in missing))
            task = Task(
                task_class=args.task_class, complexity=args.complexity,
                uncertainty=args.uncertainty, blast_radius=args.blast_radius,
                reversibility=args.reversibility, reasoning_centric=args.reasoning_centric,
                flags=_split(args.flags), prior_failures=args.prior_failures,
                prior_models=_split(args.prior_models), runtime=args.runtime,
                unavailable_roles=_split(args.unavailable),
                unavailable_models=_split(args.unavailable_models),
                isolation_available=None if args.isolation is None else args.isolation == "available",
                isolation_evidence=_split(args.isolation_evidence),
            )
        result = route(task)
    except ValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        print(json.dumps(result, indent=2))
    else:
        _print_text(result)
    return 1 if result["terminal"] else 0


def _print_text(r: dict) -> None:
    print(f"risk_score:  {r['risk_score']}")
    print(f"risk_band:   {r['risk_band']}")
    print(f"overrides:   {r['band_overrides_applied'] or '(none)'}")
    if r["band_overrides_redundant"]:
        print(f"  already satisfied by another rule: {r['band_overrides_redundant']}")
    if r["terminal"]:
        print(f"TERMINAL:    {r['terminal']}  — no executable bindings emitted")
    else:
        print(f"worker:      {r['selected_role']}  ->  {r['selected_model']}")
        print(f"effort:      {r['selected_effort']}  (native: {r['selected_effort_native']})")
    rv = r["review"]
    label = "review (policy only — not dispatchable)" if r["terminal"] else "review"
    print(f"{label}:")
    print(f"  band:            {rv['band']}")
    print(f"  reviewers:       {', '.join(rv['reviewers'])}")
    if not r["terminal"]:
        print(f"  models:          {', '.join(m for m in rv['reviewer_models'] if m)}")
        print(f"  effort:          {rv['effort']}")
    print(f"  required:        independent={rv['independence_required']}")
    print(f"  actual:          {rv['review_independence']}")
    if rv["required_checks"]:
        print(f"  checks:          {', '.join(rv['required_checks'])}")
    if rv["judge"]:
        print(f"  judge:           {rv['judge']}" + (f" -> {rv['judge_model']}" if rv["judge_model"] else ""))
    print(f"cross_family_review: {r['cross_family_review']}")
    print(f"fallbacks:   {r['fallbacks_applied'] or '(none)'}")
    if r["fallback_compensations_applied"]:
        print(f"compensations: {r['fallback_compensations_applied']}")
    if r["excluded_prior_failures"]:
        print(f"excluded:    {r['excluded_prior_failures']} (already failed)")
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
