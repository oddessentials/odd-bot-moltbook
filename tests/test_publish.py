"""Unit tests for src.publish pure functions + reconciliation.

Stdlib unittest only — no pytest dependency. Run via:

    .venv/bin/python -m unittest discover -s tests

Covers the load-bearing logic that the orchestrator depends on:

  - merge_brief: daily-only-on-new enforcement, dedupe, sort, W18 passthrough
  - discover_work: bounded backlog, start floor, exclude published
  - _load_publish_record_ids: reads `action: publish` ids from runs.jsonl
  - _reconcile_finalization: flip + append + idempotent + missing-draft +
    malformed-draft + non-daily skip

Tests for the network/git/build paths are intentionally not here — those
are integration concerns covered by the wrapper running end-to-end.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from src.publish import (
    discover_work,
    merge_brief,
    _load_publish_record_ids,
    _reconcile_finalization,
)
from src.summarize import STANDARD_DISCLAIMER


def _brief(brief_id: str, *, status: str = "published", date_: str | None = None,
           issue: int = 1) -> dict:
    """Brief shape that passes the Pydantic Brief schema."""
    return {
        "id": brief_id,
        "issueNo": issue,
        "date": date_ or brief_id,
        "title": f"title for {brief_id}",
        "dek": "d",
        "readingMinutes": 1,
        "tags": ["Agents"],
        "items": [],
        "status": status,
        "disclaimer": STANDARD_DISCLAIMER,
        "isSeed": None,
    }


class TestMergeBrief(unittest.TestCase):
    def test_rejects_non_daily_new(self):
        with self.assertRaises(ValueError) as cm:
            merge_brief([], _brief("2026-W19"))
        self.assertIn("daily slug", str(cm.exception))

    def test_passes_through_existing_weekly_unchanged(self):
        w18 = _brief("2026-W18", date_="2026-04-27")
        merged = merge_brief([w18], _brief("2026-04-28"))
        recovered = next(b for b in merged if b["id"] == "2026-W18")
        self.assertEqual(recovered, w18)

    def test_sorts_by_date_desc_id_desc_tiebreak(self):
        # Existing list contains a passthrough weekly entry (W18) and
        # a daily for the same date. Adding a newer daily must sort
        # the newer one first; same-date tiebreak puts W18 before
        # 2026-04-27 because 'W' > '0' under reverse-string sort.
        w18 = _brief("2026-W18", date_="2026-04-27")
        d27 = _brief("2026-04-27", date_="2026-04-27")
        merged = merge_brief([w18, d27], _brief("2026-04-28"))
        self.assertEqual([m["id"] for m in merged],
                         ["2026-04-28", "2026-W18", "2026-04-27"])

    def test_dedupe_by_exact_id_keeps_latest(self):
        a = _brief("2026-04-27", issue=117)
        b = _brief("2026-04-27", issue=999)
        merged = merge_brief([a], b)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["issueNo"], 999)


class TestDiscoverWork(unittest.TestCase):
    FLOOR = date(2026, 4, 27)

    def test_today_only_when_yesterday_published(self):
        cands = discover_work(date(2026, 4, 28), 3, self.FLOOR,
                              {"2026-04-27", "2026-04-26"})
        self.assertEqual([d.isoformat() for d in cands], ["2026-04-28"])

    def test_floor_caps_lookback_below_max_backlog(self):
        cands = discover_work(date(2026, 4, 27), 7, self.FLOOR, set())
        self.assertEqual([d.isoformat() for d in cands], ["2026-04-27"])

    def test_long_offline_bounded_by_max_backlog(self):
        cands = discover_work(date(2026, 8, 5), 3, self.FLOOR, set())
        self.assertEqual([d.isoformat() for d in cands],
                         ["2026-08-03", "2026-08-04", "2026-08-05"])

    def test_caught_up_returns_empty(self):
        cands = discover_work(date(2026, 4, 30), 3, self.FLOOR,
                              {"2026-04-28", "2026-04-29", "2026-04-30"})
        self.assertEqual(cands, [])

    def test_invalid_max_backlog_raises(self):
        with self.assertRaises(ValueError):
            discover_work(date(2026, 4, 27), 0, self.FLOOR, set())


class _ReconcileBase(unittest.TestCase):
    """Sandbox helper: patches RUNS_PATH (in both publish + summarize since
    append_run_record lives in summarize) and _draft_path to point at a
    tempdir, so reconcile mutations don't touch the real repo."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.runs_path = self.tmp_path / "runs.jsonl"
        self.digests_dir = self.tmp_path / "digests"
        self.digests_dir.mkdir()
        self._patches = [
            mock.patch("src.publish.RUNS_PATH", self.runs_path),
            mock.patch("src.summarize.RUNS_PATH", self.runs_path),
            mock.patch("src.publish._draft_path",
                       lambda d: self.digests_dir / d.isoformat() / "summary.json"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def _write_draft(self, brief: dict, status: str = "draft") -> Path:
        path = self.digests_dir / brief["id"] / "summary.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**brief, "status": status}, indent=2) + "\n")
        return path


class TestReconcileFinalization(_ReconcileBase):
    def test_flips_draft_and_appends_record(self):
        brief = _brief("2026-04-27")
        path = self._write_draft(brief, status="draft")
        _reconcile_finalization([brief])
        self.assertEqual(json.loads(path.read_text())["status"], "published")
        self.assertIn("2026-04-27", _load_publish_record_ids())

    def test_idempotent_on_already_reconciled(self):
        brief = _brief("2026-04-27")
        self._write_draft(brief, status="draft")
        _reconcile_finalization([brief])
        size1 = self.runs_path.stat().st_size
        _reconcile_finalization([brief])
        size2 = self.runs_path.stat().st_size
        self.assertEqual(size1, size2)

    def test_missing_draft_continues_and_still_records(self):
        brief = _brief("2026-04-27")  # no draft on disk
        _reconcile_finalization([brief])
        # Run record still appended even when on-disk draft missing
        self.assertIn("2026-04-27", _load_publish_record_ids())

    def test_malformed_draft_raises(self):
        brief = _brief("2026-04-27")
        path = self.digests_dir / brief["id"] / "summary.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        # Strip required `disclaimer` so Brief() construction fails
        broken = {**brief, "status": "draft"}
        del broken["disclaimer"]
        path.write_text(json.dumps(broken))
        with self.assertRaises(RuntimeError) as cm:
            _reconcile_finalization([brief])
        self.assertIn("malformed draft", str(cm.exception))

    def test_skips_weekly_slug(self):
        weekly = _brief("2026-W18", date_="2026-04-27")
        _reconcile_finalization([weekly])
        self.assertNotIn("2026-W18", _load_publish_record_ids())

    def test_skips_non_published_status(self):
        brief = _brief("2026-04-27", status="draft")  # not published
        _reconcile_finalization([brief])
        self.assertNotIn("2026-04-27", _load_publish_record_ids())

    def test_dry_run_does_not_write(self):
        brief = _brief("2026-04-27")
        path = self._write_draft(brief, status="draft")
        _reconcile_finalization([brief], dry_run=True)
        # Draft unchanged
        self.assertEqual(json.loads(path.read_text())["status"], "draft")
        # No runs.jsonl mutation
        self.assertFalse(self.runs_path.exists())


if __name__ == "__main__":
    unittest.main()
