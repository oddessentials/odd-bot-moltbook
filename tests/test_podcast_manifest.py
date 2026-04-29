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
    atomic_write_text,
    derive_episode_no,
    derive_hosts,
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
