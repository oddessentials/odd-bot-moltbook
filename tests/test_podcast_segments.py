"""Unit tests for src.podcast.segments validation contract.

Doesn't exercise the network paths (TTS/Hedra) — those are integration.
What's pinned here is the SegmentValidationError contract that the
process_segment idempotency-skip path and the retry wrapper both rely on
to differentiate a bad-artifacts failure from a transient network blip.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.podcast.segments import SegmentValidationError, validate_segment_outputs


class TestSegmentValidationError(unittest.TestCase):
    def test_is_runtime_error_subclass(self):
        # Existing callers using `except RuntimeError` keep working — the
        # typed subclass is purely additive.
        self.assertTrue(issubclass(SegmentValidationError, RuntimeError))


class TestValidateSegmentOutputs(unittest.TestCase):
    def test_missing_audio_raises_validation_error(self):
        with tempfile.TemporaryDirectory() as td:
            audio = Path(td) / "missing.mp3"
            clip = Path(td) / "missing.mp4"
            with self.assertRaises(SegmentValidationError) as cm:
                validate_segment_outputs(audio, clip)
            self.assertIn("audio missing", str(cm.exception))

    def test_undersized_audio_raises_validation_error(self):
        with tempfile.TemporaryDirectory() as td:
            audio = Path(td) / "tiny.mp3"
            audio.write_bytes(b"x")  # 1 byte, way under TTS_MIN_BYTES
            clip = Path(td) / "missing.mp4"
            with self.assertRaises(SegmentValidationError) as cm:
                validate_segment_outputs(audio, clip)
            self.assertIn("audio too small", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
