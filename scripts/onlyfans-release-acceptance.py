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
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
import onlyfans_acceptance_shards as shards  # noqa: E402
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


def python_versions() -> str:
    """Installed distributions (pip freeze equivalent; works in pip-less virtual environments)."""
    from importlib import metadata
    return "".join(sorted(f"{item.metadata['Name']}=={item.version}\n" for item in metadata.distributions()))


def junit_counts(path: Path) -> dict:
    cases = list(ET.parse(path).getroot().iter("testcase"))
    failed = sum(case.find("failure") is not None for case in cases)
    errored = sum(case.find("error") is not None for case in cases)
    skipped = sum(case.find("skipped") is not None for case in cases)
    return {"reported": len(cases), "passed": len(cases) - failed - errored - skipped,
            "failed": failed, "errored": errored, "skipped": skipped}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("combined", "commerce"), default="combined")
    parser.add_argument("--dashboard-dir", type=Path, required=True)
    parser.add_argument("--dashboard-sha", required=True)
    parser.add_argument("--backend-sha", default=os.environ.get("GITHUB_SHA"))
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--shard", default="0/1", help="INDEX/COUNT whole-file shard; 0/1 runs everything")
    parser.add_argument("--manifest-only", action="store_true",
                        help="Collect the complete manifest for the merger; execute nothing")
    args = parser.parse_args()
    evidence = args.evidence_dir
    if not evidence.is_absolute() or not args.dashboard_dir.is_absolute():
        parser.error("Dashboard and evidence directories must be absolute")
    evidence.mkdir(parents=True, exist_ok=True)
    index, count = shards.parse_shard(args.shard)
    report = {"schemaVersion": "of-release-acceptance.v1", "suite": args.suite,
              "scope": "manifest" if args.manifest_only else "shard" if count > 1 else "full",
              "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "commands": [], "accepted": False}
    try:
        if not re.fullmatch(r"[0-9a-f]{40}", args.dashboard_sha):
            raise ValueError("Record the exact full dashboard SHA")
        if args.dashboard_sha != (ROOT / "docs/onlyfans/ACCEPTANCE-DASHBOARD-SHA").read_text().strip():
            raise ValueError("The dashboard SHA differs from the committed acceptance pin")
        if args.manifest_only:
            # Collection-only ground truth for the merger: no database, browser or dashboard install is needed.
            report["backend"] = source_proof(ROOT, args.backend_sha)
            report["dashboard"] = {"method": "pin", "sha": args.dashboard_sha}
            (evidence / "python-versions.txt").write_text(python_versions())
            dashboard = pg_bin = None
        else:
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
            (evidence / "python-versions.txt").write_text(python_versions())
        manifest = json.loads(MANIFEST.read_text())[args.suite]
        plan = shards.plan(ROOT, manifest, count)
        all_files, all_scripts = plan["allFiles"], plan["allScripts"]
        if args.manifest_only:
            files, separate = all_files, []
        else:
            files, separate = plan["shards"][index]["files"], plan["shards"][index]["scripts"]
        report["shard"] = {"index": index, "count": count, "digest": plan["digest"], "files": files, "scripts": separate}
        (evidence / "expanded-manifest.json").write_text(json.dumps({"pytestFiles": files, "separateScripts": separate,
            "allPytestFiles": all_files, "allSeparateScripts": all_scripts, "shard": report["shard"]}, indent=2) + "\n")
        env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(ROOT / "scripts"), str(ROOT / "service"), str(ROOT))),
               "ONLYFANS_TEST_DASHBOARD": str(dashboard or ""), "ONLYFANS_TEST_EVIDENCE": str(evidence),
               "ONLYFANS_TEST_PG_BIN": str(pg_bin or os.environ.get("ONLYFANS_TEST_PG_BIN", "")),
               "ONLYFANS_TEST_CONTENT": "1", "ONLYFANS_TEST_OFFER_IMPORT": "1", "ONLYFANS_TEST_CONTENT_SYNC": "1",
               "ONLYFANS_TEST_ONLY_OFFER_IMPORT": "0", "PYTHONDONTWRITEBYTECODE": "1"}
        base = [sys.executable, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider", "-p", "onlyfans_acceptance_plugin"]
        collection = {"selected": [], "deselected": [], "collectionErrors": []}
        if files:
            collect = [*base, "--collect-only", *files]
            env["ONLYFANS_ACCEPTANCE_EVENT_FILE"] = str(evidence / "collection.json")
            report["commands"].append({"argv": collect, "log": "collection.log"})
            if execute(collect, evidence / "collection.log", env):
                raise ValueError("Required acceptance collection failed")
            collection = json.loads((evidence / "collection.json").read_text())
            if not collection["selected"] or collection["deselected"] or collection["collectionErrors"]:
                raise ValueError("Required acceptance collection was empty, errored or deselected")
        else:
            (evidence / "collection.json").write_text(json.dumps(collection) + "\n")
        if args.manifest_only:
            report["pytest"] = {"collected": len(collection["selected"]), "files": len(files)}
            report["manifestOnly"] = True
            report["accepted"] = False
            print("Collected manifest:", json.dumps(report["pytest"], sort_keys=True))
            return 0
        env["ONLYFANS_ACCEPTANCE_EVENT_FILE"] = str(evidence / "execution.json")
        if files:
            command = [*base, "--junitxml=" + str(evidence / "acceptance.xml"), *files]
            report["commands"].append({"argv": command, "log": "acceptance.log", "junit": "acceptance.xml"})
            code = execute(command, evidence / "acceptance.log", env)
            execution = json.loads((evidence / "execution.json").read_text())
            counts = junit_counts(evidence / "acceptance.xml")
        else:
            code, execution = 0, {"selected": [], "deselected": [], "collectionErrors": [], "outcomes": {}}
            (evidence / "execution.json").write_text(json.dumps(execution) + "\n")
            counts = {"reported": 0, "passed": 0, "failed": 0, "errored": 0, "skipped": 0}
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
        print("Accepted" if count == 1 else f"Shard {index}/{count} passed (not release acceptance until merged):",
              json.dumps(counts, sort_keys=True), f"; {len(script_results)} separate script commands passed")
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
