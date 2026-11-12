"""F0: scripts/check.sh harness. Live smoke refuses non-loopback targets, uses GET only, never echoes the
bearer, and fast-mode verdicts map to exit codes 0 GREEN / 1 RED / 2 INFRA (premerge stubbed)."""
import http.server, importlib.util, os, pathlib, subprocess, sys, threading

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("check_live", ROOT / "scripts" / "check_live.py")
check_live = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_live)
DOWN = "http://127.0.0.1:9"  # discard port: connection refused


@pytest.mark.parametrize("url", ["http://example.com/healthz", "http://127.0.0.1.evil.test/",
                                 "http://localhost@evil.test/", "file:///etc/passwd", "http://10.0.0.1:8787/"])
def test_live_refuses_non_loopback(url):
    with pytest.raises(SystemExit):
        check_live.loopback(url)
    with pytest.raises(SystemExit):
        check_live.get(url)


def test_live_refuses_non_loopback_db_url():
    with pytest.raises(SystemExit):
        check_live.db_loopback("postgresql://u@db.example.com:5432/x")
    assert check_live.db_loopback("postgresql://u@127.0.0.1:5432/x")
    assert check_live.db_loopback("postgresql://u@/x?host=/tmp/sock&port=5")


def _env(tmp_path, **kw):
    env = {k: v for k, v in os.environ.items() if not k.startswith("CHECK_")}
    env.update({"PY": sys.executable, "CHECK_EVIDENCE_ROOT": str(tmp_path), "CHECK_API": DOWN, "CHECK_DASH": DOWN,
                "CHECK_SIGNIN": DOWN, "CHECK_TOKEN_VAR": "CHECK_TEST_TOKEN", **kw})
    return env


def _run(mode, env):
    return subprocess.run(["bash", str(ROOT / "scripts/check.sh"), mode], env=env, cwd=ROOT,
                          capture_output=True, text=True, timeout=120)


def test_live_script_refuses_before_any_call(tmp_path):
    r = _run("live", _env(tmp_path, CHECK_API="http://192.0.2.1:8787"))
    assert r.returncode == 1 and "refused non-loopback" in r.stdout + r.stderr and "PASS" not in r.stdout


@pytest.mark.parametrize("cmd,code,verdict", [("true", 0, "GREEN"), ("exit 1", 1, "RED"), ("exit 2", 2, "INFRA"),
                                              ("sleep 30", 1, "RED")])
def test_fast_verdict_and_exit_code(tmp_path, cmd, code, verdict):
    r = _run("fast", _env(tmp_path, CHECK_PREMERGE_CMD=cmd, CHECK_BUDGET_S="3" if "sleep" in cmd else "60"))
    assert r.returncode == code, r.stdout + r.stderr
    (ev,) = tmp_path.iterdir()
    text = (ev / "verdict.txt").read_text()
    assert text.splitlines()[0] == verdict and "wall " in text
    assert "live skipped: local stack down" in text or cmd == "sleep 30"


class _Stub(http.server.BaseHTTPRequestHandler):
    seen = []
    def do_GET(self):  # noqa: N802
        self.seen.append((self.command, self.path))
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(b'{"ok": false}')
    def log_message(self, *a):
        pass


def test_live_shape_mismatch_is_red_and_token_never_printed(tmp_path):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        r = _run("live", _env(tmp_path, CHECK_API=base, CHECK_DASH=base, CHECK_SIGNIN=base,
                              CHECK_TEST_TOKEN="tok-SECRET-0f0f"))
    finally:
        srv.shutdown()
    assert r.returncode == 1 and r.stdout.count("FAIL ") == 6, r.stdout
    assert {m for m, _ in _Stub.seen} == {"GET"} and len(_Stub.seen) == 6
    dumped = r.stdout + r.stderr + "".join(p.read_text() for p in tmp_path.rglob("*") if p.is_file())
    assert "tok-SECRET-0f0f" not in dumped
