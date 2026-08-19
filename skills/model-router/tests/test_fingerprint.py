"""design §4 B1 + §5 (a)~(i): fingerprint 정규형의 계약 테스트.

정규형은 route가 각 입력을 소비하는 의미론을 그대로 따른다: set-의미
리스트는 dedup+sort, isolation_evidence는 strip 포함, prior_models는
multiplicity 보존, local_policy는 bool(lp) 의미론(falsy -> null).
"""
import json
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))

from route_task import (  # noqa: E402
    CONFIG_PATH, Task, default_config, route, request_sha256_of,
)
from policy_digest import canonical_policy_sha256  # noqa: E402
import copy  # noqa: E402

BASE = dict(task_class="IMPLEMENTATION", complexity=1, uncertainty=1,
            blast_radius=1, reversibility=0)


def _fp(**overrides):
    return request_sha256_of(Task(**{**BASE, **overrides}))


def test_same_request_twice_is_identical():                       # (a)
    assert _fp() == _fp()


def test_flag_order_and_duplicates_converge():                    # (b)(c)
    assert _fp(flags=["large_context", "tool_heavy"]) \
        == _fp(flags=["tool_heavy", "large_context", "tool_heavy"])


def test_isolation_evidence_whitespace_and_dups_converge():       # (d)
    assert _fp(isolation_evidence=["s1 ", " s2"]) \
        == _fp(isolation_evidence=["s2", "s1", "s1"])


def test_local_policy_absent_empty_and_all_null():                # (e)
    absent = _fp()
    empty = _fp(_local_policy={})
    nulls = _fp(_local_policy={
        "minimum_capability_tier": None, "minimum_effort": None,
        "minimum_reviewers": None, "minimum_provider_families": None,
        "allowed_families": None})
    assert absent == empty            # bool(lp) 의미론: 둘 다 미적용
    assert absent != nulls            # local_policy_applied False vs True


def test_unavailable_lists_and_allowed_families_converge():
    a = _fp(unavailable_models=["grok-4.6", "gpt-5.6-luna"],
            _local_policy={"allowed_families": ["openai", "claude", "openai"]})
    b = _fp(unavailable_models=["gpt-5.6-luna", "grok-4.6", "grok-4.6"],
            _local_policy={"allowed_families": ["claude", "openai"]})
    assert a == b


def test_prior_models_multiplicity_is_preserved():
    once = _fp(prior_failures=1, prior_models=["gpt-5.6-luna"])
    twice = _fp(prior_failures=2, prior_models=["gpt-5.6-luna", "gpt-5.6-luna"])
    assert once != twice


def test_route_emits_both_fields_and_terminal_keeps_them():       # (h)
    ok = route(Task(**BASE))
    assert len(ok["request_sha256"]) == 64
    assert len(ok["decision_fingerprint"]) == 64
    terminal = route(Task(**BASE, prior_failures=4,
                          prior_models=["gpt-5.6-luna"] * 4))
    assert terminal["terminal"] is not None
    assert len(terminal["request_sha256"]) == 64
    assert len(terminal["decision_fingerprint"]) == 64


def test_input_change_changes_request_sha():                      # (i)
    assert _fp(complexity=1) != _fp(complexity=2)


def test_cfg_change_moves_policy_and_fingerprint_not_request():   # (g)
    cfg = default_config()
    injected = copy.deepcopy(cfg)
    injected["role_bindings"]["default"]["worker_fast"] = "claude_worker_fast"
    a, b = route(Task(**BASE), cfg), route(Task(**BASE), injected)
    assert a["request_sha256"] == b["request_sha256"]
    assert a["policy_sha256"] != b["policy_sha256"]
    assert a["decision_fingerprint"] != b["decision_fingerprint"]
    assert b["policy_sha256"] == canonical_policy_sha256(injected)
