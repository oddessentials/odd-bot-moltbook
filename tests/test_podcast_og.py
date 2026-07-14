"""Unit tests for src.podcast.og — per-episode OG page renderer.

The pure render function is tested against a minimal SPA-shaped template
so drift detection and HTML escaping are covered without touching real
docs/index.html. The I/O entry point gets a sandbox + idempotency check.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.podcast import manifest as manifest_module
from src.podcast import og as og_module
from src.podcast.manifest import write_manifest
from src.podcast.og import generate_episode_og, render_episode_og_html
from src.podcast.schema import EpisodeRecord


_TEMPLATE_HTML = """\
<!doctype html>
<html lang="en">
  <head>
    <title>The Agent Brief — Daily AI Agent News</title>
    <meta name="description" content="A short, daily brief on AI agents." />
    <meta property="og:title" content="The Agent Brief — Daily AI Agent News" />
    <meta property="og:description" content="A short, daily brief on AI agents." />
    <meta property="og:image" content="https://agentbrief.net/og-image.png" />
    <meta property="og:url" content="https://agentbrief.net/" />
    <link rel="canonical" href="https://agentbrief.net/" />
    <meta property="og:type" content="website" />
    <meta property="og:site_name" content="Agent Brief Daily" />
    <meta name="twitter:card" content="summary_large_image" />
    <meta name="twitter:title" content="The Agent Brief — Daily AI Agent News" />
    <meta name="twitter:description" content="A short, daily brief on AI agents." />
    <meta name="twitter:image" content="https://agentbrief.net/og-image.png" />
  </head>
  <body><div id="root"></div></body>
</html>
"""


# The real post-build docs/index.html the generator reads is NOT the empty Vite
# shell above: the daily pipeline's home-route prerender fills #root with the
# homepage body (nested <div>s and all) before it is written. This fixture
# mirrors that state so the renderer is exercised against what production
# actually feeds it — the case whose absence let the prerender work drift the
# podcast pipeline into a two-week outage.
_PRERENDERED_ROOT_TEMPLATE = _TEMPLATE_HTML.replace(
    '<div id="root"></div>',
    '<div id="root"><main><h1>The Agent Brief</h1>'
    '<div class="feed"><article><a href="/brief/x">Latest</a></div></article>'
    '<nav><div><div>deeply</div> nested</div></nav></main></div>',
)


def _record(**overrides) -> EpisodeRecord:
    payload = {
        "id": "ep-001",
        "episodeNo": 1,
        "title": "When Agents Optimize the Scorecard (Ep. 1)",
        "date": "2026-04-28",
        "durationMinutes": 4,
        "youtubeId": "abc123",
        "description": "Episode description, long enough to satisfy the schema.",
        "hosts": ["Shrimp", "Carl"],
    }
    payload.update(overrides)
    return EpisodeRecord(**payload)


class TestRenderEpisodeOgHtml(unittest.TestCase):
    def test_rewrites_seven_targeted_tags(self):
        out = render_episode_og_html(_TEMPLATE_HTML, _record())
        self.assertIn(
            "<title>When Agents Optimize the Scorecard (Ep. 1) — Agent Brief Daily</title>",
            out,
        )
        self.assertIn(
            '<meta property="og:title" content="When Agents Optimize the Scorecard (Ep. 1)" />',
            out,
        )
        self.assertIn(
            'Episode 1 · Episode description, long enough to satisfy the schema.',
            out,
        )
        self.assertIn(
            '<meta property="og:url" content="https://agentbrief.net/podcast/ep-001" />',
            out,
        )
        self.assertIn('<meta property="og:type" content="article" />', out)

    def test_seo_enrichment_canonical_meta_jsonld_body(self):
        out = render_episode_og_html(_TEMPLATE_HTML, _record())
        self.assertIn(
            '<link rel="canonical" href="https://agentbrief.net/podcast/ep-001" />',
            out)
        self.assertEqual(out.count('rel="canonical"'), 1)
        self.assertIn(
            '<meta name="description" content="Episode description, '
            'long enough to satisfy the schema." />', out)
        self.assertIn('"@type":"PodcastEpisode"', out)
        self.assertNotIn('<div id="root"></div>', out)
        self.assertIn('<div id="root"><article>', out)
        self.assertIn("<h1>When Agents Optimize the Scorecard (Ep. 1)</h1>", out)

    def test_renders_over_prerendered_root(self):
        # Regression: the generator reads the post-build docs/index.html, whose
        # #root already holds the prerendered homepage body. The episode body
        # must fully replace it — this is the case that broke the pipeline.
        out = render_episode_og_html(_PRERENDERED_ROOT_TEMPLATE, _record())
        self.assertIn('<div id="root"><article>', out)
        self.assertIn("<h1>When Agents Optimize the Scorecard (Ep. 1)</h1>", out)
        # No homepage residue survives inside root.
        self.assertNotIn("<h1>The Agent Brief</h1>", out)
        self.assertNotIn('class="feed"', out)
        self.assertNotIn("deeply", out)
        self.assertNotIn("/brief/x", out)
        # Document tail past root is left intact.
        self.assertIn("</body>", out)
        self.assertIn("</html>", out)

    def test_prerendered_and_empty_root_render_identically(self):
        # The empty Vite shell and the prerendered docs/index.html differ ONLY
        # in #root's contents, which the renderer overwrites either way — so the
        # two must yield byte-identical output. This proves both that no
        # homepage residue leaks through and that the empty-root path (used by
        # the daily _emit_per_episode_pages caller) is behaviorally unchanged.
        out_empty = render_episode_og_html(_TEMPLATE_HTML, _record())
        out_filled = render_episode_og_html(_PRERENDERED_ROOT_TEMPLATE, _record())
        self.assertEqual(out_empty, out_filled)

    def test_nested_divs_in_root_not_truncated(self):
        # A naive non-greedy regex would stop at the first inner </div>, leaving
        # a broken tail. The balanced scan must consume the whole nested body.
        out = render_episode_og_html(_PRERENDERED_ROOT_TEMPLATE, _record())
        self.assertEqual(out.count('<div id="root">'), 1)
        # exactly one </div> — root's own close (episode body carries none).
        self.assertEqual(out.count("</div>"), 1)
        self.assertNotIn("<nav>", out)

    def test_div_attribute_with_gt_not_mis_terminated(self):
        # A div inside root whose attribute value holds a literal '>' must not
        # fool the boundary scan into closing early. Regression lock for the
        # adversarial-review hardening (tokeniser matches tag openers, not
        # attributes); a `<div[^>]*>` form would mis-terminate here and leave
        # residue after the tricky div.
        filled = _TEMPLATE_HTML.replace(
            '<div id="root"></div>',
            '<div id="root"><div data-q="a>b"><span>home</span></div>'
            '<p>tail still in root</p></div>',
        )
        out = render_episode_og_html(filled, _record())
        self.assertEqual(out.count('<div id="root">'), 1)
        self.assertIn('<div id="root"><article>', out)
        self.assertNotIn('data-q="a>b"', out)        # inner home div evicted
        self.assertNotIn("tail still in root", out)   # and content after it
        self.assertIn("</body>", out)

    def test_missing_root_div_raises(self):
        broken = _TEMPLATE_HTML.replace(
            '<div id="root"></div>', '<div id="app"></div>')
        with self.assertRaises(RuntimeError) as cm:
            render_episode_og_html(broken, _record())
        self.assertIn('<div id="root">', str(cm.exception))

    def test_duplicate_root_div_raises(self):
        broken = _TEMPLATE_HTML.replace(
            '<div id="root"></div>',
            '<div id="root"></div><div id="root"></div>')
        with self.assertRaises(RuntimeError) as cm:
            render_episode_og_html(broken, _record())
        self.assertIn("got 2", str(cm.exception))

    def test_unclosed_root_div_raises(self):
        broken = _TEMPLATE_HTML.replace(
            '<div id="root"></div>', '<div id="root">')
        with self.assertRaises(RuntimeError) as cm:
            render_episode_og_html(broken, _record())
        self.assertIn("never closed", str(cm.exception))

    def test_static_brand_tags_preserved(self):
        # og:image, og:site_name, twitter:image, twitter:card MUST NOT
        # be touched — brand consistency across the site.
        out = render_episode_og_html(_TEMPLATE_HTML, _record())
        self.assertIn(
            'property="og:image" content="https://agentbrief.net/og-image.png"',
            out,
        )
        self.assertIn('property="og:site_name" content="Agent Brief Daily"', out)
        self.assertIn(
            'name="twitter:image" content="https://agentbrief.net/og-image.png"',
            out,
        )
        self.assertIn('name="twitter:card" content="summary_large_image"', out)

    def test_html_escapes_title_and_description(self):
        record = _record(
            title="Quote's & Brackets <test>",
            description="Description with <html> & 'quotes' inside it now.",
        )
        out = render_episode_og_html(_TEMPLATE_HTML, record)
        self.assertIn("Quote&#x27;s &amp; Brackets &lt;test&gt;", out)
        self.assertIn("&lt;html&gt;", out)
        self.assertIn("&#x27;quotes&#x27;", out)
        self.assertNotIn("<test>", out)  # un-escaped angle brackets must not leak

    def test_template_missing_target_tag_raises(self):
        broken = _TEMPLATE_HTML.replace(
            '<meta property="og:title" content="The Agent Brief — Daily AI Agent News" />',
            "",
        )
        with self.assertRaises(RuntimeError) as cm:
            render_episode_og_html(broken, _record())
        self.assertIn("og:title", str(cm.exception))

    def test_template_with_duplicate_target_tag_raises(self):
        broken = _TEMPLATE_HTML.replace(
            '<meta property="og:url" content="https://agentbrief.net/" />',
            '<meta property="og:url" content="https://agentbrief.net/" />\n'
            '    <meta property="og:url" content="https://agentbrief.net/dupe" />',
        )
        with self.assertRaises(RuntimeError) as cm:
            render_episode_og_html(broken, _record())
        self.assertIn("og:url", str(cm.exception))

    def test_episode_no_in_description_uses_record_value(self):
        out = render_episode_og_html(_TEMPLATE_HTML, _record(episodeNo=42))
        self.assertIn("Episode 42 ·", out)

    def test_canonical_url_html_escaped_in_meta_attribute(self):
        # Defense in depth: even if a future caller bypasses
        # EpisodeRecord's id pattern (e.g., constructs the model with
        # `model_construct` and skips validation), the renderer's
        # html.escape on the URL still keeps an attacker from breaking
        # out of the og:url content="..." attribute.
        from pydantic import BaseModel

        class _Bypassed(BaseModel):
            # Mirror EpisodeRecord but without the id pattern, so we
            # can hand the renderer a record with adversarial chars.
            id: str
            episodeNo: int
            title: str
            date: str
            durationMinutes: int
            youtubeId: str
            description: str
            hosts: list[str]

        adversarial = _Bypassed(
            id='valid"><script>x</script>',
            episodeNo=1,
            title="Title",
            date="2026-04-28",
            durationMinutes=4,
            youtubeId="abc",
            description="A description that is definitely longer than the floor.",
            hosts=["Shrimp"],
        )
        out = render_episode_og_html(_TEMPLATE_HTML, adversarial)
        # Raw `<script>` tag must NOT appear unescaped in output.
        self.assertNotIn("<script>", out)
        # The URL still appears, with HTML-escaped angle brackets/quotes.
        self.assertIn("&lt;script&gt;", out)


class TestGenerateEpisodeOg(unittest.TestCase):
    def _seed(self, tdp: Path) -> Path:
        ep_dir = tdp / "data" / "episodes" / "ep-test"
        ep_dir.mkdir(parents=True)
        mpath = ep_dir / "manifest.json"
        write_manifest(
            mpath,
            {
                "id": "ep-test",
                "episode_record": _record(id="ep-test").model_dump(),
                "segments": [],
            },
        )

        docs_dir = tdp / "docs"
        docs_dir.mkdir(parents=True)
        (docs_dir / "index.html").write_text(_TEMPLATE_HTML)

        return mpath

    def _patch_dirs(self, tdp: Path):
        return mock.patch.multiple(
            og_module,
            DOCS_INDEX_PATH=tdp / "docs" / "index.html",
            PODCAST_OG_DIR=tdp / "docs" / "podcast",
            REPO_ROOT=tdp,
        ), mock.patch.multiple(
            manifest_module,
            REPO_ROOT=tdp,
            EPISODES_DIR=tdp / "data" / "episodes",
        )

    def test_writes_at_expected_path_anchored_on_manifest_parent(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            mpath = self._seed(tdp)
            og_patch, manifest_patch = self._patch_dirs(tdp)
            with og_patch, manifest_patch:
                out_path = generate_episode_og(manifest_path=mpath)
            expected = tdp / "docs" / "podcast" / "ep-test" / "index.html"
            self.assertEqual(out_path, expected)
            self.assertTrue(expected.exists())

    def test_persists_og_html_path_to_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            mpath = self._seed(tdp)
            og_patch, manifest_patch = self._patch_dirs(tdp)
            with og_patch, manifest_patch:
                generate_episode_og(manifest_path=mpath)
            recorded = json.loads(mpath.read_text())["og_html_path"]
            self.assertEqual(recorded, "docs/podcast/ep-test/index.html")

    def test_episode_id_derived_from_filesystem_not_manifest_id(self):
        # If manifest["id"] is tampered to "../malicious", generate_episode_og
        # must still write to docs/podcast/ep-test/ (the manifest's actual
        # parent dir name) — not to docs/podcast/../malicious/.
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            mpath = self._seed(tdp)
            tampered = json.loads(mpath.read_text())
            tampered["id"] = "../malicious"
            mpath.write_text(json.dumps(tampered))

            og_patch, manifest_patch = self._patch_dirs(tdp)
            with og_patch, manifest_patch:
                out_path = generate_episode_og(manifest_path=mpath)
            self.assertEqual(out_path.parent.name, "ep-test")
            self.assertFalse((tdp / "docs" / "malicious").exists())

    def test_idempotent_rerun_produces_same_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            mpath = self._seed(tdp)
            og_patch, manifest_patch = self._patch_dirs(tdp)
            with og_patch, manifest_patch:
                out_path = generate_episode_og(manifest_path=mpath)
                first = out_path.read_bytes()
                generate_episode_og(manifest_path=mpath)
                second = out_path.read_bytes()
            self.assertEqual(first, second)

    def test_missing_episode_record_raises(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            mpath = self._seed(tdp)
            payload = json.loads(mpath.read_text())
            del payload["episode_record"]
            mpath.write_text(json.dumps(payload))

            og_patch, manifest_patch = self._patch_dirs(tdp)
            with og_patch, manifest_patch, self.assertRaises(RuntimeError) as cm:
                generate_episode_og(manifest_path=mpath)
            self.assertIn("episode_record", str(cm.exception))

    def test_missing_template_raises(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            mpath = self._seed(tdp)
            (tdp / "docs" / "index.html").unlink()

            og_patch, manifest_patch = self._patch_dirs(tdp)
            with og_patch, manifest_patch, self.assertRaises(RuntimeError) as cm:
                generate_episode_og(manifest_path=mpath)
            self.assertIn("SPA template", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
