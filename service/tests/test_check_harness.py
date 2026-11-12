"""F0 scripts/check.sh: loopback/GET-only/no-redirect/no-proxy guards, bearer never echoed, verdict -> exit codes."""
import http.server, importlib.util, os, pathlib, shutil, subprocess, sys, threading, pytest

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

@pytest.mark.parametrize("url", ["postgresql://u@db.example.com/x", "postgresql://u@/x", "sqlite:///x.db",
                                 "postgresql://u@127.0.0.1/x?host=evil.test", "postgresql://u@127.0.0.1,evil.test/x",
                                 "postgresql://u@127.0.0.1/x?host=127.0.0.1,evil.test", "postgresql://u@/x?host=a&host=b",
                                 "postgresql://u@127.0.0.1/x?hostaddr=10.0.0.1", "postgresql://u@127.0.0.1/x?service=p",
                                 "postgresql://u@127.0.0.1/x?servicefile=/f", "postgresql://u@127.0.0.1/host=evil"])
def test_live_refuses_non_loopback_db_url(url):
    with pytest.raises(SystemExit):
        check_live.db_loopback(url)
    assert check_live.db_loopback("postgresql://u@127.0.0.1:5432/x")
    assert check_live.db_loopback("postgresql+psycopg2://u@/x?host=/tmp/sock&port=5")

def _env(tmp_path, **kw):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CHECK_", "ALLOW_SKIP"))}
    env.update({"PY": sys.executable, "CHECK_EVIDENCE_ROOT": str(tmp_path / "ev"), "CHECK_API": DOWN,
                "CHECK_DASH": DOWN, "CHECK_SIGNIN": DOWN, "CHECK_TOKEN_VAR": "CHECK_TEST_TOKEN", **kw})
    return env

def _run(mode, env, root=ROOT):
    return subprocess.run(["bash", str(root / "scripts/check.sh"), mode], env=env, cwd=root,
                          capture_output=True, text=True, timeout=120)

def _verdict(tmp_path):
    return next((tmp_path / "ev").iterdir()).joinpath("verdict.txt").read_text()

def test_live_script_refuses_before_any_call(tmp_path):
    r = _run("live", _env(tmp_path, CHECK_API="http://192.0.2.1:8787"))
    assert r.returncode == 1 and "refused non-loopback" in r.stdout + r.stderr and "PASS" not in r.stdout

def _fake_repo(tmp_path, premerge):  # the gate has no premerge override: run a copy with a stub premerge.sh
    (tmp_path / "scripts").mkdir()
    [shutil.copy(ROOT / "scripts" / f, tmp_path / "scripts") for f in ("check.sh", "check_live.py")]
    (p := tmp_path / "scripts/premerge.sh").write_text(f"#!/usr/bin/env bash\n{premerge}\n"), p.chmod(0o755)
    return tmp_path

@pytest.mark.parametrize("cmd,code,verdict", [("true", 0, "GREEN"), ("exit 1", 1, "RED"), ("exit 2", 2, "INFRA"),
                                              ("sleep 30", 1, "RED")])
def test_fast_verdict_and_exit_code(tmp_path, cmd, code, verdict):
    r = _run("fast", _env(tmp_path, CHECK_BUDGET_S="3" if "sleep" in cmd else "60"), _fake_repo(tmp_path, cmd))
    assert r.returncode == code, r.stdout + r.stderr
    assert (text := _verdict(tmp_path)).splitlines()[0] == verdict and "wall " in text
    assert "live skipped: local stack down" in text or cmd == "sleep 30"

def test_fast_without_timeout_binary_is_infra(tmp_path):
    (tmp_path / "bin").mkdir()
    for tool in ("bash", "env", "nice", "dirname", "git", "date", "mkdir", "tee", "grep"):
        os.symlink(shutil.which(tool), tmp_path / "bin" / tool)
    r = _run("fast", _env(tmp_path, PATH=str(tmp_path / "bin")), _fake_repo(tmp_path, "true"))
    assert r.returncode == 2 and "no timeout" in _verdict(tmp_path), r.stdout + r.stderr

class _Stub(http.server.BaseHTTPRequestHandler):
    seen, bad = [], False
    good = {"/healthz": b'{"ok": true, "role": "api"}', "/login": b"<html></html>", "/health": b'{"ok": true}'}
    def do_GET(self):  # noqa: N802
        self.seen.append((self.command, self.path))
        redirect = self.bad and self.path == "/login"
        self.send_response(302 if redirect else 200)
        self.send_header("Location", "/followed") if redirect else None
        self.end_headers()
        self.wfile.write(b'{"ok": false}' if self.bad else self.good.get(self.path, b"{}"))
    def log_message(self, *a): pass

def _live_stub(tmp_path, bad=False, **kw):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    _Stub.seen, _Stub.bad = [], bad
    try:  # a dead proxy in env: requests must still go direct
        return _run("live", _env(tmp_path, **{"CHECK_API": base, "CHECK_DASH": base, "CHECK_SIGNIN": base,
                    "HTTP_PROXY": DOWN, "http_proxy": DOWN, "NO_PROXY": "", "no_proxy": "", **kw}))
    finally:
        srv.shutdown()

_dump = lambda r, tmp_path: r.stdout + r.stderr + "".join(p.read_text() for p in tmp_path.rglob("*") if p.is_file())

def test_live_shape_mismatch_is_red_no_redirect_no_proxy_token_never_printed(tmp_path):
    r = _live_stub(tmp_path, bad=True, CHECK_TEST_TOKEN="tok-SECRET-0f0f")
    assert r.returncode == 1 and r.stdout.count("FAIL ") == 6, r.stdout
    assert {m for m, _ in _Stub.seen} == {"GET"} and len(_Stub.seen) == 6  # /login's 302 not followed
    assert "tok-SECRET-0f0f" not in _dump(r, tmp_path)

def test_live_bearer_with_newline_never_echoed(tmp_path):
    r = _live_stub(tmp_path, CHECK_TEST_TOKEN="tok-SECRET\nZQX9")
    assert r.returncode == 1 and "tok-SECRET" not in _dump(r, tmp_path) and "ZQX9" not in _dump(r, tmp_path)

def test_live_skip_is_infra_partial_down_is_red_all_down_is_infra(tmp_path):
    r = _live_stub(tmp_path / "a")  # healthy stub, no bearer, no DB URL: 4 SKIPs
    assert r.returncode == 2 and _verdict(tmp_path / "a").count("live SKIP") == 4, r.stdout
    assert _live_stub(tmp_path / "b", ALLOW_SKIP="1").returncode == 0
    r = _live_stub(tmp_path / "c", CHECK_DASH=DOWN)
    assert r.returncode == 1 and "PASS sign-in health" in r.stdout, r.stdout
    assert _run("live", _env(tmp_path / "d")).returncode == 2
