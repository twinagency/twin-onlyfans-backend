#!/usr/bin/env python3
"""Prove that disjoint acceptance shards together equal the collected manifest.

Inputs are evidence directories produced by onlyfans-release-acceptance.py:
one --manifest-only directory (the complete collection derived from this
commit) and every --shard directory. Nothing is trusted from a count or a
constant: acceptance requires that the executed node identities are exactly the
collected node identities, once each, all passed, and that every isolated
script ran exactly once and reported completed acceptance.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import onlyfans_acceptance_shards as shards  # noqa: E402


def load(directory: Path, name: str):
    try:
        return json.loads((directory / name).read_text())
    except (OSError, ValueError) as error:
        raise ValueError(f"{directory.name}: {name} is missing or unreadable") from error


def merge(manifest_dir: Path, shard_dirs: list[Path], *, backend_sha: str | None = None,
          dashboard_sha: str | None = None, suite: str | None = None) -> dict:
    manifest = load(manifest_dir, "summary.json")
    if manifest.get("scope") != "manifest" or not manifest.get("manifestOnly"):
        raise ValueError("The manifest directory is not a --manifest-only collection")
    collected = load(manifest_dir, "collection.json")
    expanded = load(manifest_dir, "expanded-manifest.json")
    nodes = collected["selected"]
    if not nodes or collected["deselected"] or collected["collectionErrors"] or len(set(nodes)) != len(nodes):
        raise ValueError("The collected manifest is empty, deselected, errored or has duplicate nodes")
    all_files, all_scripts = expanded["allPytestFiles"], expanded["allSeparateScripts"]
    if suite and manifest["suite"] != suite:
        raise ValueError("The manifest suite differs from the requested suite")
    identity = (manifest["suite"], manifest["backend"]["sha"], manifest["backend"]["tree"], manifest["dashboard"]["sha"])
    if backend_sha and manifest["backend"]["sha"] != backend_sha or dashboard_sha and manifest["dashboard"]["sha"] != dashboard_sha:
        raise ValueError("The manifest was collected from a different revision than the one being accepted")
    count = manifest["shard"]["count"]
    digest = manifest["shard"]["digest"]
    if len(shard_dirs) != count:
        raise ValueError(f"Expected {count} shard directories, received {len(shard_dirs)}")
    seen_index, seen_files, seen_scripts, executed, results = set(), [], [], [], {}
    for directory in shard_dirs:
        summary = load(directory, "summary.json")
        shard = summary.get("shard") or {}
        if summary.get("scope") != "shard" or not summary.get("accepted"):
            raise ValueError(f"{directory.name}: shard was not accepted")
        if (summary["suite"], summary["backend"]["sha"], summary["backend"]["tree"], summary["dashboard"]["sha"]) != identity:
            raise ValueError(f"{directory.name}: shard was produced from a different revision or suite")
        if shard["count"] != count or shard["digest"] != digest or shard["index"] in seen_index:
            raise ValueError(f"{directory.name}: shard plan, count or index does not match the manifest")
        seen_index.add(shard["index"])
        local = load(directory, "expanded-manifest.json")
        if local["pytestFiles"] != shard["files"] or local["separateScripts"] != shard["scripts"]:
            raise ValueError(f"{directory.name}: shard evidence disagrees with its recorded assignment")
        collection, execution = load(directory, "collection.json"), load(directory, "execution.json")
        if (execution["selected"] != collection["selected"] or execution["deselected"] or execution["collectionErrors"]
                or collection["deselected"] or collection["collectionErrors"]):
            raise ValueError(f"{directory.name}: execution differs from its collection or deselected tests")
        for node in execution["selected"]:
            outcome = execution["outcomes"].get(node)
            if outcome != "passed":
                raise ValueError(f"{directory.name}: node did not pass ({outcome or 'not executed'}): {node}")
        if set(execution["outcomes"]) != set(execution["selected"]):
            raise ValueError(f"{directory.name}: outcomes cover nodes that were not selected")
        seen_files += shard["files"]
        seen_scripts += shard["scripts"]
        executed += execution["selected"]
        for script in summary.get("separateScripts", []):
            results[script["path"]] = results.get(script["path"], 0) + 1
            if not script["passed"] or script["exitStatus"] != 0:
                raise ValueError(f"{directory.name}: isolated script did not pass: {script['path']}")
    if seen_index != set(range(count)):
        raise ValueError("Shard indexes are not exactly 0..COUNT-1")
    if len(seen_files) != len(set(seen_files)) or sorted(seen_files) != sorted(all_files):
        raise ValueError("Shard files are not a disjoint cover of the manifest files")
    if sorted(seen_scripts) != sorted(all_scripts) or len(set(seen_scripts)) != len(seen_scripts):
        raise ValueError("Isolated scripts are missing or assigned more than once")
    if sorted(results) != sorted(all_scripts) or any(number != 1 for number in results.values()):
        raise ValueError("Every isolated script must execute exactly once")
    duplicates = sorted({node for node in executed if executed.count(node) > 1})
    missing, extra = sorted(set(nodes) - set(executed)), sorted(set(executed) - set(nodes))
    if duplicates or missing or extra:
        raise ValueError(f"Executed nodes differ from the collected manifest: {len(missing)} missing, "
                         f"{len(extra)} extra, {len(duplicates)} duplicated")
    planned = shards.digest(count, [{"files": s["files"], "scripts": s["scripts"]} for s in
        sorted((load(d, "summary.json")["shard"] for d in shard_dirs), key=lambda s: s["index"])])
    if planned != digest:
        raise ValueError("The shard assignments do not reproduce the recorded partition digest")
    return {"schemaVersion": "of-release-acceptance-merged.v1", "accepted": True, "suite": identity[0],
            "backend": manifest["backend"], "dashboard": manifest["dashboard"], "shards": count,
            "partitionDigest": digest, "collectedNodes": len(nodes), "executedNodes": len(executed),
            "passed": len(executed), "pytestFiles": len(all_files), "separateScripts": len(all_scripts)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest-dir", type=Path, required=True)
    parser.add_argument("--shard-dir", type=Path, action="append", required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--suite", choices=("combined", "commerce"))
    parser.add_argument("--backend-sha")
    parser.add_argument("--dashboard-sha")
    args = parser.parse_args()
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    result = {"schemaVersion": "of-release-acceptance-merged.v1", "accepted": False,
              "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    try:
        result.update(merge(args.manifest_dir, args.shard_dir, backend_sha=args.backend_sha,
                            dashboard_sha=args.dashboard_sha, suite=args.suite))
        print("Accepted:", json.dumps({k: result[k] for k in ("collectedNodes", "executedNodes", "passed", "pytestFiles",
              "separateScripts", "shards")}, sort_keys=True))
        return 0
    except (ValueError, KeyError) as error:
        result["blocker"] = str(error)
        print("Merged release acceptance incomplete:", error, file=sys.stderr)
        return 1
    finally:
        result["finishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        (args.evidence_dir / "merged-summary.json").write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
