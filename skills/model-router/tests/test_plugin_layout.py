"""Plugin packaging layout and public-doc contract."""
import json
import re
from pathlib import Path


PUBLIC_FILES = (
    "README.md",
    "README.ko.md",
    "CHANGELOG.md",
    "CHANGELOG.ko.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "SECURITY.md",
    "package.json",
    "docs/locator.md",
)


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        manifest = parent / ".claude-plugin" / "plugin.json"
        if manifest.is_file():
            return parent
    raise AssertionError("no .claude-plugin/plugin.json above " + str(here))


def _read_json(root: Path, rel: str) -> dict:
    return json.loads((root / rel).read_text(encoding="utf-8"))


def test_scripts_can_see_plugin_manifest():
    root = _repo_root()
    data = _read_json(root, ".claude-plugin/plugin.json")
    assert data["name"] == "deep-model-router"
    assert isinstance(data["version"], str) and data["version"]


def test_skill_lives_under_skills_model_router():
    root = _repo_root()
    assert (root / "skills" / "model-router" / "SKILL.md").is_file()
    assert (root / "skills" / "model-router" / "scripts" / "route_task.py").is_file()


def test_public_plugin_surface_exists():
    root = _repo_root()
    missing = [rel for rel in PUBLIC_FILES if not (root / rel).is_file()]
    assert missing == [], missing


def test_version_triple_sync():
    root = _repo_root()
    versions = {
        rel: _read_json(root, rel)["version"]
        for rel in (
            "package.json",
            ".claude-plugin/plugin.json",
            ".codex-plugin/plugin.json",
        )
    }
    assert len(set(versions.values())) == 1, versions


def test_readme_language_toggles():
    root = _repo_root()
    en = (root / "README.md").read_text(encoding="utf-8").splitlines()[0]
    ko = (root / "README.ko.md").read_text(encoding="utf-8").splitlines()[0]
    assert en == "**English** | [한국어](./README.ko.md)"
    assert ko == "[English](./README.md) | **한국어**"


def test_changelog_records_current_version():
    root = _repo_root()
    version = _read_json(root, ".claude-plugin/plugin.json")["version"]
    heading = re.compile(rf"^## \[{re.escape(version)}\] — \d{{4}}-\d{{2}}-\d{{2}}", re.M)
    for rel in ("CHANGELOG.md", "CHANGELOG.ko.md"):
        text = (root / rel).read_text(encoding="utf-8")
        assert heading.search(text), rel


def test_agent_guides_do_not_hardcode_version():
    root = _repo_root()
    version = _read_json(root, ".claude-plugin/plugin.json")["version"]
    for rel in ("AGENTS.md", "CLAUDE.md"):
        text = (root / rel).read_text(encoding="utf-8")
        assert version not in text, rel
        assert "docs/DOCS_RULE.md" in text, rel


def test_agents_points_at_existing_locator():
    root = _repo_root()
    text = (root / "AGENTS.md").read_text(encoding="utf-8")
    assert "docs/locator.md" in text
    assert "once it exists" not in text
