"""The service runner must execute test bodies, not just import their files."""
import importlib.util
import json
import os
from pathlib import Path

import pytest


def isolated_environment():
    return {name: value for name, value in os.environ.items() if name.upper() in {
        "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "PYTHONPATH",
    }}


def driver(monkeypatch, tmp_path):
    path = Path(__file__).resolve().parents[2] / "scripts/run-service-tests.py"
    spec = importlib.util.spec_from_file_location("isolated_service_test_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    (tmp_path / "service/tests").mkdir(parents=True)
    return module


def test_pytest_functions_without_pytest_import_are_actually_executed(monkeypatch, tmp_path):
    module = driver(monkeypatch, tmp_path)
    (tmp_path / "service/tests/test_without_import.py").write_text(
        "def test_body_must_run():\n    assert False, 'body actually executed'\n"
    )
    report = tmp_path / "result.json"
    failed = module.run_modules(False, False, isolated_environment(), report=report)
    assert failed
    result = json.loads(report.read_text())["results"][0]
    assert result["status"] == "fail" and result["exit_code"] != 0


def test_guarded_script_entrypoint_keeps_its_argument_contract(monkeypatch, tmp_path):
    module = driver(monkeypatch, tmp_path)
    (tmp_path / "service/tests/test_script.py").write_text(
        "def test_with_argument(value):\n    assert value == 'script-value'\n"
        "if __name__ == '__main__':\n    test_with_argument('script-value')\n"
    )
    report = tmp_path / "result.json"
    failed = module.run_modules(False, False, isolated_environment(), report=report)
    assert not failed
    assert json.loads(report.read_text())["results"][0]["status"] == "pass"
