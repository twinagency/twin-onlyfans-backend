"""Release evidence only: record selected node identities and any deselection."""
import json
import os
from pathlib import Path

selected = []
deselected = []
collection_errors = []


def pytest_deselected(items):
    deselected.extend(item.nodeid for item in items)


def pytest_collectreport(report):
    if report.failed:
        collection_errors.append(report.nodeid)


def pytest_collection_finish(session):
    selected[:] = [item.nodeid for item in session.items]


def pytest_sessionfinish(session, exitstatus):
    path = os.environ.get("ONLYFANS_ACCEPTANCE_EVENT_FILE")
    if path:
        Path(path).write_text(json.dumps({"selected": selected, "deselected": deselected,
            "collectionErrors": collection_errors, "collected": session.testscollected,
            "exitStatus": int(exitstatus)}, indent=2) + "\n")
