"""Host-neutral locator for route_task.py (spec §11.5)."""
from __future__ import annotations

import os
import re
from pathlib import Path

_VERSION_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?")


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


def _usable(path: Path) -> bool:
    """Every tier answers the same two questions, in the same order.

    The guard used to run on the `DEEP_MODEL_ROUTER_CLI` tier only, so the
    exact path that tier refused was returned by the `DEEP_MODEL_ROUTER_ROOT`
    tier and by the cache glob. A rule enforced at one of three entrances is
    not a rule.
    """
    return _is_route_task(path) and not _looks_like_source_checkout(path)


def _version_key(path: Path) -> tuple[tuple[int, ...], str]:
    """Order cache hits by the version segment, parsed as numbers.

    A string sort ranks `1.9.0` above `1.10.0`, so the first 1.10 release would
    have silently pointed every consumer at an older router. Paths carrying no
    parseable version sort below every path that does, and the full string
    breaks ties so the answer is deterministic.
    """
    parts = path.parts
    numeric: tuple[int, ...] = ()
    if "deep-model-router" in parts:
        start = len(parts) - 1 - parts[::-1].index("deep-model-router")
        for segment in parts[start + 1:]:
            if segment == "skills":
                break
            if (m := _VERSION_RE.match(segment)):
                numeric = tuple(int(g) for g in m.groups() if g is not None)
                break
    return (numeric, str(path))


def locate_router_cli(env: dict | None = None, home: Path | None = None) -> Path | None:
    env = env if env is not None else os.environ
    home = Path(home) if home is not None else Path.home()

    cli = env.get("DEEP_MODEL_ROUTER_CLI")
    if cli:
        p = Path(cli)
        if _usable(p):
            return p.resolve()

    root = env.get("DEEP_MODEL_ROUTER_ROOT")
    if root:
        p = Path(root) / "skills" / "model-router" / "scripts" / "route_task.py"
        if _usable(p):
            return p.resolve()

    # Tier by tier, in the order docs/locator.md documents. Merging the two
    # caches into one list let lexicographic order decide which host's cache
    # won, and `.codex` sorts after `.claude` — so the documented step 4 beat
    # step 3 every time, on an ordering nobody chose.
    cache_roots = [
        home / ".claude" / "plugins" / "cache",
        home / ".codex" / "plugins",
    ]
    for cache in cache_roots:
        if not cache.is_dir():
            continue
        hits = [
            h for h in cache.glob("**/deep-model-router/**/skills/model-router/scripts/route_task.py")
            if _usable(h)
        ]
        if hits:
            return max(hits, key=_version_key).resolve()
    return None
