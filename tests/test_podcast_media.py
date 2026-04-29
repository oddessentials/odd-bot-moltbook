"""Unit tests for src.podcast.media.format_srt_timestamp.

ffprobe / ffmpeg / generate_srt themselves require real binaries +
fixture media; those are integration concerns. The pure timestamp
formatter is the load-bearing helper that determines how cumulative
audio durations render into the YouTube caption track, and it's worth
guarding against hour-boundary, sub-second, and millisecond-rounding
regressions.
"""

from __future__ import annotations

import unittest

from src.podcast.media import format_srt_timestamp


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


if __name__ == "__main__":
    unittest.main()
