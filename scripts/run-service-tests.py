#!/usr/bin/env python3
"""Run script-style and pytest checks in separate interpreters.

Hosted CI also runs the native connection/inbox smoke suite in its own
socket-only PostgreSQL cluster. Pass --postgres with ONLYFANS_TEST_PG_URL
to include every PostgreSQL module against an explicit disposable server.
"""
import argparse
import ast
from contextlib import contextmanager
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
NATIVE_PG_SMOKE = {
    "test_onlyfans_connections_pg.py",
    "test_onlyfans_connection_renewal_pg.py",
    "test_onlyfans_readonly_inbox_pg.py",
    "test_onlyfans_worker_entry_pg.py",
}


@contextmanager
def hosted_test_database(enabled, env):
    if not enabled:
        yield
        return
    # Use the runner's binaries, never its system service or any saved DB URL.
    candidates = [p.parent for p in Path("/usr/lib/postgresql").glob("*/bin/initdb")]
    if shutil.which("initdb"):
        candidates.append(Path(shutil.which("initdb")).parent)
    bins = next((p for p in sorted(candidates, reverse=True)
                 if all((p / name).is_file() for name in ("initdb", "pg_ctl", "psql"))), None)
    if bins is None:
        raise RuntimeError("Hosted native PostgreSQL checks require installed PostgreSQL binaries")
    with tempfile.TemporaryDirectory(prefix="twin-of-ci-pg-") as directory:
        root = Path(directory)
        root.chmod(0o700)
        data, socket = root / "data", root / "socket"
        socket.mkdir(mode=0o700)
        subprocess.run([str(bins / "initdb"), "-D", str(data), "-U", "twin_of_test",
                        "-A", "trust", "--no-locale", "-E", "UTF8"],
                       check=True, stdout=subprocess.DEVNULL, timeout=60)
        options = shlex.join(["-c", "listen_addresses=", "-k", str(socket), "-p", "5432",
                              "-c", "unix_socket_permissions=0700", "-c", "shared_buffers=32MB",
                              "-c", "max_connections=20"])
        started = False
        try:
            subprocess.run([str(bins / "pg_ctl"), "-D", str(data), "-l", str(root / "postgres.log"),
                            "-o", options, "-w", "-t", "30", "start"],
                           check=True, stdout=subprocess.DEVNULL, timeout=40)
            started = True
            # Prove this is our private cluster and that it has no TCP listener.
            observed = subprocess.check_output([
                str(bins / "psql"), "-h", str(socket), "-p", "5432", "-U", "twin_of_test",
                "-d", "postgres", "-A", "-t", "-c",
                "SELECT current_setting('data_directory'); SELECT current_setting('listen_addresses');",
            ], text=True, timeout=10).splitlines()
            if observed != [str(data), ""]:
                raise RuntimeError("Native PostgreSQL test-cluster isolation check failed")
            env["ONLYFANS_TEST_PG_URL"] = (
                "postgresql+psycopg2://twin_of_test@/postgres?" + urlencode({"host": str(socket), "port": "5432"})
            )
            print("Native PostgreSQL smoke checks use an owned socket-only test cluster", flush=True)
            yield
        finally:
            if started:
                subprocess.run([str(bins / "pg_ctl"), "-D", str(data), "-m", "fast", "-w", "stop"],
                               check=True, stdout=subprocess.DEVNULL, timeout=40)


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
    hosted_smoke = os.environ.get("GITHUB_ACTIONS") == "true" and not args.postgres
    with hosted_test_database(hosted_smoke, env):
        return run_modules(args.postgres, hosted_smoke, env)


def run_modules(all_postgres, hosted_smoke, env):
    failures = []
    skipped = 0
    paths = sorted((ROOT / "service" / "tests").glob("test_*.py"))
    if hosted_smoke and NATIVE_PG_SMOKE - {path.name for path in paths}:
        raise RuntimeError("Required native PostgreSQL smoke module is missing")
    for path in paths:
        if path.name.endswith("_pg.py") and not (all_postgres or hosted_smoke and path.name in NATIVE_PG_SMOKE):
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
        print(f"{skipped} other PostgreSQL modules excluded; use --postgres with a disposable server to include them.")
    if failures:
        print("Failed: " + ", ".join(failures), file=sys.stderr)
    return bool(failures)


if __name__ == "__main__":
    sys.exit(main())
