"""Unit tests for src.podcast.manifest helpers + atomic_write_text.

Coverage:
  - derive_episode_no: 1 when episodes.json missing, len + 1 otherwise.
  - derive_hosts: anchor-first order, distinct speakers, display-name map.
  - atomic_write_text: writes complete contents; the rename-based atomicity
    is asserted by checking no `.tmp.` sidecar leaks on the happy path.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.podcast.manifest import (
    EpisodeBoundaryError,
    VALIDATION_STATUS_ORDER,
    advance_validation_status,
    atomic_write_text,
    derive_episode_no,
    derive_hosts,
    is_at_or_past,
    resolve_inside_episode,
    write_manifest,
)
from src.podcast.schema import CastConfig, CastMember, EpisodeScript, Segment


def _twelve_words() -> str:
    return " ".join(["w"] * 12)


def _build_cast(slugs: list[str], anchor: str) -> CastConfig:
    members = {
        slug: CastMember(
            display_name=slug.title(),
            role="anchor" if slug == anchor else "guest",
            persona="p",
            elevenlabs_voice_id=f"voice-{slug}",
            hedra_image_asset_id=f"image-{slug}",
        )
        for slug in slugs
    }
    return CastConfig(version=1, anchor=anchor, cast=members)


def _build_script(speakers: list[str]) -> EpisodeScript:
    """Build a minimum-valid EpisodeScript whose segment-speaker order is
    `speakers`. Pads to the 8-segment floor by repeating the last speaker
    so we can exercise derive_hosts on short canonical sequences without
    hitting EpisodeScript's segment-count validator.
    """
    text = _twelve_words()
    padded = list(speakers)
    while len(padded) < 8:
        padded.append(speakers[-1])
    return EpisodeScript(
        title="T",
        description="D" * 100,
        segments=[Segment(speaker=s, text=text) for s in padded],
    )


class TestDeriveEpisodeNo(unittest.TestCase):
    def test_returns_1_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "episodes.json"
            self.assertEqual(derive_episode_no(path), 1)

    def test_returns_count_plus_one_when_present(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "episodes.json"
            path.write_text(json.dumps([{"id": "ep-001"}, {"id": "ep-002"}]))
            self.assertEqual(derive_episode_no(path), 3)

    def test_non_list_payload_raises(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "episodes.json"
            path.write_text(json.dumps({"oops": "object not list"}))
            with self.assertRaises(ValueError):
                derive_episode_no(path)


class TestDeriveHosts(unittest.TestCase):
    def test_anchor_first_when_present(self):
        cast = _build_cast(["shrimp", "carl"], anchor="shrimp")
        # Carl speaks first, then Shrimp — derive_hosts must still put
        # the anchor at index 0.
        script = _build_script(["carl", "shrimp", "carl"])
        self.assertEqual(derive_hosts(script, cast), ["Shrimp", "Carl"])

    def test_anchor_only(self):
        cast = _build_cast(["shrimp"], anchor="shrimp")
        script = _build_script(["shrimp"] * 12)
        self.assertEqual(derive_hosts(script, cast), ["Shrimp"])

    def test_anchor_absent_from_speakers(self):
        # Plausible after retries / hand-edits — anchor key in cast but
        # never speaks. derive_hosts must still emit the speakers in
        # insertion order without crashing.
        cast = _build_cast(["shrimp", "carl"], anchor="shrimp")
        script = _build_script(["carl"] * 12)
        self.assertEqual(derive_hosts(script, cast), ["Carl"])

    def test_dedup_preserves_first_occurrence_order(self):
        cast = _build_cast(["shrimp", "carl"], anchor="shrimp")
        script = _build_script(["shrimp", "carl", "shrimp", "carl"])
        self.assertEqual(derive_hosts(script, cast), ["Shrimp", "Carl"])


class TestAdvanceValidationStatus(unittest.TestCase):
    def _seed(self, td: Path, status: str | None) -> Path:
        path = td / "manifest.json"
        write_manifest(path, {"id": "ep-test", "validation_status": status})
        return path

    def test_advance_from_none(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._seed(Path(td), None)
            landed = advance_validation_status(path, "script_generated")
            self.assertEqual(landed, "script_generated")
            with open(path) as f:
                self.assertEqual(json.load(f)["validation_status"], "script_generated")

    def test_advance_forward(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._seed(Path(td), "script_generated")
            landed = advance_validation_status(path, "segments_complete")
            self.assertEqual(landed, "segments_complete")

    def test_does_not_roll_back(self):
        # The load-bearing case: re-running an earlier phase (e.g.,
        # produce-segments after upload completed) must NOT clobber the
        # later phase marker.
        with tempfile.TemporaryDirectory() as td:
            path = self._seed(Path(td), "uploaded")
            landed = advance_validation_status(path, "segments_complete")
            self.assertEqual(landed, "uploaded")
            with open(path) as f:
                self.assertEqual(json.load(f)["validation_status"], "uploaded")

    def test_same_state_is_a_noop(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._seed(Path(td), "stitched")
            landed = advance_validation_status(path, "stitched")
            self.assertEqual(landed, "stitched")

    def test_unknown_target_raises(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._seed(Path(td), "script_generated")
            with self.assertRaises(ValueError):
                advance_validation_status(path, "bogus")

    def test_unknown_current_treated_as_pre_initial(self):
        # An unrecognized current (e.g., a manifest written by a future
        # version of the engine) must not crash the ratchet — treat as
        # pre-initial so any known target moves forward.
        with tempfile.TemporaryDirectory() as td:
            path = self._seed(Path(td), "weird_future_state")
            landed = advance_validation_status(path, "uploaded")
            self.assertEqual(landed, "uploaded")

    def test_validation_status_order_is_canonical(self):
        # Lock the canonical order so reorderings show up as test churn,
        # not silent semantic drift. "published" is the Phase 2 phase
        # marker for "data/episodes.json publish event has occurred".
        self.assertEqual(
            VALIDATION_STATUS_ORDER,
            (
                "script_generated",
                "segments_complete",
                "stitched",
                "video_uploaded",
                "uploaded",
                "published",
            ),
        )


class TestIsAtOrPast(unittest.TestCase):
    def test_published_is_at_or_past_stitched(self):
        # The exact regression Codex caught: re-running cmd_stitch on a
        # published episode used to return False from a hard-coded
        # ("stitched", "video_uploaded", "uploaded") tuple — missing
        # "published" — and would attempt to re-stitch.
        self.assertTrue(is_at_or_past("published", "stitched"))

    def test_published_is_at_or_past_uploaded(self):
        self.assertTrue(is_at_or_past("published", "uploaded"))

    def test_segments_complete_is_not_at_or_past_stitched(self):
        self.assertFalse(is_at_or_past("segments_complete", "stitched"))

    def test_same_state_is_true(self):
        self.assertTrue(is_at_or_past("uploaded", "uploaded"))

    def test_none_current_is_false(self):
        self.assertFalse(is_at_or_past(None, "script_generated"))

    def test_unknown_current_is_false(self):
        # Future engine version writing an unrecognized state must not
        # accidentally short-circuit a phase here.
        self.assertFalse(is_at_or_past("future_phase", "stitched"))

    def test_unknown_target_raises(self):
        with self.assertRaises(ValueError):
            is_at_or_past("uploaded", "bogus_target")


class TestResolveInsideEpisode(unittest.TestCase):
    def _setup(self, td: Path) -> Path:
        ep_dir = td / "data" / "episodes" / "ep-test"
        ep_dir.mkdir(parents=True)
        return ep_dir / "manifest.json"

    def test_newline_in_recorded_rel_rejected(self):
        # The newline could otherwise break a one-directive-per-line
        # format (ffmpeg concat list) into multiple directives that
        # ingest unrelated files.
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            mpath = self._setup(tdp)
            with self.assertRaises(EpisodeBoundaryError) as cm:
                resolve_inside_episode(
                    manifest_path=mpath,
                    recorded_rel="data/episodes/ep-test/audio/seg00.mp3\nfile '/etc/passwd'",
                    repo_root=tdp,
                )
            self.assertIn("newline", str(cm.exception))

    def test_carriage_return_in_recorded_rel_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            mpath = self._setup(tdp)
            with self.assertRaises(EpisodeBoundaryError):
                resolve_inside_episode(
                    manifest_path=mpath,
                    recorded_rel="data/episodes/ep-test/audio/seg00.mp3\r;injected",
                    repo_root=tdp,
                )

    def test_clean_relative_path_inside_boundary_resolves(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            mpath = self._setup(tdp)
            audio = mpath.parent / "audio" / "seg00.mp3"
            audio.parent.mkdir(parents=True)
            audio.write_bytes(b"a")
            resolved = resolve_inside_episode(
                manifest_path=mpath,
                recorded_rel="data/episodes/ep-test/audio/seg00.mp3",
                repo_root=tdp,
            )
            self.assertEqual(resolved.resolve(), audio.resolve())


class TestAtomicWriteText(unittest.TestCase):
    def test_writes_full_contents(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "x" / "out.json"
            atomic_write_text(path, '{"hello":"world"}\n')
            self.assertEqual(path.read_text(), '{"hello":"world"}\n')

    def test_no_temp_sidecar_left_after_success(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "out.json"
            atomic_write_text(path, "ok\n")
            sidecars = list(Path(td).glob(".out.json.tmp.*"))
            self.assertEqual(sidecars, [])

    def test_overwrite_replaces_existing(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "out.json"
            atomic_write_text(path, "first\n")
            atomic_write_text(path, "second\n")
            self.assertEqual(path.read_text(), "second\n")


if __name__ == "__main__":
    unittest.main()
