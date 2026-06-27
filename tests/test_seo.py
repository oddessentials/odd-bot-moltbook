"""Tests for src.seo — SEO/LLM-visibility artifact generators.

Covers XML well-formedness, JSON-LD validity + XSS-safe embedding, the
daily-only public-surface filter (weekly/legacy slugs excluded), escaping of
hostile content, and determinism (no wall-clock reads).
"""

import json
import re
import unittest
import xml.etree.ElementTree as ET

from src import seo

SITE = "https://agentbrief.net"

# Hostile content in title/dek/body to exercise escaping.
BRIEF = {
    "id": "2026-06-27",
    "issueNo": 178,
    "date": "2026-06-27",
    "title": "The Plumbing <Beneath> & the Agent",
    "dek": "A dek with <html> & an ampersand.",
    "readingMinutes": 6,
    "tags": ["Agents", "Research"],
    "items": [
        {"headline": "First <item>", "body": "Para one.\n\nPara two & more."},
        {"headline": "Second", "body": "Body two."},
    ],
    "status": "published",
    "disclaimer": "x",
}
OLDER = {**BRIEF, "id": "2026-06-10", "date": "2026-06-10", "title": "Older"}
WEEKLY = {**BRIEF, "id": "2026-W18", "date": "2026-W18", "title": "Weekly legacy"}
EPISODE = {
    "id": "ep-004",
    "episodeNo": 4,
    "title": "Agents & Homework",
    "date": "2026-06-14",
    "durationMinutes": 4,
    "youtubeId": "xVWNcgEKyxI",
    "description": "Two weeks of <AI> agent news distilled into one conversation.",
    "hosts": ["Shrimp", "Carl"],
}


class TestSitemap(unittest.TestCase):
    def test_well_formed_and_scoped(self):
        xml = seo.build_sitemap([BRIEF, OLDER, WEEKLY], [EPISODE], SITE)
        root = ET.fromstring(xml)  # raises if malformed
        ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
        locs = [el.text for el in root.iter(f"{ns}loc")]
        self.assertIn(f"{SITE}/", locs)
        self.assertIn(f"{SITE}/podcast", locs)
        self.assertIn(f"{SITE}/brief/2026-06-27", locs)
        self.assertIn(f"{SITE}/brief/2026-06-10", locs)
        self.assertIn(f"{SITE}/podcast/ep-004", locs)
        # weekly/legacy slug is NOT a public SEO URL
        self.assertNotIn(f"{SITE}/brief/2026-W18", locs)

    def test_lastmod_present_for_brief(self):
        xml = seo.build_sitemap([BRIEF], [], SITE)
        self.assertIn("<lastmod>2026-06-27</lastmod>", xml)


class TestRss(unittest.TestCase):
    def test_well_formed_rfc822_and_scoped(self):
        xml = seo.build_rss([BRIEF, OLDER, WEEKLY], SITE)
        root = ET.fromstring(xml)
        titles = [el.text for el in root.iter("title")]
        self.assertIn("The Plumbing <Beneath> & the Agent", titles)  # ET unescapes
        self.assertNotIn("Weekly legacy", titles)
        pubdates = [el.text for el in root.iter("pubDate")]
        # RFC 822 / 2822 shape, e.g. "Sat, 27 Jun 2026 05:00:00 -0400"
        self.assertTrue(
            all(re.match(r"^[A-Z][a-z]{2}, \d{2} [A-Z][a-z]{2} \d{4} ", d) for d in pubdates)
        )

    def test_limit(self):
        many = [{**BRIEF, "id": f"2026-06-{d:02d}", "date": f"2026-06-{d:02d}"} for d in range(1, 25)]
        xml = seo.build_rss(many, SITE, limit=5)
        self.assertEqual(xml.count("<item>"), 5)


class TestLlmsTxt(unittest.TestCase):
    def test_structure_and_scope(self):
        txt = seo.build_llms_txt([BRIEF, WEEKLY], [EPISODE], SITE)
        self.assertTrue(txt.startswith(f"# {seo.SITE_NAME}"))
        self.assertIn("## Daily briefs", txt)
        self.assertIn("## Podcast episodes", txt)
        self.assertIn(f"{SITE}/brief/2026-06-27", txt)
        self.assertIn(f"{SITE}/podcast/ep-004", txt)
        self.assertNotIn("2026-W18", txt)  # weekly excluded


class TestBriefJsonLd(unittest.TestCase):
    def test_valid_json_and_type(self):
        script = seo.brief_jsonld(BRIEF, SITE)
        body = re.search(r">(.*)<", script, re.DOTALL).group(1)
        data = json.loads(body)  # raises if not valid JSON
        self.assertEqual(data["@type"], "NewsArticle")
        self.assertEqual(data["headline"], BRIEF["title"])
        self.assertEqual(data["url"], f"{SITE}/brief/2026-06-27")
        self.assertIn("Para two & more.", data["articleBody"])
        self.assertEqual(data["publisher"]["name"], "Odd Essentials, LLC")
        self.assertEqual(data["datePublished"], "2026-06-27T05:00:00-04:00")

    def test_xss_safe_embedding(self):
        script = seo.brief_jsonld(BRIEF, SITE)
        inner = script[script.index(">") + 1 : script.rindex("<")]
        # raw < > & must be escaped so the payload can't break out of <script>
        self.assertNotIn("<", inner)
        self.assertNotIn("</script", script.lower()[: script.rindex("<")])


class TestEpisodeJsonLd(unittest.TestCase):
    def test_podcast_episode(self):
        script = seo.episode_jsonld(EPISODE, SITE)
        data = json.loads(script[script.index(">") + 1 : script.rindex("<")])
        self.assertEqual(data["@type"], "PodcastEpisode")
        self.assertEqual(data["duration"], "PT4M")
        self.assertEqual(data["partOfSeries"]["@type"], "PodcastSeries")
        self.assertIn("xVWNcgEKyxI", data["associatedMedia"]["contentUrl"])


class TestHeadExtras(unittest.TestCase):
    def test_brief_head_extras(self):
        out = seo.brief_head_extras(BRIEF, SITE)
        self.assertIn('property="article:published_time"', out)
        self.assertIn('property="article:section" content="Agents"', out)
        self.assertIn('property="article:tag" content="Research"', out)
        self.assertIn('type="application/ld+json"', out)
        # canonical is a per-page template rewrite, NOT injected here (would dupe)
        self.assertNotIn("canonical", out)


class TestPrerender(unittest.TestCase):
    def test_brief_body_escaped_and_complete(self):
        out = seo.prerender_brief_html(BRIEF)
        self.assertIn("<h1>The Plumbing &lt;Beneath&gt; &amp; the Agent</h1>", out)
        self.assertIn("<h2>First &lt;item&gt;</h2>", out)
        self.assertIn("<p>Para one.</p>", out)
        self.assertIn("<p>Para two &amp; more.</p>", out)  # split on blank line
        self.assertIn("Issue 178", out)

    def test_episode_body(self):
        out = seo.prerender_episode_html(EPISODE)
        self.assertIn("<h1>Agents &amp; Homework</h1>", out)
        self.assertIn("Episode 4", out)
        self.assertIn("&lt;AI&gt;", out)


class TestDeterminism(unittest.TestCase):
    def test_no_wallclock(self):
        data = [BRIEF, OLDER]
        self.assertEqual(seo.build_rss(data, SITE), seo.build_rss(data, SITE))
        self.assertEqual(
            seo.build_sitemap(data, [EPISODE], SITE),
            seo.build_sitemap(data, [EPISODE], SITE),
        )
        self.assertEqual(
            seo.build_llms_txt(data, [EPISODE], SITE),
            seo.build_llms_txt(data, [EPISODE], SITE),
        )


if __name__ == "__main__":
    unittest.main()
