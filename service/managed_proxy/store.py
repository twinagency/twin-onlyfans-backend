"""Short PostgreSQL transactions; all provider HTTP stays outside these locks."""
import base64
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import os

from .provider import ProxyError, address_hash, COUNTRY_NAMES


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def owner_key(tenant, creator, handle):
    return hashlib.sha256(canonical([tenant, creator, handle.lower()]).encode()).hexdigest()


class Store:
    def __init__(self, url, key):
        from psycopg2.pool import ThreadedConnectionPool
        self.pool = ThreadedConnectionPool(1, 5, url, options="-c timezone=UTC -c statement_timeout=5000 -c lock_timeout=3000")
        self.key = bytes.fromhex(key)
        if len(self.key) != 32:
            raise ProxyError("configuration_unavailable")

    @contextmanager
    def transaction(self):
        from psycopg2.extras import RealDictCursor
        connection = self.pool.getconn()
        try:
            with connection:
                with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                    yield cursor
        finally:
            self.pool.putconn(connection)

    def close(self):
        self.pool.closeall()

    def initialize(self):
        with self.transaction() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(7811092)")
            cursor.execute("SELECT to_regnamespace('onlyfans_proxy') AS present")
            if cursor.fetchone()["present"] is None:
                cursor.execute(Path(__file__).with_name("schema.sql").read_text())
        self.check()

    def check(self):
        with self.transaction() as cursor:
            cursor.execute("SELECT address_hash,country,order_id,expires_at,sealed_url,state,owner_key,tenant_id,creator_id,handle,account_id,assigned_at,observed_at FROM onlyfans_proxy.inventory LIMIT 0")
            cursor.execute("SELECT id,address_hash,tenant_id,creator_id,handle,actor,action,recorded_at FROM onlyfans_proxy.audit LIMIT 0")

    def seal(self, url, digest):
        nonce = os.urandom(12)
        return base64.b64encode(nonce + AESGCM(self.key).encrypt(nonce, url.encode(), ("twin-managed-proxy.v1:" + digest).encode())).decode()

    def open(self, row):
        try:
            raw = base64.b64decode(row["sealed_url"], validate=True)
            return AESGCM(self.key).decrypt(raw[:12], raw[12:], ("twin-managed-proxy.v1:" + row["address_hash"]).encode()).decode()
        except Exception:
            raise ProxyError("saved_proxy_unavailable") from None

    def existing_connections(self, cursor):
        cursor.execute("""SELECT c.account_id,c.binding_id,c.revision,c.sealed_config,b.tenant_id,b.creator_id,b.handle
            FROM public.of_provider_connection c JOIN public.of_creator_bindings b ON b.id=c.binding_id
            WHERE b.revoked_at IS NULL""")
        result = {}
        for row in cursor.fetchall():
            try:
                raw = base64.b64decode(row["sealed_config"], validate=True)
                aad = canonical([row["tenant_id"],row["account_id"],row["binding_id"],row["revision"]]).encode()
                value = json.loads(AESGCM(self.key).decrypt(raw[:12],raw[12:],aad))
                proxy = value.get("proxyUrl")
                if not proxy:
                    continue
                host = urlsplit(proxy).hostname
                try:
                    ipaddress.ip_address(host)
                except ValueError:
                    # Without an observed address we cannot prove this stock IP is unused.
                    # Require an explicit inventory review instead of silently sharing it.
                    raise ProxyError("existing_proxy_check_failed")
                digest = address_hash(host)
                if digest in result and result[digest]["account_id"] != row["account_id"]:
                    result[digest] = {"account_id":"shared-existing", "tenant_id":None,"creator_id":None,"handle":None}
                else:
                    result[digest] = row
            except Exception:
                raise ProxyError("existing_proxy_check_failed") from None
        return result

    def sync(self, candidates):
        with self.transaction() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(7811093)")
            existing = self.existing_connections(cursor)
            seen = [candidate.address_hash for candidate in candidates]
            for candidate in candidates:
                cursor.execute("SELECT * FROM onlyfans_proxy.inventory WHERE address_hash=%s FOR UPDATE", (candidate.address_hash,))
                row = cursor.fetchone()
                reserved = existing.get(candidate.address_hash)
                if row is None:
                    cursor.execute("""INSERT INTO onlyfans_proxy.inventory(address_hash,country,order_id,expires_at,sealed_url,state,account_id,observed_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP)""", (
                        candidate.address_hash,candidate.country,candidate.order_id,candidate.expires_at,self.seal(candidate.url,candidate.address_hash),
                        "legacy_reserved" if reserved else "available", reserved["account_id"] if reserved else None))
                    continue
                state = row["state"]
                if state == "assigned":
                    matches = reserved and (reserved["tenant_id"],reserved["creator_id"],(reserved["handle"] or "").lower()) == (row["tenant_id"],row["creator_id"],row["handle"])
                    if self.open(row) != candidate.url or row["country"] != candidate.country or (reserved and not matches):
                        state = "quarantined"
                    if matches:
                        cursor.execute("UPDATE onlyfans_proxy.inventory SET account_id=%s WHERE address_hash=%s", (reserved["account_id"],candidate.address_hash))
                elif reserved:
                    state = "legacy_reserved" if row["owner_key"] is None else "quarantined"
                elif state == "retired" and row["owner_key"] is None:
                    state = "available"
                # A retired/quarantined assignment is never silently replaced or recycled.
                sealed = self.seal(candidate.url,candidate.address_hash) if state == "available" else row["sealed_url"]
                cursor.execute("UPDATE onlyfans_proxy.inventory SET expires_at=%s,observed_at=CURRENT_TIMESTAMP,state=%s,sealed_url=%s WHERE address_hash=%s", (candidate.expires_at,state,sealed,candidate.address_hash))
            cursor.execute("UPDATE onlyfans_proxy.inventory SET state='retired' WHERE NOT (address_hash=ANY(%s))", (seen,))

    def stock(self):
        with self.transaction() as cursor:
            cursor.execute("""SELECT country,count(*) AS available FROM onlyfans_proxy.inventory
                WHERE state='available' AND expires_at>CURRENT_TIMESTAMP+INTERVAL '1 hour'
                AND observed_at>CURRENT_TIMESTAMP-INTERVAL '5 minutes' GROUP BY country ORDER BY country""")
            return [{"code":r["country"],"name":COUNTRY_NAMES.get(r["country"],r["country"]),"available":r["available"]} for r in cursor.fetchall()]

    def assign(self, tenant, creator, handle, country, actor):
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,64}", tenant or "") or not re.fullmatch(r"[A-Za-z0-9._:-]{1,64}", creator or ""):
            raise ProxyError("invalid_assignment")
        if not re.fullmatch(r"[a-z0-9_.-]{1,80}",handle or "") or country not in COUNTRY_NAMES or not re.fullmatch(r"op-session:[a-f0-9]{64}",actor or ""):
            raise ProxyError("invalid_assignment")
        key = owner_key(tenant,creator,handle)
        with self.transaction() as cursor:
            lock = int.from_bytes(bytes.fromhex(key)[:8],"big",signed=True)
            cursor.execute("SELECT pg_advisory_xact_lock(%s)",(lock,))
            cursor.execute("SELECT * FROM onlyfans_proxy.inventory WHERE owner_key=%s FOR UPDATE",(key,))
            row = cursor.fetchone()
            if row:
                # A dashboard country is a preference; retries keep their fixed IP.
                if row["state"] != "assigned" or row["expires_at"] <= datetime.now(timezone.utc)+timedelta(hours=1) or row["observed_at"] <= datetime.now(timezone.utc)-timedelta(minutes=5):
                    raise ProxyError("assigned_proxy_needs_attention")
                return self.open(row)
            cursor.execute("""SELECT * FROM onlyfans_proxy.inventory WHERE state='available'
                AND expires_at>CURRENT_TIMESTAMP+INTERVAL '1 hour' AND observed_at>CURRENT_TIMESTAMP-INTERVAL '5 minutes'
                ORDER BY (country=%s) DESC,expires_at DESC,address_hash FOR UPDATE SKIP LOCKED LIMIT 1""",(country,))
            row = cursor.fetchone()
            if not row:
                raise ProxyError("proxy_stock_empty")
            cursor.execute("""UPDATE onlyfans_proxy.inventory SET state='assigned',owner_key=%s,tenant_id=%s,creator_id=%s,handle=%s,assigned_at=CURRENT_TIMESTAMP
                WHERE address_hash=%s""",(key,tenant,creator,handle,row["address_hash"]))
            cursor.execute("INSERT INTO onlyfans_proxy.audit(address_hash,tenant_id,creator_id,handle,actor,action) VALUES (%s,%s,%s,%s,%s,'assign')",(row["address_hash"],tenant,creator,handle,actor))
            return self.open(row)
