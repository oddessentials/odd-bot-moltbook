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
from datetime import date
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


class LoadEligibleCorpusRollingWindow(unittest.TestCase):
    """The 2026-05-25 corpus-window correction.

    Each episode's corpus is bounded to a rolling 14-day window ending
    at the run's local editorial date. A failed publish does NOT widen
    the next run's window — missed windows are accepted losses per
    `feedback_accepted_single_day_miss.md`. Cold-start (no prior
    episodes) bypasses the window and uses the full eligible corpus.
    """

    def _write_pair(self, td: Path, briefs: list, episodes: list) -> tuple[Path, Path]:
        bp = td / "briefs.json"
        ep = td / "episodes.json"
        bp.write_text(json.dumps(briefs))
        ep.write_text(json.dumps(episodes))
        return bp, ep

    def test_failed_publish_does_not_widen_window_backward(self):
        # The 2026-05-25 incident shape: ep-002 was the last successful
        # publish (2026-05-10); the 2026-05-24 fire failed at Hedra so
        # episodes.json still ends at ep-002. Run date 2026-05-31. The
        # window MUST be [2026-05-18, 2026-05-31] — i.e., it does NOT
        # snap back to 2026-05-11 just because the prior episode was
        # 2026-05-10. Missed windows are accepted losses.
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            bp, ep = self._write_pair(
                td,
                briefs=[_brief(
                    id=f"2026-05-{d:02d}", issueNo=100 + d, date=f"2026-05-{d:02d}",
                ) for d in range(10, 32)],  # 5-10 through 5-31 inclusive
                episodes=[
                    {"id": "ep-001", "episodeNo": 1, "date": "2026-04-28"},
                    {"id": "ep-002", "episodeNo": 2, "date": "2026-05-10"},
                ],
            )
            corpus = load_eligible_corpus(
                bp, episodes_path=ep, run_date=date(2026, 5, 31),
            )
            ids = [b.id for b in corpus]
            self.assertEqual(ids[0], "2026-05-18")
            self.assertEqual(ids[-1], "2026-05-31")
            self.assertEqual(len(ids), 14)

    def test_window_covers_14_days_ending_at_run_date(self):
        # Run date 2026-05-24 → window [2026-05-11, 2026-05-24].
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            bp, ep = self._write_pair(
                td,
                briefs=[_brief(
                    id=f"2026-05-{d:02d}", issueNo=100 + d, date=f"2026-05-{d:02d}",
                ) for d in range(1, 25)],  # 5-01 through 5-24 inclusive
                episodes=[
                    {"id": "ep-002", "episodeNo": 2, "date": "2026-05-10"},
                ],
            )
            corpus = load_eligible_corpus(
                bp, episodes_path=ep, run_date=date(2026, 5, 24),
            )
            ids = [b.id for b in corpus]
            self.assertEqual(ids[0], "2026-05-11")
            self.assertEqual(ids[-1], "2026-05-24")
            self.assertEqual(len(ids), 14)

    def test_gaps_inside_window_do_not_expand_window_backward(self):
        # Run date 2026-05-24, window [2026-05-11, 2026-05-24]. Briefs
        # for 5-13, 5-15, 5-19 are missing from briefs.json (gaps).
        # The corpus must contain only the briefs that DID publish in
        # window — the window must NOT silently expand to 5-10 or
        # earlier to make up for the gaps.
        days_present = [11, 12, 14, 16, 17, 18, 20, 21, 22, 23, 24]
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            bp, ep = self._write_pair(
                td,
                briefs=[_brief(
                    id=f"2026-05-{d:02d}", issueNo=100 + d, date=f"2026-05-{d:02d}",
                ) for d in [9, 10] + days_present],  # 5-09 + 5-10 outside window
                episodes=[
                    {"id": "ep-002", "episodeNo": 2, "date": "2026-05-10"},
                ],
            )
            corpus = load_eligible_corpus(
                bp, episodes_path=ep, run_date=date(2026, 5, 24),
            )
            ids = [b.id for b in corpus]
            self.assertEqual(
                ids,
                [f"2026-05-{d:02d}" for d in days_present],
            )
            self.assertNotIn("2026-05-09", ids)
            self.assertNotIn("2026-05-10", ids)

    def test_briefs_after_window_end_are_excluded(self):
        # Run date 2026-05-24, window ends at 2026-05-24. Briefs dated
        # 2026-05-25 or later must NOT be included (unusual but possible
        # if a brief gets pre-dated or backfilled).
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            bp, ep = self._write_pair(
                td,
                briefs=[
                    _brief(id="2026-05-23", issueNo=123, date="2026-05-23"),
                    _brief(id="2026-05-24", issueNo=124, date="2026-05-24"),
                    _brief(id="2026-05-25", issueNo=125, date="2026-05-25"),
                    _brief(id="2026-05-26", issueNo=126, date="2026-05-26"),
                ],
                episodes=[
                    {"id": "ep-002", "episodeNo": 2, "date": "2026-05-10"},
                ],
            )
            corpus = load_eligible_corpus(
                bp, episodes_path=ep, run_date=date(2026, 5, 24),
            )
            ids = [b.id for b in corpus]
            self.assertEqual(ids, ["2026-05-23", "2026-05-24"])

    def test_empty_episodes_returns_full_corpus(self):
        # Cold start (Episode 1) — empty episodes.json bypasses the
        # window entirely. The Episode 1 carve-out from the original
        # plan is preserved.
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
            # run_date is far past the briefs; cold-start bypasses
            # window so both still qualify.
            corpus = load_eligible_corpus(
                bp, episodes_path=ep, run_date=date(2026, 6, 30),
            )
            self.assertEqual([b.id for b in corpus], ["2026-04-27", "2026-04-28"])

    def test_missing_episodes_file_returns_full_corpus(self):
        # Cold start — a path that doesn't exist also bypasses the
        # window. Same Episode 1 carve-out semantics.
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            bp = td / "briefs.json"
            bp.write_text(json.dumps([
                _brief(id="2026-04-27", issueNo=117, date="2026-04-27"),
            ]))
            corpus = load_eligible_corpus(
                bp,
                episodes_path=td / "does-not-exist.json",
                run_date=date(2026, 6, 30),
            )
            self.assertEqual([b.id for b in corpus], ["2026-04-27"])


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
