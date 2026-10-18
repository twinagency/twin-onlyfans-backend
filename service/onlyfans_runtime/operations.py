"""Encrypted public-schema snapshots and permanently quarantined restores.

No runtime or provider is started. Restore accepts only an empty target and
commits a fence outside the archived schema before pg_restore. Existing startup
checks refuse that fence, including if this process dies halfway through.
There is deliberately no release/clear-fence command in this module.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import tempfile
import threading
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from db.schema import HEAD, check_schema

MAGIC = b"TWINOF-BACKUP\x01"
CHUNK = 65536
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_OPERATION_SECONDS = 120
FENCE_SCHEMA = "onlyfans_restore"
QUARANTINE_REVISION = "restore_quarantine_" + HEAD.split("_", 1)[0]


class RecoveryRefused(ValueError):
    """Safe operational error; no credentials, SQL or subprocess output."""


def _key(raw):
    try:
        value = base64.b64decode(raw, altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise RecoveryRefused("A base64 encoded 32-byte backup key is required") from exc
    if len(value) != 32:
        raise RecoveryRefused("A base64 encoded 32-byte backup key is required")
    return value


def _url(raw):
    try:
        url = make_url(raw)
    except Exception as exc:
        raise RecoveryRefused("An explicit PostgreSQL connection is required") from exc
    if (url.get_backend_name() != "postgresql" or not url.host or not url.database or not url.username
            or re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_$-]{0,62}", url.database) is None
            or set(url.query) - {"sslmode"}
            or url.query.get("sslmode", "prefer") not in {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}):
        raise RecoveryRefused("Unsupported PostgreSQL connection settings")
    return url


def _engine(raw):
    return create_engine(_url(raw).set(drivername="postgresql+psycopg2"), pool_size=1,
                         max_overflow=0, pool_timeout=3,
                         connect_args={"options": "-c timezone=UTC -c statement_timeout=10000 -c lock_timeout=3000"})


def _pg_env(raw):
    url = _url(raw)
    # Do not inherit service credentials, provider cookies, PGPASSFILE or
    # arbitrary PGOPTIONS. Connection secrets never appear in argv/logs.
    env = {k: v for k, v in os.environ.items() if k in {
        "PATH", "SystemRoot", "WINDIR", "LD_LIBRARY_PATH", "TMPDIR", "TEMP", "TMP",
    }}
    env.update(PGHOST=url.host, PGPORT=str(url.port or 5432), PGDATABASE=url.database,
               PGUSER=url.username, PGPASSWORD=url.password or "",
               PGPASSFILE=os.devnull, PGCONNECT_TIMEOUT="5",
               PGSSLMODE=url.query.get("sslmode", "prefer"), PGAPPNAME="twin-onlyfans-recovery")
    return env


@contextmanager
def _client(binary, args, url, *, pg_bin, timeout, stdin=False):
    if type(timeout) not in {int, float} or not 0 < timeout <= MAX_OPERATION_SECONDS:
        raise RecoveryRefused("Invalid operation deadline")
    executable = str(Path(pg_bin) / binary) if pg_bin is not None else binary
    process = subprocess.Popen([executable, *args], shell=False, env=_pg_env(url),
                               stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL if stdin else subprocess.PIPE,
                               stderr=subprocess.DEVNULL)
    expired = threading.Event()

    def kill():
        expired.set()
        try:
            process.kill()
        except ProcessLookupError:
            pass

    timer = threading.Timer(timeout, kill)
    timer.daemon = True
    timer.start()
    try:
        yield process
        if stdin:
            process.stdin.close()
        code = process.wait(timeout=timeout)
        if code or expired.is_set():
            raise RecoveryRefused("PostgreSQL recovery command failed or exceeded its deadline")
    except (OSError, subprocess.SubprocessError) as exc:
        raise RecoveryRefused("PostgreSQL recovery command did not complete") from exc
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()


def backup(url, destination, *, key, pg_bin=None, timeout=MAX_OPERATION_SECONDS):
    """One consistent pg_dump snapshot; encrypted before any archive byte is saved."""
    secret = _key(key)
    target = Path(destination)
    if target.exists() or target.is_symlink():
        raise RecoveryRefused("Backup destination already exists")
    engine = _engine(url)
    try:
        with engine.connect() as c:
            check_schema(c)
            stamp = c.scalar(text("SELECT clock_timestamp()"))
        metadata = dict(format="twin-onlyfans-backup.v1", schema=HEAD, scope="public",
                        backupId=uuid.uuid4().hex, capturedAt=stamp.isoformat(), releaseVersionOmitted=True)
        raw = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode()
        nonce = os.urandom(12)
        aad = MAGIC + struct.pack("!I", len(raw)) + raw + nonce
        encryptor = Cipher(algorithms.AES(secret), modes.GCM(nonce)).encryptor()
        encryptor.authenticate_additional_data(aad)
        # No plaintext archive is written, including temporary/error paths.
        with tempfile.TemporaryDirectory(prefix=".of-backup-", dir=target.parent) as tmp:
            staged = Path(tmp) / "encrypted"
            with staged.open("xb") as out:
                os.chmod(staged, 0o600)
                out.write(aad)
                size = 0
                # An old binary also refuses the restored DB: never restore a
                # ready Alembic row, even in the interval before final sealing.
                with _client("pg_dump", ["--format=custom", "--schema=public", "--exclude-table-data=public.alembic_version", "--no-owner", "--no-privileges"],
                             url, pg_bin=pg_bin, timeout=timeout) as process:
                    while chunk := process.stdout.read(CHUNK):
                        size += len(chunk)
                        if size > MAX_ARCHIVE_BYTES:
                            raise RecoveryRefused("Backup exceeds the bounded archive size")
                        out.write(encryptor.update(chunk))
                out.write(encryptor.finalize())
                out.write(encryptor.tag)
                out.flush()
                os.fsync(out.fileno())
            # Atomic no-replace publication; a concurrent backup cannot overwrite.
            os.link(staged, target)
            if os.name == "posix":
                directory = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        return metadata
    except FileExistsError as exc:
        raise RecoveryRefused("Backup destination already exists") from exc
    finally:
        engine.dispose()


def _header(file):
    prefix = file.read(len(MAGIC) + 4)
    if len(prefix) != len(MAGIC) + 4 or not prefix.startswith(MAGIC):
        raise RecoveryRefused("Unsupported encrypted backup")
    length = struct.unpack("!I", prefix[-4:])[0]
    if not 1 <= length <= 4096:
        raise RecoveryRefused("Invalid backup header")
    raw, nonce = file.read(length), file.read(12)
    if len(raw) != length or len(nonce) != 12:
        raise RecoveryRefused("Truncated backup")
    try:
        metadata = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise RecoveryRefused("Invalid backup metadata") from exc
    if (type(metadata) is not dict or set(metadata) != {"format", "schema", "scope", "backupId", "capturedAt", "releaseVersionOmitted"}
            or metadata.get("format") != "twin-onlyfans-backup.v1" or metadata.get("schema") != HEAD
            or metadata.get("releaseVersionOmitted") is not True
            or metadata.get("scope") != "public" or type(metadata.get("backupId")) is not str
            or len(metadata["backupId"]) != 32 or any(v not in "0123456789abcdef" for v in metadata["backupId"])):
        raise RecoveryRefused("Backup does not match this release")
    try:
        stamp = datetime.fromisoformat(metadata["capturedAt"])
        if stamp.tzinfo is None:
            raise ValueError()
    except (TypeError, ValueError) as exc:
        raise RecoveryRefused("Invalid backup capture time") from exc
    return metadata, prefix + raw + nonce, nonce


def _decrypt(file, secret, *, sink=None):
    file.seek(0)
    metadata, aad, nonce = _header(file)
    start = file.tell()
    file.seek(0, os.SEEK_END)
    size = file.tell() - start - 16
    if not 0 < size <= MAX_ARCHIVE_BYTES:
        raise RecoveryRefused("Invalid bounded backup size")
    file.seek(-16, os.SEEK_END)
    decryptor = Cipher(algorithms.AES(secret), modes.GCM(nonce, file.read(16))).decryptor()
    decryptor.authenticate_additional_data(aad)
    file.seek(start)
    remaining = size
    try:
        while remaining:
            chunk = file.read(min(CHUNK, remaining))
            if not chunk:
                raise RecoveryRefused("Truncated backup")
            remaining -= len(chunk)
            value = decryptor.update(chunk)
            if sink is not None:
                sink.write(value)
        final = decryptor.finalize()
        if sink is not None:
            sink.write(final)
    except InvalidTag as exc:
        raise RecoveryRefused("Backup authentication failed") from exc
    return metadata


def restore(url, archive, *, key, pg_bin=None, timeout=MAX_OPERATION_SECONDS):
    """Authenticate first, restore only empty storage, retain an uncancellable fence."""
    secret = _key(key)
    engine = _engine(url)
    try:
        # Copy encrypted bytes to a private snapshot. The authenticated first pass
        # and subsequent decrypt see identical bytes even if the input is replaced.
        with tempfile.TemporaryFile() as snapshot:
            fd = os.open(archive, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(fd, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise RecoveryRefused("A regular encrypted backup file is required")
                size = 0
                while chunk := source.read(CHUNK):
                    size += len(chunk)
                    if size > MAX_ARCHIVE_BYTES + 8192:
                        raise RecoveryRefused("Backup exceeds the bounded archive size")
                    snapshot.write(chunk)
            metadata = _decrypt(snapshot, secret)  # no unverified byte reaches pg_restore
            with engine.begin() as c:
                c.execute(text("SELECT pg_advisory_xact_lock(7811070)"))
                schemas = set(inspect(c).get_schema_names()) - {"public", "information_schema"}
                schemas = {v for v in schemas if not v.startswith("pg_")}
                occupied = c.scalar(text("""SELECT EXISTS (
                    SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public'
                    UNION ALL
                    SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public'
                    UNION ALL
                    SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace WHERE n.nspname='public'
                )"""))
                if occupied or schemas:
                    raise RecoveryRefused("Restore requires a new empty target database")
                # A --schema=public archive includes CREATE SCHEMA public.
                # Only this already-proven empty schema is removed; existing
                # user objects/other schemas make the operation refuse above.
                c.exec_driver_sql("DROP SCHEMA IF EXISTS public")
                c.exec_driver_sql("CREATE SCHEMA onlyfans_restore")
                c.exec_driver_sql("CREATE TABLE onlyfans_restore.fence (backup_id text PRIMARY KEY, status text NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp())")
                c.execute(text("INSERT INTO onlyfans_restore.fence(backup_id,status) VALUES(:backup,'in_progress')"), {"backup": metadata["backupId"]})
            # Any failure or process death after this point leaves the fence.
            with _client("pg_restore", ["--format=custom", "--no-owner", "--no-privileges", "--exit-on-error", "--single-transaction", "--dbname", _url(url).database],
                         url, pg_bin=pg_bin, timeout=timeout, stdin=True) as process:
                _decrypt(snapshot, secret, sink=process.stdin)
            with engine.begin() as c:
                if c.scalar(text("SELECT count(*) FROM alembic_version")) != 0:
                    raise RecoveryRefused("Archive unexpectedly restored an active release version")
                c.execute(text("INSERT INTO alembic_version(version_num) VALUES(:revision)"), {"revision": QUARANTINE_REVISION})
                c.execute(text("UPDATE onlyfans_restore.fence SET status='quarantined'"))
        return {**metadata, "quarantined": True, "releasePermitted": False}
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("backup", "restore"))
    parser.add_argument("file", type=Path)
    parser.add_argument("--url-env", default="ONLYFANS_RECOVERY_URL")
    parser.add_argument("--key-env", default="ONLYFANS_BACKUP_KEY")
    parser.add_argument("--pg-bin", type=Path)
    args = parser.parse_args()
    try:
        result = (backup if args.action == "backup" else restore)(
            os.environ.get(args.url_env, ""), args.file,
            key=os.environ.get(args.key_env, ""), pg_bin=args.pg_bin)
    except Exception:
        parser.exit(1, "Recovery operation refused or failed. No sender was started; a restore target may remain quarantined.\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
