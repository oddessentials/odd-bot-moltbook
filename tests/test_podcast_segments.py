"""Unit tests for src.podcast.segments validation + idempotency contract.

Doesn't exercise the network paths (TTS/Hedra) — those are integration.
What's pinned here is:

  - SegmentValidationError typed-exception contract.
  - is_segment_complete_and_valid: manifest is source of truth for
    artifact paths; on-disk files at convention locations are not
    consulted unless the manifest happens to point there.

A failed re-validation must reset audio_status / clip_status to
"pending" so the caller re-renders rather than registering a fake skip.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.podcast import segments as segments_module
from src.podcast.manifest import write_manifest
from src.podcast.segments import (
    SegmentValidationError,
    is_segment_complete_and_valid,
    validate_segment_outputs,
)


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


class TestIsSegmentCompleteAndValid(unittest.TestCase):
    def _seed(self, td: Path, segments: list[dict]) -> Path:
        path = td / "manifest.json"
        write_manifest(path, {"id": "ep-test", "segments": segments})
        return path

    def _seg(self, **overrides) -> dict:
        seg = {
            "idx": 0,
            "audio_status": "complete",
            "clip_status": "complete",
            "audio_path": "data/episodes/ep-test/audio/seg00.mp3",
            "clip_path": "data/episodes/ep-test/clips/seg00.mp4",
        }
        seg.update(overrides)
        return seg

    def test_returns_false_when_audio_status_not_complete(self):
        with tempfile.TemporaryDirectory() as td:
            mpath = self._seed(Path(td), [self._seg(audio_status="pending")])
            self.assertFalse(
                is_segment_complete_and_valid(
                    manifest_path=mpath,
                    seg=self._seg(audio_status="pending"),
                    idx=0,
                )
            )

    def test_returns_false_when_clip_status_not_complete(self):
        with tempfile.TemporaryDirectory() as td:
            mpath = self._seed(Path(td), [self._seg(clip_status="pending")])
            self.assertFalse(
                is_segment_complete_and_valid(
                    manifest_path=mpath,
                    seg=self._seg(clip_status="pending"),
                    idx=0,
                )
            )

    def test_returns_false_when_manifest_audio_path_missing(self):
        # The load-bearing case for Codex flag #3: the manifest is the
        # source of truth. If it doesn't record an audio_path despite
        # claiming complete, that's a corrupt manifest entry and we must
        # NOT silently skip on the assumption that a convention-path
        # file might exist.
        with tempfile.TemporaryDirectory() as td:
            mpath = self._seed(Path(td), [self._seg(audio_path=None)])
            self.assertFalse(
                is_segment_complete_and_valid(
                    manifest_path=mpath,
                    seg=self._seg(audio_path=None),
                    idx=0,
                )
            )

    def test_returns_false_when_recorded_audio_does_not_exist(self):
        # Manifest claims a file at a specific path; the file isn't there.
        # Convention-path lookup must NOT be substituted in.
        with tempfile.TemporaryDirectory() as td:
            mpath = self._seed(
                Path(td),
                [self._seg(audio_path="not/on/disk.mp3")],
            )
            self.assertFalse(
                is_segment_complete_and_valid(
                    manifest_path=mpath,
                    seg=self._seg(audio_path="not/on/disk.mp3"),
                    idx=0,
                )
            )

    def test_validates_at_manifest_recorded_paths_not_convention(self):
        # Two real files exist: a "manifest-recorded" path and a separate
        # "convention" path. The helper must validate the recorded one
        # specifically; otherwise an out-of-band rename or hand-edit
        # could end up validating the wrong file.
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            recorded_audio = tdp / "recorded.mp3"
            recorded_audio.write_bytes(b"recorded-audio-bytes")
            recorded_clip = tdp / "recorded.mp4"
            recorded_clip.write_bytes(b"recorded-clip-bytes")

            seg = self._seg(
                audio_path=str(recorded_audio.relative_to(tdp)),
                clip_path=str(recorded_clip.relative_to(tdp)),
            )
            mpath = self._seed(tdp, [seg])

            captured: list[tuple[Path, Path]] = []

            def fake_validate(audio_path: Path, clip_path: Path) -> None:
                captured.append((audio_path, clip_path))
                # Pass — no raise.

            # Patch REPO_ROOT to the temp dir so relative paths resolve
            # under the test fixture.
            with mock.patch.object(segments_module, "REPO_ROOT", tdp), \
                 mock.patch.object(segments_module, "validate_segment_outputs", side_effect=fake_validate):
                self.assertTrue(
                    is_segment_complete_and_valid(
                        manifest_path=mpath, seg=seg, idx=0,
                    )
                )
            self.assertEqual(len(captured), 1)
            audio_arg, clip_arg = captured[0]
            self.assertEqual(audio_arg.resolve(), recorded_audio.resolve())
            self.assertEqual(clip_arg.resolve(), recorded_clip.resolve())

    def test_failed_revalidation_resets_status_and_returns_false(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            audio = tdp / "audio.mp3"
            audio.write_bytes(b"a")
            clip = tdp / "clip.mp4"
            clip.write_bytes(b"c")
            seg = self._seg(
                audio_path=str(audio.relative_to(tdp)),
                clip_path=str(clip.relative_to(tdp)),
            )
            mpath = self._seed(tdp, [seg])

            def fake_validate(audio_path, clip_path):
                raise SegmentValidationError("simulated bad clip")

            with mock.patch.object(segments_module, "REPO_ROOT", tdp), \
                 mock.patch.object(segments_module, "validate_segment_outputs", side_effect=fake_validate):
                result = is_segment_complete_and_valid(
                    manifest_path=mpath, seg=seg, idx=0,
                )
            self.assertFalse(result)

            # Manifest must have its statuses rolled back so the caller
            # falls through to re-render.
            updated = json.loads(mpath.read_text())["segments"][0]
            self.assertEqual(updated["audio_status"], "pending")
            self.assertEqual(updated["clip_status"], "pending")


if __name__ == "__main__":
    unittest.main()
