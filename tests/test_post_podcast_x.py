"""Unit tests for src.post_podcast_x — podcast X-post downstream consumer.

stdlib unittest only. Run via:

    .venv/bin/python -m unittest discover -s tests

Mirrors tests/test_post_x.py with episode-specific shapes. Network +
Anthropic + tweepy paths are NOT covered here — the orchestrator
(run_post_podcast_x) is fully testable with injected callables.

Coverage:

  TestDiscoverNewPublishedEpisodeIds — diff gate, episodeNo desc sort
  TestSelectPostTarget               — catch-up policy (mirrors daily)
  TestRunPostPodcastX                — orchestrator + ordering invariant
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.post_podcast_x import (
    discover_new_published_episode_ids,
    run_post_podcast_x,
    select_post_target,
)


def _episode(episode_id: str, *, episode_no: int = 1, title: str | None = None,
             description: str | None = None) -> dict:
    """Episode shape that passes EpisodeRecord validation if you want to feed it
    through the schema. Tests below use it directly without that step."""
    return {
        "id": episode_id,
        "episodeNo": episode_no,
        "title": title or f"Title for {episode_id}",
        "date": "2026-04-28",
        "durationMinutes": 4,
        "youtubeId": "abc123",
        "description": description or "Description that is long enough to satisfy the schema.",
        "hosts": ["Shrimp"],
    }


class TestDiscoverNewPublishedEpisodeIds(unittest.TestCase):
    def test_initial_publish_returns_single_id(self):
        self.assertEqual(
            discover_new_published_episode_ids(
                [],
                [_episode("ep-001", episode_no=1)],
            ),
            ["ep-001"],
        )

    def test_already_present_in_before_not_surfaced(self):
        before = [_episode("ep-001", episode_no=1)]
        after = [_episode("ep-001", episode_no=1)]
        self.assertEqual(
            discover_new_published_episode_ids(before, after),
            [],
        )

    def test_multiple_new_episodes_sorted_newest_first(self):
        # episodeNo desc — ep-003 is newest.
        after = [
            _episode("ep-001", episode_no=1),
            _episode("ep-003", episode_no=3),
            _episode("ep-002", episode_no=2),
        ]
        self.assertEqual(
            discover_new_published_episode_ids([], after),
            ["ep-003", "ep-002", "ep-001"],
        )

    def test_id_descending_tiebreak_when_episode_no_collides(self):
        # Defensive: if two entries somehow share an episodeNo (re-issue
        # of the same episode under a new id), sort by id descending so
        # the order is deterministic.
        after = [
            _episode("ep-001a", episode_no=1),
            _episode("ep-001b", episode_no=1),
        ]
        self.assertEqual(
            discover_new_published_episode_ids([], after),
            ["ep-001b", "ep-001a"],
        )

    def test_only_genuinely_new_ids_surface_in_catchup(self):
        # before has ep-001; after adds ep-002 + ep-003. Only the new
        # two surface, newest first.
        before = [_episode("ep-001", episode_no=1)]
        after = [
            _episode("ep-001", episode_no=1),
            _episode("ep-002", episode_no=2),
            _episode("ep-003", episode_no=3),
        ]
        self.assertEqual(
            discover_new_published_episode_ids(before, after),
            ["ep-003", "ep-002"],
        )


class TestSelectPostTarget(unittest.TestCase):
    """Mirror of the daily flow's select_post_target tests — identical
    contract since the function is shape-equivalent."""

    def test_single_eligible(self):
        post_id, skipped = select_post_target(["ep-001"], set())
        self.assertEqual(post_id, "ep-001")
        self.assertEqual(skipped, [])

    def test_multi_id_picks_latest_others_become_catchup(self):
        post_id, skipped = select_post_target(
            ["ep-003", "ep-002", "ep-001"], set(),
        )
        self.assertEqual(post_id, "ep-003")
        self.assertEqual(skipped, ["ep-002", "ep-001"])

    def test_latest_already_posted_no_fall_through(self):
        # Replay: latest already in sidecar. Must NOT post the
        # next-newest (it was classified as skipped_catchup on the
        # original run).
        post_id, skipped = select_post_target(
            ["ep-002", "ep-001"], {"ep-002"},
        )
        self.assertIsNone(post_id)
        self.assertEqual(skipped, ["ep-001"])

    def test_all_already_posted_returns_none(self):
        post_id, skipped = select_post_target(
            ["ep-002", "ep-001"], {"ep-001", "ep-002"},
        )
        self.assertIsNone(post_id)
        self.assertEqual(skipped, [])

    def test_empty_eligibles(self):
        self.assertEqual(
            select_post_target([], set()),
            (None, []),
        )


class TestRunPostPodcastX(unittest.TestCase):
    """End-to-end orchestrator with injected callables. The ordering
    invariant — tweet BEFORE sidecar write — is the load-bearing
    property covered here."""

    def _setup(self, td: Path, after: list[dict], before: list[dict] | None = None):
        sidecar = td / "podcast-x-posts.jsonl"
        return sidecar, before or [], after

    def test_initial_publish_tweets_and_writes_sidecar(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            sidecar, before, after = self._setup(
                tdp, [_episode("ep-001", episode_no=1, title="Title")]
            )
            tweet_calls: list[str] = []

            def joke(_e):
                return "joke text"

            def post(text):
                tweet_calls.append(text)
                return "tweet-id-1"

            rc = run_post_podcast_x(
                sidecar, before, after,
                joke_synthesizer=joke,
                tweet_poster=post,
                url_prober=lambda _u: True,
            )
            self.assertEqual(rc, 0)
            self.assertEqual(len(tweet_calls), 1)
            self.assertEqual(
                tweet_calls[0],
                "joke text\nhttps://news.oddessentials.ai/podcast/ep-001",
            )

            entries = [
                json.loads(line)
                for line in sidecar.read_text().splitlines() if line.strip()
            ]
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["id"], "ep-001")
            self.assertEqual(entries[0]["tweet_id"], "tweet-id-1")
            self.assertEqual(
                entries[0]["url"], "https://news.oddessentials.ai/podcast/ep-001",
            )
            self.assertIn("posted_at", entries[0])

    def test_url_prober_failure_aborts_with_no_tweet_no_sidecar(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            sidecar, before, after = self._setup(tdp, [_episode("ep-001")])

            def post(_t):
                raise AssertionError("tweet must not be called when probe fails")

            rc = run_post_podcast_x(
                sidecar, before, after,
                joke_synthesizer=lambda _e: "joke",
                tweet_poster=post,
                url_prober=lambda _u: False,
            )
            self.assertEqual(rc, 1)
            self.assertFalse(sidecar.exists())

    def test_tweet_poster_exception_leaves_no_sidecar_entry(self):
        # Ordering invariant: any exception from tweet_poster must
        # propagate AND the sidecar must NOT have a row for the failed
        # post. Replay-safe.
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            sidecar, before, after = self._setup(tdp, [_episode("ep-001")])

            def post(_t):
                raise RuntimeError("simulated X 503")

            with self.assertRaises(RuntimeError):
                run_post_podcast_x(
                    sidecar, before, after,
                    joke_synthesizer=lambda _e: "joke",
                    tweet_poster=post,
                    url_prober=lambda _u: True,
                )
            self.assertFalse(sidecar.exists())

    def test_catchup_writes_skipped_rows_after_tweet(self):
        # Multi-id catch-up: ep-003 tweets, ep-002 + ep-001 become
        # skipped_catchup rows. Sidecar has 1 posted + 2 skipped.
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            after = [
                _episode("ep-001", episode_no=1),
                _episode("ep-002", episode_no=2),
                _episode("ep-003", episode_no=3),
            ]
            sidecar, before, after = self._setup(tdp, after)

            def post(_t):
                return "tid-3"

            rc = run_post_podcast_x(
                sidecar, before, after,
                joke_synthesizer=lambda _e: "joke",
                tweet_poster=post,
                url_prober=lambda _u: True,
            )
            self.assertEqual(rc, 0)
            entries = [
                json.loads(line)
                for line in sidecar.read_text().splitlines() if line.strip()
            ]
            self.assertEqual(len(entries), 3)
            self.assertEqual(entries[0]["id"], "ep-003")
            self.assertEqual(entries[0]["tweet_id"], "tid-3")
            self.assertEqual(entries[1]["status"], "skipped_catchup")
            self.assertEqual(entries[1]["id"], "ep-002")
            self.assertEqual(entries[2]["status"], "skipped_catchup")
            self.assertEqual(entries[2]["id"], "ep-001")

    def test_replay_with_latest_in_sidecar_writes_catchup_audit_only(self):
        # Sidecar already has ep-002 (latest tweeted on prior run). On
        # replay, must NOT post anything; must complete the audit by
        # writing skipped_catchup row for the still-unrecorded ep-001.
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            sidecar = tdp / "podcast-x-posts.jsonl"
            sidecar.write_text(
                json.dumps({
                    "id": "ep-002",
                    "tweet_id": "prior-tid",
                    "url": "https://news.oddessentials.ai/podcast/ep-002",
                    "posted_at": "2026-04-28T00:00:00+00:00",
                }) + "\n"
            )
            after = [
                _episode("ep-001", episode_no=1),
                _episode("ep-002", episode_no=2),
            ]

            def post(_t):
                raise AssertionError("tweet must not be called on replay")

            rc = run_post_podcast_x(
                sidecar, [], after,
                joke_synthesizer=lambda _e: "joke",
                tweet_poster=post,
                url_prober=lambda _u: True,
            )
            self.assertEqual(rc, 0)
            entries = [
                json.loads(line)
                for line in sidecar.read_text().splitlines() if line.strip()
            ]
            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[0]["id"], "ep-002")  # original
            self.assertEqual(entries[1]["id"], "ep-001")  # new catchup
            self.assertEqual(entries[1]["status"], "skipped_catchup")


if __name__ == "__main__":
    unittest.main()
