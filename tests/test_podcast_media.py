"""Unit tests for src.podcast.media.

format_srt_timestamp gets pure-function coverage. generate_srt gets a
boundary-derivation test that proves the SRT is written next to the
manifest (operator-supplied filesystem location), not under
episode_dir(manifest["id"]) — same trust correction applied to
process_segment + stitch_episode.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.podcast import media as media_module
from src.podcast.manifest import write_manifest
from src.podcast.media import format_srt_timestamp, generate_srt


class TestFormatSrtTimestamp(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(format_srt_timestamp(0.0), "00:00:00,000")

    def test_negative_clamps_to_zero(self):
        self.assertEqual(format_srt_timestamp(-1.5), "00:00:00,000")

    def test_sub_second(self):
        self.assertEqual(format_srt_timestamp(0.5), "00:00:00,500")

    def test_one_second(self):
        self.assertEqual(format_srt_timestamp(1.0), "00:00:01,000")

    def test_minute_boundary(self):
        self.assertEqual(format_srt_timestamp(60.0), "00:01:00,000")

    def test_hour_boundary(self):
        self.assertEqual(format_srt_timestamp(3600.0), "01:00:00,000")

    def test_mixed_h_m_s_ms(self):
        # 1h 2m 3s 456ms.
        self.assertEqual(format_srt_timestamp(3723.456), "01:02:03,456")

    def test_milliseconds_clamped_to_999(self):
        # 1.9999... rounds to 1000ms. The clamp prevents a malformed
        # SRT timestamp like 00:00:01,1000.
        self.assertEqual(format_srt_timestamp(1.9999999), "00:00:01,999")

    def test_typical_segment_end(self):
        # 14.875s — the duration ffprobe reported for ep-001 seg00.
        self.assertEqual(format_srt_timestamp(14.875), "00:00:14,875")


class TestGenerateSrtWritePath(unittest.TestCase):
    def test_writes_at_manifest_parent_not_manifest_id(self):
        # Manifest sits at data/episodes/ep-test/manifest.json. The
        # mutable manifest["id"] field is tampered to "../malicious".
        # Output SRT must land in ep-test/, not under
        # episode_dir("../malicious").
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            ep_dir = tdp / "data" / "episodes" / "ep-test"
            audio_dir = ep_dir / "audio"
            audio_dir.mkdir(parents=True)
            for i in range(2):
                (audio_dir / f"seg{i:02d}.mp3").write_bytes(b"audio")

            mpath = ep_dir / "manifest.json"
            write_manifest(
                mpath,
                {
                    "id": "../malicious",
                    "segments": [
                        {
                            "audio_status": "complete",
                            "audio_path": f"data/episodes/ep-test/audio/seg{i:02d}.mp3",
                            "text": f"line {i}",
                        }
                        for i in range(2)
                    ],
                },
            )

            def fake_probe(_path):
                return {"format": {"duration": "3.5"}, "streams": []}

            from src.podcast import manifest as manifest_module

            with mock.patch.object(manifest_module, "REPO_ROOT", tdp), \
                 mock.patch.object(media_module, "ffprobe_streams", side_effect=fake_probe):
                srt_path = generate_srt(manifest_path=mpath)

            self.assertEqual(srt_path, ep_dir / "captions.srt")
            self.assertTrue(srt_path.is_file())
            # Confirm nothing was written under the malicious path.
            self.assertFalse((tdp / "data" / "episodes" / "..").exists() and (tdp / "malicious").exists())


if __name__ == "__main__":
    unittest.main()
