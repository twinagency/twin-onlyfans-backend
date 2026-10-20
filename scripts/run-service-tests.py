#!/usr/bin/env python3
"""Run script-style and pytest checks in separate interpreters.

PostgreSQL acceptance belongs to the disposable-database workflow. Pass
--postgres with ONLYFANS_TEST_PG_URL to include those modules locally.
"""
import argparse
import ast
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres", action="store_true")
    args = parser.parse_args()
    if args.postgres and not os.environ.get("ONLYFANS_TEST_PG_URL"):
        parser.error("--postgres requires ONLYFANS_TEST_PG_URL for a disposable server")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "service"), str(ROOT), env.get("PYTHONPATH", "")]
    )
    failures = []
    skipped = 0
    for path in sorted((ROOT / "service" / "tests").glob("test_*.py")):
        if path.name.endswith("_pg.py") and not args.postgres:
            skipped += 1
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        uses_pytest = any(
            (isinstance(node, ast.Import) and any(
                name.name == "pytest" or name.name.startswith("pytest.")
                for name in node.names
            )) or (
                isinstance(node, ast.ImportFrom)
                and (node.module or "").split(".")[0] == "pytest"
            )
            for node in ast.walk(tree)
        )
        command = [sys.executable]
        if uses_pytest:
            command += ["-m", "pytest", "-q"]
        command.append(str(path))
        print(f"Running {path.relative_to(ROOT)}", flush=True)
        if subprocess.run(command, cwd=ROOT, env=env).returncode:
            failures.append(path.name)
    if skipped:
        print(f"{skipped} PostgreSQL modules excluded; use --postgres with a disposable server to include them.")
    if failures:
        print("Failed: " + ", ".join(failures), file=sys.stderr)
    return bool(failures)


if __name__ == "__main__":
    sys.exit(main())
