"""F0 scripts/check.sh: loopback/GET-only/no-redirect/no-proxy guards, bearer never echoed, verdict -> exit codes."""
import http.server, importlib.util, os, pathlib, shutil, subprocess, sys, threading, types, pytest, sqlalchemy

ROOT = pathlib.Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("check_live", ROOT / "scripts" / "check_live.py")
check_live = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_live)
DOWN = "http://127.0.0.1:9"  # discard port: connection refused

@pytest.mark.parametrize("url", ["http://example.com/healthz", "http://127.0.0.1.evil.test/", "http://u:pw@127.0.0.1/",
                                 "http://localhost@evil.test/", "file:///etc/passwd", "http://10.0.0.1:8787/"])
def test_live_refuses_non_loopback(url):
    for guard in (check_live.loopback, check_live.get):
        pytest.raises(SystemExit, guard, url)

@pytest.mark.parametrize("url", ["postgresql://u@db.example.com/x", "postgresql://u@/x", "sqlite:///x.db",
    "postgresql://u@127.0.0.1,evil.test/x", "postgresql://u@/x?host=a&host=b"] + ["postgresql://u@127.0.0.1/" + q for q in (
    "x?host=evil.test", "x?host=127.0.0.1,evil.test", "x?hostaddr=10.0.0.1", "x?service=p", "x?servicefile=/f",
    "host=evil", "x?dsn=hostaddr%3D10.0.0.1", "x?options=-c", "x?host=/tmp,evil.test")])
def test_live_refuses_non_loopback_db_url(url):
    with pytest.raises(SystemExit):
        check_live.db_loopback(url)
    assert check_live.db_loopback("postgresql://u@127.0.0.1:5432/x")
    assert check_live.db_loopback("postgresql+psycopg2://u@/x?host=/tmp/sock&port=5")

def test_db_checks_strip_pg_env_before_connect(monkeypatch):
    for k in [k for k in os.environ if k.startswith("PG")] + ["PGHOSTADDR", "PGSERVICE"]:  # restored after the test
        monkeypatch.setenv(k, os.environ.get(k, "10.0.0.1"))
    seen = []
    def connect(): seen.append([k for k in os.environ if k.startswith("PG")]); raise OSError
    monkeypatch.setattr(sqlalchemy, "create_engine", lambda *a, **k: types.SimpleNamespace(connect=connect))
    assert check_live.db_checks("postgresql://u@127.0.0.1/x") == "DOWN" and seen == [[]]
    pytest.raises(SystemExit, check_live.db_checks, "postgresql://u@db.example.com/x")  # FL-L1: own guard, no engine

def _env(tmp_path, **kw):
    return {**{k: v for k, v in os.environ.items() if not k.startswith(("CHECK_", "ALLOW_SKIP"))}, "PY": sys.executable,
            "CHECK_EVIDENCE_ROOT": str(tmp_path / "ev"), "CHECK_API": DOWN, "CHECK_DASH": DOWN, "CHECK_SIGNIN": DOWN,
            "CHECK_TOKEN_VAR": "CHECK_TEST_TOKEN", **kw}

def _run(mode, env, root=ROOT):
    if env.get("PATH") == os.environ.get("PATH") and not (shutil.which("timeout") or shutil.which("gtimeout")):
        pytest.skip("no timeout/gtimeout on PATH: check.sh refuses capped steps (INFRA)")
    return subprocess.run(["bash", str(root / "scripts/check.sh"), mode], env=env, cwd=root,
                          capture_output=True, text=True, timeout=120)
_verdict = lambda tmp_path: next((tmp_path / "ev").iterdir()).joinpath("verdict.txt").read_text()

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

@pytest.mark.parametrize("budget,why", [("0", "positive int"), ("-5", "positive int"), ("abc", "positive int"),
                                        ("9223372036854775808", "positive int"), ("60", "no timeout"), ("60", "not writable")])
def test_fast_bad_budget_no_timeout_or_evidence_is_infra_nothing_run(tmp_path, budget, why):
    (tmp_path / "bin").mkdir()  # the run's whole PATH: no timeout/gtimeout
    (tmp_path / "ev").mkdir(mode=0o500 if why == "not writable" else 0o700)  # evidence root
    for tool in ("bash", "env", "nice", "dirname", "git", "date", "mkdir", "tee", "grep", "touch"):
        os.symlink(shutil.which(tool), tmp_path / "bin" / tool)
    r = _run("fast", _env(tmp_path, CHECK_BUDGET_S=budget, PATH=str(tmp_path / "bin")), _fake_repo(tmp_path, "touch ran"))
    assert r.returncode == 2 and why in r.stdout + r.stderr and not (tmp_path / "ran").exists(), r.stdout + r.stderr

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

@pytest.mark.parametrize("mode", ["fast", "wide"])
def test_deferred_touched_area_is_listed_and_noted_fast_defers_wide_never(tmp_path, mode):
    stub = 'echo "defer=${PREMERGE_DEFER_AFTER_S:-none}"; echo "DEFERRED to wide: service/tests/test_a.py"'
    r = _run(mode, _env(tmp_path, CHECK_BUDGET_S="900"), _fake_repo(tmp_path, stub))
    assert r.returncode == 0 and ("defer=600" if mode == "fast" else "defer=none") in r.stdout, r.stdout + r.stderr
    assert "deferred to wide: 1 touched-area tests not run" in _verdict(tmp_path)
    assert next((tmp_path / "ev").iterdir()).joinpath("deferred.txt").read_text() == "DEFERRED to wide: service/tests/test_a.py\n"

def _git(repo, *a): subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)

def test_premerge_parallel_pool_keeps_order_counts_known_red_and_planted_failure(tmp_path):
    repo, t = tmp_path / "r", tmp_path / "r/service/tests"
    t.mkdir(parents=True), (repo / "scripts").mkdir(), shutil.copy(ROOT / "scripts/premerge.sh", repo / "scripts")
    (repo / "scripts/premerge-promoted.txt").write_text("# none\n"), (repo / "service/m.py").write_text("X = 1\n")
    (repo / "scripts/premerge-known-red.txt").write_text("service/tests/test_z3.py::test_kr\n")
    (t / "test_uses_m.py").write_text("import m\ndef test_m(): assert m.X\n")
    _git(repo, "init", "-q"), _git(repo, "add", "."), _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "b")
    (repo / "service/m.py").write_text("X = 2\n")  # touched module -> test_uses_m.py is touched-area (d)
    (t / "test_z1.py").write_text("import time\ndef test_slow(): time.sleep(1.5)\n")  # finishes last, prints first
    (t / "test_z2.py").write_text("def test_planted(): assert False, 'planted'\n")
    (t / "test_z3.py").write_text("def test_ok(): pass\ndef test_kr(): assert False\n")
    env = {**_env(tmp_path), "BASE": "HEAD", "CHECK_JOBS": "3"}
    run = lambda **kw: subprocess.run(["bash", "scripts/premerge.sh"], cwd=repo, env={**env, **kw}, capture_output=True, text=True, timeout=120)
    r = run()
    lines = [l.split(":")[0] for l in r.stdout.splitlines() if l.startswith(("PASS ", "FAIL ", "KNOWN-RED ", "== (b", "== (c", "== (d"))]
    assert r.returncode == 1 and lines == ["== (b) changed tests (3)", "PASS service/tests/test_z1.py",
        "FAIL service/tests/test_z2.py", "KNOWN-RED service/tests/test_z3.py", "PASS service/tests/test_z3.py",
        "== (c) promoted tests (0)", "== (d) touched-area tests (1, CHECK_JOBS=3)", "PASS service/tests/test_uses_m.py"], r.stdout
    (t / "test_z2.py").unlink()
    r = run(PREMERGE_DEFER_AFTER_S="0")
    assert r.returncode == 0 and "DEFERRED to wide: service/tests/test_uses_m.py" in r.stdout, r.stdout
    assert "premerge: OK (0 skipped, 1 known-red, 1 deferred to wide)" in r.stdout and "PASS service/tests/test_z1.py" in r.stdout
