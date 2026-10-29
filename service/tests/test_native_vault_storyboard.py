"""Regression coverage for video collection inside dedicated workers."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import vault_storyboard as storyboard


class StoryboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patch = patch.object(storyboard, "_STORYBOARD_DIR", self.root)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_signature_refresh_keeps_cache_key(self):
        self.assertEqual(
            storyboard._video_path_hash("https://media.example/a.mp4?signature=old"),
            storyboard._video_path_hash("https://media.example/a.mp4?signature=new"))

    def test_different_media_do_not_share_key(self):
        self.assertNotEqual(storyboard._video_path_hash("https://media.example/a.mp4"),
                            storyboard._video_path_hash("https://media.example/b.mp4"))

    def test_old_or_partial_cache_is_not_claimed_complete(self):
        key = "example"
        dest = self.root / key
        dest.mkdir()
        for i in storyboard._ALL_FRAME_INDICES:
            (dest / f"{i}.jpg").write_bytes(b"frame")
        self.assertFalse(storyboard._storyboard_all_frames_present(key))
        (dest / storyboard._COMPLETE).write_text("sampled-v1\n")
        self.assertTrue(storyboard._storyboard_all_frames_present(key))
        (dest / "5.jpg").unlink()
        self.assertFalse(storyboard._storyboard_all_frames_present(key))

    def test_partial_decode_does_not_invent_missing_frames(self):
        source = self.root / "source.mp4"
        source.write_bytes(b"fixture")
        dest = self.root / "frames"
        def partial(*args, **kwargs):
            (dest / "0.jpg").write_bytes(b"one actual frame")
            return SimpleNamespace(returncode=1)
        with patch.object(storyboard, "_storyboard_probe_duration", return_value=2), \
                patch.object(storyboard.subprocess, "run", side_effect=partial):
            result = storyboard._storyboard_extract_batch(
                source, dest, storyboard._ALL_FRAME_INDICES, 2)
        self.assertFalse(result)
        self.assertFalse((dest / storyboard._COMPLETE).exists())
        self.assertEqual(len(list(dest.glob("*.jpg"))), 1)

    def test_timeout_is_incomplete(self):
        source = self.root / "source.mp4"
        source.write_bytes(b"fixture")
        with patch.object(storyboard, "_storyboard_probe_duration", return_value=2), \
                patch.object(storyboard.subprocess, "run",
                             side_effect=subprocess.TimeoutExpired("ffmpeg", 30)):
            self.assertFalse(storyboard._storyboard_extract_batch(
                source, self.root / "frames", storyboard._ALL_FRAME_INDICES, 2))

    def test_missing_source_is_rejected(self):
        self.assertFalse(storyboard._storyboard_extract_batch(
            self.root / "missing", self.root / "frames", (0,), 2))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                         "ffmpeg and ffprobe required")
    def test_real_video_is_sampled_across_its_measured_duration(self):
        source = self.root / "fixture.mp4"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                        "-i", "testsrc2=size=96x64:rate=12:duration=2",
                        "-threads", "1", "-c:v", "mpeg4", str(source)],
                       check=True, capture_output=True, timeout=20)
        dest = self.root / "video"
        self.assertTrue(storyboard._storyboard_extract_batch(
            source, dest, storyboard._ALL_FRAME_INDICES, 999))
        self.assertTrue(storyboard._storyboard_all_frames_present("video"))
        self.assertNotEqual((dest / "0.jpg").read_bytes(), (dest / "11.jpg").read_bytes())

    def test_worker_can_check_video_cache_without_legacy_server(self):
        env = dict(os.environ, ONLYFANS_PROCESS_ROLE="worker",
                   STORYBOARD_DIR=str(self.root / "empty"),
                   DATABASE_URL="postgresql://test:test@127.0.0.1:1/test")
        result = subprocess.run(
            [sys.executable, "-c",
             "import sys, vault_frames; "
             "assert vault_frames.storyboard_frames('https://media.example/a.mp4', 9) == []; "
             "assert 'server' not in sys.modules"],
            cwd=Path(__file__).resolve().parents[1], env=env,
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
