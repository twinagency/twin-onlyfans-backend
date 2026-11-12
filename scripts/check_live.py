#!/usr/bin/env python3
"""Read-only smoke of the running local stack, for `scripts/check.sh live`. Exit 0 GREEN, 1 RED, 2 INFRA (down).

Hard rules, enforced here: GET only; every URL and CHECK_DB_URL must be loopback, else refused before any call;
no proxies, no redirects followed; the bearer is read per call from env var $CHECK_TOKEN_VAR (or the file
$CHECK_ENV_FILE) and never printed; the DB is read in a READ ONLY transaction (SELECT / inspect only).

Why no endpoint reaches onlyfans.com (verified in the server code at F0):
  API /healthz                  onlyfans_app.health(): a literal dict.
  API /onlyfans/api/v1/status   onlyfans_read/api.status(): bearer + schema header only, touches no storage.
  API /onlyfans/api/v1/ready    onlyfans_app.ready(): bearer + db.schema.check_schema (local DB inspect).
  API /onlyfans/api/v1/accounts onlyfans_read list_accounts -> SqlOnlyFansReadRepo, SQL only (onlyfans_read/
                                imports no HTTP client).
  dash /login                   static sign-in page; middleware skips session renewal on login paths.
                                (NOT /api/health: it reads Supabase and calls Telegram.)
  sign-in /health               creator-sign-in/server.mjs answers {ok:true} before auth; launches no browser.
"""
import json, os, re, sys, time, urllib.error, urllib.request
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
TIMEOUT = 10  # per call: 6 calls + one DB connect stay far under the 3-min live budget
KEY_TABLES = ("alembic_version", "users", "accounts", "of_creator_bindings", "messages")


def loopback(url):
    u = urlsplit(url)
    if u.scheme not in ("http", "https") or u.hostname not in LOOPBACK:
        raise SystemExit(f"live: refused non-loopback url {u.scheme}://{u.hostname}")
    return url


def db_loopback(url):
    from sqlalchemy.engine import make_url
    u = make_url(url)
    host = u.host or str(u.query.get("host") or "")
    if u.get_backend_name() != "postgresql" or not (host in LOOPBACK or host.startswith("/") or not host):
        raise SystemExit("live: refused non-loopback CHECK_DB_URL")
    return url


def token():
    name = os.environ.get("CHECK_TOKEN_VAR", "ONLYFANS_API_TOKEN")
    if (os.environ.get(name) or "").strip():
        return os.environ[name].strip()
    f = os.environ.get("CHECK_ENV_FILE")
    if f and Path(f).is_file():
        for line in Path(f).read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip().removeprefix("export ").strip() == name:
                return v.strip().strip("'\"")
    return ""


def schema_version():
    text = (ROOT / "service/onlyfans_read/contract.py").read_text()
    return re.search(r'^SCHEMA_VERSION = "([^"]+)"', text, re.M).group(1)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # a 3xx is reported as-is, never followed
        return None


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def get(url, bearer=None):
    """GET only. Returns (status|None, body|reason). Headers are never logged."""
    req = urllib.request.Request(loopback(url), method="GET")
    if bearer:
        req.add_header("Authorization", "Bearer " + bearer)
        req.add_header("X-OnlyFans-Schema-Version", schema_version())
    try:
        with OPENER.open(req, timeout=TIMEOUT) as r:
            return r.status, r.read(1 << 20)
    except urllib.error.HTTPError as e:
        return e.code, e.read(1 << 20)
    except (urllib.error.URLError, OSError) as e:
        return None, type(getattr(e, "reason", e)).__name__


def jd(body):
    try:
        j = json.loads(body)
    except (ValueError, TypeError):
        return {}
    return j if isinstance(j, dict) else {}


API, DASH, SIGNIN = (("CHECK_API", "http://127.0.0.1:8787"), ("CHECK_DASH", "http://127.0.0.1:3001"),
                     ("CHECK_SIGNIN", "http://127.0.0.1:8479"))
CHECKS = [  # (label, base, path, needs bearer, expected shape)
    ("api health", API, "/healthz", False, lambda b: jd(b) == {"ok": True, "role": "api"}),
    ("api status", API, "/onlyfans/api/v1/status", True,
     lambda b: jd(b).get("ok") is True and isinstance(jd(b).get("schemaVersion"), str)),
    ("api ready", API, "/onlyfans/api/v1/ready", True, lambda b: jd(b).get("schemaReady") is True),
    ("api accounts", API, "/onlyfans/api/v1/accounts", True, lambda b: isinstance(jd(b).get("accounts"), list)),
    ("dashboard login", DASH, "/login", False, lambda b: b"<html" in b[:4000].lower()),
    ("sign-in health", SIGNIN, "/health", False, lambda b: jd(b) == {"ok": True}),
]


def db_checks(url):
    if not url:
        print("SKIP db: CHECK_DB_URL unset")
        return 0, 0
    sys.path.insert(0, str(ROOT / "service"))
    from sqlalchemy import create_engine, inspect, text
    from db.schema import HEAD, REVISION_RANK
    eng = create_engine(url, connect_args={"options": "-c default_transaction_read_only=on", "connect_timeout": TIMEOUT})
    try:
        try:
            c = eng.connect()
        except Exception as e:  # noqa: BLE001 - type only: a message could carry the URL
            print(f"DOWN db: {type(e).__name__}")
            return 0, 1
        with c:
            ro = c.execute(text("SHOW transaction_read_only")).scalar()
            revs = c.execute(text("SELECT version_num FROM alembic_version")).scalars().all() if ro == "on" else []
            missing = [t for t in KEY_TABLES if not inspect(c).has_table(t)]
    finally:
        eng.dispose()
    ok = ro == "on" and len(revs) == 1 and revs[0] in REVISION_RANK and not missing
    note = "" if revs == [HEAD] else f" (worktree head {HEAD})"
    print(f"{'PASS' if ok else 'FAIL'} db: read_only={ro} revision={revs}{note} missing_tables={missing}")
    return (0 if ok else 1), 0


def main():
    t0 = time.monotonic()
    urls = [loopback(os.environ.get(env, default).rstrip("/") + path) for _, (env, default), path, _, _ in CHECKS]
    db_url = os.environ.get("CHECK_DB_URL", "")
    if db_url:
        db_loopback(db_url)
    red = down = 0
    for url, (label, _, _, auth, shape) in zip(urls, CHECKS):
        bearer = token() if auth else None
        if auth and not bearer:
            print(f"SKIP {label}: no bearer in ${os.environ.get('CHECK_TOKEN_VAR', 'ONLYFANS_API_TOKEN')}")
            continue
        s = time.monotonic()
        status, body = get(url, bearer)
        ms = int((time.monotonic() - s) * 1000)
        if status is None:
            down += 1
            print(f"DOWN {label}: GET {url} ({body})")
        elif status == 200 and shape(body):
            print(f"PASS {label}: GET {url} 200 {ms}ms")
        else:
            red += 1
            print(f"FAIL {label}: GET {url} {status} {ms}ms keys={sorted(jd(body))[:8]}")
    r, d = db_checks(db_url)
    red, down = red + r, down + d
    print(f"live: wall {time.monotonic() - t0:.1f}s")
    return 1 if red else 2 if down else 0


if __name__ == "__main__":
    sys.exit(main())
