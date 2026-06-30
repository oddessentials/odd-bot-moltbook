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


_SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


def _sitemap_lastmods(xml: str) -> dict:
    """Map loc -> lastmod (or None) from a sitemap XML string."""
    root = ET.fromstring(xml)
    out = {}
    for url in root.iter(f"{_SITEMAP_NS}url"):
        loc = url.find(f"{_SITEMAP_NS}loc").text
        lm = url.find(f"{_SITEMAP_NS}lastmod")
        out[loc] = lm.text if lm is not None else None
    return out


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

    def test_includes_every_static_route(self):
        locs = list(_sitemap_lastmods(seo.build_sitemap([BRIEF], [EPISODE], SITE)))
        for route in seo.STATIC_ROUTES:
            self.assertIn(f"{SITE}{route.path}", locs)

    def test_evergreen_lastmod_is_honest_not_faked_fresh(self):
        # Regression: about/privacy/disclosures must NOT inherit the newest-brief
        # date (a false-freshness signal that makes Google distrust lastmod
        # site-wide); they carry the stable STATIC_PAGE_LASTMOD instead, while
        # data-driven hubs (home/archive) legitimately track the newest brief.
        m = _sitemap_lastmods(seo.build_sitemap([BRIEF], [], SITE))
        self.assertEqual(m[f"{SITE}/about"], seo.STATIC_PAGE_LASTMOD)
        self.assertNotEqual(m[f"{SITE}/about"], "2026-06-27")
        self.assertEqual(m[f"{SITE}/"], "2026-06-27")
        self.assertEqual(m[f"{SITE}/archive"], "2026-06-27")

    def test_podcast_lastmod_tracks_episode_not_brief(self):
        m = _sitemap_lastmods(seo.build_sitemap([BRIEF], [EPISODE], SITE))
        self.assertEqual(m[f"{SITE}/podcast"], "2026-06-14")  # episode date
        # no episodes → no (false) lastmod on the podcast hub
        m2 = _sitemap_lastmods(seo.build_sitemap([BRIEF], [], SITE))
        self.assertIsNone(m2[f"{SITE}/podcast"])


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

    def test_content_encoded_full_text(self):
        xml = seo.build_rss([BRIEF], SITE)
        self.assertIn(
            'xmlns:content="http://purl.org/rss/1.0/modules/content/"', xml
        )
        self.assertIn("<content:encoded><![CDATA[", xml)
        # full item body (beyond the dek-only <description>) is present, and as
        # HTML inside CDATA the ampersand is an entity, not a raw char.
        self.assertIn("Para two &amp; more.", xml)
        ET.fromstring(xml)  # CDATA + namespaced element still well-formed

    def test_content_encoded_cdata_guarded(self):
        hostile = {**BRIEF, "items": [{"headline": "H", "body": "x ]]> y"}]}
        xml = seo.build_rss([hostile], SITE)
        root = ET.fromstring(xml)  # must stay well-formed despite ]]> in content
        ns = "{http://purl.org/rss/1.0/modules/content/}"
        encoded = [e.text for e in root.iter(f"{ns}encoded")]
        self.assertTrue(encoded)
        # the raw CDATA terminator was neutralised to the HTML entity form
        self.assertNotIn("]]>", encoded[0])
        self.assertIn("]]&gt;", encoded[0])


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

    def test_brief_head_extras_has_breadcrumb(self):
        out = seo.brief_head_extras(BRIEF, SITE)
        self.assertIn('"@type":"BreadcrumbList"', out)
        # two JSON-LD scripts now: NewsArticle + BreadcrumbList
        self.assertEqual(out.count('type="application/ld+json"'), 2)

    def test_episode_head_extras_has_breadcrumb(self):
        out = seo.episode_head_extras(EPISODE, SITE)
        self.assertIn('"@type":"BreadcrumbList"', out)
        self.assertEqual(out.count('type="application/ld+json"'), 2)


class TestBreadcrumb(unittest.TestCase):
    def test_structure_and_absolute_urls(self):
        script = seo.breadcrumb_jsonld(
            [("Home", "/"), ("Archive", "/archive"), ("X", "/brief/x")], SITE
        )
        data = json.loads(script[script.index(">") + 1 : script.rindex("<")])
        self.assertEqual(data["@type"], "BreadcrumbList")
        items = data["itemListElement"]
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["position"], 1)
        self.assertEqual(items[0]["item"], f"{SITE}/")
        self.assertEqual(items[2]["item"], f"{SITE}/brief/x")
        self.assertEqual(items[2]["position"], 3)


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


class TestStaticRoutePrerender(unittest.TestCase):
    def test_home_links_today_recent_and_episode(self):
        out = seo.prerender_route_body("/", [BRIEF, OLDER], [EPISODE])
        self.assertIn('<a href="/brief/2026-06-27">', out)  # today
        self.assertIn('<a href="/brief/2026-06-10">', out)  # recent
        self.assertIn('<a href="/podcast/ep-004">', out)    # latest episode
        self.assertIn("&lt;Beneath&gt;", out)               # title escaped

    def test_archive_indexes_all_daily_briefs_excluding_weekly(self):
        out = seo.prerender_route_body("/archive", [BRIEF, OLDER, WEEKLY], [])
        self.assertIn('<a href="/brief/2026-06-27">', out)
        self.assertIn('<a href="/brief/2026-06-10">', out)
        self.assertNotIn("2026-W18", out)  # weekly excluded from the daily index

    def test_podcast_indexes_episodes(self):
        out = seo.prerender_route_body("/podcast", [], [EPISODE])
        self.assertIn('<a href="/podcast/ep-004">', out)
        self.assertIn("Agents &amp; Homework", out)

    def test_static_pages_render_faithful_body(self):
        about = seo.prerender_route_body("/about", [], [])
        self.assertIn("<h1>About The Agent Brief</h1>", about)
        self.assertIn("legal@oddessentials.com", about)
        privacy = seo.prerender_route_body("/privacy", [], [])
        self.assertIn("<h1>Privacy Policy</h1>", privacy)
        disclosures = seo.prerender_route_body("/disclosures", [], [])
        self.assertIn("<h1>Disclosures</h1>", disclosures)

    def test_every_prerendered_page_carries_internal_nav(self):
        for body in (
            seo.prerender_route_body("/", [BRIEF], [EPISODE]),
            seo.prerender_route_body("/archive", [BRIEF], []),
            seo.prerender_route_body("/podcast", [], [EPISODE]),
            seo.prerender_route_body("/about", [], []),
            seo.prerender_brief_html(BRIEF),
            seo.prerender_episode_html(EPISODE),
        ):
            self.assertIn('<nav aria-label="Site">', body)
            self.assertIn('href="/archive"', body)

    def test_static_routes_match_sitemap(self):
        # The emitter and the sitemap share STATIC_ROUTES; assert every route is
        # dispatchable (no KeyError) so the two can never disagree.
        for route in seo.STATIC_ROUTES:
            body = seo.prerender_route_body(route.path, [BRIEF], [EPISODE])
            self.assertIn("<main>", body)


class TestIndexNow(unittest.TestCase):
    def test_payload_shape(self):
        urls = [f"{SITE}/", f"{SITE}/brief/2026-06-27"]
        payload = seo.build_indexnow_payload(urls, SITE)
        self.assertEqual(payload["host"], "agentbrief.net")
        self.assertEqual(payload["key"], seo.INDEXNOW_KEY)
        self.assertEqual(payload["keyLocation"], f"{SITE}/{seo.INDEXNOW_KEY}.txt")
        self.assertEqual(payload["urlList"], urls)


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
        self.assertEqual(
            seo.prerender_route_body("/", data, [EPISODE]),
            seo.prerender_route_body("/", data, [EPISODE]),
        )


if __name__ == "__main__":
    unittest.main()
