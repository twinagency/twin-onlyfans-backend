"""Run the actual dashboard/read service with a disposable local PG database.

From the backend repository:
  ONLYFANS_TEST_PG_URL=postgresql://...@127.0.0.1:5432/postgres \
    python service/tests/onlyfans_demo.py --dashboard ../twin-dashboard
"""
from contextlib import ExitStack
import argparse
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import uuid
from http.server import ThreadingHTTPServer

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

def isolated_environment():
    """Keep runtime paths; never inherit provider, DB or dashboard credentials."""
    names=("PATH","SystemRoot","COMSPEC","PATHEXT","WINDIR","LD_LIBRARY_PATH","LANG","LC_ALL",
        "LC_CTYPE","TMPDIR","TEMP","TMP","HOME","USERPROFILE","CODEX_PRIMARY_RUNTIME_NODE",
        "CODEX_PRIMARY_RUNTIME_NODE_MODULES","PREVIEW_CHROME","FONTCONFIG_PATH","ONLYFANS_TEST_CHROMIUM_MODULE")
    return {name:os.environ[name] for name in names if name in os.environ}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dashboard", type=Path, required=True)
    parser.add_argument("--port", type=int, default=3018)
    parser.add_argument("--smoke", action="store_true", help="Run the full synthetic browser flow, shut down and remove the demo database")
    args = parser.parse_args()
    dashboard = args.dashboard.resolve()
    if not 0 < args.port < 65536:parser.error("Choose a valid local port")
    if not (dashboard/"node_modules/next/dist/bin/next").is_file():parser.error("Install dashboard dependencies first")
    if list((dashboard/"preview/onlyfans-integration").glob(".env*")):parser.error("The isolated harness must not load environment files")
    raw = os.environ.get("ONLYFANS_TEST_PG_URL", "")
    if not raw or make_url(raw).host not in {"localhost", "127.0.0.1", "::1"}:
        parser.error("Use an explicit disposable local ONLYFANS_TEST_PG_URL")
    # Select a dedicated role before project imports, so service/.env and old
    # provider clients cannot become a source of credentials or state. Every
    # Store below receives the newly-created owned database URL explicitly.
    os.environ["ONLYFANS_PROCESS_ROLE"]="worker"
    os.environ["DATABASE_URL"]="postgresql://synthetic@127.0.0.1:1/offline_demo_unconnected"
    from test_onlyfans_foundation_pg import bootstrap
    from test_onlyfans_http_pg import IdentityStub, port, process, wait_http, synthetic_scenario
    admin = create_engine(raw, isolation_level="AUTOCOMMIT")
    name = "of_demo_" + uuid.uuid4().hex
    with admin.connect() as c: c.exec_driver_sql(f'CREATE DATABASE "{name}" TEMPLATE template0 ENCODING \'UTF8\'')
    url = admin.url.set(database=name).render_as_string(hide_password=False)
    engine = create_engine(url)
    store = other = None
    try:
        with engine.begin() as c: bootstrap(c)
        with pytest.MonkeyPatch.context() as scoped:
            store,other,*_=synthetic_scenario((url,engine),scoped)
            from onlyfans_worker_fixture import run_worker_scenario
            run_worker_scenario((url,engine),scoped)
        api_port = port()
        login = secrets.token_urlsafe(24)
        env = {**isolated_environment(), "ONLYFANS_PROCESS_ROLE":"api", "DATABASE_URL":url,
               "ONLYFANS_API_TOKEN":secrets.token_urlsafe(32), "ONLYFANS_API_TENANT":"tenant-a",
               "ONLYFANS_CONTROL_TOKEN":secrets.token_urlsafe(32), "ONLYFANS_CONTROL_TENANT":"tenant-a",
               "ONLYFANS_CONFIGURATION_TOKEN":secrets.token_urlsafe(32), "ONLYFANS_CONFIGURATION_TENANT":"tenant-a", "ONLYFANS_TEST_CONFIGURATION":"1", "ONLYFANS_TEST_WORKER":"1",
               "ONLYFANS_API_BASE_URL":f"http://127.0.0.1:{api_port}",
               "DASHBOARD_SESSION_SECRET":secrets.token_urlsafe(32), "SYNTHETIC_DEMO_LOGIN_TOKEN":login,
               "SUPABASE_SERVICE_KEY":"synthetic-service-key", "NEXT_TELEMETRY_DISABLED":"1", "TZ":"UTC"}
        with tempfile.TemporaryDirectory(prefix="of-demo-logs-") as logs, ExitStack() as stack:
            identity = ThreadingHTTPServer(("127.0.0.1",0), IdentityStub)
            threading.Thread(target=identity.serve_forever, daemon=True).start()
            stack.callback(identity.server_close); stack.callback(identity.shutdown)
            env["SUPABASE_URL"] = f"http://127.0.0.1:{identity.server_port}"
            backend = Path(__file__).resolve().parents[2]
            api = stack.enter_context(process([sys.executable,"-m","uvicorn","onlyfans_app:app","--app-dir","service","--host","127.0.0.1","--port",str(api_port)], cwd=backend,env=env,log=Path(logs)/"api.log"))
            wait_http(env["ONLYFANS_API_BASE_URL"]+"/healthz", api)
            node = os.environ.get("CODEX_PRIMARY_RUNTIME_NODE", "node")
            web = stack.enter_context(process([node,str(dashboard/"node_modules/next/dist/bin/next"),"dev","--hostname","127.0.0.1","--port",str(args.port)],cwd=dashboard/"preview/onlyfans-integration",env=env,log=Path(logs)/"next.log"))
            wait_http(f"http://127.0.0.1:{args.port}/api/onlyfans/accounts", web)
            if args.smoke:
                with httpx.Client(timeout=30,trust_env=False) as client:
                    response=client.get(f"http://127.0.0.1:{args.port}/test-login",params={"token":login},follow_redirects=False)
                    cookie=response.cookies.get("allie_admin_session")
                    if response.status_code not in {302,303,307} or not cookie:raise RuntimeError("Synthetic demo session failed")
                subprocess.run([node,str(dashboard/"scripts/check-onlyfans-http-browser.mjs")],cwd=dashboard,
                    env={**env,"ONLYFANS_TEST_WEB_URL":f"http://127.0.0.1:{args.port}","ONLYFANS_TEST_COOKIE":cookie,
                         "ONLYFANS_TEST_EVIDENCE":logs},check=True,timeout=100)
                print("Synthetic demo smoke passed; stopping servers and removing its owned database.",flush=True)
                return
            print(f"Synthetic demo ready: http://127.0.0.1:{args.port}/test-login?token={login}", flush=True)
            print("Local synthetic history only. Ctrl-C stops both servers and drops the disposable database.", flush=True)
            stop = threading.Event()
            signal.signal(signal.SIGINT, lambda *_: stop.set())
            signal.signal(signal.SIGTERM, lambda *_: stop.set())
            while not stop.wait(1):
                if api.poll() is not None or web.poll() is not None:
                    raise RuntimeError("A demo server stopped")
    finally:
        if store: store.close()
        if other: other.close()
        engine.dispose()
        with admin.connect() as c: c.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')
        admin.dispose()


if __name__ == "__main__": main()
