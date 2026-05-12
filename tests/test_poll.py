"""Unit tests for src.poll.

Stdlib unittest only — no pytest dependency. Run via:

    .venv/bin/python -m unittest discover -s tests

Covers the persistence boundary that the daily orchestrator relies on
when the upstream fetch yields an empty post list (e.g., RunAtLoad
boot after UTC midnight where the live API's `time=day` window does
not overlap the requested UTC-calendar-day window). The contract is
'safe no-op; table stays usable'; downstream `filter_and_rank` then
raises the documented zero-posts ValueError which `summarize.run_daily`
handles as a clean no-draft skip.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import duckdb

from src.poll import persist_raw


class TestPersistRaw(unittest.TestCase):
    def test_empty_input_is_noop_and_creates_table(self) -> None:
        """`persist_raw([], ...)` must not raise (DuckDB's `executemany`
        rejects empty parameter sets) and must leave `posts_raw` queryable.
        """
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "moltbook.duckdb"
            persist_raw([], "daily-2026-05-11-000000Z", db_path)
            self.assertTrue(db_path.exists())
            con = duckdb.connect(str(db_path), read_only=True)
            try:
                rows = con.execute(
                    "SELECT COUNT(*) FROM posts_raw"
                ).fetchone()
                self.assertEqual(rows[0], 0)
            finally:
                con.close()

    def test_empty_then_nonempty_persists_correctly(self) -> None:
        """After an empty call ensures the table, a subsequent non-empty
        call must persist normally — i.e., the empty-input guard does
        not break the durable replayability contract.
        """
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "moltbook.duckdb"
            persist_raw([], "daily-2026-05-11-000000Z", db_path)
            persist_raw(
                [
                    {"id": "post-a", "title": "t", "content": "c"},
                    {"id": "post-b", "title": "t2", "content": "c2"},
                ],
                "daily-2026-05-12-090000Z",
                db_path,
            )
            con = duckdb.connect(str(db_path), read_only=True)
            try:
                rows = con.execute(
                    "SELECT run_id, post_id FROM posts_raw ORDER BY post_id"
                ).fetchall()
            finally:
                con.close()
            self.assertEqual(
                rows,
                [
                    ("daily-2026-05-12-090000Z", "post-a"),
                    ("daily-2026-05-12-090000Z", "post-b"),
                ],
            )

    def test_nonempty_input_persists(self) -> None:
        """Sanity: the empty-input guard does not affect the normal path."""
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "moltbook.duckdb"
            persist_raw(
                [{"id": "post-x", "title": "t", "content": "c"}],
                "daily-2026-05-12-090000Z",
                db_path,
            )
            con = duckdb.connect(str(db_path), read_only=True)
            try:
                rows = con.execute(
                    "SELECT post_id FROM posts_raw"
                ).fetchall()
            finally:
                con.close()
            self.assertEqual(rows, [("post-x",)])


if __name__ == "__main__":
    unittest.main()
