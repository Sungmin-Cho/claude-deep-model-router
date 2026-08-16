"""Canonical SHA-256 of model-routing.yaml (spec §11.4)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml


def _canonical(obj):
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


_DIGEST_CACHE: dict[str, tuple[int, str]] = {}


def policy_sha256(config_path: Path) -> str:
    path = Path(config_path)
    key = str(path.resolve())
    mtime_ns = path.stat().st_mtime_ns
    cached = _DIGEST_CACHE.get(key)
    if cached and cached[0] == mtime_ns:
        return cached[1]
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    blob = json.dumps(
        raw, sort_keys=True, separators=(",", ":"),
        ensure_ascii=True, default=_canonical,
    )
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    _DIGEST_CACHE[key] = (mtime_ns, digest)
    return digest
