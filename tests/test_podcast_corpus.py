"""Unit tests for src.podcast.corpus.load_eligible_corpus.

Verifies the load-bearing corpus filter encoded in plan §Locked decisions:
  - status == "published"
  - id matches the daily-shape regex (^\\d{4}-\\d{2}-\\d{2}$)
  - results sorted ascending by id

The grandfathered weekly artifact 2026-W18 is excluded by id-shape filter.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.podcast.corpus import load_eligible_corpus, summarize_corpus
from src.podcast.schema import BriefSummary


def _brief(**overrides):
    base = {
        "id": "2026-04-27",
        "issueNo": 117,
        "date": "2026-04-27",
        "title": "T",
        "dek": "D",
        "items": [{"headline": "h", "body": "b"}],
        "status": "published",
    }
    base.update(overrides)
    return base


class TestLoadEligibleCorpus(unittest.TestCase):
    def _write_briefs(self, tmpdir: Path, payload: list) -> Path:
        path = tmpdir / "briefs.json"
        path.write_text(json.dumps(payload))
        return path

    def test_returns_only_published(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write_briefs(
                Path(td),
                [
                    _brief(id="2026-04-27", issueNo=117),
                    _brief(id="2026-04-28", issueNo=118, status="draft"),
                ],
            )
            corpus = load_eligible_corpus(path, episodes_path=None)
            self.assertEqual([b.id for b in corpus], ["2026-04-27"])

    def test_excludes_weekly_id_shape(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write_briefs(
                Path(td),
                [
                    _brief(id="2026-04-27", issueNo=117),
                    _brief(id="2026-W18", issueNo=18),  # weekly — must drop
                    _brief(id="2026-04-28", issueNo=118),
                ],
            )
            corpus = load_eligible_corpus(path, episodes_path=None)
            self.assertEqual([b.id for b in corpus], ["2026-04-27", "2026-04-28"])

    def test_sorted_ascending_by_id(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write_briefs(
                Path(td),
                [
                    _brief(id="2026-04-28", issueNo=118),
                    _brief(id="2026-04-27", issueNo=117),
                    _brief(id="2026-04-26", issueNo=116),
                ],
            )
            corpus = load_eligible_corpus(path, episodes_path=None)
            self.assertEqual(
                [b.id for b in corpus],
                ["2026-04-26", "2026-04-27", "2026-04-28"],
            )

    def test_returns_brief_summaries(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write_briefs(
                Path(td),
                [_brief(id="2026-04-28", issueNo=118)],
            )
            corpus = load_eligible_corpus(path, episodes_path=None)
            self.assertEqual(len(corpus), 1)
            self.assertIsInstance(corpus[0], BriefSummary)
            self.assertEqual(corpus[0].issue_no, 118)
            self.assertIsInstance(corpus[0].items, tuple)

    def test_empty_corpus_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write_briefs(Path(td), [])
            self.assertEqual(load_eligible_corpus(path, episodes_path=None), [])

    def test_non_list_payload_raises(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "briefs.json"
            path.write_text(json.dumps({"oops": "object not list"}))
            with self.assertRaises(ValueError):
                load_eligible_corpus(path, episodes_path=None)


class LoadEligibleCorpusEpisodeWindow(unittest.TestCase):
    """The 2026-05-21 corpus-window fix.

    Each episode's corpus is bounded to briefs strictly newer than the
    most-recent published episode's date. Episode 1 cold-start carve-out
    is preserved via the empty-/missing-episodes fallback. Floor is
    computed from max episode date, NOT episodeNo, so a backfill cannot
    expand the corpus floor.
    """

    def _write_pair(self, td: Path, briefs: list, episodes: list) -> tuple[Path, Path]:
        bp = td / "briefs.json"
        ep = td / "episodes.json"
        bp.write_text(json.dumps(briefs))
        ep.write_text(json.dumps(episodes))
        return bp, ep

    def test_filters_briefs_at_or_before_prior_episode_date(self):
        # Floor = max(ep-001=04-28, ep-002=05-10) = 05-10. Strict > 05-10
        # drops 04-27/04-28/04-29/05-10 and keeps 05-11.
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            bp, ep = self._write_pair(
                td,
                briefs=[
                    _brief(id="2026-04-27", issueNo=117, date="2026-04-27"),
                    _brief(id="2026-04-28", issueNo=118, date="2026-04-28"),
                    _brief(id="2026-04-29", issueNo=119, date="2026-04-29"),
                    _brief(id="2026-05-10", issueNo=130, date="2026-05-10"),
                    _brief(id="2026-05-11", issueNo=131, date="2026-05-11"),
                ],
                episodes=[
                    {"id": "ep-001", "episodeNo": 1, "date": "2026-04-28"},
                    {"id": "ep-002", "episodeNo": 2, "date": "2026-05-10"},
                ],
            )
            corpus = load_eligible_corpus(bp, episodes_path=ep)
            self.assertEqual([b.id for b in corpus], ["2026-05-11"])

    def test_empty_episodes_returns_full_corpus(self):
        # Cold start — empty episodes.json must not filter (Episode 1
        # carve-out preserved).
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            bp, ep = self._write_pair(
                td,
                briefs=[
                    _brief(id="2026-04-27", issueNo=117, date="2026-04-27"),
                    _brief(id="2026-04-28", issueNo=118, date="2026-04-28"),
                ],
                episodes=[],
            )
            corpus = load_eligible_corpus(bp, episodes_path=ep)
            self.assertEqual([b.id for b in corpus], ["2026-04-27", "2026-04-28"])

    def test_missing_episodes_file_returns_full_corpus(self):
        # Cold start — a path that doesn't exist must not filter.
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            bp = td / "briefs.json"
            bp.write_text(json.dumps([
                _brief(id="2026-04-27", issueNo=117, date="2026-04-27"),
            ]))
            corpus = load_eligible_corpus(
                bp, episodes_path=td / "does-not-exist.json",
            )
            self.assertEqual([b.id for b in corpus], ["2026-04-27"])

    def test_uses_max_date_not_max_episode_no(self):
        # Backfill: ep-003 inserted out of order with a higher
        # episodeNo (3) but an earlier date (05-05) than ep-002 (05-10).
        # Floor MUST be max date = 05-10 (ep-002), not max episodeNo's
        # date = 05-05. Selecting by episodeNo would (wrongly) expand
        # the corpus to include 05-06 through 05-10.
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            bp, ep = self._write_pair(
                td,
                briefs=[
                    _brief(id="2026-05-05", issueNo=5, date="2026-05-05"),
                    _brief(id="2026-05-06", issueNo=6, date="2026-05-06"),
                    _brief(id="2026-05-10", issueNo=10, date="2026-05-10"),
                    _brief(id="2026-05-11", issueNo=11, date="2026-05-11"),
                ],
                episodes=[
                    {"id": "ep-001", "episodeNo": 1, "date": "2026-04-28"},
                    {"id": "ep-002", "episodeNo": 2, "date": "2026-05-10"},
                    # Backfilled out of order: higher episodeNo, earlier date.
                    {"id": "ep-003", "episodeNo": 3, "date": "2026-05-05"},
                ],
            )
            corpus = load_eligible_corpus(bp, episodes_path=ep)
            self.assertEqual([b.id for b in corpus], ["2026-05-11"])


class TestSummarizeCorpus(unittest.TestCase):
    def test_format_includes_id_issue_title_item_count(self):
        corpus = [
            BriefSummary(
                id="2026-04-28",
                issue_no=118,
                date="2026-04-28",
                title="T",
                dek="D",
                items=({"headline": "h"},),
            )
        ]
        text = summarize_corpus(corpus)
        self.assertIn("2026-04-28", text)
        self.assertIn("issue 118", text)
        self.assertIn("'T'", text)
        self.assertIn("1 items", text)


if __name__ == "__main__":
    unittest.main()
