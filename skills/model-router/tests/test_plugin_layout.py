"""Plugin packaging layout."""
import json
from pathlib import Path


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        manifest = parent / ".claude-plugin" / "plugin.json"
        if manifest.is_file():
            return parent
    raise AssertionError("no .claude-plugin/plugin.json above " + str(here))


def test_scripts_can_see_plugin_manifest():
    root = _repo_root()
    data = json.loads((root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert data["name"] == "deep-model-router"
    assert data["version"] == "1.0.0"


def test_skill_lives_under_skills_model_router():
    root = _repo_root()
    assert (root / "skills" / "model-router" / "SKILL.md").is_file()
    assert (root / "skills" / "model-router" / "scripts" / "route_task.py").is_file()
