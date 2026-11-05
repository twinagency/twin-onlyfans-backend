"""Shared storyboard cache and frame extraction for the relay and vault worker."""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger("of-relay.vault_storyboard")

# Configurable cache root. /tmp is fine on macOS (cleared at boot, so
# eviction handles itself). In Docker /tmp is per-container and goes
# away on restart — point STORYBOARD_DIR at a mounted volume in prod
# so storyboards survive across container restarts.
STORYBOARD_DIR = Path(os.environ.get("STORYBOARD_DIR", "/tmp/of-relay-storyboard"))
STORYBOARD_FRAMES = 12
STORYBOARD_WIDTH = 400
# Drop from 90s → 30s. Past 30s Next dev's HTTP proxy and most browsers
# have already given up; holding the uvicorn worker thread any longer
# just leaks the slot for nothing. The client retries naturally on the
# next hover cycle so failing fast is cheaper than queueing zombies.
STORYBOARD_BUILD_TIMEOUT_S = 30


def video_path_hash(u: str) -> str:
    """SHA-1 of host+path (signature stripped, account-agnostic). Same
    physical video → same hash → same on-disk frames, regardless of
    which account fetched it or how OF signed the URL today."""
    parsed = urlparse(u)
    base = (parsed.hostname or "").lower() + (parsed.path or "")
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:20]


ALL_FRAME_INDICES = tuple(range(STORYBOARD_FRAMES))


def frame_path(h: str, i: int) -> Path:
    return STORYBOARD_DIR / h / f"{i}.jpg"


def all_frames_present(h: str) -> bool:
    return all(frame_path(h, i).is_file() for i in ALL_FRAME_INDICES)


def extract_batch(
    src_path: Path, dest: Path, indices: tuple[int, ...], dur: float,
) -> bool:
    """Extract all `indices` frames in a SINGLE ffmpeg invocation, using
    repeated `-ss TIME -i SRC -frames:v 1 OUT` blocks. ffmpeg reopens the
    file per `-i` (keyframe seek is fast on local disk) but the process
    spawn happens only once per batch — cuts pass-1 latency by ~4x for a
    4-frame batch vs. one subprocess per frame. Frames write directly to
    their final 0-based name.

    Tolerates partial success: if ffmpeg returns non-zero (common when
    the final frame's seek lands at/past EOF on short clips with
    rounded-up duration metadata) but at least one jpg landed on disk,
    we treat the batch as good. Returns False only if zero frames came
    out — that's the case where the source mp4 is unreadable or the
    timeout fired."""
    import subprocess
    # Stay at least 100ms before the encoded EOF — ffmpeg can't seek past
    # the last decodable frame, and short clips whose container metadata
    # rounds the duration up (e.g. "4.0s" for a 3.93s file) would otherwise
    # blow up on the last index.
    safe_dur = max(0.1, dur - 0.1)
    args: list[str] = ["ffmpeg", "-y", "-nostdin", "-loglevel", "error"]
    for pos, i in enumerate(indices):
        # Mid-segment seek (i + 0.5) lands away from cut boundaries where
        # an I-frame might be black or a hard transition.
        ts = (i + 0.5) * dur / STORYBOARD_FRAMES
        ts = min(ts, safe_dur)
        out_path = dest / f"{i}.jpg"
        args += [
            "-ss", f"{ts:.4f}", "-i", str(src_path),
            "-map", f"{pos}:v:0",
            "-frames:v", "1",
            "-vf", f"scale={STORYBOARD_WIDTH}:-2",
            "-q:v", "5",
            str(out_path),
        ]
    rc = -1
    try:
        proc = subprocess.run(
            args, check=False, capture_output=True,
            timeout=STORYBOARD_BUILD_TIMEOUT_S,
        )
        rc = proc.returncode
    except Exception:
        log.warning(
            "storyboard batch extract crashed for %s indices=%s",
            dest.name, indices, exc_info=True,
        )

    landed = [i for i in indices if (dest / f"{i}.jpg").is_file()]
    if not landed:
        log.warning(
            "storyboard batch extract produced no frames for %s "
            "(rc=%s indices=%s dur=%.3f)",
            dest.name, rc, indices, dur,
        )
        return False
    if len(landed) < len(indices):
        # Backfill missing indices by copying the nearest neighbour so the
        # storyboard is always complete-shaped after a build. Without this
        # any missing frame would trigger a fresh download+extract on the
        # next request and fail the same way — an infinite loop.
        import shutil
        for i in indices:
            if (dest / f"{i}.jpg").is_file():
                continue
            nearest = min(landed, key=lambda j: abs(j - i))
            try:
                shutil.copyfile(dest / f"{nearest}.jpg", dest / f"{i}.jpg")
            except Exception:
                log.warning(
                    "storyboard backfill copy %s→%s failed for %s",
                    nearest, i, dest.name, exc_info=True,
                )
        log.info(
            "storyboard partial extract for %s: %d/%d frames (rc=%s dur=%.3f, backfilled rest)",
            dest.name, len(landed), len(indices), rc, dur,
        )
    return True
