"""Release evidence only: record selected node identities and any deselection."""
import json
import os
from pathlib import Path

selected = []
deselected = []
collection_errors = []
outcomes = {}


def pytest_deselected(items):
    deselected.extend(item.nodeid for item in items)


def pytest_collectreport(report):
    if report.failed:
        collection_errors.append(report.nodeid)


def pytest_runtest_logreport(report):
    """Per-node outcome: the call result, or the first setup/teardown problem."""
    state = "error" if report.when != "call" and report.failed else (
        "skipped" if report.skipped else "failed" if report.failed else "passed")
    if report.when == "call" or state != "passed":
        if outcomes.get(report.nodeid) in {None, "passed"}:
            outcomes[report.nodeid] = state


def pytest_collection_finish(session):
    selected[:] = [item.nodeid for item in session.items]


def pytest_sessionfinish(session, exitstatus):
    path = os.environ.get("ONLYFANS_ACCEPTANCE_EVENT_FILE")
    if path:
        Path(path).write_text(json.dumps({"selected": selected, "deselected": deselected,
            "collectionErrors": collection_errors, "outcomes": outcomes, "collected": session.testscollected,
            "exitStatus": int(exitstatus)}, indent=2) + "\n")
