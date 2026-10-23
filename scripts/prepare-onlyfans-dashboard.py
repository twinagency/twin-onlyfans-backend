#!/usr/bin/env python3
"""Verify and unpack one pinned dashboard source archive for release acceptance.

Only a bounded HTTPS S3 download is accepted. Signed URLs and credentials are
never printed. Archive provenance is checked again by the acceptance runner.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
PIN = ROOT / "docs/onlyfans/ACCEPTANCE-DASHBOARD-SHA"
MAX_DOWNLOAD = 128 * 1024 * 1024
MAX_UNPACKED = 256 * 1024 * 1024
MAX_FILES = 20000
MARKER = ".onlyfans-acceptance-source.json"


class SourceRefused(ValueError):
    pass


def archive_url() -> str:
    """Read dispatch data without interpolating the signed URL into CI logs."""
    explicit = os.environ.get("ONLYFANS_DASHBOARD_ARCHIVE_URL", "")
    if explicit:
        return explicit
    event = os.environ.get("GITHUB_EVENT_PATH")
    if event:
        value = json.loads(Path(event).read_text()).get("inputs", {}).get("dashboard_archive_url", "")
        if type(value) is str:
            return value
    return ""


def require_s3_url(raw: str) -> str:
    value = urllib.parse.urlsplit(raw)
    host = value.hostname or ""
    if (len(raw) > 16384 or value.scheme != "https" or value.username or value.password
            or value.port not in (None, 443) or value.fragment
            or not re.fullmatch(r"(?:[a-z0-9][a-z0-9.-]*\.)?s3(?:[.-][a-z0-9-]+)?\.amazonaws\.com", host)
            or not value.path or value.path == "/"):
        raise SourceRefused("Dashboard source must be a bounded HTTPS Amazon S3 object URL")
    return raw


class S3Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        require_s3_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def pinned_ref(requested: str) -> str:
    pin = PIN.read_text().strip()
    if not re.fullmatch(r"[0-9a-f]{40}", pin):
        raise SourceRefused("The committed dashboard acceptance pin must be one full commit SHA")
    if requested and requested != pin:
        raise SourceRefused("Requested dashboard_ref differs from the committed paired acceptance SHA")
    return pin


def select_source(ref: str, digest: str, output: Path | None) -> tuple[str, str]:
    sha = pinned_ref(ref)
    raw = archive_url()
    if raw:
        require_s3_url(raw)
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise SourceRefused("A signed archive requires its independently recorded SHA256")
        mode = "archive"
    elif os.environ.get("ONLYFANS_DASHBOARD_READ_TOKEN"):
        mode = "git"
    else:
        raise SourceRefused("Paired source is unavailable: provide dashboard_archive_url plus dashboard_archive_sha256 "
                         "for the committed dashboard_ref, or explicitly provision a scoped dashboard Contents-read token. "
                         "No cross-repository reader is assumed.")
    if output:
        with output.open("a") as handle:
            handle.write(f"mode={mode}\ndashboard_sha={sha}\n")
    return mode, sha


def extract_archive(destination: Path, sha: str, digest: str) -> dict:
    if destination.exists() and any(destination.iterdir()):
        raise SourceRefused("The dashboard destination must be empty")
    url = require_s3_url(archive_url())
    opener = urllib.request.build_opener(S3Redirects())
    with tempfile.TemporaryDirectory(prefix="onlyfans-paired-source-") as temporary:
        archive = Path(temporary) / "source.tar.gz"
        measured = hashlib.sha256()
        length = 0
        deadline = time.monotonic() + 180
        with opener.open(url, timeout=60) as response, archive.open("wb") as handle:
            claimed = response.headers.get("Content-Length")
            if claimed and int(claimed) > MAX_DOWNLOAD:
                raise SourceRefused("Dashboard source archive exceeds its download limit")
            while block := response.read(1024 * 1024):
                if time.monotonic() > deadline:
                    raise SourceRefused("Dashboard source download exceeded its time limit")
                length += len(block)
                if length > MAX_DOWNLOAD:
                    raise SourceRefused("Dashboard source archive exceeds its download limit")
                measured.update(block)
                handle.write(block)
        if measured.hexdigest() != digest:
            raise SourceRefused("Dashboard source archive SHA256 mismatch")
        # Bound every decompressed byte before tarfile can parse global PAX
        # metadata, extension headers or padding, not just file payload sizes.
        raw_archive = Path(temporary) / "source.tar"
        expanded = 0
        with gzip.open(archive, "rb") as compressed, raw_archive.open("wb") as handle:
            while block := compressed.read(1024 * 1024):
                if time.monotonic() > deadline:
                    raise SourceRefused("Dashboard source expansion exceeded its time limit")
                expanded += len(block)
                if expanded > MAX_UNPACKED:
                    raise SourceRefused("Dashboard archive exceeds its expanded tar size limit")
                handle.write(block)
        with tarfile.open(raw_archive, "r:") as source:
            members, unpacked = [], 0
            for member in source:
                members.append(member)
                unpacked += member.size
                if len(members) > MAX_FILES or unpacked > MAX_UNPACKED:
                    raise SourceRefused("Dashboard archive exceeds its unpacked size/file limits")
            if source.pax_headers.get("comment") != sha:
                raise SourceRefused("The git-archive commit proof differs from the pinned dashboard SHA")
            if not members:
                raise SourceRefused("Dashboard archive exceeds its unpacked size/file limits")
            names = set()
            for member in members:
                path = PurePosixPath(member.name)
                if (path.is_absolute() or ".." in path.parts or "\\" in member.name or "\x00" in member.name
                        or not path.parts or path.parts[0] in {".git", "node_modules"}
                        or path.as_posix() == MARKER or path.as_posix() in names
                        or not (member.isdir() or member.isfile())):
                    raise SourceRefused("Dashboard archive contains an unsafe or duplicate entry")
                names.add(path.as_posix())
            required = {"package.json", "package-lock.json", "preview/onlyfans-integration/package.json",
                        "scripts/check-onlyfans-http-browser.mjs"}
            if not required.issubset(names):
                raise SourceRefused("The archive is not a complete rooted dashboard checkout")
            destination.mkdir(parents=True, exist_ok=True)
            files = {}
            for member in members:
                target = destination / member.name
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                stream = source.extractfile(member)
                if stream is None:
                    raise SourceRefused("Dashboard archive file could not be read")
                data = stream.read(MAX_UNPACKED + 1)
                if len(data) != member.size:
                    raise SourceRefused("Dashboard archive file length is invalid")
                target.write_bytes(data)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
                files[member.name] = {"sha256": hashlib.sha256(data).hexdigest(), "executable": bool(member.mode & 0o111)}
        proof = {"schemaVersion": "of-acceptance-source.v1", "commit": sha, "archiveSha256": digest,
                 "archiveBytes": length, "files": files}
        (destination / MARKER).write_text(json.dumps(proof, sort_keys=True, indent=2) + "\n")
        return proof


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dashboard-ref", default="")
    parser.add_argument("--archive-sha256", default="")
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    try:
        mode, sha = select_source(args.dashboard_ref, args.archive_sha256, args.github_output)
        if args.destination:
            if mode != "archive":
                raise SourceRefused("Git source checkout must use the scoped actions/checkout path")
            proof = extract_archive(args.destination.resolve(), sha, args.archive_sha256)
            print(f"Verified paired dashboard {sha}: {len(proof['files'])} source files; archive SHA256 {args.archive_sha256}")
        else:
            print(f"Pinned dashboard {sha}; source capability: {mode}")
        return 0
    except Exception as error:
        # urllib errors can contain the presigned URL. Report a classified,
        # sanitized failure, never the exception's arbitrary external text.
        if isinstance(error, SourceRefused):
            print(f"Paired dashboard preparation refused: {error}")
        else:
            print(f"Paired dashboard preparation failed ({type(error).__name__}); signed URL omitted")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
