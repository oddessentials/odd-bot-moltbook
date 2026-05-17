"""Unit tests for src.podcast_cadence_guard.

Covers the two layered checks that gate weekly podcast spend:

  1. days-since: REFUSE if `today_local - latest.date < min_days`.
     Anti-spam against rapid manual reinvocations.

  2. weekly-window: REFUSE if not inside an open Sunday-09:00-or-later
     window in America/New_York, or if the open window is already filled.

Also covers the file-reading entrypoint (`main(argv)`) so the bash
wrapper's contract — no/empty episodes.json proceeds; corrupt date
raises — is locked down.

Stdlib unittest only — run via:

    .venv/bin/python -m unittest discover -s tests
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timezone
from pathlib import Path

from src.editorial_time import EDITORIAL_TZ
from src.podcast_cadence_guard import evaluate, main


def _local(year: int, month: int, day: int, hour: int, minute: int = 0,
           second: int = 0) -> datetime:
    return (
        datetime(year, month, day, hour, minute, second, tzinfo=EDITORIAL_TZ)
        .astimezone(timezone.utc)
    )


class TestEvaluateCadenceDays(unittest.TestCase):
    """The min-days floor — refuses fast reinvocations regardless of weekday."""

    def test_two_days_after_publish_refuses_cadence(self):
        # The exact cadence-days bug class: a Tuesday reboot after a
        # Sunday publish must REFUSE on cadence (not on window).
        out = evaluate(
            now_utc=_local(2026, 4, 28, 14, 0),  # Tuesday
            latest_id="ep-001",
            latest_date=date(2026, 4, 26),
            min_days=6,
        )
        self.assertEqual(out, "REFUSE:cadence:2:ep-001:2026-04-26")

    def test_five_days_after_publish_refuses_cadence(self):
        # Friday after Sunday publish → days_since=5 < min_days=6 → refuse.
        out = evaluate(
            now_utc=_local(2026, 5, 1, 9, 0),  # Friday
            latest_id="ep-001",
            latest_date=date(2026, 4, 26),
            min_days=6,
        )
        self.assertEqual(out, "REFUSE:cadence:5:ep-001:2026-04-26")

    def test_too_soon_refuses_even_on_sunday_in_window(self):
        # User-explicit: cadence-days check fires BEFORE weekly-window.
        # Sunday 2026-05-10 09:00 ET, latest=Friday 2026-05-08 →
        # days_since=2 < 6 → REFUSE:cadence (not :window) regardless of
        # whether the window is open.
        out = evaluate(
            now_utc=_local(2026, 5, 10, 9, 0),  # Sunday in window
            latest_id="ep-X",
            latest_date=date(2026, 5, 8),  # 2 days ago
            min_days=6,
        )
        self.assertEqual(out, "REFUSE:cadence:2:ep-X:2026-05-08")

    def test_six_days_exact_passes_cadence_but_window_decides(self):
        # MIN_DAYS=6 is the floor; days_since=6 must pass cadence and
        # then route to the window check. Saturday 22:00 ET → window
        # refuses on day-of-week.
        out = evaluate(
            now_utc=_local(2026, 5, 2, 22, 0),  # Saturday, 6 days after
            latest_id="ep-001",
            latest_date=date(2026, 4, 26),
            min_days=6,
        )
        self.assertEqual(out, "REFUSE:window:6:ep-001:2026-04-26")


class TestEvaluateWeeklyWindow(unittest.TestCase):
    """The day-of-week + hour gate that refuses Saturday-night reboots."""

    def test_saturday_evening_reboot_refuses_window(self):
        # The 2026-05-10 incident shape. Saturday 22:57 ET, latest is a
        # Tuesday 11 days ago (cadence-days passes), missed Sunday
        # 2026-05-03. Pre-fix the wrapper PROCEEDed and started TTS.
        # Post-fix this must REFUSE:window before script-gen / TTS spend.
        out = evaluate(
            now_utc=_local(2026, 5, 9, 22, 57),  # Saturday
            latest_id="ep-001",
            latest_date=date(2026, 4, 28),
            min_days=6,
        )
        self.assertEqual(out, "REFUSE:window:11:ep-001:2026-04-28")

    def test_sunday_08_59_refuses_window(self):
        # One minute before the weekly anchor. Cadence-days would pass
        # (12 days since latest); window check refuses.
        out = evaluate(
            now_utc=_local(2026, 5, 10, 8, 59),  # Sunday before window
            latest_id="ep-001",
            latest_date=date(2026, 4, 28),
            min_days=6,
        )
        self.assertEqual(out, "REFUSE:window:12:ep-001:2026-04-28")

    def test_sunday_09_00_proceeds(self):
        # The canonical PROCEED case: Sunday 09:00 ET, latest >= MIN_DAYS
        # ago, current weekly window not yet filled.
        out = evaluate(
            now_utc=_local(2026, 5, 10, 9, 0),  # Sunday at window open
            latest_id="ep-001",
            latest_date=date(2026, 4, 28),
            min_days=6,
        )
        self.assertEqual(out, "PROCEED:12:ep-001:2026-04-28")

    def test_sunday_10_00_overslept_proceeds(self):
        # launchd missed 09:00 (machine asleep) and fires at 10:00.
        # Still in the open window.
        out = evaluate(
            now_utc=_local(2026, 5, 10, 10, 0),
            latest_id="ep-001",
            latest_date=date(2026, 4, 28),
            min_days=6,
        )
        self.assertEqual(out, "PROCEED:12:ep-001:2026-04-28")

    def test_sunday_window_already_filled_refuses(self):
        # Sunday 12:00 ET, this Sunday already published. Re-fire from
        # RunAtLoad must refuse on already-satisfied window.
        out = evaluate(
            now_utc=_local(2026, 5, 10, 12, 0),
            latest_id="ep-002",
            latest_date=date(2026, 5, 10),
            min_days=6,
        )
        # days_since=0 < min_days=6 → REFUSE:cadence wins (correct
        # layering — the rapid-reinvocation floor catches this case
        # before the window branch).
        self.assertEqual(out, "REFUSE:cadence:0:ep-002:2026-05-10")

    def test_sunday_window_already_filled_with_min_days_zero_refuses_window(self):
        # Force the window branch to fire by lowering min_days. Useful
        # for surfacing the window-level satisfied logic in isolation.
        out = evaluate(
            now_utc=_local(2026, 5, 10, 12, 0),
            latest_id="ep-002",
            latest_date=date(2026, 5, 10),
            min_days=0,
        )
        self.assertEqual(out, "REFUSE:window:0:ep-002:2026-05-10")

    def test_unknown_id_falls_back_to_unknown(self):
        out = evaluate(
            now_utc=_local(2026, 5, 9, 22, 57),
            latest_id=None,
            latest_date=date(2026, 4, 28),
            min_days=6,
        )
        self.assertEqual(out, "REFUSE:window:11:unknown:2026-04-28")

    def test_naive_now_utc_raises(self):
        with self.assertRaises(ValueError):
            evaluate(
                now_utc=datetime(2026, 5, 10, 13, 0),  # naive
                latest_id="ep-001",
                latest_date=date(2026, 4, 28),
                min_days=6,
            )


class BiweeklyMinDays(unittest.TestCase):
    """The 2026-05-17 retune from MIN_DAYS=6 to MIN_DAYS=13.

    Verifies the every-other-Sunday cadence emerges from a weekly launchd
    fire: the Sunday-after-publish (days_since=7) refuses, the next
    Sunday (days_since=14) proceeds, and non-Sunday fires past the
    min-days floor still refuse on the window check.
    """

    def test_sunday_day_7_refuses_at_min13(self):
        # Sunday 2026-05-17 09:00 ET, latest published Sunday 2026-05-10.
        # days_since=7 < min_days=13 → REFUSE:cadence (cadence floor wins
        # before the window check would have passed).
        out = evaluate(
            now_utc=_local(2026, 5, 17, 9, 0),
            latest_id="ep-002",
            latest_date=date(2026, 5, 10),
            min_days=13,
        )
        self.assertEqual(out, "REFUSE:cadence:7:ep-002:2026-05-10")

    def test_sunday_day_14_proceeds_at_min13(self):
        # Sunday 2026-05-24 09:00 ET, latest published Sunday 2026-05-10.
        # days_since=14 >= min_days=13 → cadence passes; Sunday in open
        # window → PROCEED. This is the next eligible publish date after
        # the retune.
        out = evaluate(
            now_utc=_local(2026, 5, 24, 9, 0),
            latest_id="ep-002",
            latest_date=date(2026, 5, 10),
            min_days=13,
        )
        self.assertEqual(out, "PROCEED:14:ep-002:2026-05-10")

    def test_wednesday_past_min_days_refuses_via_window(self):
        # Wed 2026-05-27, days_since=17 > min_days=13 → cadence passes.
        # Non-Sunday → REFUSE:window. Proves the day-of-week gate still
        # bites once the larger min-days floor is cleared.
        out = evaluate(
            now_utc=_local(2026, 5, 27, 9, 0),
            latest_id="ep-002",
            latest_date=date(2026, 5, 10),
            min_days=13,
        )
        self.assertEqual(out, "REFUSE:window:17:ep-002:2026-05-10")


class TestMainEntrypoint(unittest.TestCase):
    """Wraps the bash-facing CLI: argparse, episodes.json reading, stdout."""

    def _run(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(argv)
        return rc, buf.getvalue().strip()

    def test_no_episodes_file_proceeds(self):
        # Cold start: data/episodes.json doesn't exist yet. PROCEED so
        # the very first weekly run can publish.
        with tempfile.TemporaryDirectory() as td:
            ep_path = Path(td) / "episodes.json"  # does not exist
            rc, out = self._run([
                "--min-days", "6", "--episodes", str(ep_path),
            ])
            self.assertEqual(rc, 0)
            self.assertEqual(out, "PROCEED:no_episodes_json")

    def test_empty_episodes_proceeds(self):
        with tempfile.TemporaryDirectory() as td:
            ep_path = Path(td) / "episodes.json"
            ep_path.write_text("[]")
            rc, out = self._run([
                "--min-days", "6", "--episodes", str(ep_path),
            ])
            self.assertEqual(rc, 0)
            self.assertEqual(out, "PROCEED:empty_episodes")

    def test_malformed_date_raises(self):
        # The bash wrapper relies on this raise: a corrupt episodes.json
        # is a fail-loud signal, not a permissive PROCEED.
        with tempfile.TemporaryDirectory() as td:
            ep_path = Path(td) / "episodes.json"
            ep_path.write_text(json.dumps([
                {"id": "ep-001", "episodeNo": 1, "date": "not-a-date"},
            ]))
            with self.assertRaises(ValueError):
                self._run([
                    "--min-days", "6", "--episodes", str(ep_path),
                ])

    def test_picks_max_episode_no_not_last_in_file(self):
        # Most-recent is by episodeNo, not file order. Proves the
        # selection logic moved over from the bash inline cleanly.
        with tempfile.TemporaryDirectory() as td:
            ep_path = Path(td) / "episodes.json"
            ep_path.write_text(json.dumps([
                {"id": "ep-002", "episodeNo": 2, "date": "2026-05-10"},
                {"id": "ep-001", "episodeNo": 1, "date": "2026-04-28"},
            ]))
            rc, out = self._run([
                "--min-days", "6", "--episodes", str(ep_path),
            ])
            self.assertEqual(rc, 0)
            # Latest is ep-002 (2026-05-10). days_since on the test host
            # is non-deterministic, but the latest_id and date prefixes
            # are pinned: the line ends with `:ep-002:2026-05-10`.
            self.assertTrue(
                out.endswith(":ep-002:2026-05-10")
                or out == "PROCEED:no_episodes_json",
                f"unexpected output: {out!r}",
            )


if __name__ == "__main__":
    unittest.main()
