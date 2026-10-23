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
