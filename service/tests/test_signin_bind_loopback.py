"""Owner item (b): the private creator sign-in service (:8479) must not listen on every interface.

Default bind is loopback; the Docker deploy opts into the container-private network with HOST=0.0.0.0
(compose `expose`, never `ports`). Spawns the real server.mjs with a throwaway token and checks the bound address."""
import os
import pathlib
import shutil
import socket
import subprocess
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVER = ROOT / "creator-sign-in" / "server.mjs"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _bound(env_extra):
    if not shutil.which("node") or not shutil.which("lsof") or not (ROOT / "creator-sign-in" / "node_modules" / "playwright").exists():
        pytest.skip("node, lsof + creator-sign-in/node_modules required")
    port = _free_port()
    env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp"),
           "ONLYFANS_SIGNIN_TOKEN": "t" * 40, "PORT": str(port), **env_extra}
    proc = subprocess.Popen(["node", str(SERVER)], env=env, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        assert "ready" in proc.stdout.readline()
        deadline = time.time() + 5
        while time.time() < deadline:
            out = subprocess.run(["lsof", "-nP", "-a", "-p", str(proc.pid), f"-iTCP:{port}", "-sTCP:LISTEN"],
                                 capture_output=True, text=True).stdout
            if str(port) in out:
                return out.splitlines()[-1].split()[-2]
            time.sleep(0.1)
        raise AssertionError("server never listened")
    finally:
        proc.kill()
        proc.wait(5)


def test_signin_binds_loopback_by_default():
    addr = _bound({})
    assert addr.startswith("127.0.0.1:"), addr


def test_signin_host_env_opts_into_private_network():
    addr = _bound({"HOST": "0.0.0.0"})
    assert addr.startswith("*:"), addr


def test_signin_empty_host_stays_loopback():
    addr = _bound({"HOST": ""})
    assert addr.startswith("127.0.0.1:"), addr
