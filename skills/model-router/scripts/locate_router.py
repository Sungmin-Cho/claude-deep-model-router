"""Host-neutral locator for route_task.py (spec §11.5)."""
from __future__ import annotations

import os
from pathlib import Path


def _is_route_task(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return resolved.is_file() and resolved.name == "route_task.py"


def _looks_like_source_checkout(path: Path) -> bool:
    """Reject personal skill symlinks and sibling source trees."""
    text = str(path)
    if "/.claude/skills/model-router" in text or "/.codex/skills/model-router" in text:
        return True
    if text.endswith("/claude-plugins/deep-model-router/skills/model-router/scripts/route_task.py"):
        return True
    return False


def locate_router_cli(env: dict | None = None, home: Path | None = None) -> Path | None:
    env = env if env is not None else os.environ
    home = Path(home) if home is not None else Path.home()

    cli = env.get("DEEP_MODEL_ROUTER_CLI")
    if cli:
        p = Path(cli)
        if _is_route_task(p) and not _looks_like_source_checkout(p):
            return p.resolve()

    root = env.get("DEEP_MODEL_ROUTER_ROOT")
    if root:
        p = Path(root) / "skills" / "model-router" / "scripts" / "route_task.py"
        if _is_route_task(p):
            return p.resolve()

    cache_roots = [
        home / ".claude" / "plugins" / "cache",
        home / ".codex" / "plugins",
    ]
    hits: list[Path] = []
    for cache in cache_roots:
        if not cache.is_dir():
            continue
        hits.extend(cache.glob("**/deep-model-router/**/skills/model-router/scripts/route_task.py"))
    real = [h.resolve() for h in hits if _is_route_task(h)]
    if not real:
        return None
    return sorted(real, key=lambda p: str(p))[-1]
