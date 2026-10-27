"""Source-archive trust boundaries and required-release result classification."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


source = load("acceptance_source_tools", "prepare-onlyfans-dashboard.py")
release = load("acceptance_release_tools", "onlyfans-release-acceptance.py")
SHA = "a" * 40
URL = "https://twin-worker-transfer-38e051d1.s3.ap-southeast-2.amazonaws.com/onlyfans/source.tar.gz?secret=must-not-print"
FILES = {"package.json": b"{}", "package-lock.json": b"{}",
         "preview/onlyfans-integration/package.json": b"{}", "scripts/check-onlyfans-http-browser.mjs": b"// synthetic"}


def archive(extra=None, *, revision=SHA):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz", format=tarfile.PAX_FORMAT, pax_headers={"comment": revision}) as handle:
        for name, body in FILES.items():
            info = tarfile.TarInfo(name); info.size = len(body); info.mode = 0o644
            handle.addfile(info, io.BytesIO(body))
        if extra:
            info, body = extra
            handle.addfile(info, io.BytesIO(body))
    return data.getvalue()


def deliver(monkeypatch, data):
    class Response(io.BytesIO):
        headers = {"Content-Length": str(len(data))}
    class Opener:
        def open(self, url, timeout):
            assert url == URL and timeout == 60
            return Response(data)
    monkeypatch.setenv("ONLYFANS_DASHBOARD_ARCHIVE_URL", URL)
    monkeypatch.setattr(source.urllib.request, "build_opener", lambda *args: Opener())


@pytest.mark.parametrize("url", ["http://bucket.s3.amazonaws.com/source", "https://example.com/source",
    "https://s3.amazonaws.com.evil.invalid/source", "https://bucket.s3.amazonaws.com:444/source",
    "https://user:password@bucket.s3.amazonaws.com/source", "https://bucket.s3.amazonaws.com/",
    "https://bucket.s3.amazonaws.com/source#fragment", "file:///tmp/source"])
def test_source_rejects_unapproved_transport_without_printing_signed_url(url):
    with pytest.raises(ValueError):
        source.require_s3_url(url)
    assert source.require_s3_url(URL) == URL


def test_verified_archive_is_rechecked_before_use_and_mutation_is_refused(tmp_path, monkeypatch):
    data = archive(); deliver(monkeypatch, data)
    digest = hashlib.sha256(data).hexdigest(); destination = tmp_path / "paired"
    proof = source.extract_archive(destination, SHA, digest)
    assert proof["commit"] == SHA and proof["archiveSha256"] == digest
    accepted = release.source_proof(destination, SHA)
    assert accepted["method"] == "verified-git-archive" and accepted["verifiedFiles"] == len(FILES)
    (destination / "package.json").write_text('{"changed":true}')
    with pytest.raises(ValueError, match="changed"):
        release.source_proof(destination, SHA)


def test_archive_proof_refuses_added_route_source(tmp_path, monkeypatch):
    data = archive(); deliver(monkeypatch, data)
    destination = tmp_path / "paired"
    source.extract_archive(destination, SHA, hashlib.sha256(data).hexdigest())
    extra = destination / "app/api/unrecorded/route.ts"; extra.parent.mkdir(parents=True)
    extra.write_text("export function GET() {}")
    with pytest.raises(ValueError, match="Unrecorded"):
        release.source_proof(destination, SHA)


def test_real_git_archive_commit_header_is_accepted(tmp_path, monkeypatch):
    repository = tmp_path / "repository"; repository.mkdir()
    for name, data in FILES.items():
        path = repository / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repository), *args], stderr=subprocess.DEVNULL)
    git("init", "--quiet"); git("config", "user.name", "Synthetic acceptance"); git("config", "user.email", "synthetic@example.invalid")
    git("add", "."); git("commit", "--quiet", "-m", "Synthetic source proof")
    sha = git("rev-parse", "HEAD").decode().strip()
    data = git("archive", "--format=tar.gz", sha); deliver(monkeypatch, data)
    proof = source.extract_archive(tmp_path / "paired", sha, hashlib.sha256(data).hexdigest())
    assert proof["commit"] == sha and len(proof["files"]) == len(FILES)


def test_dispatch_source_url_stays_in_event_file(tmp_path, monkeypatch):
    pin = tmp_path / "pin"; pin.write_text(SHA + "\n"); monkeypatch.setattr(source, "PIN", pin)
    event = tmp_path / "event.json"; event.write_text(json.dumps({"inputs": {"dashboard_archive_url": URL}}))
    monkeypatch.delenv("ONLYFANS_DASHBOARD_ARCHIVE_URL", raising=False)
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    assert source.select_source(SHA, "b" * 64, None) == ("archive", SHA)


@pytest.mark.parametrize("kind", ["traversal", "absolute", "symlink", "hardlink", "duplicate", "marker"])
def test_source_refuses_unsafe_archive_before_writing_files(tmp_path, monkeypatch, kind):
    name = {"traversal": "../escaped", "absolute": "/escaped", "duplicate": "package.json",
            "marker": source.MARKER}.get(kind, "unsafe-link")
    info = tarfile.TarInfo(name); info.size = 1
    if kind in {"symlink", "hardlink"}:
        info.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
        info.linkname = "../../escaped"; info.size = 0
    data = archive((info, b"x")); deliver(monkeypatch, data)
    target = tmp_path / "paired"
    with pytest.raises(ValueError, match="unsafe"):
        source.extract_archive(target, SHA, hashlib.sha256(data).hexdigest())
    assert not target.exists() and not (tmp_path / "escaped").exists()


def test_source_keeps_a_contained_tracked_symlink(tmp_path, monkeypatch):
    # The dashboard tracks preview/onlyfans-integration/public -> ../../public (git symlink).
    info = tarfile.TarInfo("preview/onlyfans-integration/public"); info.type = tarfile.SYMTYPE
    info.linkname = "../../public"; info.size = 0
    data = archive((info, b"")); deliver(monkeypatch, data)
    target = tmp_path / "paired"
    proof = source.extract_archive(target, SHA, hashlib.sha256(data).hexdigest())
    link = target / "preview/onlyfans-integration/public"
    assert link.is_symlink() and os.readlink(link) == "../../public"
    assert proof["files"]["preview/onlyfans-integration/public"] == {"symlink": "../../public"}


@pytest.mark.parametrize("child_first", [False, True])
def test_source_refuses_files_below_a_symlink_before_writing(tmp_path, monkeypatch, child_first):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz", format=tarfile.PAX_FORMAT, pax_headers={"comment": SHA}) as handle:
        for name, body in FILES.items():
            info = tarfile.TarInfo(name); info.size = len(body)
            handle.addfile(info, io.BytesIO(body))
        link = tarfile.TarInfo("alias"); link.type = tarfile.SYMTYPE; link.linkname = "public"
        child = tarfile.TarInfo("alias/config.json"); child.size = 2
        entries = [(child, b"{}"), (link, b"")] if child_first else [(link, b""), (child, b"{}")]
        for info, body in entries:
            handle.addfile(info, io.BytesIO(body))
    payload = data.getvalue(); deliver(monkeypatch, payload)
    target = tmp_path / "paired"
    with pytest.raises(source.SourceRefused, match="symlink ancestor"):
        source.extract_archive(target, SHA, hashlib.sha256(payload).hexdigest())
    assert not target.exists()


def test_source_refuses_link_targets_that_traverse_another_link(tmp_path, monkeypatch):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz", format=tarfile.PAX_FORMAT, pax_headers={"comment": SHA}) as handle:
        for name, body in FILES.items():
            info = tarfile.TarInfo(name); info.size = len(body)
            handle.addfile(info, io.BytesIO(body))
        for name, target in [("redirect", "public"), ("shortcut", "redirect/assets")]:
            info = tarfile.TarInfo(name); info.type = tarfile.SYMTYPE; info.linkname = target
            handle.addfile(info)
    payload = data.getvalue(); deliver(monkeypatch, payload)
    target = tmp_path / "paired"
    with pytest.raises(source.SourceRefused, match="symlink chain"):
        source.extract_archive(target, SHA, hashlib.sha256(payload).hexdigest())
    assert not target.exists()


@pytest.mark.parametrize("defect", ["wrong_digest", "wrong_commit", "download_limit", "unpacked_limit", "file_limit"])
def test_source_rejects_unverified_or_unbounded_archive(tmp_path, monkeypatch, defect):
    data = archive(revision="b" * 40 if defect == "wrong_commit" else SHA)
    deliver(monkeypatch, data)
    if defect == "download_limit": monkeypatch.setattr(source, "MAX_DOWNLOAD", 1)
    if defect == "unpacked_limit": monkeypatch.setattr(source, "MAX_UNPACKED", 1)
    if defect == "file_limit": monkeypatch.setattr(source, "MAX_FILES", 1)
    digest = "0" * 64 if defect == "wrong_digest" else hashlib.sha256(data).hexdigest()
    with pytest.raises(ValueError):
        source.extract_archive(tmp_path / "paired", SHA, digest)


def test_source_bounds_global_pax_metadata_before_tar_parser_runs(tmp_path, monkeypatch):
    limit = 16 * 1024
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz", format=tarfile.PAX_FORMAT,
                     pax_headers={"comment": SHA, "large_metadata": "x" * (limit * 2)}) as handle:
        for name, body in FILES.items():
            info = tarfile.TarInfo(name); info.size = len(body)
            handle.addfile(info, io.BytesIO(body))
    data = payload.getvalue(); deliver(monkeypatch, data)
    monkeypatch.setattr(source, "MAX_UNPACKED", limit)

    def forbidden_parser(*args, **kwargs):
        raise AssertionError("Oversized metadata reached the tar parser")

    monkeypatch.setattr(source.tarfile, "open", forbidden_parser)
    target = tmp_path / "paired"
    with pytest.raises(source.SourceRefused, match="expanded tar size"):
        source.extract_archive(target, SHA, hashlib.sha256(data).hexdigest())
    assert not target.exists()


def test_source_requires_exact_committed_pin_and_concrete_capability(tmp_path, monkeypatch):
    pin = tmp_path / "pin"; pin.write_text(SHA + "\n"); monkeypatch.setattr(source, "PIN", pin)
    monkeypatch.delenv("ONLYFANS_DASHBOARD_ARCHIVE_URL", raising=False)
    monkeypatch.delenv("ONLYFANS_DASHBOARD_READ_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    with pytest.raises(ValueError, match="unavailable"):
        source.select_source(SHA, "", None)
    with pytest.raises(ValueError, match="differs"):
        source.pinned_ref("b" * 40)
    monkeypatch.setenv("ONLYFANS_DASHBOARD_ARCHIVE_URL", URL)
    assert source.select_source(SHA, "c" * 64, None) == ("archive", SHA)


def test_junit_classification_keeps_failed_errored_and_skipped_distinct(tmp_path):
    report = tmp_path / "junit.xml"
    report.write_text('<testsuites><testsuite><testcase name="pass"/><testcase name="fail"><failure/></testcase>'
                      '<testcase name="error"><error/></testcase><testcase name="skip"><skipped/></testcase></testsuite></testsuites>')
    assert release.junit_counts(report) == {"reported": 4, "passed": 1, "failed": 1, "errored": 1, "skipped": 1}


@pytest.fixture
def sharded_evidence(tmp_path, monkeypatch):
    root = tmp_path / "backend"; root.mkdir()
    (root / "service/tests").mkdir(parents=True)
    files = [f"service/tests/test_part_{index:02}.py" for index in range(12)]
    scripts = ["service/tests/read_script.py", "service/tests/migrate_script.py"]
    for name in [*files, *scripts]:
        (root / name).write_text("# synthetic source\n")
    manifest = root / "manifest.json"
    manifest.write_text(json.dumps({"combined": {"patterns": ["service/tests/test_part_*.py"], "separateScripts": scripts}}))
    pin = root / "docs/onlyfans/ACCEPTANCE-DASHBOARD-SHA"; pin.parent.mkdir(parents=True); pin.write_text(SHA)
    monkeypatch.setattr(release, "ROOT", root)
    monkeypatch.setattr(release, "MANIFEST", manifest)
    backend = {"method": "git", "sha": "b" * 40, "tree": "c" * 40}
    dashboard = {"method": "verified-git-archive", "sha": SHA, "archiveSha256": "d" * 64, "verifiedFiles": 4}
    monkeypatch.setattr(release, "source_proof", lambda *args: backend)

    def build(count=4):
        evidence = tmp_path / f"evidence-{count}"; evidence.mkdir()
        for index in range(count):
            folder = evidence / f"shard-{index}"; folder.mkdir()
            assigned, separate = release.shard_manifest("combined", index, count)
            nodes = [name + "::test_case" for name in assigned]
            events = {"selected": nodes, "collected": len(nodes), "deselected": [], "collectionErrors": [], "exitStatus": 0}
            for name in ("collection.json", "execution.json"):
                (folder / name).write_text(json.dumps(events))
            xml = '<testsuites><testsuite>' + ''.join('<testcase name="test_case"/>' for _ in nodes) + '</testsuite></testsuites>'
            (folder / "acceptance.xml").write_text(xml)
            (folder / "expanded-manifest.json").write_text(json.dumps({"pytestFiles": assigned, "separateScripts": separate}))
            counts = release.junit_counts(folder / "acceptance.xml")
            counts.update(collected=len(nodes), deselected=0)
            report = {"suite": "combined", "accepted": True, "backend": backend, "dashboard": dashboard,
                      "shard": {"index": index, "count": count, "pytestFiles": assigned, "separateScripts": separate},
                      "pytest": counts, "separateScripts": [{"path": name, "passed": True, "exitStatus": 0} for name in separate]}
            (folder / "summary.json").write_text(json.dumps(report))
            for script_index in range(len(separate)):
                (folder / f"script-{script_index}.log").write_text("all cases passed\n")
        return evidence, backend, files, scripts
    return build


@pytest.mark.parametrize("count", [1, 4, 6])
def test_shards_cover_every_file_and_isolated_script_once(sharded_evidence, count):
    evidence, backend, files, scripts = sharded_evidence(count)
    assignments = [release.shard_manifest("combined", index, count) for index in range(count)]
    assigned = [name for partition, _ in assignments for name in partition]
    assert sorted(assigned) == files and len(assigned) == len(set(assigned))
    assert [name for _, separate in assignments for name in separate] == scripts
    result = release.aggregate_shards(evidence, "combined", count, backend["sha"], SHA)
    assert result["accepted"] and result["pytest"]["passed"] == 12
    assert result["pytestFiles"] == files and result["separateScripts"] == scripts
    assert len(result["nodeIDs"]) == 12


@pytest.mark.parametrize("index,count", [(0, 0), (-1, 4), (4, 4), (0, 33), (12, 13)])
def test_invalid_or_empty_shards_are_refused(sharded_evidence, index, count):
    sharded_evidence()
    with pytest.raises(ValueError, match="shard"):
        release.shard_manifest("combined", index, count)


@pytest.mark.parametrize("defect", ["missing_shard", "duplicate_index", "missing_file", "backend_tree", "dashboard_sha",
    "dashboard_digest", "duplicate_nodes", "execution_nodes", "deselected", "node_outside_assignment", "junit_skip",
    "junit_failure", "junit_error", "junit_missing_case", "missing_script", "duplicate_script", "failed_script_log"])
def test_aggregate_refuses_incomplete_or_inconsistent_shard_evidence(sharded_evidence, defect):
    evidence, backend, _, _ = sharded_evidence()
    folder = evidence / ("shard-1" if defect in {"duplicate_index", "duplicate_script"} else "shard-0")
    summary = folder / "summary.json"; report = json.loads(summary.read_text())
    if defect == "missing_shard":
        summary.unlink()
    elif defect == "duplicate_index":
        report["shard"]["index"] = 0
    elif defect == "missing_file":
        report["shard"]["pytestFiles"].pop()
    elif defect == "backend_tree":
        report["backend"]["tree"] = "e" * 40
    elif defect == "dashboard_sha":
        report["dashboard"]["sha"] = "e" * 40
    elif defect == "dashboard_digest":
        report["dashboard"]["archiveSha256"] = "e" * 64
    elif defect in {"duplicate_nodes", "execution_nodes", "deselected", "node_outside_assignment"}:
        event_path = folder / ("execution.json" if defect in {"execution_nodes", "deselected"} else "collection.json")
        events = json.loads(event_path.read_text())
        if defect == "duplicate_nodes": events["selected"].append(events["selected"][0]); events["collected"] += 1
        elif defect == "deselected": events["deselected"] = ["excluded-node"]
        else: events["selected"][0] = "service/tests/foreign.py::test_case"
        event_path.write_text(json.dumps(events))
        if defect == "node_outside_assignment":
            (folder / "execution.json").write_text(json.dumps(events))
    elif defect.startswith("junit_"):
        xml_path = folder / "acceptance.xml"; xml = xml_path.read_text()
        if defect == "junit_missing_case": xml = xml.replace('<testcase name="test_case"/>', '', 1)
        else: xml = xml.replace('<testcase name="test_case"/>', '<testcase name="test_case"><' + {"junit_skip": "skipped", "junit_failure": "failure", "junit_error": "error"}[defect] + '/></testcase>', 1)
        xml_path.write_text(xml)
    elif defect == "missing_script": report["separateScripts"] = []
    elif defect == "duplicate_script": report["separateScripts"] = [{"path": "service/tests/read_script.py", "passed": True, "exitStatus": 0}]
    elif defect == "failed_script_log": (folder / "script-0.log").write_text("all cases passed\n1 FAILED\n")
    if defect != "missing_shard": summary.write_text(json.dumps(report))
    with pytest.raises(ValueError):
        release.aggregate_shards(evidence, "combined", 4, backend["sha"], SHA)
