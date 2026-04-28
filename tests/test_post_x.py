"""Unit tests for src.post_x — X-post downstream consumer.

stdlib unittest only. Run via:

    .venv/bin/python -m unittest discover -s tests

Phase 2 RED-FIRST suite. The seams in src.post_x are stubs; every
test in this file is expected to fail with NotImplementedError until
Phase 3 lands. The point of running the suite now is to confirm the
test scaffolding wires up cleanly and the failures point at the right
places — not to assert correct behavior.

Coverage maps to the test contract in the locked spec:
  Discovery (gate logic)            → TestDiscoverNewPublishedDailyIds
  Catch-up selection                → TestSelectPostTarget
  Orchestrator + ordering invariant → TestRunPostX

Externals are injected callables (joke synth, tweet poster, URL probe)
so the orchestrator stays unit-testable with no live network.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.post_x import (
    discover_new_published_daily_ids,
    run_post_x,
    select_post_target,
)
from src.summarize import STANDARD_DISCLAIMER

# Phase 2 red-first scaffold. While src/post_x.py is stubbed, every
# test_* method on the decorated TestCases below is wrapped in
# `unittest.expectedFailure` — the suite RUNS, fails as expected, and
# CI (`.github/workflows/tests.yml`) stays green because expected
# failures aren't real failures. Two safety properties this preserves:
#   1. An accidental green (a stub that returns a coincidentally
#      correct value) trips `unexpectedSuccess` and DOES fail CI.
#   2. The tests are exercised on every push, not inert.
#
# Phase 3 cleanup is a one-line flip of `PHASE_3_IMPLEMENTED` to True.
# After the flip, `_phase2_red_first` becomes a no-op; tests run
# normally; CI is RED until production code in src/post_x.py satisfies
# every assertion. That is the intended forcing function.
#
# See memory `x_post_downstream_plan.md` for the contract these tests
# encode.
PHASE_3_IMPLEMENTED = True


def _phase2_red_first(cls):
    """Wrap every `test_*` method in `unittest.expectedFailure` while
    `PHASE_3_IMPLEMENTED` is False. No-op when the flag is True.
    """
    if PHASE_3_IMPLEMENTED:
        return cls
    for name in list(vars(cls)):
        attr = getattr(cls, name)
        if name.startswith("test_") and callable(attr):
            setattr(cls, name, unittest.expectedFailure(attr))
    return cls


def _brief(brief_id: str, *, status: str = "published",
           date_: str | None = None, issue: int = 1,
           title: str | None = None, dek: str | None = None) -> dict:
    """Brief shape that passes the Pydantic Brief schema."""
    return {
        "id": brief_id,
        "issueNo": issue,
        "date": date_ or brief_id,
        "title": title or f"title for {brief_id}",
        "dek": dek or "dek",
        "readingMinutes": 1,
        "tags": ["Agents"],
        "items": [],
        "status": status,
        "disclaimer": STANDARD_DISCLAIMER,
        "isSeed": None,
    }


@_phase2_red_first
class TestDiscoverNewPublishedDailyIds(unittest.TestCase):
    """Diff-gate: extract new published-daily ids from before/after."""

    def test_single_new_daily(self):
        self.assertEqual(
            discover_new_published_daily_ids([], [_brief("2026-04-28")]),
            ["2026-04-28"],
        )

    def test_multi_id_catchup_single_commit(self):
        # 65c3962 reconcile shape: two new daily ids in one commit.
        after = [_brief("2026-04-28"), _brief("2026-04-27")]
        self.assertEqual(
            discover_new_published_daily_ids([], after),
            ["2026-04-28", "2026-04-27"],
        )

    def test_weekly_id_ignored(self):
        after = [
            _brief("2026-W18", date_="2026-04-27"),
            _brief("2026-04-28"),
        ]
        self.assertEqual(
            discover_new_published_daily_ids([], after),
            ["2026-04-28"],
        )

    def test_draft_only_ignored(self):
        after = [_brief("2026-04-28", status="draft")]
        self.assertEqual(
            discover_new_published_daily_ids([], after),
            [],
        )

    def test_status_transition_draft_to_published_counts_as_new(self):
        before = [_brief("2026-04-28", status="draft")]
        after = [_brief("2026-04-28", status="published")]
        self.assertEqual(
            discover_new_published_daily_ids(before, after),
            ["2026-04-28"],
        )

    def test_already_published_in_before_not_surfaced(self):
        before = [_brief("2026-04-28")]
        after = [_brief("2026-04-28")]
        self.assertEqual(
            discover_new_published_daily_ids(before, after),
            [],
        )

    def test_deferred_push_multi_commit_range(self):
        # The push event delivered N commits at once (deferred-push
        # catch-up via _try_push). The diff over the full range must
        # surface every eligible new daily id, while filtering weekly
        # noise. Returned latest-first.
        before = []  # remote tip was empty before this push
        after = [
            _brief("2026-04-28"),
            _brief("2026-W18", date_="2026-04-27"),  # weekly — filtered
            _brief("2026-04-27"),
            _brief("2026-04-26"),
        ]
        self.assertEqual(
            discover_new_published_daily_ids(before, after),
            ["2026-04-28", "2026-04-27", "2026-04-26"],
        )


@_phase2_red_first
class TestSelectPostTarget(unittest.TestCase):
    """Catch-up policy: latest only; older eligibles become skipped_catchup."""

    def test_single_eligible(self):
        post_id, skipped = select_post_target(["2026-04-28"], set())
        self.assertEqual(post_id, "2026-04-28")
        self.assertEqual(skipped, [])

    def test_multi_id_picks_latest(self):
        post_id, skipped = select_post_target(
            ["2026-04-28", "2026-04-27", "2026-04-26"], set(),
        )
        self.assertEqual(post_id, "2026-04-28")
        self.assertEqual(skipped, ["2026-04-27", "2026-04-26"])

    def test_latest_already_posted_no_fall_through(self):
        # Retry replay: latest is in the sidecar. Must NOT fall
        # through to next-newest (that id was classified as
        # skipped_catchup on the original run and posting it now
        # would publish a catch-up item). Return None for post_id
        # and surface still-unrecorded older eligibles so the
        # orchestrator can complete the audit.
        post_id, skipped = select_post_target(
            ["2026-04-28", "2026-04-27"], {"2026-04-28"},
        )
        self.assertIsNone(post_id)
        self.assertEqual(skipped, ["2026-04-27"])

    def test_latest_already_posted_with_older_also_posted(self):
        # All eligibles already in sidecar — no post, no audit gaps.
        post_id, skipped = select_post_target(
            ["2026-04-28", "2026-04-27"],
            {"2026-04-28", "2026-04-27"},
        )
        self.assertIsNone(post_id)
        self.assertEqual(skipped, [])

    def test_all_already_posted_returns_none(self):
        post_id, skipped = select_post_target(
            ["2026-04-28"], {"2026-04-28"},
        )
        self.assertIsNone(post_id)
        self.assertEqual(skipped, [])

    def test_empty_eligible_returns_none(self):
        post_id, skipped = select_post_target([], set())
        self.assertIsNone(post_id)
        self.assertEqual(skipped, [])


@_phase2_red_first
class TestRunPostX(unittest.TestCase):
    """End-to-end orchestrator with injected callables for externals."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.sidecar_path = self.tmp / "x-posts.jsonl"
        self.tweet_calls: list[str] = []
        self.posted_tweet_id = "1234567890"

    def _ok_tweet_poster(self, text: str) -> str:
        self.tweet_calls.append(text)
        return self.posted_tweet_id

    def _failing_tweet_poster(self, text: str) -> str:
        self.tweet_calls.append(text)
        raise RuntimeError("simulated X API failure")

    def _ok_url_prober(self, url: str) -> bool:
        return True

    def _failing_url_prober(self, url: str) -> bool:
        return False

    def _joke(self, brief: dict) -> str:
        return f"[joke for {brief['id']}]"

    def _read_sidecar(self) -> list[dict]:
        if not self.sidecar_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.sidecar_path.read_text().splitlines()
            if line.strip()
        ]

    def _seed_sidecar(self, entries: list[dict]) -> None:
        self.sidecar_path.write_text(
            "\n".join(json.dumps(e) for e in entries) + "\n"
        )

    # ─── Spec test 1: new daily published brief → posts + sidecar ──────

    def test_new_daily_posts_and_appends_sidecar(self):
        rc = run_post_x(
            self.sidecar_path, [], [_brief("2026-04-28")],
            joke_synthesizer=self._joke,
            tweet_poster=self._ok_tweet_poster,
            url_prober=self._ok_url_prober,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.tweet_calls), 1)
        sidecar = self._read_sidecar()
        self.assertEqual(len(sidecar), 1)
        self.assertEqual(sidecar[0]["id"], "2026-04-28")
        self.assertEqual(sidecar[0]["tweet_id"], self.posted_tweet_id)

    # ─── Spec test 2: already-posted brief → no-op ─────────────────────

    def test_already_posted_brief_is_no_op(self):
        self._seed_sidecar([
            {"id": "2026-04-28", "tweet_id": "999",
             "url": "https://news.oddessentials.ai/brief/2026-04-28"},
        ])
        rc = run_post_x(
            self.sidecar_path, [], [_brief("2026-04-28")],
            joke_synthesizer=self._joke,
            tweet_poster=self._ok_tweet_poster,
            url_prober=self._ok_url_prober,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(self.tweet_calls, [])
        self.assertEqual(len(self._read_sidecar()), 1)  # unchanged

    # ─── Spec test 3: draft-only brief → no-op ─────────────────────────

    def test_draft_only_brief_is_no_op(self):
        rc = run_post_x(
            self.sidecar_path, [],
            [_brief("2026-04-28", status="draft")],
            joke_synthesizer=self._joke,
            tweet_poster=self._ok_tweet_poster,
            url_prober=self._ok_url_prober,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(self.tweet_calls, [])
        self.assertEqual(self._read_sidecar(), [])

    # ─── Spec test 4: weekly id → ignored ──────────────────────────────

    def test_weekly_id_ignored(self):
        rc = run_post_x(
            self.sidecar_path, [],
            [_brief("2026-W18", date_="2026-04-27")],
            joke_synthesizer=self._joke,
            tweet_poster=self._ok_tweet_poster,
            url_prober=self._ok_url_prober,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(self.tweet_calls, [])
        self.assertEqual(self._read_sidecar(), [])

    # ─── Spec test 5: failed X post → no sidecar entry (ordering) ──────

    def test_failed_tweet_leaves_no_sidecar_entry(self):
        with self.assertRaises(RuntimeError):
            run_post_x(
                self.sidecar_path, [], [_brief("2026-04-28")],
                joke_synthesizer=self._joke,
                tweet_poster=self._failing_tweet_poster,
                url_prober=self._ok_url_prober,
            )
        # Tweet WAS attempted (the mock recorded the call) but the
        # orchestrator must NOT have written a sidecar row.
        self.assertEqual(len(self.tweet_calls), 1)
        self.assertEqual(self._read_sidecar(), [])

    # ─── Spec test 6: replay same input → zero new side effects ────────

    def test_replay_same_input_zero_new_side_effects(self):
        after = [_brief("2026-04-28")]
        rc1 = run_post_x(
            self.sidecar_path, [], after,
            joke_synthesizer=self._joke,
            tweet_poster=self._ok_tweet_poster,
            url_prober=self._ok_url_prober,
        )
        rc2 = run_post_x(
            self.sidecar_path, [], after,
            joke_synthesizer=self._joke,
            tweet_poster=self._ok_tweet_poster,
            url_prober=self._ok_url_prober,
        )
        self.assertEqual(rc1, 0)
        self.assertEqual(rc2, 0)
        self.assertEqual(len(self.tweet_calls), 1)  # exactly one tweet
        self.assertEqual(len(self._read_sidecar()), 1)  # exactly one row

    # ─── Spec test 7: pre-populated sidecar (recovery scenario) ────────

    def test_sidecar_pre_populated_recovery_scenario(self):
        # Operator manually appended an entry after a partial failure
        # (e.g., tweet sent but commit-back died). A subsequent run
        # against the same id must respect the sidecar.
        self._seed_sidecar([
            {"id": "2026-04-28", "tweet_id": "manual-recovery",
             "url": "https://news.oddessentials.ai/brief/2026-04-28"},
        ])
        rc = run_post_x(
            self.sidecar_path, [], [_brief("2026-04-28")],
            joke_synthesizer=self._joke,
            tweet_poster=self._ok_tweet_poster,
            url_prober=self._ok_url_prober,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(self.tweet_calls, [])

    # ─── Partial-recovery: latest in sidecar, older eligibles missing ──

    def test_partial_recovery_completes_audit_no_new_post(self):
        # Sidecar has the latest-posted entry from a prior run, but
        # the prior run's catch-up rows for older eligibles never
        # landed (process crashed between writes, or manual repair
        # added only the latest). Orchestrator must NOT post — the
        # next-newest is a skipped_catchup id by classification — but
        # SHOULD complete the audit by writing the missing rows.
        self._seed_sidecar([
            {"id": "2026-04-28", "tweet_id": "abc",
             "url": "https://news.oddessentials.ai/brief/2026-04-28"},
        ])
        after = [
            _brief("2026-04-28"),
            _brief("2026-04-27"),
            _brief("2026-04-26"),
        ]
        rc = run_post_x(
            self.sidecar_path, [], after,
            joke_synthesizer=self._joke,
            tweet_poster=self._ok_tweet_poster,
            url_prober=self._ok_url_prober,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(self.tweet_calls, [])  # NO new tweet
        sidecar = self._read_sidecar()
        # 1 original posted + 2 newly-completed skipped_catchup = 3
        self.assertEqual(len(sidecar), 3)
        skipped_ids = {
            r["id"] for r in sidecar
            if r.get("status") == "skipped_catchup"
        }
        self.assertEqual(skipped_ids, {"2026-04-27", "2026-04-26"})

    # ─── Skipped_catchup rows count as consumed on rerun ───────────────

    def test_skipped_catchup_id_not_promoted_on_rerun(self):
        # Natural state after a successful multi-id run: latest as
        # `posted`, older eligibles as `skipped_catchup`. A subsequent
        # workflow_dispatch on the same data must NOT promote a
        # skipped_catchup id into a tweet — both row types are
        # consumed by the sidecar dedupe.
        self._seed_sidecar([
            {"id": "2026-04-28", "tweet_id": "abc",
             "url": "https://news.oddessentials.ai/brief/2026-04-28"},
            {"id": "2026-04-27", "status": "skipped_catchup"},
            {"id": "2026-04-26", "status": "skipped_catchup"},
        ])
        after = [
            _brief("2026-04-28"),
            _brief("2026-04-27"),
            _brief("2026-04-26"),
        ]
        rc = run_post_x(
            self.sidecar_path, [], after,
            joke_synthesizer=self._joke,
            tweet_poster=self._ok_tweet_poster,
            url_prober=self._ok_url_prober,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(self.tweet_calls, [])
        self.assertEqual(len(self._read_sidecar()), 3)  # unchanged

    # ─── Spec test 8: multi-id single commit → latest + skipped_catchup ─

    def test_multi_id_single_commit_posts_latest_skips_rest(self):
        after = [
            _brief("2026-04-28"),
            _brief("2026-04-27"),
            _brief("2026-04-26"),
        ]
        rc = run_post_x(
            self.sidecar_path, [], after,
            joke_synthesizer=self._joke,
            tweet_poster=self._ok_tweet_poster,
            url_prober=self._ok_url_prober,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.tweet_calls), 1)
        sidecar = self._read_sidecar()
        # 1 posted + 2 skipped_catchup
        self.assertEqual(len(sidecar), 3)
        posted = [r for r in sidecar if r.get("status") != "skipped_catchup"]
        skipped = [r for r in sidecar if r.get("status") == "skipped_catchup"]
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0]["id"], "2026-04-28")
        self.assertEqual(
            {s["id"] for s in skipped},
            {"2026-04-27", "2026-04-26"},
        )

    # ─── Spec test 9 (NEW): deferred-push multi-commit range ───────────

    def test_deferred_push_multi_commit_range_posts_latest_skips_rest(self):
        # Push event delivers N commits at once (deferred-push catch-up).
        # Diff over the full range surfaces 3 new daily ids + 1 weekly.
        # Orchestrator must: post latest only, write skipped_catchup for
        # the two older daily ids, and ignore the weekly entirely.
        before: list[dict] = []
        after = [
            _brief("2026-04-28"),
            _brief("2026-W18", date_="2026-04-27"),  # weekly — filtered
            _brief("2026-04-27"),
            _brief("2026-04-26"),
        ]
        rc = run_post_x(
            self.sidecar_path, before, after,
            joke_synthesizer=self._joke,
            tweet_poster=self._ok_tweet_poster,
            url_prober=self._ok_url_prober,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.tweet_calls), 1)
        sidecar = self._read_sidecar()
        self.assertEqual(len(sidecar), 3)  # 1 posted + 2 skipped_catchup
        posted = [r for r in sidecar if r.get("status") != "skipped_catchup"]
        skipped = [r for r in sidecar if r.get("status") == "skipped_catchup"]
        self.assertEqual(posted[0]["id"], "2026-04-28")
        self.assertEqual(
            {s["id"] for s in skipped},
            {"2026-04-27", "2026-04-26"},
        )
        # No weekly id appears anywhere in the sidecar.
        self.assertNotIn("2026-W18", {r["id"] for r in sidecar})

    # ─── Pages 404 probe: aborts cleanly, no side effects ──────────────

    def test_url_probe_failure_aborts_with_no_side_effects(self):
        rc = run_post_x(
            self.sidecar_path, [], [_brief("2026-04-28")],
            joke_synthesizer=self._joke,
            tweet_poster=self._ok_tweet_poster,
            url_prober=self._failing_url_prober,
        )
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.tweet_calls, [])
        self.assertEqual(self._read_sidecar(), [])


if __name__ == "__main__":
    unittest.main()
