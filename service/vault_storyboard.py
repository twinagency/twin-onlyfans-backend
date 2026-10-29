"""Local video-frame extraction shared by vault analysis workers.

This module has no HTTP app, scheduler, credentials or database startup.
Only complete sets of genuinely sampled frames are reusable for analysis.
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
from pathlib import Path
import subprocess
from urllib.parse import urlparse

log = logging.getLogger("of-relay.vault_storyboard")
_STORYBOARD_DIR = Path(os.environ.get("STORYBOARD_DIR", "/tmp/of-relay-storyboard"))
_STORYBOARD_FRAMES = 12
_STORYBOARD_WIDTH = 400
_STORYBOARD_BUILD_TIMEOUT_S = 30
_ALL_FRAME_INDICES = tuple(range(_STORYBOARD_FRAMES))
_COMPLETE = ".sampled-v1"


def _video_path_hash(url: str) -> str:
    parsed = urlparse(url)
    key = (parsed.hostname or "").lower() + (parsed.path or "")
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]


def _storyboard_frame_path(key: str, index: int) -> Path:
    return _STORYBOARD_DIR / key / f"{index}.jpg"


def _storyboard_all_frames_present(key: str) -> bool:
    return ((_STORYBOARD_DIR / key / _COMPLETE).is_file()
            and all(_storyboard_frame_path(key, i).is_file()
                    and _storyboard_frame_path(key, i).stat().st_size > 0
                    for i in _ALL_FRAME_INDICES))


def _storyboard_probe_duration(source: Path) -> float:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(source)],
            capture_output=True, text=True, timeout=10, check=True)
        duration = float(result.stdout.strip())
        return duration if math.isfinite(duration) and duration > 0 else 0.0
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def _storyboard_extract_batch(
        source: Path, destination: Path, indices: tuple[int, ...],
        duration: float) -> bool:
    if (not source.is_file() or not indices or len(set(indices)) != len(indices)
            or any(type(i) is not int or i not in _ALL_FRAME_INDICES for i in indices)):
        return False
    # The file's measured duration wins over stale or rounded provider metadata.
    measured = _storyboard_probe_duration(source)
    duration = measured or duration
    if not math.isfinite(duration) or duration <= 0:
        return False
    destination.mkdir(parents=True, exist_ok=True)
    marker = destination / _COMPLETE
    marker.unlink(missing_ok=True)
    for i in indices:
        (destination / f"{i}.jpg").unlink(missing_ok=True)
    command = ["ffmpeg", "-y", "-nostdin", "-loglevel", "error",
               "-filter_threads", "1", "-filter_complex_threads", "1"]
    for position, index in enumerate(indices):
        timestamp = min((index + 0.5) * duration / _STORYBOARD_FRAMES,
                        max(0.0, duration - 0.1))
        command += ["-ss", f"{timestamp:.4f}", "-threads", "1", "-i", str(source),
                    "-map", f"{position}:v:0", "-frames:v", "1",
                    "-vf", f"scale={_STORYBOARD_WIDTH}:-2", "-threads", "1",
                    "-q:v", "5", str(destination / f"{index}.jpg")]
    try:
        subprocess.run(command, capture_output=True, check=False,
                       timeout=_STORYBOARD_BUILD_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return False
    complete = all((destination / f"{i}.jpg").is_file()
                   and (destination / f"{i}.jpg").stat().st_size > 0 for i in indices)
    # Never manufacture missing scenes by copying neighbouring frames.
    if complete and set(indices) == set(_ALL_FRAME_INDICES):
        marker.write_text("sampled-v1\n")
    return complete
