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

import contextlib
import io
import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

from src.editorial_time import EDITORIAL_TZ
from src.publish import (
    _emit_per_brief_pages,
    _emit_per_episode_pages,
    _load_publish_record_ids,
    _reconcile_finalization,
    _render_per_brief_html,
    discover_work,
    merge_brief,
    run_daily_publish,
)
from src.summarize import STANDARD_DISCLAIMER


# Mirrors agent-brief/client/index.html's <head> block: the seven targeted tags
# the renderer rewrites + the static tags (image, card, site_name) the renderer
# must NOT touch. Used by both the renderer tests and the emit-loop tests.
_TEMPLATE_HTML = """\
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <title>The Agent Brief — Daily AI Agent News</title>
    <meta property="og:title" content="The Agent Brief — Daily AI Agent News" />
    <meta property="og:description" content="A short, daily brief on AI agents." />
    <meta property="og:image" content="https://news.oddessentials.ai/og-image.png" />
    <meta property="og:image:width" content="1200" />
    <meta property="og:image:height" content="630" />
    <meta property="og:url" content="https://news.oddessentials.ai/" />
    <meta property="og:type" content="website" />
    <meta property="og:site_name" content="Agent Brief Daily" />
    <meta name="twitter:card" content="summary_large_image" />
    <meta name="twitter:title" content="The Agent Brief — Daily AI Agent News" />
    <meta name="twitter:description" content="A short, daily brief on AI agents." />
    <meta name="twitter:image" content="https://news.oddessentials.ai/og-image.png" />
  </head>
  <body><div id="root"></div></body>
</html>
"""


def _brief(brief_id: str, *, status: str = "published", date_: str | None = None,
           issue: int = 1, title: str | None = None, dek: str = "d") -> dict:
    """Brief shape that passes the Pydantic Brief schema."""
    return {
        "id": brief_id,
        "issueNo": issue,
        "date": date_ or brief_id,
        "title": title if title is not None else f"title for {brief_id}",
        "dek": dek,
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


class TestRenderPerBriefHtml(unittest.TestCase):
    """Per-brief OG/Twitter meta rewriter. Pure function — no I/O."""

    def test_basic_render_replaces_seven_tags(self):
        brief = _brief("2026-04-28", issue=118,
                       title="Headline of the day", dek="A subtitle.")
        out = _render_per_brief_html(_TEMPLATE_HTML, brief)
        self.assertIn(
            "<title>Headline of the day — Agent Brief Daily</title>", out)
        self.assertIn(
            '<meta property="og:title" content="Headline of the day" />', out)
        self.assertIn(
            '<meta property="og:description" content="Issue 118 · A subtitle." />',
            out)
        self.assertIn(
            '<meta property="og:url" '
            'content="https://news.oddessentials.ai/brief/2026-04-28" />',
            out)
        self.assertIn(
            '<meta property="og:type" content="article" />', out)
        self.assertIn(
            '<meta name="twitter:title" content="Headline of the day" />', out)
        self.assertIn(
            '<meta name="twitter:description" content="Issue 118 · A subtitle." />',
            out)

    def test_html_escaping_title(self):
        brief = _brief("2026-04-28", issue=1,
                       title='Foo "bar" & <baz>')
        out = _render_per_brief_html(_TEMPLATE_HTML, brief)
        self.assertIn(
            '<meta property="og:title" '
            'content="Foo &quot;bar&quot; &amp; &lt;baz&gt;" />',
            out)
        self.assertIn(
            '<meta name="twitter:title" '
            'content="Foo &quot;bar&quot; &amp; &lt;baz&gt;" />',
            out)
        # The unescaped form must not survive into the rendered output.
        self.assertNotIn('Foo "bar" & <baz>', out)

    def test_html_escaping_dek(self):
        brief = _brief("2026-04-28", issue=1,
                       dek='A & B "quoted" <c>')
        out = _render_per_brief_html(_TEMPLATE_HTML, brief)
        self.assertIn(
            '<meta property="og:description" '
            'content="Issue 1 · A &amp; B &quot;quoted&quot; &lt;c&gt;" />',
            out)
        self.assertIn(
            '<meta name="twitter:description" '
            'content="Issue 1 · A &amp; B &quot;quoted&quot; &lt;c&gt;" />',
            out)
        self.assertNotIn('A & B "quoted" <c>', out)

    def test_description_includes_issue_number_and_escaped_dek(self):
        """Locked spec: og:description = 'Issue {issueNo} · {dek}', dek escaped."""
        brief = _brief("2026-04-28", issue=99,
                       dek='A & B "quoted"')
        out = _render_per_brief_html(_TEMPLATE_HTML, brief)
        # Both descriptions carry the issue number AND the escaped dek.
        expected = (
            'content="Issue 99 · A &amp; B &quot;quoted&quot;" />'
        )
        self.assertIn('<meta property="og:description" ' + expected, out)
        self.assertIn('<meta name="twitter:description" ' + expected, out)
        # Unescaped dek must not appear.
        self.assertNotIn('A & B "quoted"', out)

    def test_og_url_uses_brief_id(self):
        brief = _brief("2026-12-31", issue=1)
        out = _render_per_brief_html(_TEMPLATE_HTML, brief)
        self.assertIn(
            '<meta property="og:url" '
            'content="https://news.oddessentials.ai/brief/2026-12-31" />',
            out)

    def test_og_type_flipped_to_article(self):
        brief = _brief("2026-04-28", issue=1)
        out = _render_per_brief_html(_TEMPLATE_HTML, brief)
        self.assertIn('<meta property="og:type" content="article" />', out)
        self.assertNotIn(
            '<meta property="og:type" content="website" />', out)

    def test_static_image_and_card_tags_unchanged(self):
        brief = _brief("2026-04-28", issue=1)
        out = _render_per_brief_html(_TEMPLATE_HTML, brief)
        # Image, card, and site_name tags must pass through verbatim — those
        # are the static-across-all-pages meta the renderer must NOT touch.
        for unchanged in (
            '<meta property="og:image" '
            'content="https://news.oddessentials.ai/og-image.png" />',
            '<meta property="og:image:width" content="1200" />',
            '<meta property="og:image:height" content="630" />',
            '<meta property="og:site_name" content="Agent Brief Daily" />',
            '<meta name="twitter:card" content="summary_large_image" />',
            '<meta name="twitter:image" '
            'content="https://news.oddessentials.ai/og-image.png" />',
        ):
            self.assertIn(unchanged, out)

    def test_missing_meta_raises_on_drift(self):
        # Strip og:title to simulate a future SPA refactor that drops it.
        broken = _TEMPLATE_HTML.replace(
            '<meta property="og:title" '
            'content="The Agent Brief — Daily AI Agent News" />',
            "",
        )
        brief = _brief("2026-04-28", issue=1)
        with self.assertRaises(RuntimeError) as cm:
            _render_per_brief_html(broken, brief)
        msg = str(cm.exception)
        self.assertIn("drifted", msg)
        self.assertIn('og:title', msg)

    def test_duplicate_meta_raises_on_drift(self):
        # Two og:url tags also count as drift (count != 1).
        broken = _TEMPLATE_HTML.replace(
            '<meta property="og:url" content="https://news.oddessentials.ai/" />',
            '<meta property="og:url" content="https://news.oddessentials.ai/" />\n'
            '    <meta property="og:url" '
            'content="https://news.oddessentials.ai/extra" />',
        )
        brief = _brief("2026-04-28", issue=1)
        with self.assertRaises(RuntimeError) as cm:
            _render_per_brief_html(broken, brief)
        self.assertIn("drifted", str(cm.exception))


class TestEmitPerBriefPages(unittest.TestCase):
    """Per-brief emit loop. Filters to daily-slugged briefs only — weeklies
    and any other non-daily slug do not produce static OG-card pages.
    """

    def test_emits_only_daily_slugged_briefs(self):
        with tempfile.TemporaryDirectory() as tmp:
            docs_root = Path(tmp)
            briefs = [
                _brief("2026-04-28", issue=118, title="Daily one"),
                _brief("2026-W18", date_="2026-04-27", issue=18,
                       title="Weekly legacy"),
                _brief("2026-04-27", issue=117, title="Daily two"),
            ]
            emitted = _emit_per_brief_pages(briefs, _TEMPLATE_HTML, docs_root)
            self.assertEqual(set(emitted), {"2026-04-28", "2026-04-27"})
            self.assertTrue(
                (docs_root / "brief" / "2026-04-28" / "index.html").exists())
            self.assertTrue(
                (docs_root / "brief" / "2026-04-27" / "index.html").exists())
            # Weekly id must NOT produce any directory or file.
            self.assertFalse((docs_root / "brief" / "2026-W18").exists())

    def test_empty_briefs_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            docs_root = Path(tmp)
            emitted = _emit_per_brief_pages([], _TEMPLATE_HTML, docs_root)
            self.assertEqual(emitted, [])
            # The brief/ directory should not even exist.
            self.assertFalse((docs_root / "brief").exists())

    def test_all_weekly_briefs_writes_nothing(self):
        # Defensive: a briefs list of only legacy weeklies emits zero pages
        # AND zero directories — no orphan `brief/` shell.
        with tempfile.TemporaryDirectory() as tmp:
            docs_root = Path(tmp)
            briefs = [
                _brief("2026-W17", date_="2026-04-20", issue=17),
                _brief("2026-W18", date_="2026-04-27", issue=18),
            ]
            emitted = _emit_per_brief_pages(briefs, _TEMPLATE_HTML, docs_root)
            self.assertEqual(emitted, [])
            self.assertFalse((docs_root / "brief").exists())


class TestEmitPerEpisodePages(unittest.TestCase):
    """Per-episode emit loop. Reads data/episodes.json (engine-owned) and
    re-emits docs/podcast/<id>/index.html on each daily build so vite's
    emptyOutDir wipe doesn't strand the per-episode OG artifacts.
    """

    def _setup(self, tmp: Path, episodes_payload: object):
        docs_root = tmp / "docs"
        docs_root.mkdir()
        data_dir = tmp / "data"
        data_dir.mkdir()
        episodes_path = data_dir / "episodes.json"
        episodes_path.write_text(json.dumps(episodes_payload))
        return docs_root, data_dir

    def _record(self, **overrides) -> dict:
        payload = {
            "id": "ep-001",
            "episodeNo": 1,
            "title": "Episode 1 title",
            "date": "2026-04-28",
            "durationMinutes": 4,
            "youtubeId": "abc123",
            "description": "An episode description that is long enough to satisfy the schema.",
            "hosts": ["Shrimp"],
        }
        payload.update(overrides)
        return payload

    def _patch_data_dir(self, data_dir: Path):
        from src import publish as publish_module
        return mock.patch.object(publish_module, "DATA_DIR", data_dir)

    def test_emits_for_each_published_episode(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpp = Path(tmp)
            docs_root, data_dir = self._setup(
                tmpp,
                [self._record(id="ep-001", episodeNo=1),
                 self._record(id="ep-002", episodeNo=2, title="Episode 2 title")],
            )
            with self._patch_data_dir(data_dir):
                emitted = _emit_per_episode_pages(_TEMPLATE_HTML, docs_root)
            self.assertEqual(set(emitted), {"ep-001", "ep-002"})
            for eid in ("ep-001", "ep-002"):
                self.assertTrue(
                    (docs_root / "podcast" / eid / "index.html").exists()
                )

    def test_missing_episodes_json_is_silent_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpp = Path(tmp)
            docs_root = tmpp / "docs"
            docs_root.mkdir()
            data_dir = tmpp / "data"
            data_dir.mkdir()
            with self._patch_data_dir(data_dir):
                emitted = _emit_per_episode_pages(_TEMPLATE_HTML, docs_root)
            self.assertEqual(emitted, [])
            self.assertFalse((docs_root / "podcast").exists())

    def test_empty_list_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpp = Path(tmp)
            docs_root, data_dir = self._setup(tmpp, [])
            with self._patch_data_dir(data_dir):
                emitted = _emit_per_episode_pages(_TEMPLATE_HTML, docs_root)
            self.assertEqual(emitted, [])
            self.assertFalse((docs_root / "podcast").exists())

    def test_malformed_entry_skipped_does_not_block_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpp = Path(tmp)
            docs_root, data_dir = self._setup(
                tmpp,
                [
                    {"id": "ep-001"},  # missing required fields → skip
                    self._record(id="ep-002", episodeNo=2),
                ],
            )
            with self._patch_data_dir(data_dir):
                emitted = _emit_per_episode_pages(_TEMPLATE_HTML, docs_root)
            self.assertEqual(emitted, ["ep-002"])
            self.assertTrue(
                (docs_root / "podcast" / "ep-002" / "index.html").exists()
            )
            self.assertFalse((docs_root / "podcast" / "ep-001").exists())

    def test_corrupt_json_silent_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpp = Path(tmp)
            docs_root = tmpp / "docs"
            docs_root.mkdir()
            data_dir = tmpp / "data"
            data_dir.mkdir()
            (data_dir / "episodes.json").write_text("{not json")
            with self._patch_data_dir(data_dir):
                emitted = _emit_per_episode_pages(_TEMPLATE_HTML, docs_root)
            self.assertEqual(emitted, [])


class TestEditorialTimeGuard(unittest.TestCase):
    """Locks down the 2026-04-29 incident: a UTC-derived `today` plus
    `RunAtLoad=true` published the next local-date's brief 6.5 hours
    early. The orchestrator now anchors `today` and the publish window
    to America/New_York. See plans/incident-2026-04-29-runatload-utc.md.

    Tests exercise the dry-run path because it executes the same
    editorial-date selection and the same per-date guard logic as the
    real flow, without touching git/build/network.
    """

    FLOOR_PUBLISHED = [
        # Briefs.json fixture: caught-up through April 29.
        {"id": "2026-04-27", "issueNo": 117, "date": "2026-04-27",
         "title": "April 27", "dek": "d", "readingMinutes": 1,
         "tags": ["Agents"], "items": [], "status": "published",
         "disclaimer": STANDARD_DISCLAIMER, "isSeed": None},
        {"id": "2026-04-28", "issueNo": 118, "date": "2026-04-28",
         "title": "April 28", "dek": "d", "readingMinutes": 1,
         "tags": ["Agents"], "items": [], "status": "published",
         "disclaimer": STANDARD_DISCLAIMER, "isSeed": None},
        {"id": "2026-04-29", "issueNo": 119, "date": "2026-04-29",
         "title": "April 29", "dek": "d", "readingMinutes": 1,
         "tags": ["Agents"], "items": [], "status": "published",
         "disclaimer": STANDARD_DISCLAIMER, "isSeed": None},
    ]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.runs_path = self.tmp_path / "runs.jsonl"
        self.digests_dir = self.tmp_path / "digests"
        self.digests_dir.mkdir()
        self._patches = [
            mock.patch("src.publish.RUNS_PATH", self.runs_path),
            mock.patch("src.summarize.RUNS_PATH", self.runs_path),
            mock.patch(
                "src.publish._draft_path",
                lambda d: self.digests_dir / d.isoformat() / "summary.json",
            ),
            mock.patch("src.publish._commits_ahead", return_value=0),
        ]
        for p in self._patches:
            p.start()
        # _load_briefs gets its own per-test mock so individual tests can
        # override the return value without breaking setUp's patches.
        self._load_briefs_patch = mock.patch(
            "src.publish._load_briefs",
            return_value=list(self.FLOOR_PUBLISHED),
        )
        self._load_briefs_mock = self._load_briefs_patch.start()

    def tearDown(self):
        self._load_briefs_patch.stop()
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def _dry_run_at(self, when_utc: datetime, briefs=None) -> str:
        if briefs is not None:
            self._load_briefs_mock.return_value = list(briefs)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = run_daily_publish(dry_run=True, now_utc=when_utc)
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def test_2026_04_29_22_40_EDT_does_not_target_apr_30(self):
        # The exact incident moment. Editorial today=April 29 (already
        # published) — discover_work returns no candidates and April 30
        # is never named in the output.
        out = self._dry_run_at(self._local_to_utc(2026, 4, 29, 22, 40))
        self.assertIn("today=2026-04-29", out)
        self.assertIn("no candidates", out)
        self.assertNotIn("2026-04-30", out)

    def test_2026_04_30_04_59_EDT_skips_apr_30_window_closed(self):
        # Editorial today=April 30 but the 05:00 local window has not
        # opened. Candidate 2026-04-30 must be reported as a skip with
        # the "editorial window not open" reason.
        out = self._dry_run_at(self._local_to_utc(2026, 4, 30, 4, 59))
        self.assertIn("today=2026-04-30", out)
        self.assertIn("2026-04-30: would skip (editorial window not open", out)
        self.assertNotIn("would fetch live", out)

    def test_2026_04_30_05_00_EDT_targets_apr_30(self):
        # Window open. Candidate 2026-04-30 is reported as a fetch+synth
        # target. Idempotency for already-published 04-29 and 04-28
        # excludes them upstream in discover_work.
        out = self._dry_run_at(self._local_to_utc(2026, 4, 30, 5, 0))
        self.assertIn("today=2026-04-30", out)
        self.assertIn("2026-04-30: would fetch live + synthesize (today)", out)
        self.assertNotIn("editorial window not open", out)

    def test_2026_04_30_09_00_UTC_scheduled_fire_targets_apr_30(self):
        # Scheduled launchd fire: Hour=5 local in EDT = 09:00 UTC.
        # Canonical "every day works" path; must remain green.
        out = self._dry_run_at(
            datetime(2026, 4, 30, 9, 0, tzinfo=timezone.utc),
        )
        self.assertIn("today=2026-04-30", out)
        self.assertIn("2026-04-30: would fetch live + synthesize (today)", out)

    def test_already_published_idempotency_at_window_closed(self):
        # April 30 already in briefs.json. At ANY time of day (here:
        # 04:59 EDT, before the window) the orchestrator must exit
        # cleanly with no candidates — idempotency takes precedence.
        briefs = list(self.FLOOR_PUBLISHED) + [
            {"id": "2026-04-30", "issueNo": 120, "date": "2026-04-30",
             "title": "April 30", "dek": "d", "readingMinutes": 1,
             "tags": ["Agents"], "items": [], "status": "published",
             "disclaimer": STANDARD_DISCLAIMER, "isSeed": None},
        ]
        out = self._dry_run_at(
            self._local_to_utc(2026, 4, 30, 4, 59), briefs=briefs,
        )
        self.assertIn("no candidates", out)
        self.assertNotIn("would fetch live", out)
        self.assertNotIn("editorial window not open", out)

    def test_already_published_idempotency_at_window_open(self):
        # Same fixture, window now open. Still no candidates — already
        # published.
        briefs = list(self.FLOOR_PUBLISHED) + [
            {"id": "2026-04-30", "issueNo": 120, "date": "2026-04-30",
             "title": "April 30", "dek": "d", "readingMinutes": 1,
             "tags": ["Agents"], "items": [], "status": "published",
             "disclaimer": STANDARD_DISCLAIMER, "isSeed": None},
        ]
        out = self._dry_run_at(
            self._local_to_utc(2026, 4, 30, 5, 0), briefs=briefs,
        )
        self.assertIn("no candidates", out)
        self.assertNotIn("would fetch live", out)

    def test_captured_too_early_window_reopens_during_run(self):
        """Codex stop-time finding: a reboot at 04:59:30 EDT followed by
        a slow reconciliation/pre-flight that crosses 05:00 must NOT
        carry the stale `window_open=False` snapshot into the per-date
        loop. The orchestrator re-evaluates at decision time via
        `_now()`, so today's brief becomes eligible mid-run.

        Test exercises the production code path (`now_utc=None`) and
        patches `_now_utc()` to return:
          - first call (`started` capture): 04:59:30 EDT (window closed)
          - subsequent calls (per-iteration check): 05:00:05 EDT (open)
        """
        started_utc = self._local_to_utc(2026, 4, 30, 4, 59, 30)
        decision_utc = self._local_to_utc(2026, 4, 30, 5, 0, 5)

        clock = iter([started_utc, decision_utc, decision_utc, decision_utc])
        with mock.patch("src.publish._now_utc", side_effect=lambda: next(clock)):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = run_daily_publish(dry_run=True, now_utc=None)
            out = buf.getvalue()

        self.assertEqual(rc, 0)
        # `started`-time `today` derivation reads 2026-04-30 (local date
        # is the same on both sides of the boundary).
        self.assertIn("today=2026-04-30", out)
        # Per-iteration check uses the decision-time clock — window is
        # open, so the candidate is reported as a fetch target, not a
        # window-closed skip.
        self.assertIn("2026-04-30: would fetch live + synthesize (today)", out)
        self.assertNotIn("editorial window not open", out)

    @staticmethod
    def _local_to_utc(year, month, day, hour, minute=0, second=0) -> datetime:
        return (
            datetime(year, month, day, hour, minute, second, tzinfo=EDITORIAL_TZ)
            .astimezone(timezone.utc)
        )


class TestReloadBriefsAfterReconcileMutatesCheckout(unittest.TestCase):
    """Regression test for the stale in-memory briefs bug — Codex
    stop-time finding on PR #16.

    When `reconcile_with_origin` fast-forwards or rebases between
    daily runs, `data/briefs.json` on disk may have changed (operator
    pushed a brief edit, future bot type writes briefs.json, etc.).
    Without reloading, the orchestrator computes `published_ids` from
    the pre-reconcile in-memory list, misses any entries reconcile
    pulled in from origin, and at the post-discover write step
    overwrites tracked origin edits — silent data loss.

    The fix reloads `briefs` from disk after a non-noop reconcile.
    """

    def test_post_reconcile_briefs_drive_published_ids(self) -> None:
        from src.git_sync import ReconcileResult

        # Pre-reconcile snapshot: briefs through 2026-04-30 only.
        briefs_before = [{"id": "2026-04-30", "status": "published"}]
        # Post-reconcile state: an additional 2026-05-01 entry that
        # reconcile pulled in from origin between this run and the
        # prior daily. If the orchestrator acted on `briefs_before`,
        # it would later overwrite the operator's edit.
        briefs_after = [
            {"id": "2026-04-30", "status": "published"},
            {"id": "2026-05-01", "status": "published"},
        ]

        captured: dict[str, set[str]] = {}

        def fake_discover_work(today, max_backlog, floor, published_ids):
            captured["published_ids"] = set(published_ids)
            return []  # no candidates → orchestrator exits cleanly

        now = datetime(2026, 5, 1, 14, 0, tzinfo=timezone.utc)
        with mock.patch(
            "src.publish._load_briefs",
            side_effect=[briefs_before, briefs_after],
        ) as load_briefs_mock, mock.patch(
            "src.publish.reconcile_with_origin",
            return_value=ReconcileResult(
                status="ok", action="fast-forward", behind=1
            ),
        ), mock.patch(
            "src.publish._reconcile_finalization"
        ), mock.patch(
            "src.publish.discover_work", side_effect=fake_discover_work
        ), mock.patch(
            "src.publish._write_run_state"
        ), mock.patch(
            "src.publish._head_sha", return_value="deadbeef"
        ):
            rc = run_daily_publish(dry_run=False, now_utc=now)

        self.assertEqual(rc, 0)
        # Reload contract: _load_briefs called once at function entry
        # AND once again after the non-noop reconcile.
        self.assertEqual(load_briefs_mock.call_count, 2)
        # Discovery exclusion set was computed from POST-reconcile
        # briefs. Without the fix, captured["published_ids"] would be
        # {"2026-04-30"} only (the pre-reconcile snapshot), and the
        # orchestrator would treat 2026-05-01 as unpublished — then
        # the post-discover write at L879 would clobber the operator's
        # edit by writing the stale in-memory list back to briefs.json.
        self.assertEqual(
            captured["published_ids"], {"2026-04-30", "2026-05-01"}
        )


if __name__ == "__main__":
    unittest.main()
