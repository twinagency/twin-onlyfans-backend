#!/usr/bin/env python3
"""Read-only smoke of the local stack for `scripts/check.sh live`.
Exit 0 GREEN, 1 RED (incl. partial DOWN), 2 INFRA (a check SKIPped; ALLOW_SKIP=1 accepts), 3 all probes DOWN.
Enforced: GET only; URLs and CHECK_DB_URL loopback or refused before any call; no proxy, no redirect; bearer read
per call from env var $CHECK_TOKEN_VAR (or file $CHECK_ENV_FILE), never printed; DB READ ONLY + statement_timeout.
Why no endpoint reaches onlyfans.com (read in this repo's code at F0):
  API /healthz, /onlyfans/api/v1/status  literal dicts after bearer/schema-header checks; no storage.
  API /onlyfans/api/v1/ready     db.schema.check_schema: local DB inspect.
  API /onlyfans/api/v1/accounts  SqlOnlyFansReadRepo, SQL only; onlyfans_read/ imports no HTTP client.
  :3001 /login                   twin-dashboard (separate repo), its sign-in page; checked there. Never /api/health.
  sign-in /health                creator-sign-in/server.mjs answers {ok:true} before auth; launches no browser.
"""
import json, os, re, sys, time, urllib.error, urllib.request
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
TIMEOUT = 10  # per call; check.sh caps the whole run at 180 s
KEY_TABLES = ("alembic_version", "users", "accounts", "of_creator_bindings", "messages")

def loopback(url):
    u = urlsplit(url)
    if u.scheme not in ("http", "https") or u.hostname not in LOOPBACK or "@" in u.netloc:
        raise SystemExit(f"live: refused non-loopback url {u.scheme}://{u.hostname}")
    return url

def db_loopback(url):
    """Allowlist: postgresql[+psycopg2], query keys host/port/sslmode only, one loopback host or absolute socket path."""
    from sqlalchemy.engine import make_url
    try:
        u = make_url(url)
        hosts = [h for h in (u.host, u.query.get("host")) if h]
        ok = (u.drivername in {"postgresql", "postgresql+psycopg2"} and set(u.query) <= {"host", "port", "sslmode"}
              and hosts and "=" not in (u.database or "")
              and all(isinstance(h, str) and "," not in h and (h in LOOPBACK or h.startswith("/")) for h in hosts))
    except Exception:  # noqa: BLE001
        ok = False
    if not ok:
        raise SystemExit("live: refused CHECK_DB_URL (need one loopback host or socket path)")
    return url

def token():
    name, f = os.environ.get("CHECK_TOKEN_VAR", "ONLYFANS_API_TOKEN"), os.environ.get("CHECK_ENV_FILE")
    t = (os.environ.get(name) or "").strip()
    for line in Path(f).read_text().splitlines() if not t and f and Path(f).is_file() else []:
        k, _, v = line.partition("=")
        t = v.strip().strip("'\"") if k.strip().removeprefix("export ").strip() == name else t
    if t and not re.fullmatch(r"[\x21-\x7e]+", t):  # a bad header value would echo it in a traceback
        raise SystemExit(f"live: bearer in ${name} has non-printable characters (value not shown)")
    return t

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None  # a 3xx is reported as-is, never followed

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)

def get(url, bearer=None):
    """GET only -> (status, body); status None = down. Nothing about the request is echoed."""
    req = urllib.request.Request(loopback(url), method="GET")
    try:
        if bearer:
            schema = re.search(r'^SCHEMA_VERSION = "([^"]+)"', (ROOT / "service/onlyfans_read/contract.py").read_text(), re.M)
            req.add_header("Authorization", "Bearer " + bearer), req.add_header("X-OnlyFans-Schema-Version", schema.group(1))
        with OPENER.open(req, timeout=TIMEOUT) as r:
            return r.status, r.read(1 << 20)
    except urllib.error.HTTPError as e:
        return e.code, e.read(1 << 20)
    except (urllib.error.URLError, OSError) as e:
        return None, type(getattr(e, "reason", e)).__name__
    except Exception as e:  # noqa: BLE001 - type name only, never the message
        return -1, type(e).__name__.encode()

def jd(body):
    try: return isinstance(j := json.loads(body), dict) and j or {}
    except (ValueError, TypeError): return {}

API, DASH, SIGNIN = ("CHECK_API", "http://127.0.0.1:8787"), ("CHECK_DASH", "http://127.0.0.1:3001"), \
    ("CHECK_SIGNIN", "http://127.0.0.1:8479")
CHECKS = [  # (label, base, path, needs bearer, expected shape)
    ("api health", API, "/healthz", False, lambda b: jd(b) == {"ok": True, "role": "api"}),
    ("api status", API, "/onlyfans/api/v1/status", True, lambda b: jd(b).get("ok") is True and "schemaVersion" in jd(b)),
    ("api ready", API, "/onlyfans/api/v1/ready", True, lambda b: jd(b).get("schemaReady") is True),
    ("api accounts", API, "/onlyfans/api/v1/accounts", True, lambda b: isinstance(jd(b).get("accounts"), list)),
    ("dashboard login", DASH, "/login", False, lambda b: b"<html" in b[:4000].lower()),
    ("sign-in health", SIGNIN, "/health", False, lambda b: jd(b) == {"ok": True}),
]

def db_checks(url):
    if not url:
        return print("SKIP db: CHECK_DB_URL unset") or "SKIP"
    for k in [k for k in os.environ if k.startswith("PG")]:  # PGHOSTADDR/PGSERVICE etc. could redirect libpq
        del os.environ[k]
    sys.path.insert(0, str(ROOT / "service"))
    from sqlalchemy import create_engine, inspect, text
    from db.schema import HEAD, REVISION_RANK
    eng = create_engine(url, connect_args={"connect_timeout": TIMEOUT,
                        "options": "-c default_transaction_read_only=on -c statement_timeout=10000"})
    try:
        c = eng.connect()
    except Exception as e:  # noqa: BLE001 - type only: a message could carry the URL
        return print(f"DOWN db: {type(e).__name__}") or "DOWN"
    with c:
        ro = c.execute(text("SHOW transaction_read_only")).scalar()
        revs = c.execute(text("SELECT version_num FROM alembic_version")).scalars().all() if ro == "on" else []
        missing = [t for t in KEY_TABLES if not inspect(c).has_table(t)]
    eng.dispose()
    v = "PASS" if ro == "on" and len(revs) == 1 and revs[0] in REVISION_RANK and not missing else "FAIL"
    note = "" if revs == [HEAD] else f" (worktree head {HEAD})"
    return print(f"{v} db: read_only={ro} revision={revs}{note} missing_tables={missing}") or v

def main():
    t0, seen, db_url = time.monotonic(), [], os.environ.get("CHECK_DB_URL", "")
    urls = [loopback(os.environ.get(env, default).rstrip("/") + path) for _, (env, default), path, _, _ in CHECKS]
    db_loopback(db_url) if db_url else None
    for url, (label, _, _, auth, shape) in zip(urls, CHECKS):
        bearer, s = (token() if auth else None), time.monotonic()
        if auth and not bearer:
            seen.append(print(f"SKIP {label}: no bearer in ${os.environ.get('CHECK_TOKEN_VAR', 'ONLYFANS_API_TOKEN')}") or "SKIP")
            continue
        status, body = get(url, bearer)
        seen.append(v := "DOWN" if status is None else "PASS" if status == 200 and shape(body) else "FAIL")
        print(f"{v} {label}: GET {url} " + (f"({body})" if v == "DOWN" else f"{status} {int((time.monotonic() - s) * 1000)}ms")
              + (f" keys={sorted(jd(body))[:8]}" if v == "FAIL" else ""))
    seen.append(db_checks(db_url))
    print(f"live: wall {time.monotonic() - t0:.1f}s")
    ran = {s for s in seen if s != "SKIP"}  # all that ran DOWN = stack down (3); one down while others answer = RED
    rc = 3 if ran == {"DOWN"} else 1 if ran & {"FAIL", "DOWN"} else 0
    return 2 if rc == 0 and "SKIP" in seen and os.environ.get("ALLOW_SKIP") != "1" else rc

if __name__ == "__main__":
    sys.exit(main())
