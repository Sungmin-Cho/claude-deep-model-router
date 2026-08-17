"""RouteRequestV1, identity fields, local_policy merge, locator."""
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from locate_router import locate_router_cli
from policy_digest import policy_sha256
from route_task import (  # noqa: E402
    CONFIG_PATH,
    Task,
    main,
    plugin_manifest_version,
    route,
)


def r(**kw):
    kw.setdefault("complexity", 0)
    kw.setdefault("uncertainty", 0)
    kw.setdefault("blast_radius", 0)
    kw.setdefault("reversibility", 0)
    return route(Task(**kw))


def test_decision_carries_identity_fields():
    out = r(task_class="MECHANICAL")
    assert out["route_schema_version"] == 1
    assert out["router_plugin_version"] == plugin_manifest_version()
    assert re.fullmatch(r"[0-9a-f]{64}", out["policy_sha256"])
    assert out["policy_sha256"] == policy_sha256(CONFIG_PATH)


def test_policy_digest_ignores_comments(tmp_path):
    src = CONFIG_PATH.read_text(encoding="utf-8")
    a = policy_sha256(CONFIG_PATH)
    copy = tmp_path / "cfg.yaml"
    copy.write_text("# extra comment\n" + src, encoding="utf-8")
    assert policy_sha256(copy) == a


def test_plugin_manifest_walks_ancestors():
    scripts = Path(__file__).resolve().parent.parent / "scripts" / "route_task.py"
    root = Path(__file__).resolve()
    for parent in root.parents:
        manifest = parent / ".claude-plugin" / "plugin.json"
        if manifest.is_file():
            expected = json.loads(manifest.read_text(encoding="utf-8"))["version"]
            break
    else:
        raise AssertionError("no .claude-plugin/plugin.json above " + str(root))
    assert plugin_manifest_version(scripts) == expected


def test_request_json_unknown_field_exits_2(tmp_path):
    p = tmp_path / "req.json"
    p.write_text(json.dumps({
        "route_schema_version": 1, "task_class": "MECHANICAL",
        "complexity": 0, "uncertainty": 0, "blast_radius": 0, "reversibility": 0,
        "extra": True,
    }))
    assert main(["--request-json", str(p), "--format", "json"]) == 2


def test_unsupported_schema_version_exits_2(tmp_path):
    p = tmp_path / "req.json"
    p.write_text(json.dumps({
        "route_schema_version": 99, "task_class": "MECHANICAL",
        "complexity": 0, "uncertainty": 0, "blast_radius": 0, "reversibility": 0,
    }))
    assert main(["--request-json", str(p), "--format", "json"]) == 2


def test_allowed_families_empty_is_unsatisfiable(tmp_path):
    p = tmp_path / "req.json"
    p.write_text(json.dumps({
        "route_schema_version": 1, "task_class": "MECHANICAL",
        "complexity": 0, "uncertainty": 0, "blast_radius": 0, "reversibility": 0,
        "local_policy": {"allowed_families": []},
    }))
    assert main(["--request-json", str(p), "--format", "json"]) == 1
    # parse via route()
    out = route(Task(
        task_class="MECHANICAL", complexity=0, uncertainty=0,
        blast_radius=0, reversibility=0,
        _local_policy={"allowed_families": []},
    ))
    assert out["terminal"] == "UNSATISFIABLE_LOCAL_POLICY"


def test_allowed_families_claude_never_emits_openai():
    out = route(Task(
        task_class="MECHANICAL", complexity=0, uncertainty=0,
        blast_radius=0, reversibility=0,
        _local_policy={"allowed_families": ["claude"]},
    ))
    assert out["terminal"] is None
    from route_task import Policy, load_config
    fam = Policy.of(load_config()).family_of
    assert fam[out["selected_model"]] == "claude"


def test_minimum_effort_is_monotonic():
    out = route(Task(
        task_class="MECHANICAL", complexity=0, uncertainty=0,
        blast_radius=0, reversibility=0,
        _local_policy={"minimum_effort": "HIGH"},
    ))
    assert out["selected_effort"] == "HIGH"
    assert out["effective_policy"]["minimum_effort"] == "HIGH"


def test_isolation_evidence_without_isolation_exits_2(tmp_path):
    p = tmp_path / "req.json"
    p.write_text(json.dumps({
        "route_schema_version": 1, "task_class": "MECHANICAL",
        "complexity": 0, "uncertainty": 0, "blast_radius": 0, "reversibility": 0,
        "availability_snapshot": {"isolation_evidence": ["abc"]},
    }))
    assert main(["--request-json", str(p), "--format", "json"]) == 2


def test_nested_unknown_local_policy_field_exits_2(tmp_path):
    p = tmp_path / "req.json"
    p.write_text(json.dumps({
        "route_schema_version": 1, "task_class": "MECHANICAL",
        "complexity": 0, "uncertainty": 0, "blast_radius": 0, "reversibility": 0,
        "local_policy": {"nope": 1},
    }))
    assert main(["--request-json", str(p), "--format", "json"]) == 2


def test_gemini_is_never_selected():
    out = r(task_class="IMPLEMENTATION", complexity=2, uncertainty=2, blast_radius=2)
    assert out.get("selected_model") != "gemini-3.6-flash-high"
    emitted = set()
    if out.get("selected_model"):
        emitted.add(out["selected_model"])
    emitted.update(out.get("review", {}).get("reviewer_models") or [])
    assert "gemini-3.6-flash-high" not in emitted


def test_locator_env_hit(tmp_path):
    target = tmp_path / "route_task.py"
    target.write_text("# stub\n")
    found = locate_router_cli({"DEEP_MODEL_ROUTER_CLI": str(target)}, home=tmp_path)
    assert found == target.resolve()


def test_locator_rejects_personal_symlink(tmp_path):
    skill = tmp_path / ".claude" / "skills" / "model-router" / "scripts"
    skill.mkdir(parents=True)
    target = skill / "route_task.py"
    target.write_text("# stub\n")
    found = locate_router_cli(
        {"DEEP_MODEL_ROUTER_CLI": str(target)},
        home=tmp_path,
    )
    assert found is None
