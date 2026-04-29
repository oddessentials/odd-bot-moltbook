"""Unit tests for src.podcast.cast.

cast_config_hash is the manifest fingerprint that records which cast
contract produced an episode — its stability across calls and its
sensitivity to byte changes are both load-bearing properties for
downstream cross-run consistency checks.

load_cast also enforces the load-bearing invariant that the anchor slug
exists in the cast map — without that check, the engine would emit an
Episode record with hosts derived from a speaker that has no voice or
image asset_id.
"""

from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from src.podcast.cast import cast_config_hash, load_cast


_VALID_YAML = textwrap.dedent(
    """
    version: 1
    anchor: shrimp
    cast:
      shrimp:
        display_name: Shrimp
        role: anchor
        persona: small witty energetic crustacean anchor
        elevenlabs_voice_id: voice-shrimp
        hedra_image_asset_id: image-shrimp
      carl:
        display_name: Carl
        role: guest
        persona: sardonic husky-voiced crab guest
        elevenlabs_voice_id: voice-carl
        hedra_image_asset_id: image-carl
    """
).strip()


class TestLoadCast(unittest.TestCase):
    def test_happy_path(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cast.yaml"
            path.write_text(_VALID_YAML)
            cfg = load_cast(path)
            self.assertEqual(cfg.anchor, "shrimp")
            self.assertEqual(cfg.slugs(), ["shrimp", "carl"])
            self.assertEqual(cfg.cast["shrimp"].display_name, "Shrimp")

    def test_anchor_must_exist_in_cast(self):
        bad_yaml = textwrap.dedent(
            """
            version: 1
            anchor: ghost
            cast:
              shrimp:
                display_name: Shrimp
                role: anchor
                persona: p
                elevenlabs_voice_id: v
                hedra_image_asset_id: i
            """
        ).strip()
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cast.yaml"
            path.write_text(bad_yaml)
            with self.assertRaises(ValueError) as cm:
                load_cast(path)
            self.assertIn("ghost", str(cm.exception))


class TestCastConfigHash(unittest.TestCase):
    def test_stable_across_calls(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cast.yaml"
            path.write_text(_VALID_YAML)
            self.assertEqual(cast_config_hash(path), cast_config_hash(path))

    def test_returns_12_char_hex(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cast.yaml"
            path.write_text(_VALID_YAML)
            h = cast_config_hash(path)
            self.assertEqual(len(h), 12)
            int(h, 16)  # raises if non-hex

    def test_changes_when_bytes_change(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cast.yaml"
            path.write_text(_VALID_YAML)
            before = cast_config_hash(path)
            # Add a trailing comment — semantically a no-op, byte-level
            # different. The fingerprint MUST move so that a change to
            # the contract is auditable from the manifest alone.
            path.write_text(_VALID_YAML + "\n# operator note\n")
            after = cast_config_hash(path)
            self.assertNotEqual(before, after)


if __name__ == "__main__":
    unittest.main()
