#!/usr/bin/env python3
"""Run one pinned backend/dashboard acceptance command with complete evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = Path(__file__).with_name("onlyfans-release-acceptance-manifest.json")
MARKER = ".onlyfans-acceptance-source.json"


def git(directory, *arguments):
    return subprocess.check_output(["git", "-C", str(directory), *arguments], text=True).strip()


def source_proof(directory: Path, expected: str | None = None) -> dict:
    if (directory / ".git").exists():
        sha = git(directory, "rev-parse", "HEAD")
        if expected and sha != expected:
            raise ValueError("The paired checkout HEAD differs from the recorded candidate SHA")
        if git(directory, "status", "--porcelain", "--untracked-files=normal"):
            raise ValueError("Acceptance requires a clean candidate checkout; commit source changes first")
        return {"method": "git", "sha": sha, "tree": git(directory, "rev-parse", "HEAD^{tree}")}
    marker = json.loads((directory / MARKER).read_text())
    if marker.get("schemaVersion") != "of-acceptance-source.v1" or marker.get("commit") != expected:
        raise ValueError("The paired archive provenance does not match the candidate SHA")
    for name, record in marker["files"].items():
        if Path(name).is_absolute() or ".." in Path(name).parts:
            raise ValueError("Paired archive source manifest contains an unsafe path")
        path = directory / name
        if "symlink" in record:
            # A contained tracked symlink kept by prepare-onlyfans-dashboard.py: verify the link itself.
            if not path.is_symlink() or os.readlink(path) != record["symlink"]:
                raise ValueError("Paired archive source changed after its digest was verified")
            continue
        if not path.is_file() or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]:
            raise ValueError("Paired archive source changed after its digest was verified")
        if bool(path.stat().st_mode & 0o111) != record["executable"]:
            raise ValueError("Paired archive source executable mode changed")
    generated_dirs = {"node_modules", ".next", ".git", "out", "dist", "coverage", "test-results", "playwright-report", "__pycache__"}
    for base, directories, names in os.walk(directory):
        directories[:] = [name for name in directories if name not in generated_dirs]
        for name in names:
            relative = (Path(base) / name).relative_to(directory).as_posix()
            if (relative not in marker["files"] and relative != MARKER and name != "next-env.d.ts"
                    and not name.endswith(".tsbuildinfo")):
                raise ValueError("Unrecorded source appeared in the paired archive checkout")
    return {"method": "verified-git-archive", "sha": expected, "archiveSha256": marker["archiveSha256"],
            "verifiedFiles": len(marker["files"])}


def execute(command: list[str], logfile: Path, env: dict) -> int:
    with logfile.open("w") as log:
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        assert process.stdout is not None
        for line in process.stdout:
            print(line, end="", flush=True)
            log.write(line)
            log.flush()
        return process.wait()


def junit_counts(path: Path) -> dict:
    cases = list(ET.parse(path).getroot().iter("testcase"))
    failed = sum(case.find("failure") is not None for case in cases)
    errored = sum(case.find("error") is not None for case in cases)
    skipped = sum(case.find("skipped") is not None for case in cases)
    return {"reported": len(cases), "passed": len(cases) - failed - errored - skipped,
            "failed": failed, "errored": errored, "skipped": skipped}


def expanded_manifest(suite: str) -> tuple[list[str], list[str]]:
    manifest = json.loads(MANIFEST.read_text())[suite]
    separate = manifest["separateScripts"]
    if len(separate) != len(set(separate)):
        raise ValueError("Required isolated scripts are duplicated")
    files = set()
    for pattern in manifest["patterns"]:
        matches = list(ROOT.glob(pattern))
        if not matches:
            raise ValueError(f"Required acceptance pattern matched no tests: {pattern}")
        files.update(str(path.relative_to(ROOT)) for path in matches)
    files.difference_update(separate)
    if any(not (ROOT / name).is_file() for name in separate):
        raise ValueError("A required isolated script is missing")
    return sorted(files), separate


def shard_manifest(suite: str, index: int, count: int) -> tuple[list[str], list[str]]:
    if not 1 <= count <= 32 or not 0 <= index < count:
        raise ValueError("Acceptance shard index/count is invalid")
    files, separate = expanded_manifest(suite)
    assigned = files[index::count]
    if not assigned:
        raise ValueError("Acceptance shard has no assigned test files")
    return assigned, separate if index == 0 else []


def checked_events(path: Path) -> dict:
    events = json.loads(path.read_text())
    selected = events.get("selected", [])
    if (not selected or len(selected) != len(set(selected)) or events.get("collected") != len(selected) or events.get("exitStatus") != 0
            or events.get("deselected") or events.get("collectionErrors")):
        raise ValueError("Shard collection/execution is empty, duplicated, failed or deselected")
    return events


def aggregate_shards(directory: Path, suite: str, count: int, backend_sha: str, dashboard_sha: str) -> dict:
    """Recompute completeness and outcomes from every shard's evidence, never its green label alone."""
    if not 1 <= count <= 32:
        raise ValueError("Acceptance shard count is invalid")
    if (not re.fullmatch(r"[0-9a-f]{40}", backend_sha or "")
            or not re.fullmatch(r"[0-9a-f]{40}", dashboard_sha or "")):
        raise ValueError("Record both exact full candidate SHAs")
    if dashboard_sha != (ROOT / "docs/onlyfans/ACCEPTANCE-DASHBOARD-SHA").read_text().strip():
        raise ValueError("The dashboard SHA differs from the committed acceptance pin")
    expected_backend = source_proof(ROOT, backend_sha)
    files, separate = expanded_manifest(suite)
    summaries = list(directory.rglob("summary.json"))
    if len(summaries) != count:
        raise ValueError("Required shard evidence is missing or duplicated")
    seen_indexes, seen_files, seen_nodes, seen_scripts = set(), [], set(), []
    dashboard_proof = None
    total = {key: 0 for key in ("reported", "passed", "failed", "errored", "skipped", "collected", "deselected")}
    for summary in summaries:
        report = json.loads(summary.read_text())
        shard = report.get("shard", {})
        index = shard.get("index")
        if (type(index) is not int or not 0 <= index < count or index in seen_indexes
                or shard.get("count") != count or report.get("suite") != suite or report.get("accepted") is not True):
            raise ValueError("Shard identity, suite or acceptance is invalid")
        seen_indexes.add(index)
        assigned, scripts = shard_manifest(suite, index, count)
        expanded = json.loads((summary.parent / "expanded-manifest.json").read_text())
        if (expanded != {"pytestFiles": assigned, "separateScripts": scripts}
                or shard.get("pytestFiles") != assigned or shard.get("separateScripts") != scripts):
            raise ValueError("Shard manifest differs from its complete deterministic assignment")
        seen_files.extend(assigned)
        if report.get("backend") != expected_backend or report.get("dashboard", {}).get("sha") != dashboard_sha:
            raise ValueError("Shard candidate source proof differs from the final pair")
        if dashboard_proof is None:
            dashboard_proof = report["dashboard"]
        elif report["dashboard"] != dashboard_proof:
            raise ValueError("Shard dashboard source proofs differ")
        collection = checked_events(summary.parent / "collection.json")
        execution = checked_events(summary.parent / "execution.json")
        nodes = collection["selected"]
        if (execution["selected"] != nodes or any(node.split("::", 1)[0] not in assigned for node in nodes)
                or seen_nodes.intersection(nodes)):
            raise ValueError("Shard node identities changed, overlap or escape their assigned files")
        seen_nodes.update(nodes)
        counts = junit_counts(summary.parent / "acceptance.xml")
        counts.update(collected=len(nodes), deselected=len(execution["deselected"]))
        if (counts != report.get("pytest") or counts["reported"] != len(nodes)
                or any(counts[key] for key in ("failed", "errored", "skipped", "deselected"))):
            raise ValueError("Shard JUnit outcomes are incomplete, failed, errored or skipped")
        for key in total:
            total[key] += counts[key]
        results = report.get("separateScripts", [])
        if [item.get("path") for item in results] != scripts:
            raise ValueError("Required isolated scripts are missing or duplicated")
        for index_in_shard, result in enumerate(results):
            output = (summary.parent / f"script-{index_in_shard}.log").read_text()
            if (result.get("passed") is not True or result.get("exitStatus") != 0
                    or "all cases passed" not in output or re.search(r"\bSKIPPED\b|\b[1-9][0-9]* FAILED\b", output)):
                raise ValueError("An isolated script did not complete acceptance")
        seen_scripts.extend(scripts)
    if (seen_indexes != set(range(count)) or len(seen_files) != len(set(seen_files))
            or sorted(seen_files) != files or seen_scripts != separate):
        raise ValueError("Shard union does not cover the full acceptance manifest exactly once")
    return {"schemaVersion": "of-release-acceptance.v1", "suite": suite, "accepted": True,
            "shardCount": count, "backend": expected_backend, "dashboard": dashboard_proof,
            "pytest": total, "pytestFiles": files, "nodeIDs": sorted(seen_nodes), "separateScripts": separate}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("combined", "commerce"), default="combined")
    parser.add_argument("--dashboard-dir", type=Path)
    parser.add_argument("--dashboard-sha", required=True)
    parser.add_argument("--backend-sha", default=os.environ.get("GITHUB_SHA"))
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--aggregate", action="store_true")
    args = parser.parse_args()
    evidence = args.evidence_dir
    if not evidence.is_absolute() or (not args.aggregate and (args.dashboard_dir is None or not args.dashboard_dir.is_absolute())):
        parser.error("Dashboard and evidence directories must be absolute")
    evidence.mkdir(parents=True, exist_ok=True)
    if args.aggregate:
        try:
            report = aggregate_shards(evidence, args.suite, args.shard_count, args.backend_sha, args.dashboard_sha)
            print("Accepted complete shard union:", json.dumps(report["pytest"], sort_keys=True))
            code = 0
        except Exception as error:
            report = {"accepted": False, "blocker": str(error)}
            print("Release acceptance incomplete:", error, file=sys.stderr)
            code = 1
        (evidence / "aggregate-summary.json").write_text(json.dumps(report, indent=2) + "\n")
        return code
    report = {"schemaVersion": "of-release-acceptance.v1", "suite": args.suite,
              "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "commands": [], "accepted": False}
    try:
        if not re.fullmatch(r"[0-9a-f]{40}", args.dashboard_sha):
            raise ValueError("Record the exact full dashboard SHA")
        if args.dashboard_sha != (ROOT / "docs/onlyfans/ACCEPTANCE-DASHBOARD-SHA").read_text().strip():
            raise ValueError("The dashboard SHA differs from the committed acceptance pin")
        if os.environ.get("PYTEST_ADDOPTS"):
            raise ValueError("PYTEST_ADDOPTS is not permitted in required release acceptance")
        if not all(os.environ.get(key) for key in ("ONLYFANS_TEST_PG_URL", "ONLYFANS_PERSONALITY_TEST_DATABASE_URL")):
            raise ValueError("Both disposable PostgreSQL URL configurations are required; no PostgreSQL proof may skip")
        configured_pg_bin = os.environ.get("ONLYFANS_TEST_PG_BIN")
        if not configured_pg_bin:
            raise ValueError("ONLYFANS_TEST_PG_BIN must name the matching PostgreSQL client directory for restore proof")
        pg_bin = Path(configured_pg_bin)
        if not pg_bin.is_absolute() or any(not (pg_bin / name).is_file() or not os.access(pg_bin / name, os.X_OK)
                                            for name in ("pg_dump", "pg_restore")):
            raise ValueError("ONLYFANS_TEST_PG_BIN must be an absolute directory containing executable pg_dump and pg_restore")
        pg_bin = pg_bin.resolve()
        dashboard = args.dashboard_dir.resolve()
        for name in ("node_modules/next/dist/bin/next", "preview/onlyfans-integration/package.json",
                     "scripts/check-onlyfans-http-browser.mjs"):
            if not (dashboard / name).is_file():
                raise ValueError("Install the complete paired dashboard and lockfile dependencies before acceptance")
        if list((dashboard / "preview/onlyfans-integration").glob(".env*")):
            raise ValueError("The isolated dashboard harness must not load environment files")
        report["backend"] = source_proof(ROOT, args.backend_sha)
        report["dashboard"] = source_proof(dashboard, args.dashboard_sha)
        versions = {"python": sys.version, "executable": sys.executable}
        for name, command in {"node": ["node", "--version"], "npm": ["npm", "--version"],
                              "pg_dump": [str(pg_bin / "pg_dump"), "--version"], "pg_restore": [str(pg_bin / "pg_restore"), "--version"]}.items():
            versions[name] = subprocess.check_output(command, text=True, stderr=subprocess.STDOUT).strip()
        versions["postgresClientDirectory"] = str(pg_bin)
        report["environment"] = versions
        (evidence / "python-versions.txt").write_text(subprocess.check_output([sys.executable, "-m", "pip", "freeze"], text=True))
        files, separate = shard_manifest(args.suite, args.shard_index, args.shard_count)
        report["shard"] = {"index": args.shard_index, "count": args.shard_count,
                           "pytestFiles": files, "separateScripts": separate}
        (evidence / "expanded-manifest.json").write_text(json.dumps({"pytestFiles": files, "separateScripts": separate}, indent=2) + "\n")
        env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(ROOT / "scripts"), str(ROOT / "service"), str(ROOT))),
               "ONLYFANS_TEST_DASHBOARD": str(dashboard), "ONLYFANS_TEST_EVIDENCE": str(evidence),
               "ONLYFANS_TEST_PG_BIN": str(pg_bin),
               "ONLYFANS_TEST_CONTENT": "1", "ONLYFANS_TEST_OFFER_IMPORT": "1", "ONLYFANS_TEST_CONTENT_SYNC": "1",
               "ONLYFANS_TEST_ONLY_OFFER_IMPORT": "0", "PYTHONDONTWRITEBYTECODE": "1"}
        base = [sys.executable, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider", "-p", "onlyfans_acceptance_plugin"]
        collect = [*base, "--collect-only", *files]
        env["ONLYFANS_ACCEPTANCE_EVENT_FILE"] = str(evidence / "collection.json")
        report["commands"].append({"argv": collect, "log": "collection.log"})
        if execute(collect, evidence / "collection.log", env):
            raise ValueError("Required acceptance collection failed")
        collection = json.loads((evidence / "collection.json").read_text())
        if not collection["selected"] or collection["deselected"] or collection["collectionErrors"]:
            raise ValueError("Required acceptance collection was empty, errored or deselected")
        env["ONLYFANS_ACCEPTANCE_EVENT_FILE"] = str(evidence / "execution.json")
        command = [*base, "--junitxml=" + str(evidence / "acceptance.xml"), *files]
        report["commands"].append({"argv": command, "log": "acceptance.log", "junit": "acceptance.xml"})
        code = execute(command, evidence / "acceptance.log", env)
        execution = json.loads((evidence / "execution.json").read_text())
        counts = junit_counts(evidence / "acceptance.xml")
        counts.update(collected=len(collection["selected"]), deselected=len(execution["deselected"]))
        report["pytest"] = counts
        if (code or execution["selected"] != collection["selected"] or execution["collectionErrors"]
                or counts["failed"] or counts["errored"] or counts["skipped"] or counts["deselected"]
                or counts["reported"] != counts["collected"]):
            raise ValueError("Required acceptance failed, errored, skipped, deselected or changed collection")
        script_results = []
        for index, name in enumerate(separate):
            command = [sys.executable, name]
            logfile = evidence / f"script-{index}.log"
            report["commands"].append({"argv": command, "log": logfile.name})
            code = execute(command, logfile, env)
            output = logfile.read_text()
            passed = code == 0 and "all cases passed" in output and not re.search(r"\bSKIPPED\b|\b[1-9][0-9]* FAILED\b", output)
            script_results.append({"path": name, "passed": passed, "exitStatus": code})
            if not passed:
                raise ValueError("Required isolated script did not report completed acceptance")
        report["separateScripts"] = script_results
        if source_proof(ROOT, args.backend_sha) != report["backend"] or source_proof(dashboard, args.dashboard_sha) != report["dashboard"]:
            raise ValueError("Candidate source changed during acceptance")
        report["accepted"] = True
        print("Accepted:", json.dumps(counts, sort_keys=True), f"; {len(script_results)} separate script commands passed")
        return 0
    except Exception as error:
        report["blocker"] = str(error)
        print("Release acceptance incomplete:", error, file=sys.stderr)
        return 1
    finally:
        report["finishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        (evidence / "summary.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
