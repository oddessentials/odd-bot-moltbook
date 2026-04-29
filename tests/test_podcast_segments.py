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
    """Covers the manifest-as-source-of-truth contract + the boundary
    sandbox guard. All tests patch REPO_ROOT (in segments) and
    EPISODES_DIR (in manifest) so fixtures live under a tempdir-rooted
    fake repo without writing to data/episodes/ in the real working
    tree.
    """

    EPISODE_ID = "ep-test"

    def _seed(self, td: Path, segments: list[dict]) -> Path:
        path = td / "manifest.json"
        write_manifest(path, {"id": self.EPISODE_ID, "segments": segments})
        return path

    def _seg(self, **overrides) -> dict:
        seg = {
            "idx": 0,
            "audio_status": "complete",
            "clip_status": "complete",
            "audio_path": f"data/episodes/{self.EPISODE_ID}/audio/seg00.mp3",
            "clip_path": f"data/episodes/{self.EPISODE_ID}/clips/seg00.mp4",
        }
        seg.update(overrides)
        return seg

    def _patch_repo(self, tdp: Path):
        """Stack of patches that re-roots REPO_ROOT + EPISODES_DIR under
        the test's tempdir. Returns a context manager."""
        from src.podcast import manifest as manifest_module
        return mock.patch.multiple(
            segments_module, REPO_ROOT=tdp,
        ), mock.patch.multiple(
            manifest_module, EPISODES_DIR=tdp / "data" / "episodes",
        )

    def _layout_recorded(self, tdp: Path, audio_rel: str, clip_rel: str) -> tuple[Path, Path]:
        audio_abs = tdp / audio_rel
        clip_abs = tdp / clip_rel
        audio_abs.parent.mkdir(parents=True, exist_ok=True)
        clip_abs.parent.mkdir(parents=True, exist_ok=True)
        audio_abs.write_bytes(b"audio")
        clip_abs.write_bytes(b"clip")
        return audio_abs, clip_abs

    def _call(self, *, tdp: Path, mpath: Path, seg: dict) -> bool:
        repo_patch, episodes_patch = self._patch_repo(tdp)
        with repo_patch, episodes_patch:
            return is_segment_complete_and_valid(
                manifest_path=mpath,
                episode_id=self.EPISODE_ID,
                seg=seg,
                idx=0,
            )

    # ---- status / path-presence preconditions ----

    def test_returns_false_when_audio_status_not_complete(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            mpath = self._seed(tdp, [self._seg(audio_status="pending")])
            self.assertFalse(
                self._call(tdp=tdp, mpath=mpath, seg=self._seg(audio_status="pending"))
            )

    def test_returns_false_when_clip_status_not_complete(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            mpath = self._seed(tdp, [self._seg(clip_status="pending")])
            self.assertFalse(
                self._call(tdp=tdp, mpath=mpath, seg=self._seg(clip_status="pending"))
            )

    def test_returns_false_when_manifest_audio_path_missing(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            mpath = self._seed(tdp, [self._seg(audio_path=None)])
            self.assertFalse(
                self._call(tdp=tdp, mpath=mpath, seg=self._seg(audio_path=None))
            )

    def test_returns_false_when_recorded_audio_does_not_exist(self):
        # Recorded path is INSIDE the boundary but no file is there. Helper
        # returns False without mutating the manifest — caller's re-render
        # will overwrite the path naturally.
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            seg = self._seg(
                audio_path=f"data/episodes/{self.EPISODE_ID}/audio/missing.mp3",
            )
            mpath = self._seed(tdp, [seg])
            self.assertFalse(self._call(tdp=tdp, mpath=mpath, seg=seg))

    # ---- manifest is source of truth ----

    def test_validates_at_manifest_recorded_paths_not_convention(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            audio_rel = f"data/episodes/{self.EPISODE_ID}/audio/recorded.mp3"
            clip_rel = f"data/episodes/{self.EPISODE_ID}/clips/recorded.mp4"
            audio_abs, clip_abs = self._layout_recorded(tdp, audio_rel, clip_rel)

            seg = self._seg(audio_path=audio_rel, clip_path=clip_rel)
            mpath = self._seed(tdp, [seg])

            captured: list[tuple[Path, Path]] = []

            def fake_validate(audio_path: Path, clip_path: Path) -> None:
                captured.append((audio_path, clip_path))

            repo_patch, episodes_patch = self._patch_repo(tdp)
            with repo_patch, episodes_patch, \
                 mock.patch.object(segments_module, "validate_segment_outputs", side_effect=fake_validate):
                self.assertTrue(
                    is_segment_complete_and_valid(
                        manifest_path=mpath,
                        episode_id=self.EPISODE_ID,
                        seg=seg,
                        idx=0,
                    )
                )
            self.assertEqual(len(captured), 1)
            audio_arg, clip_arg = captured[0]
            self.assertEqual(audio_arg.resolve(), audio_abs.resolve())
            self.assertEqual(clip_arg.resolve(), clip_abs.resolve())

    def test_failed_revalidation_resets_status_and_returns_false(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            audio_rel = f"data/episodes/{self.EPISODE_ID}/audio/seg.mp3"
            clip_rel = f"data/episodes/{self.EPISODE_ID}/clips/seg.mp4"
            self._layout_recorded(tdp, audio_rel, clip_rel)
            seg = self._seg(audio_path=audio_rel, clip_path=clip_rel)
            mpath = self._seed(tdp, [seg])

            def fake_validate(audio_path, clip_path):
                raise SegmentValidationError("simulated bad clip")

            repo_patch, episodes_patch = self._patch_repo(tdp)
            with repo_patch, episodes_patch, \
                 mock.patch.object(segments_module, "validate_segment_outputs", side_effect=fake_validate):
                result = is_segment_complete_and_valid(
                    manifest_path=mpath,
                    episode_id=self.EPISODE_ID,
                    seg=seg,
                    idx=0,
                )
            self.assertFalse(result)
            updated = json.loads(mpath.read_text())["segments"][0]
            self.assertEqual(updated["audio_status"], "pending")
            self.assertEqual(updated["clip_status"], "pending")

    # ---- boundary sandbox guard ----

    def _assert_escape_refused(self, tdp: Path, mpath: Path, seg: dict) -> None:
        # Even if the escape target literally exists on disk, validate
        # must not run. Plant a real file at the escape destination so
        # the test fails loudly if the guard is missing.
        repo_patch, episodes_patch = self._patch_repo(tdp)
        validate_called = False

        def fake_validate(*a, **kw):
            nonlocal validate_called
            validate_called = True

        with repo_patch, episodes_patch, \
             mock.patch.object(segments_module, "validate_segment_outputs", side_effect=fake_validate):
            result = is_segment_complete_and_valid(
                manifest_path=mpath,
                episode_id=self.EPISODE_ID,
                seg=seg,
                idx=0,
            )
        self.assertFalse(result)
        self.assertFalse(validate_called, "validate_segment_outputs ran on an escape path")
        # Status must be reset so the caller re-renders at convention paths
        # and overwrites the bogus manifest entries.
        updated = json.loads(mpath.read_text())["segments"][0]
        self.assertEqual(updated["audio_status"], "pending")
        self.assertEqual(updated["clip_status"], "pending")

    def test_dotdot_path_refuses_to_validate(self):
        # A hand-edited manifest tries to point at a sibling repo path.
        # Plant a real .mp3 + .mp4 there to prove the refusal isn't just
        # "file doesn't exist."
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            outside_dir = tdp / "outside"
            outside_dir.mkdir()
            (outside_dir / "evil.mp3").write_bytes(b"audio")
            (outside_dir / "evil.mp4").write_bytes(b"clip")

            # Recorded path uses ".." to escape data/episodes/ep-test/
            seg = self._seg(
                audio_path=f"data/episodes/{self.EPISODE_ID}/../../../outside/evil.mp3",
                clip_path=f"data/episodes/{self.EPISODE_ID}/../../../outside/evil.mp4",
            )
            mpath = self._seed(tdp, [seg])
            self._assert_escape_refused(tdp, mpath, seg)

    def test_absolute_path_refuses_to_validate(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            absolute_audio = tdp / "absolute_audio.mp3"
            absolute_clip = tdp / "absolute_clip.mp4"
            absolute_audio.write_bytes(b"audio")
            absolute_clip.write_bytes(b"clip")

            seg = self._seg(
                audio_path=str(absolute_audio),
                clip_path=str(absolute_clip),
            )
            mpath = self._seed(tdp, [seg])
            self._assert_escape_refused(tdp, mpath, seg)

    def test_symlink_escape_refuses_to_validate(self):
        # A symlink inside the episode dir pointing outside. resolve()
        # follows the symlink, so the resolved path lands outside the
        # boundary and the guard refuses.
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            outside_audio = tdp / "outside_audio.mp3"
            outside_clip = tdp / "outside_clip.mp4"
            outside_audio.write_bytes(b"audio")
            outside_clip.write_bytes(b"clip")

            ep_audio_dir = tdp / "data" / "episodes" / self.EPISODE_ID / "audio"
            ep_clip_dir = tdp / "data" / "episodes" / self.EPISODE_ID / "clips"
            ep_audio_dir.mkdir(parents=True)
            ep_clip_dir.mkdir(parents=True)
            (ep_audio_dir / "seg00.mp3").symlink_to(outside_audio)
            (ep_clip_dir / "seg00.mp4").symlink_to(outside_clip)

            seg = self._seg()  # default convention paths
            mpath = self._seed(tdp, [seg])
            self._assert_escape_refused(tdp, mpath, seg)


if __name__ == "__main__":
    unittest.main()
