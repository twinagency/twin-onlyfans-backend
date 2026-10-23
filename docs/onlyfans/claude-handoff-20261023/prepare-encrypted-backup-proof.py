"""Reviewed preparation only; root release owner decides whether to execute on host.

Uses existing 0088 runtime operations without modifying running containers.
Copies matching clients from the existing PostgreSQL container into a mounted
ops directory. A new key stays in /etc/twin-onlyfans, separate from encrypted
backups. Restore targets a disposable container on a new internal-only network.
No credentials or private archive contents are printed.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
from datetime import datetime, timezone

LIVE_IMAGE = "sha256:7e23784ff8cd3bcbd43c13cbcbe9b2eb8117fb0010285ddf1926d232596edc2f"
LIVE_REVISION = "107af2a4a8c2a17fc9a673fea60a1dff21d381bf"
DB_CONTAINER = "twin-onlyfans-database-1"
NATIVE_NETWORK = "twin-onlyfans_default"
LIBRARIES = ["libpq.so.5", "libzstd.so.1", "liblz4.so.1", "libcrypto.so.3", "libz.so.1",
             "libssl.so.3", "libgssapi_krb5.so.2", "libldap.so.2", "libxxhash.so.0",
             "libkrb5.so.3", "libk5crypto.so.3", "libcom_err.so.2", "libkrb5support.so.0",
             "liblber.so.2", "libsasl2.so.2", "libkeyutils.so.1"]


def run(args, *, data=None, timeout=180):
    result = subprocess.run(args, input=data, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError("bounded recovery command failed; no sender started")
    return result.stdout.strip()


def private_file(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def main():
    if os.geteuid() != 0 or sys.argv[1:] != ["--execute-reviewed-backup-only"]:
        raise RuntimeError("root and explicit reviewed backup-only invocation required")
    lock = open("/var/lock/twin-onlyfans-native-release.lock", "a")
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    for container in ["twin-onlyfans-api-1", "twin-onlyfans-worker-1"]:
        info = json.loads(run(["docker", "inspect", container]))[0]
        if info["Image"] != LIVE_IMAGE or info["Config"]["Labels"].get("org.opencontainers.image.revision") != LIVE_REVISION:
            raise RuntimeError("live image changed; re-inspect before backup")
    api = json.loads(run(["docker", "inspect", "twin-onlyfans-api-1"]))[0]
    environment = dict(item.split("=", 1) for item in api["Config"]["Env"] if "=" in item)
    source_url = environment.get("DATABASE_URL", "")
    if not source_url.startswith("postgresql") or any(c in source_url for c in ["\n", "\r"]):
        raise RuntimeError("unexpected source database authority")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)
    root = Path("/var/backups/twin-onlyfans") / ("final-0088-" + stamp)
    root.mkdir(mode=0o700)
    clients = root / "pg16"
    (clients / "bin").mkdir(parents=True, mode=0o700)
    (clients / "lib").mkdir(mode=0o700)
    key = Path("/etc/twin-onlyfans") / ("backup-key-0088-" + stamp)
    private_file(key, base64.b64encode(secrets.token_bytes(32)).decode())
    source_env = root / "source.env"
    target_env = root / "target.env"
    db_env = root / "proof-db.env"
    private_file(source_env, "ONLYFANS_RECOVERY_URL=" + source_url + "\nPYTHONPATH=/app/service\nLD_LIBRARY_PATH=/ops/pg16/lib\n")
    proof_network = "twin-of-restore-" + stamp.lower()
    proof_container = "twin-of-restore-db-" + stamp.lower()
    proof_created = network_created = False
    try:
        for name in ["pg_dump", "pg_restore"]:
            run(["docker", "cp", "-L", DB_CONTAINER + ":/usr/lib/postgresql/16/bin/" + name, str(clients / "bin" / name)])
        for name in LIBRARIES:
            run(["docker", "cp", "-L", DB_CONTAINER + ":/lib/x86_64-linux-gnu/" + name, str(clients / "lib" / name)])
        common = ["docker", "run", "--rm", "--entrypoint", "python", "--mount", f"type=bind,source={clients},target=/ops/pg16,readonly", "--mount", f"type=bind,source={root},target=/backup", "--mount", f"type=bind,source={key},target=/run/backup-key,readonly"]
        check = "import subprocess; print(subprocess.check_output(['/ops/pg16/bin/pg_dump','--version'],text=True).strip()); print(subprocess.check_output(['/ops/pg16/bin/pg_restore','--version'],text=True).strip())"
        versions = run(common + ["--network", "none", "--env-file", str(source_env), LIVE_IMAGE, "-c", check])
        parsed_versions = [re.fullmatch(r"(pg_dump|pg_restore) \(PostgreSQL\) (\d+\.\d+)(?: .*)?", line)
                           for line in versions.splitlines()]
        if (len(parsed_versions) != 2 or any(match is None for match in parsed_versions)
            or [(match.group(1), match.group(2)) for match in parsed_versions] != [("pg_dump", "16.15"), ("pg_restore", "16.15")]):
            raise RuntimeError("matching PostgreSQL16.15 clients required")
        call = "import os,json; from pathlib import Path; from onlyfans_runtime.operations import {action}; result={action}(os.environ['ONLYFANS_RECOVERY_URL'],'/backup/checkpoint.twinof',key=Path('/run/backup-key').read_text(),pg_bin='/ops/pg16/bin'); print(json.dumps(result,sort_keys=True))"
        backup = json.loads(run(common + ["--network", NATIVE_NETWORK, "--env-file", str(source_env), LIVE_IMAGE, "-c", call.format(action="backup")]))
        if backup.get("schema") != "0088_native_vault_media":
            raise RuntimeError("backup schema differs from inspected live schema")
        source_env.unlink()
        password = secrets.token_urlsafe(32)
        private_file(db_env, "POSTGRES_USER=recovery\nPOSTGRES_DB=proof\nPOSTGRES_PASSWORD=" + password + "\n")
        private_file(target_env, "ONLYFANS_RECOVERY_URL=postgresql+psycopg2://recovery:" + password + "@proof-db:5432/proof\nPYTHONPATH=/app/service\nLD_LIBRARY_PATH=/ops/pg16/lib\n")
        run(["docker", "network", "create", "--internal", proof_network]); network_created = True
        db_image = json.loads(run(["docker", "inspect", DB_CONTAINER]))[0]["Image"]
        run(["docker", "run", "-d", "--name", proof_container, "--network", proof_network, "--network-alias", "proof-db", "--env-file", str(db_env), db_image]); proof_created = True
        run(["docker", "exec", proof_container, "sh", "-c", "for i in $(seq 1 30); do pg_isready -U recovery -d proof >/dev/null 2>&1 && exit 0; sleep 1; done; exit 1"], timeout=40)
        restored = json.loads(run(common + ["--network", proof_network, "--env-file", str(target_env), LIVE_IMAGE, "-c", call.format(action="restore")]))
        verification = run(["docker", "exec", proof_container, "psql", "-U", "recovery", "-d", "proof", "-At", "-c", "SELECT version_num FROM alembic_version; SELECT status FROM onlyfans_restore.fence; SELECT count(*) FROM messages;"])
        fields = verification.splitlines()
        if restored.get("quarantined") is not True or restored.get("releasePermitted") is not False or fields[:2] != ["restore_quarantine_0088", "quarantined"]:
            raise RuntimeError("quarantined restore verification failed")
        archive = root / "checkpoint.twinof"
        proof = {"schemaVersion": "of-live-encrypted-backup-proof.v1", "sourceRevision": LIVE_REVISION, "sourceImage": LIVE_IMAGE, "backup": backup, "restore": restored, "archive": str(archive), "archiveSha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "archiveBytes": archive.stat().st_size, "privateKeyPath": str(key), "restoredMessageRows": int(fields[2]), "noSenderStarted": True, "productionRuntimeUnchanged": True}
        private_file(root / "proof.json", json.dumps(proof, indent=2) + "\n")
        print(json.dumps(proof, separators=(",", ":")))
    finally:
        if proof_created:
            run(["docker", "rm", "-f", "-v", proof_container])
        if network_created:
            run(["docker", "network", "rm", proof_network])
        for path in [source_env, target_env, db_env]:
            path.unlink(missing_ok=True)
        shutil.rmtree(clients)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"state": "failed", "reason": str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__, "noProductionRestoreAttempted": True}), file=sys.stderr)
        raise SystemExit(1)
