"""Unit tests for src.podcast.spa_rebuild.

Pins the SPA-rebuild verification contract that gates the podcast
publish commit. The Vite build itself is exercised in the daily
pipeline tests; here we cover only the publish-time additions:

  - `_load_episode_record` finds the entry by id, raises on missing.
  - `verify_bundle_contains_episode` accepts a bundle that contains
    both the `id` and `youtubeId`, rejects either alone, and rejects
    a bundle that doesn't exist on disk.

The 2026-05-10 incident shape: ep-002 was published, data/episodes.json
got the new entry, the per-episode OG page was rendered, but the SPA
bundle in docs/assets/index-*.js stayed at the 09:00 UTC daily build's
snapshot (only ep-001). /podcast listing surfaced stale. This module's
hard gate makes that state unreachable: the publish commit refuses to
form until the rebuilt bundle contains the new episode's markers.

Stdlib unittest only — run via:

    .venv/bin/python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.podcast.spa_rebuild import (
    _load_episode_record,
    verify_bundle_contains_episode,
)


EP_002_FIXTURE = {
    "id": "ep-002",
    "episodeNo": 2,
    "title": "Agents Auditing Themselves: The Self-Report Problem",
    "date": "2026-05-10",
    "durationMinutes": 4,
    "youtubeId": "_nNjfWCPUPU",
    "description": "x",
    "hosts": ["Shrimp", "Carl"],
}
EP_001_FIXTURE = {
    "id": "ep-001",
    "episodeNo": 1,
    "title": "When Agents Optimize the Scorecard (Ep. 1)",
    "date": "2026-04-28",
    "durationMinutes": 4,
    "youtubeId": "leasI1ssr2U",
    "description": "x",
    "hosts": ["Shrimp", "Carl"],
}


class TestLoadEpisodeRecord(unittest.TestCase):
    def test_finds_entry_by_id(self):
        with tempfile.TemporaryDirectory() as td:
            ep_path = Path(td) / "episodes.json"
            ep_path.write_text(json.dumps([EP_002_FIXTURE, EP_001_FIXTURE]))
            got = _load_episode_record("ep-002", episodes_path=ep_path)
            self.assertEqual(got["youtubeId"], "_nNjfWCPUPU")
            self.assertEqual(got["id"], "ep-002")

    def test_raises_when_id_absent(self):
        with tempfile.TemporaryDirectory() as td:
            ep_path = Path(td) / "episodes.json"
            ep_path.write_text(json.dumps([EP_001_FIXTURE]))
            with self.assertRaises(RuntimeError) as ctx:
                _load_episode_record("ep-002", episodes_path=ep_path)
            self.assertIn("ep-002", str(ctx.exception))

    def test_raises_when_episodes_json_missing(self):
        with tempfile.TemporaryDirectory() as td:
            ep_path = Path(td) / "episodes.json"  # not created
            with self.assertRaises(RuntimeError) as ctx:
                _load_episode_record("ep-002", episodes_path=ep_path)
            self.assertIn(str(ep_path), str(ctx.exception))


class TestVerifyBundleContainsEpisode(unittest.TestCase):
    """`docs/assets/index-*.js` MUST contain id + youtubeId after rebuild."""

    def _write_bundle(self, assets_dir: Path, *snippets: str) -> Path:
        assets_dir.mkdir(parents=True, exist_ok=True)
        # Vite uses content-hashed filenames; we mirror that shape.
        path = assets_dir / "index-FAKE12345.js"
        path.write_text("/* fake bundle */\n" + "\n".join(snippets))
        return path

    def test_passes_when_both_markers_present(self):
        with tempfile.TemporaryDirectory() as td:
            assets = Path(td) / "assets"
            self._write_bundle(
                assets,
                'const e = {"id":"ep-002","youtubeId":"_nNjfWCPUPU"};',
            )
            # Should NOT raise.
            verify_bundle_contains_episode(
                episode=EP_002_FIXTURE, docs_assets_dir=assets,
            )

    def test_rejects_when_id_missing(self):
        # The 2026-05-10 incident shape: bundle is from an older build
        # that pre-dates ep-002. Has ep-001's markers but not ep-002's.
        with tempfile.TemporaryDirectory() as td:
            assets = Path(td) / "assets"
            self._write_bundle(
                assets,
                'const e = {"id":"ep-001","youtubeId":"leasI1ssr2U"};',
                # youtubeId for ep-002 happens not to appear either,
                # but the id check fires first.
            )
            with self.assertRaises(RuntimeError) as ctx:
                verify_bundle_contains_episode(
                    episode=EP_002_FIXTURE, docs_assets_dir=assets,
                )
            self.assertIn("id='ep-002'", str(ctx.exception))

    def test_rejects_when_youtube_id_missing(self):
        # Edge case: id substring matches (e.g., a placeholder fixture
        # like "ep-002-cold-open" already in the design fixtures) but
        # the youtubeId doesn't appear. Verifier must still reject —
        # both markers are required.
        with tempfile.TemporaryDirectory() as td:
            assets = Path(td) / "assets"
            self._write_bundle(
                assets,
                'const fixture = {"id":"ep-002-cold-open"};',
            )
            with self.assertRaises(RuntimeError) as ctx:
                verify_bundle_contains_episode(
                    episode=EP_002_FIXTURE, docs_assets_dir=assets,
                )
            msg = str(ctx.exception)
            # id substring "ep-002" matches via the fixture, so the
            # only listed-missing marker is the youtubeId.
            self.assertIn("youtubeId='_nNjfWCPUPU'", msg)
            self.assertNotIn("id='ep-002'", msg)

    def test_rejects_when_both_missing(self):
        with tempfile.TemporaryDirectory() as td:
            assets = Path(td) / "assets"
            self._write_bundle(assets, "const x = 1;")
            with self.assertRaises(RuntimeError) as ctx:
                verify_bundle_contains_episode(
                    episode=EP_002_FIXTURE, docs_assets_dir=assets,
                )
            msg = str(ctx.exception)
            self.assertIn("id='ep-002'", msg)
            self.assertIn("youtubeId='_nNjfWCPUPU'", msg)

    def test_rejects_when_no_bundle_present(self):
        # `_run_build` failed to produce any chunk. Surface that as a
        # distinct error message rather than a misleading "missing
        # markers" one.
        with tempfile.TemporaryDirectory() as td:
            assets = Path(td) / "assets"
            assets.mkdir()
            with self.assertRaises(RuntimeError) as ctx:
                verify_bundle_contains_episode(
                    episode=EP_002_FIXTURE, docs_assets_dir=assets,
                )
            self.assertIn("no JS bundle", str(ctx.exception))

    def test_scans_all_index_chunks_not_just_one(self):
        # Vite may split into multiple `index-HASH.js` chunks. The
        # verifier must find markers across the whole set.
        with tempfile.TemporaryDirectory() as td:
            assets = Path(td) / "assets"
            assets.mkdir()
            (assets / "index-AAAA.js").write_text(
                'const e = {"id":"ep-002"};',
            )
            (assets / "index-BBBB.js").write_text(
                'const v = "_nNjfWCPUPU";',
            )
            # Should NOT raise — markers are spread across chunks.
            verify_bundle_contains_episode(
                episode=EP_002_FIXTURE, docs_assets_dir=assets,
            )

    def test_scans_non_index_chunk_names(self):
        # Issue #38: the manualChunks split makes Vite emit a non-`index`
        # chunk (data-briefs-*.js), and future chunking could add more.
        # The build-time episodes.json data can land in any chunk, so the
        # verifier must scan all `*.js`, not just `index-*.js`. Markers
        # here live ONLY in non-`index` chunks; the old glob would have
        # scanned just the index chunk, missed both, and wrongly failed.
        with tempfile.TemporaryDirectory() as td:
            assets = Path(td) / "assets"
            assets.mkdir()
            (assets / "index-AAAA.js").write_text("const app = 1;")
            (assets / "vendor-radix-BBBB.js").write_text(
                'const e = {"id":"ep-002"};',
            )
            (assets / "data-briefs-CCCC.js").write_text(
                'const v = "_nNjfWCPUPU";',
            )
            # Should NOT raise — markers are present, just not in index-*.js.
            verify_bundle_contains_episode(
                episode=EP_002_FIXTURE, docs_assets_dir=assets,
            )


if __name__ == "__main__":
    unittest.main()
