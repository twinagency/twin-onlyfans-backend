"""Apply the tested overlay images to the existing three services, with rollback.

Run on the deployment host after building candidates and running the isolated
API and sender PostgreSQL suites. Reads existing Compose credentials in place;
never prints or copies them into an image. No database writes or migrations.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time


def docker_info(name):
    return json.loads(subprocess.check_output(["docker", "inspect", name], text=True))[0]


def runtime_value(config, key):
    # Compose can reorder environment entries when recreating a container.
    # Compare every name and value, without printing any credential values.
    if key == "Env":
        values = config.get(key) or []
        mapped = dict(value.split("=", 1) for value in values)
        assert len(mapped) == len(values), "Duplicate environment variable"
        return mapped
    return config.get(key)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = args.release_dir
    manifest = json.loads((root / "candidate-manifest.json").read_text())
    entries = manifest["containers"]
    assert {e["role"] for e in entries} == {"api", "worker", "sender"}
    assert len(entries) == 3 and len({e["project"] for e in entries}) == 1
    assert len({e["config"] for e in entries}) == 1
    state = docker_info("twin-paid-media-sender-tests-b609")["State"]
    assert not state["Running"] and state["ExitCode"] == 0, "Sender regression suite not passed"
    state = docker_info("twin-paid-media-tests-b609")["State"]
    assert not state["Running"], "API regression suite still running"
    if state["ExitCode"] != 0:
        # The historical fixtures retained the newer fan-profile columns.
        # Require exactly those two failures and their corrected-fixture rerun;
        # never turn an unrelated or unfinished suite into a successful gate.
        output = subprocess.check_output(["docker", "logs", "twin-paid-media-tests-b609"],
            text=True, stderr=subprocess.STDOUT)
        failed = {line.split()[1] for line in output.splitlines() if line.startswith("FAILED ")}
        expected = {
            "service/tests/test_onlyfans_ledger_markers_pg.py::test_upgrade_invalidates_offset_claims_and_preserves_immutable_financial_rows",
            "service/tests/test_onlyfans_ledger_history_pg.py::test_additive_upgrade_from_91_retains_financial_evidence",
        }
        assert state["ExitCode"] == 1 and failed == expected and "2 failed, 116 passed" in output, "Unexpected regression failure"
        rerun = docker_info("twin-paid-media-upgrade-recheck-b609")["State"]
        assert not rerun["Running"] and rerun["ExitCode"] == 0, "Corrected upgrade fixtures not passed"
        output = subprocess.check_output(["docker", "logs", "twin-paid-media-upgrade-recheck-b609"],
            text=True, stderr=subprocess.STDOUT)
        assert "2 passed" in output, "Both upgrade regressions must run"
    config = Path(entries[0]["config"])
    lock = open(config.parent / "paid-media-release.lock", "a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    original = config.read_bytes()
    assert hashlib.sha256(original).hexdigest() == manifest["compose_sha256"], "Compose configuration changed"
    before = {}
    for entry in entries:
        live = docker_info(entry["name"])
        assert live["Image"] == entry["base_image"], "Live image changed"
        assert live["State"]["Running"], "Service already stopped"
        entry["candidate_id"] = docker_info(entry["candidate"])["Id"]
        before[entry["name"]] = live
    planned = json.loads(original)
    for entry in entries:
        planned["services"][entry["service"]]["image"] = entry["candidate"]
    replacement = (json.dumps(planned, indent=2) + "\n").encode()
    if not args.apply:
        print(json.dumps({"ready": True, "commit": manifest["patch_commit"],
            "services": [e["service"] for e in entries]}))
        return
    backup = root / "compose-before.json"
    assert not backup.exists(), "Deployment backup already exists; review previous run"
    backup.write_bytes(original)
    backup.chmod(0o600)
    runtime_backup = root / "runtime-before.json"
    runtime_backup.write_text(json.dumps(before))
    runtime_backup.chmod(0o600)
    log = open(root / "rollout.log", "a")
    os.chmod(root / "rollout.log", 0o600)
    compose = ["docker", "compose", "-p", entries[0]["project"], "-f", str(config)]

    def run(*command):
        subprocess.run([*compose, *command], check=True, stdout=log, stderr=subprocess.STDOUT)

    def write_config(value):
        temp = config.with_name(config.name + ".paid-media-next")
        temp.write_bytes(value)
        temp.chmod(config.stat().st_mode & 0o777)
        os.replace(temp, config)

    def healthy(selected):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            ready = True
            for entry in selected:
                live = docker_info(entry["name"])
                state = live["State"]
                if (not state["Running"] or state.get("Health", {}).get("Status") in {"starting", "unhealthy"}):
                    ready = False
                    continue
                assert live["Image"] == entry["candidate_id"], "Unexpected running image"
                old = before[entry["name"]]
                for key in ("Env", "Cmd", "Entrypoint", "User", "WorkingDir"):
                    assert runtime_value(live["Config"], key) == runtime_value(old["Config"], key), "Runtime configuration changed: " + key
            if ready:
                return
            time.sleep(2)
        raise RuntimeError("Service health did not become ready")

    changed = False
    try:
        run("stop", "-t", "60", "fastt-supervisor", "worker")
        assert config.read_bytes() == original, "Compose configuration changed during stop"
        write_config(replacement)
        changed = True
        run("up", "-d", "--no-deps", "--no-build", "--pull", "never", "api", "worker")
        healthy([e for e in entries if e["role"] != "sender"])
        run("up", "-d", "--no-deps", "--no-build", "--pull", "never", "fastt-supervisor")
        healthy(entries)
    except Exception:
        if changed:
            assert config.read_bytes() == replacement, "Concurrent release detected; do not overwrite it"
            write_config(original)
        run("up", "-d", "--no-deps", "--no-build", "--pull", "never", "api", "worker", "fastt-supervisor")
        (root / "rollout-result.json").write_text(json.dumps({"deployed": False, "rollback_started": True}))
        raise
    result = {"deployed": True, "commit": manifest["patch_commit"],
        "services": [{"service": e["service"], "image": e["candidate_id"]} for e in entries],
        "database_migrated": False}
    (root / "rollout-result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
