"""Offline browser guard is part of declared release acceptance."""
import subprocess
from pathlib import Path
def test_browser_start_requires_broker_fence_and_late_cancel_never_launches():
    root=Path(__file__).resolve().parents[2]
    result=subprocess.run(["node","creator-sign-in/check-network-guard.mjs"],cwd=root,text=True,capture_output=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
    assert "PASS broker guard" in result.stdout
