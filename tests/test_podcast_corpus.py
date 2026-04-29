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
            corpus = load_eligible_corpus(path)
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
            corpus = load_eligible_corpus(path)
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
            corpus = load_eligible_corpus(path)
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
            corpus = load_eligible_corpus(path)
            self.assertEqual(len(corpus), 1)
            self.assertIsInstance(corpus[0], BriefSummary)
            self.assertEqual(corpus[0].issue_no, 118)
            self.assertIsInstance(corpus[0].items, tuple)

    def test_empty_corpus_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write_briefs(Path(td), [])
            self.assertEqual(load_eligible_corpus(path), [])

    def test_non_list_payload_raises(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "briefs.json"
            path.write_text(json.dumps({"oops": "object not list"}))
            with self.assertRaises(ValueError):
                load_eligible_corpus(path)


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
