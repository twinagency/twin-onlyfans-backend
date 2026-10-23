"""Deterministic whole-file shard assignment for the OnlyFans release acceptance.

A shard owns complete test files (and isolated scripts), never individual test
nodes, so fixtures, module state and per-file databases stay inside one job.
The assignment is a pure function of the expanded manifest and the shard count,
so every job and the merger derive and compare the same digest independently.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

SCHEMA = "of-acceptance-shards.v1"


def parse_shard(value: str) -> tuple[int, int]:
    try:
        index, count = (int(part) for part in value.split("/"))
    except ValueError as error:
        raise ValueError("Shard must be INDEX/COUNT, for example 0/4") from error
    if not 1 <= count <= 64 or not 0 <= index < count:
        raise ValueError("Shard index must be within 0..COUNT-1 and COUNT within 1..64")
    return index, count


def expand(root: Path, manifest: dict) -> tuple[list[str], list[str]]:
    """Return (sorted pytest files, scripts in manifest order), exactly as the runner does."""
    separate = list(manifest["separateScripts"])
    files: set[str] = set()
    for pattern in manifest["patterns"]:
        matches = list(root.glob(pattern))
        if not matches:
            raise ValueError(f"Required acceptance pattern matched no tests: {pattern}")
        files.update(str(path.relative_to(root)) for path in matches)
    files.difference_update(separate)
    for name in separate:
        if not (root / name).is_file():
            raise ValueError("A required isolated script is missing")
    return sorted(files), separate


def assign(root: Path, files: list[str], scripts: list[str], count: int) -> list[dict]:
    """Greedy longest-first bin packing by file size; ties break on name. Disjoint by construction."""
    items = [(name, "file") for name in files] + [(name, "script") for name in scripts]
    weight = {name: max((root / name).stat().st_size, 1) for name, _ in items}
    load = [0] * count
    shards = [{"files": [], "scripts": []} for _ in range(count)]
    for name, kind in sorted(items, key=lambda item: (-weight[item[0]], item[0])):
        target = min(range(count), key=lambda index: (load[index], index))
        load[target] += weight[name]
        shards[target]["files" if kind == "file" else "scripts"].append(name)
    for shard in shards:
        shard["files"].sort()
        shard["scripts"] = [name for name in scripts if name in shard["scripts"]]
    return shards


def digest(count: int, shards: list[dict]) -> str:
    body = json.dumps({"schema": SCHEMA, "count": count, "shards": shards}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def plan(root: Path, manifest: dict, count: int) -> dict:
    files, scripts = expand(root, manifest)
    shards = assign(root, files, scripts, count)
    return {"schema": SCHEMA, "count": count, "digest": digest(count, shards), "shards": shards,
            "allFiles": files, "allScripts": scripts}
