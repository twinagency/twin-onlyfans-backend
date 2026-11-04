import asyncio
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_worker_collects_video_without_importing_legacy_relay(monkeypatch, tmp_path):
    import vault_frames
    import vault_storyboard

    monkeypatch.delitem(sys.modules, "server", raising=False)
    monkeypatch.setenv("ONLYFANS_PROCESS_ROLE", "worker")
    monkeypatch.setattr(vault_storyboard, "STORYBOARD_DIR", tmp_path / "storyboards")

    clip = tmp_path / "synthetic.mp4"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
         "color=c=blue:s=96x96:r=24", "-t", "2", "-c:v", "mpeg4", str(clip)],
        check=True, timeout=15,
    )
    url = "https://example.invalid/synthetic.mp4?signature=one"
    media = {"duration": 2, "videoSources": {"240": url}}

    class Response:
        status_code = 200

        def iter_content(self, *, chunk_size):
            return (clip.read_bytes(),)

        def close(self):
            pass

    client = SimpleNamespace(
        timeout_s=1, http=SimpleNamespace(get=lambda *args, **kwargs: Response()),
    )

    async def fresh(*args):
        return SimpleNamespace(media=media, reason="ok")

    monkeypatch.setattr(vault_frames.ax, "_make_client", lambda account: client)
    monkeypatch.setattr(vault_frames.vault_stills, "resolve_fresh", fresh)
    item = SimpleNamespace(media_id=1, kind="video")

    result = asyncio.run(vault_frames.collect("of-test", item, media, want=9))

    assert (result.reason, result.source, len(result.images)) == ("ok", "storyboard", 9)
    assert vault_frames.storyboard_frames(url.replace("one", "two"), 9) == result.images
    assert "server" not in sys.modules
