"""Unit tests for src.podcast.schema validators.

Stdlib unittest only — run via:

    .venv/bin/python -m unittest discover -s tests

Covers the Pydantic field-level guards that protect the engine against
out-of-bounds LLM output or operator-side cast misconfiguration. Behavior
beyond schema (script generation, manifest writes) is tested elsewhere or
covered by the integration run.
"""

from __future__ import annotations

import unittest

from pydantic import ValidationError

from src.podcast.schema import (
    CastConfig,
    EpisodeRecord,
    EpisodeScript,
    Segment,
)


class TestSegment(unittest.TestCase):
    def test_min_word_count(self):
        # 11 words — below the 12-word floor.
        text = "one two three four five six seven eight nine ten eleven"
        with self.assertRaises(ValidationError) as cm:
            Segment(speaker="shrimp", text=text)
        self.assertIn("word count", str(cm.exception))

    def test_max_word_count(self):
        text = " ".join(["w"] * 46)  # 46 words — above the 45-word ceiling.
        with self.assertRaises(ValidationError) as cm:
            Segment(speaker="shrimp", text=text)
        self.assertIn("word count", str(cm.exception))

    def test_lower_boundary_accepts(self):
        text = " ".join(["w"] * 12)
        seg = Segment(speaker="shrimp", text=text)
        self.assertEqual(len(seg.text.split()), 12)

    def test_upper_boundary_accepts(self):
        text = " ".join(["w"] * 45)
        seg = Segment(speaker="shrimp", text=text)
        self.assertEqual(len(seg.text.split()), 45)

    def test_empty_text_rejected(self):
        with self.assertRaises(ValidationError):
            Segment(speaker="shrimp", text="")

    def test_delivery_note_max_length(self):
        with self.assertRaises(ValidationError):
            Segment(
                speaker="shrimp",
                text=" ".join(["w"] * 12),
                delivery_note="x" * 201,
            )


def _ok_segments(n: int) -> list[Segment]:
    text = " ".join(["w"] * 20)
    return [Segment(speaker="shrimp", text=text) for _ in range(n)]


class TestEpisodeScript(unittest.TestCase):
    def test_segment_count_lower_bound(self):
        with self.assertRaises(ValidationError):
            EpisodeScript(
                title="Title",
                description="D" * 100,
                segments=_ok_segments(7),
            )

    def test_segment_count_upper_bound(self):
        with self.assertRaises(ValidationError):
            EpisodeScript(
                title="Title",
                description="D" * 100,
                segments=_ok_segments(17),
            )

    def test_title_max_length(self):
        with self.assertRaises(ValidationError):
            EpisodeScript(
                title="x" * 81,
                description="D" * 100,
                segments=_ok_segments(8),
            )

    def test_description_min_length(self):
        with self.assertRaises(ValidationError):
            EpisodeScript(
                title="Title",
                description="too short",
                segments=_ok_segments(8),
            )

    def test_description_max_length(self):
        with self.assertRaises(ValidationError):
            EpisodeScript(
                title="Title",
                description="x" * 501,
                segments=_ok_segments(8),
            )

    def test_happy_path(self):
        script = EpisodeScript(
            title="Title",
            description="D" * 200,
            segments=_ok_segments(12),
        )
        self.assertEqual(len(script.segments), 12)


class TestEpisodeRecord(unittest.TestCase):
    def _ok_payload(self, **overrides) -> dict:
        payload = {
            "id": "ep-001",
            "episodeNo": 1,
            "title": "Title",
            "date": "2026-04-28",
            "durationMinutes": 4,
            "youtubeId": "abc123",
            "description": "Some description",
            "hosts": ["Shrimp"],
        }
        payload.update(overrides)
        return payload

    def test_happy_path(self):
        record = EpisodeRecord(**self._ok_payload())
        self.assertEqual(record.id, "ep-001")
        self.assertEqual(record.episodeNo, 1)

    def test_invalid_date_format_rejected(self):
        with self.assertRaises(ValidationError):
            EpisodeRecord(**self._ok_payload(date="04/28/2026"))

    def test_iso_week_id_date_field_rejected(self):
        # Engine emits daily-shape episode dates only; weekly ISO-week
        # ids must not slip into the SPA Episode shape.
        with self.assertRaises(ValidationError):
            EpisodeRecord(**self._ok_payload(date="2026-W18"))

    def test_episode_no_must_be_positive(self):
        with self.assertRaises(ValidationError):
            EpisodeRecord(**self._ok_payload(episodeNo=0))

    def test_duration_minutes_must_be_positive(self):
        with self.assertRaises(ValidationError):
            EpisodeRecord(**self._ok_payload(durationMinutes=0))

    def test_hosts_nonempty(self):
        with self.assertRaises(ValidationError):
            EpisodeRecord(**self._ok_payload(hosts=[]))

    def test_title_max_length(self):
        with self.assertRaises(ValidationError):
            EpisodeRecord(**self._ok_payload(title="x" * 81))


class TestCastConfig(unittest.TestCase):
    def _member(self, **overrides) -> dict:
        m = {
            "display_name": "Shrimp",
            "role": "anchor",
            "persona": "small witty energetic crustacean",
            "elevenlabs_voice_id": "voice-id",
            "hedra_image_asset_id": "image-id",
        }
        m.update(overrides)
        return m

    def test_happy_path(self):
        cfg = CastConfig(
            version=1,
            anchor="shrimp",
            cast={"shrimp": self._member()},
        )
        self.assertEqual(cfg.slugs(), ["shrimp"])

    def test_empty_cast_rejected(self):
        with self.assertRaises(ValidationError):
            CastConfig(version=1, anchor="shrimp", cast={})

    def test_slugs_preserve_insertion_order(self):
        cfg = CastConfig(
            version=1,
            anchor="shrimp",
            cast={
                "shrimp": self._member(),
                "carl": self._member(display_name="Carl", role="guest"),
            },
        )
        self.assertEqual(cfg.slugs(), ["shrimp", "carl"])


if __name__ == "__main__":
    unittest.main()
