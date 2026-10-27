#!/usr/bin/env python3
"""Regression test for the Windows test runner's pass/fail decision (R6).

The runner must require BOTH of these to report a pass:

  1. Python's own process exit status is 0, and
  2. the suite's output contains the house success marker "all cases passed".

Before the repair the runner checked only the marker (via `findstr`, whose exit
status overwrote Python's), so a suite that printed the marker and then exited
non-zero was reported as "1 passed, 0 failed" and the runner exited 0. These
cases pin that down.

Every case runs against TEMPORARY probe suites created under a private temp
directory and removed in a `finally` block, so no fake test is ever left in the
real suite and a crashed case cannot leak files.

Windows:  .venv\\Scripts\\python.exe service\\tests\\test_twin_bridge_runner.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SERVICE_DIR = TESTS_DIR.parent
RUNNER = SERVICE_DIR / "run_tests.cmd"

failures: list[str] = []


def check(label, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def check_true(label, got) -> None:
    check(label, bool(got), True)


# ── the probe suites ──────────────────────────────────────────────────
# Each is a minimal stand-in for a real suite: the runners only look at the
# exit status and the output marker, never at the module's contents.

PROBES = {
    "test_zzprobe_ok.py": (
        "import sys\n"
        "print('all cases passed (synthetic runner probe)')\n"
        "sys.exit(0)\n"
    ),
    # The exact defect: the marker is printed, then the process fails.
    "test_zzprobe_marker_exit7.py": (
        "import sys\n"
        "print('all cases passed (synthetic runner probe)')\n"
        "sys.exit(7)\n"
    ),
    "test_zzprobe_no_marker.py": "print('nothing to report')\n",
    "test_zzprobe_crash.py": "raise RuntimeError('synthetic crashing probe')\n",
}


def invoke(probe_dir: Path, args: list[str]) -> tuple[int, str]:
    """Run the Windows test runner against one temporary probe directory."""
    env = dict(os.environ)
    env["RUN_TESTS_DIR"] = str(probe_dir)
    proc = subprocess.run(
        ["cmd", "/c", str(RUNNER), *args],
        capture_output=True, text=True, env=env, cwd=str(SERVICE_DIR),
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def scenario(label, probe_names, expect_pass, marker, extra=(), args=("",)):
    """Write one probe set, run the runner once, and assert the verdict."""
    probe_dir = Path(tempfile.mkdtemp(prefix="bridge-runner-probe-"))
    try:
        for name in probe_names:
            (probe_dir / name).write_text(PROBES[name], encoding="utf-8")
        calls = [invoke(probe_dir, [a] if a else []) for a in args]
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)
    code, output = calls[0]
    if expect_pass:
        check(f"{label}: the runner exits zero", code, 0)
        check_true(f"{label}: it reports a pass", "all backend suites passed" in output)
    else:
        check_true(f"{label}: the runner exits non-zero", code != 0)
        check_true(f"{label}: it reports a failure", "SOME TESTS FAILED" in output)
        check_true(f"{label}: it names the failing suite", "[FAIL]" in output)
    check(f"{label}: the tally", marker in output, True)
    for needle in extra:
        check_true(f"{label}: {needle!r} appears", needle in output)
    check_true(f"{label}: it leaves no probe behind",
               not probe_dir.exists())
    print()


print("-- R6: the runner trusts both the exit status and the marker --")
check_true("the Windows runner is present", RUNNER.is_file())

if not RUNNER.is_file():
    print(f"{len(failures)} FAILED")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)

scenario("a passing suite", ["test_zzprobe_ok.py"], True, "1 passed, 0 failed")
scenario(
    "marker then exit 7", ["test_zzprobe_marker_exit7.py"], False,
    "0 passed, 1 failed", extra=("python exit 7",),
)
scenario("a suite with no marker", ["test_zzprobe_no_marker.py"], False,
         "0 passed, 1 failed", extra=("all cases passed",))
scenario("a crashing suite", ["test_zzprobe_crash.py"], False, "0 passed, 1 failed",
         extra=("Traceback",))
scenario("a suite that is not there", [], False, "0 passed, 1 failed",
         args=("test_zzprobe_missing",))
scenario("all four probes together", list(PROBES), False, "1 passed, 3 failed")

print()
if failures:
    print(f"{len(failures)} FAILED")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print("all cases passed (twin bridge runner)")
