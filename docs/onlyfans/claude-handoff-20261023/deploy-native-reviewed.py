"""Prepare/review locally; execute only with the accepted immutable digest.

Native API/worker/migration only. Preserves the observed resolved compose and
all secrets on the host. Never enables an account, delivery policy or flag.
No automatic downgrade or old-binary restart after a failed migration.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

OLD_IMAGE = "sha256:7e23784ff8cd3bcbd43c13cbcbe9b2eb8117fb0010285ddf1926d232596edc2f"
OLD_SHA = "107af2a4a8c2a17fc9a673fea60a1dff21d381bf"
BACKEND_SHA = "14bc99ebde648c5d77be47d9bd0bf516db9b1959"
COMPOSE = Path("/etc/twin-onlyfans/media-agency-release-compose.json")
COMPOSE_HASH = "4341f4000e98debad2c44b960085d26e329ddcf0844940008bb7e0eafc2a3869"
BACKUP = Path("/var/backups/twin-onlyfans/final-0088-20261023T122645Z-9739cd/proof.json")
BACKUP_HASH = "4875ff84a98438e932c0fa472d4a8cc19be2bea862ef991a1781c6a99abaa43e"
ACCOUNT = "of-176774528"
NEW_FLAGS = ["ONLYFANS_OFFER_IMPORT_ENABLED", "ONLYFANS_CONTENT_SYNC_ENABLED",
             "ONLYFANS_HISTORY_RECOVERY_ENABLED", "ONLYFANS_HISTORY_MEMORY_ENABLED",
             "ONLYFANS_ENGAGEMENT_ENABLED"]


def call(args, *, data=None, timeout=180, stage="command"):
    result = subprocess.run(args, input=data, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(stage + " failed; no automatic rollback performed")
    return result.stdout.strip()


def inspect(container):
    return json.loads(call(["docker", "inspect", container], stage="container inspection"))[0]


def write_private(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


SNAPSHOT = r'''
import os,json,hashlib
from sqlalchemy import create_engine,text
e=create_engine(os.environ["DATABASE_URL"])
with e.connect() as c:
 c.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
 c.execute(text("SET LOCAL statement_timeout='20s'"))
 def rows(sql): return [dict(x) for x in c.execute(text(sql)).mappings()]
 authority={
  "binding":rows("select id,tenant_id,creator_id,account_id,handle,channel,revoked_at from of_creator_bindings order by id"),
  "runtime":rows("select account_id,binding_id,enabled from of_runtime order by account_id"),
  "policy":rows("select account_id,enabled from of_delivery_policy order by account_id"),
  "conversation":rows("select account_id,mode,count(*) n from of_conversation_state group by account_id,mode order by account_id,mode"),
 }
 out={"schema":c.execute(text("select version_num from alembic_version")).scalar(),
  "authorityHash":hashlib.sha256(json.dumps(authority,sort_keys=True,default=str).encode()).hexdigest(),
  "bindings":authority["binding"],"runtimes":authority["runtime"],"policies":authority["policy"],
  "intents":c.execute(text("select count(*) from of_text_intent")).scalar(),
  "vaultRows":c.execute(text("select count(*) from vault_items where account_id='of-176774528'")).scalar(),
  "messages":c.execute(text("select count(*) from messages where account_id='of-176774528'")).scalar(),
  "catalogRows":c.execute(text("select count(*) from catalog_items where account_id='of-176774528'")).scalar()}
 if out["schema"]=="0091_launch_controls":
  out["workerHealth"]=rows("select account_id,observed_at,last_error from of_launch_worker_health order by account_id")
 c.rollback()
print(json.dumps(out,default=str))
'''


def snapshot(compose=None):
    command = (["docker", "exec", "-i", "twin-onlyfans-api-1", "python", "-"] if compose is None
               else compose + ["--profile", "ops", "run", "--rm", "--no-deps", "-T", "--entrypoint", "python", "migrate", "-"])
    return json.loads(call(command, data=SNAPSHOT, stage="read-only database snapshot"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--execute-reviewed-native-upgrade", action="store_true")
    args = parser.parse_args()
    if not args.execute_reviewed_native_upgrade or os.geteuid() != 0:
        raise RuntimeError("explicit reviewed native upgrade and root required")
    if not re.fullmatch(r"323149985112\.dkr\.ecr\.ap-southeast-2\.amazonaws\.com/twin-onlyfans@sha256:[0-9a-f]{64}", args.image):
        raise RuntimeError("accepted same-account immutable native digest required")
    lock = open("/var/lock/twin-onlyfans-native-release.lock", "a")
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    raw = COMPOSE.read_bytes()
    if hashlib.sha256(raw).hexdigest() != COMPOSE_HASH:
        raise RuntimeError("observed compose changed; review required")
    original = json.loads(raw)
    proof = json.loads(BACKUP.read_text())
    archive = Path(proof["archive"])
    if (proof.get("sourceImage") != OLD_IMAGE or proof.get("sourceRevision") != OLD_SHA
        or proof.get("backup", {}).get("schema") != "0088_native_vault_media"
        or proof.get("restore", {}).get("quarantined") is not True
        or proof.get("restore", {}).get("releasePermitted") is not False
        or proof.get("archiveSha256") != BACKUP_HASH
        or hashlib.sha256(archive.read_bytes()).hexdigest() != BACKUP_HASH):
        raise RuntimeError("verified encrypted backup proof is unavailable")
    for name in ["twin-onlyfans-api-1", "twin-onlyfans-worker-1"]:
        item = inspect(name)
        if (item["Image"] != OLD_IMAGE or item["Config"]["Labels"].get("org.opencontainers.image.revision") != OLD_SHA
            or item["State"]["Running"] is not True):
            raise RuntimeError("native runtime changed; review required")
    before = snapshot()
    if (before["schema"] != "0088_native_vault_media" or before["intents"] != 0
        or len(before["bindings"]) != 1 or before["bindings"][0]["account_id"] != ACCOUNT
        or any(row["enabled"] for row in before["runtimes"] + before["policies"])
        or before["vaultRows"] != 1064):
        raise RuntimeError("observed account scope/authority changed; review required")
    pinned = copy.deepcopy(original)
    for service in ["api", "worker", "migrate"]:
        if "build" in pinned["services"][service]:
            raise RuntimeError("unexpected build-based production compose")
        pinned["services"][service]["image"] = args.image
    for service in ["api", "worker"]:
        env = pinned["services"][service]["environment"]
        if any(str(env.get(key, "0")).strip().lower() not in {"", "0", "false"} for key in NEW_FLAGS):
            raise RuntimeError("launch feature flag already changed; review required")
    call(["docker", "pull", args.image], timeout=300, stage="immutable image pull")
    image = json.loads(call(["docker", "image", "inspect", args.image], stage="image inspection"))[0]
    if (image["Config"]["Labels"].get("org.opencontainers.image.revision") != BACKEND_SHA
        or image["Architecture"] != "amd64" or image["Os"] != "linux"
        or args.image not in image.get("RepoDigests", [])):
        raise RuntimeError("pulled image identity does not match accepted source")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    release = Path("/etc/twin-onlyfans") / ("native-release-" + stamp)
    release.mkdir(mode=0o700)
    write_private(release / "previous-compose.json", raw.decode())
    destination = release / "compose.json"
    write_private(destination, json.dumps(pinned, indent=2) + "\n")
    compose = ["docker", "compose", "--project-name", "twin-onlyfans", "--file", str(destination)]
    # No secrets leave the host: validate without printing the resolved config.
    call(compose + ["config", "--quiet"], stage="pinned compose validation")
    write_private(release / "preflight.json", json.dumps(before, indent=2) + "\n")
    original_ids = {name: inspect(name)["Id"] for name in ["twin-onlyfans-database-1", "twin-onlyfans-creator-sign-in-1", "next-app", "flirt-dashboard", "flirt-bot"]}
    call(compose + ["stop", "--timeout", "60", "api", "worker"], timeout=150, stage="native stop")
    if any(inspect(name)["State"]["Running"] for name in ["twin-onlyfans-api-1", "twin-onlyfans-worker-1"]):
        raise RuntimeError("native processes did not stop")
    # The old worker may complete read-only history ingestion while the image
    # downloads. Freeze the preservation baseline only after its clean stop.
    stopped = snapshot(compose)
    if (stopped["schema"] != before["schema"] or stopped["bindings"] != before["bindings"]
        or stopped["runtimes"] != before["runtimes"] or stopped["policies"] != before["policies"]
        or stopped["intents"] != 0 or stopped["vaultRows"] != before["vaultRows"]
        or stopped["catalogRows"] != before["catalogRows"] or stopped["messages"] < before["messages"]):
        raise RuntimeError("authority or content changed before maintenance; owner inspection required")
    before = stopped
    write_private(release / "before.json", json.dumps(before, indent=2) + "\n")
    call(compose + ["--profile", "ops", "run", "--rm", "--no-deps", "migrate", "upgrade", "--url-env", "ONLYFANS_MIGRATION_URL"], timeout=600, stage="schema upgrade")
    call(compose + ["--profile", "ops", "run", "--rm", "--no-deps", "migrate", "check", "--url-env", "ONLYFANS_MIGRATION_URL"], timeout=120, stage="schema structural check")
    # Host restart must use the compatible pinned image after the schema bump.
    # The previous private compose remains in the release directory as evidence.
    canonical_temp = COMPOSE.with_name(COMPOSE.name + ".new-" + stamp)
    write_private(canonical_temp, json.dumps(pinned, indent=2) + "\n")
    os.replace(canonical_temp, COMPOSE)
    directory_descriptor = os.open(COMPOSE.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
    call(compose + ["up", "-d", "--no-build", "--no-deps", "api"], stage="native API startup")
    ready_script = "import os,urllib.request,json; r=urllib.request.Request('http://127.0.0.1:8787/onlyfans/api/v1/ready',headers={'Authorization':'Bearer '+os.environ['ONLYFANS_API_TOKEN'],'X-OnlyFans-Schema-Version':'of-preview.v2'}); j=json.load(urllib.request.urlopen(r,timeout=5)); print(json.dumps({'ok':j.get('ok')}))"
    ready = False
    for _ in range(30):
        try:
            value = json.loads(call(["docker", "exec", "twin-onlyfans-api-1", "python", "-c", ready_script], timeout=10, stage="API readiness"))
            ready = value.get("ok") is True
        except RuntimeError:
            pass
        if ready:
            break
        time.sleep(2)
    if not ready:
        raise RuntimeError("new API not ready; worker remains stopped")
    after_api = snapshot()
    if (after_api["schema"] != "0091_launch_controls" or after_api["authorityHash"] != before["authorityHash"]
        or after_api["vaultRows"] != before["vaultRows"] or after_api["intents"] != 0
        or after_api["catalogRows"] != before["catalogRows"] or after_api["messages"] < before["messages"]):
        raise RuntimeError("post-upgrade preservation check failed; worker remains stopped")
    worker_started_at = datetime.now(timezone.utc)
    call(compose + ["up", "-d", "--no-build", "--no-deps", "worker"], stage="native worker startup")
    for name in ["twin-onlyfans-api-1", "twin-onlyfans-worker-1"]:
        item = inspect(name)
        if item["Image"] != image["Id"] or item["State"]["Running"] is not True:
            raise RuntimeError("new runtime identity or running state mismatch")
    for name, identity in original_ids.items():
        if inspect(name)["Id"] != identity:
            raise RuntimeError("unrelated container changed during coordinated deployment")
    health = None
    fresh_health = False
    for _ in range(30):
        current = snapshot()
        health = next((row for row in current.get("workerHealth", []) if row["account_id"] == ACCOUNT), None)
        if health and health["last_error"] is None:
            observed = datetime.fromisoformat(health["observed_at"].replace("Z", "+00:00"))
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=timezone.utc)
            fresh_health = observed >= worker_started_at
            if fresh_health:
                break
        time.sleep(2)
    if not fresh_health:
        raise RuntimeError("fresh error-free worker heartbeat was not observed; deployment not accepted")
    result = {"schemaVersion": "of-native-deployment-proof.v1", "backendSha": BACKEND_SHA,
              "imageDigest": args.image, "imageId": image["Id"], "compose": str(destination),
              "backupProof": str(BACKUP), "schema": "0091_launch_controls", "apiReady": True,
              "workerHealth": health, "accountAuthorityUnchanged": True, "vaultRows": after_api["vaultRows"],
              "messagesAtUpgrade": after_api["messages"], "autonomousHandlingEnabled": False,
              "launchFlagsEnabled": False, "dashboardDeploymentIncluded": False,
              "completedAt": datetime.now(timezone.utc).isoformat()}
    write_private(release / "proof.json", json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, separators=(",", ":")))
    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    lock.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"state": "failed", "reason": str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__,
                          "noAutomaticRollback": True, "requiresOwnerInspection": True}), file=sys.stderr)
        raise SystemExit(1)
