"""SEO + LLM-visibility artifacts — pure, deterministic generators.

Everything here derives from `data/briefs.json` / `data/episodes.json` so the
daily/weekly build (`src.publish._run_build`) regenerates it automatically — no
hand maintenance. Two kinds of output:

  * Site-level files — `build_sitemap` / `build_rss` / `build_llms_txt` — written
    to `docs/` by `_run_build` behind failure isolation: a generator bug must
    never block a brief from publishing.
  * Per-page fragments — `brief_head_extras` / `episode_head_extras` /
    `prerender_brief_html` / `prerender_episode_html` — injected into the
    per-brief / per-episode static pages so crawlers and AI engines see real
    content + JSON-LD in the *initial* HTML. The SPA mounts with
    `createRoot().render()` (not `hydrateRoot`), which clears `#root` on mount,
    so prerendered body content is replaced cleanly with no hydration mismatch.

Determinism: no wall-clock reads. Every timestamp derives from an item's
editorial date at the publish hour (America/New_York), so rebuilds are
byte-stable and don't churn git diffs.
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime
from email.utils import format_datetime
from typing import NamedTuple
from urllib.parse import urlsplit

from src.editorial_time import DAILY_WINDOW_HOUR, EDITORIAL_TZ

# Brand / publisher identity used across schema + feeds.
ORG_NAME = "Agent Brief"
ORG_LEGAL_NAME = "Odd Essentials, LLC"
SITE_NAME = "Agent Brief Daily"
SITE_TAGLINE = "A short, daily brief on AI agents."
SITE_DESCRIPTION = (
    "An AI-run newsroom covering the moltbook agent community — a short daily "
    "brief on AI agents plus a periodic podcast, written and edited by AI agents."
)

# Evergreen pages (about / privacy / disclosures) change rarely. Stamping them
# with the newest-brief date on every build would be a false freshness signal,
# and Google distrusts <lastmod> site-wide once it catches one being inaccurate.
# Use this honest, manually-bumped date for those pages; bump it when their copy
# materially changes (it tracks the Privacy "last updated" date).
STATIC_PAGE_LASTMOD = "2026-06-30"

# IndexNow — instant crawl notification for Bing / Yandex / Naver / Seznam
# (Google does not participate). The key is published at https://<host>/<key>.txt
# from agent-brief/client/public/<key>.txt, which Vite copies into docs/.
INDEXNOW_KEY = "a3b30102ce6d4ea9a20edf647444ee45515ad4f3c8aa4715a63a8c2a845ebf72"
INDEXNOW_ENDPOINT = "https://api.indexnow.org/indexnow"

# Daily brief ids are calendar dates; weekly/legacy slugs are excluded from the
# public SEO surface (mirrors src.publish._emit_per_brief_pages' filter).
_DAILY_ID = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _is_daily(brief: dict) -> bool:
    return bool(_DAILY_ID.match(str(brief.get("id", ""))))


def _daily_sorted(briefs: list[dict]) -> list[dict]:
    """Daily briefs only, newest-first (id == date, lexicographic == chrono)."""
    return sorted(
        (b for b in briefs if _is_daily(b)),
        key=lambda b: b["id"],
        reverse=True,
    )


def _episodes_sorted(episodes: list[dict]) -> list[dict]:
    return sorted(episodes, key=lambda e: e.get("episodeNo", 0), reverse=True)


def _pub_datetime(date_str: str) -> datetime:
    """`YYYY-MM-DD` → tz-aware datetime at the editorial publish hour (ET)."""
    year, month, day = (int(p) for p in date_str.split("-"))
    return datetime(year, month, day, DAILY_WINDOW_HOUR, 0, 0, tzinfo=EDITORIAL_TZ)


def _article_body(brief: dict) -> str:
    blocks = []
    for item in brief.get("items", []):
        headline = str(item.get("headline", "")).strip()
        body = str(item.get("body", "")).strip()
        blocks.append(f"{headline}\n{body}".strip())
    return "\n\n".join(b for b in blocks if b)


def _paragraphs(text: str) -> list[str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n", str(text).strip()) if p.strip()]
    return paras or ([text.strip()] if str(text).strip() else [])


# ---------------------------------------------------------------------------
# JSON-LD
# ---------------------------------------------------------------------------

def _jsonld_script(payload: dict) -> str:
    """Serialize JSON-LD into a script tag, escaping the three characters that
    could break out of `<script>` or be misparsed as markup (`<`, `>`, `&`)."""
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    raw = raw.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return f'<script type="application/ld+json">{raw}</script>'


def brief_jsonld(brief: dict, site_url: str) -> str:
    """NewsArticle JSON-LD for a daily brief (organization author/publisher)."""
    url = f"{site_url}/brief/{brief['id']}"
    published = _pub_datetime(brief["date"]).isoformat()
    body = _article_body(brief)
    tags = [str(t) for t in (brief.get("tags") or [])]
    payload: dict = {
        "@context": "https://schema.org",
        "@type": "NewsArticle",
        "headline": brief["title"],
        "description": brief["dek"],
        "datePublished": published,
        "dateModified": published,
        "url": url,
        "mainEntityOfPage": {"@type": "WebPage", "@id": url},
        "image": [f"{site_url}/og-image.png"],
        "isAccessibleForFree": True,
        "inLanguage": "en",
        "wordCount": len(body.split()),
        "articleBody": body,
        "author": {"@type": "Organization", "name": ORG_NAME, "url": site_url},
        "publisher": {
            "@type": "Organization",
            "name": ORG_LEGAL_NAME,
            "url": site_url,
            "logo": {
                "@type": "ImageObject",
                "url": f"{site_url}/android-chrome-512x512.png",
            },
        },
    }
    if tags:
        payload["articleSection"] = tags[0]
        payload["keywords"] = ", ".join(tags)
    return _jsonld_script(payload)


def episode_jsonld(episode: dict, site_url: str) -> str:
    """PodcastEpisode JSON-LD (part of the Agent Brief PodcastSeries)."""
    url = f"{site_url}/podcast/{episode['id']}"
    published = _pub_datetime(episode["date"]).isoformat()
    payload: dict = {
        "@context": "https://schema.org",
        "@type": "PodcastEpisode",
        "name": episode["title"],
        "episodeNumber": episode.get("episodeNo"),
        "description": episode["description"],
        "datePublished": published,
        "url": url,
        "duration": f"PT{int(episode['durationMinutes'])}M",
        "inLanguage": "en",
        "image": [f"{site_url}/og-image.png"],
        "associatedMedia": {
            "@type": "MediaObject",
            "contentUrl": f"https://www.youtube.com/watch?v={episode['youtubeId']}",
        },
        "partOfSeries": {
            "@type": "PodcastSeries",
            "name": ORG_NAME,
            "url": f"{site_url}/podcast",
        },
        "publisher": {"@type": "Organization", "name": ORG_LEGAL_NAME, "url": site_url},
    }
    return _jsonld_script(payload)


def breadcrumb_jsonld(trail: list[tuple[str, str]], site_url: str) -> str:
    """BreadcrumbList JSON-LD. `trail` = [(name, site-relative path), ...].

    Still a live (desktop) Google rich result post-Jan-2025, and a cheap
    page-structure signal for AI extraction. Requires >= 2 items; each carries
    a 1-based position, name, and absolute `item` URL.
    """
    items = [
        {
            "@type": "ListItem",
            "position": i,
            "name": name,
            "item": f"{site_url}{path}",
        }
        for i, (name, path) in enumerate(trail, start=1)
    ]
    return _jsonld_script(
        {
            "@context": "https://schema.org",
            "@type": "BreadcrumbList",
            "itemListElement": items,
        }
    )


# ---------------------------------------------------------------------------
# Per-page <head> extras (canonical + article meta + JSON-LD)
# ---------------------------------------------------------------------------

def brief_head_extras(brief: dict, site_url: str) -> str:
    """Additive <head> content for a daily brief — OpenGraph article tags plus
    NewsArticle JSON-LD — inserted before </head> by the per-brief emitter.

    Canonical and meta-description are NOT here: they already exist in the
    template (homepage values) and are rewritten in place per page, so adding
    them here would duplicate the tag.
    """
    published = _pub_datetime(brief["date"]).isoformat()
    tags = [str(t) for t in (brief.get("tags") or [])]
    parts = [
        f'<meta property="article:published_time" '
        f'content="{html.escape(published, quote=True)}" />',
    ]
    if tags:
        parts.append(
            f'<meta property="article:section" '
            f'content="{html.escape(tags[0], quote=True)}" />'
        )
        parts.extend(
            f'<meta property="article:tag" content="{html.escape(t, quote=True)}" />'
            for t in tags
        )
    parts.append(brief_jsonld(brief, site_url))
    parts.append(
        breadcrumb_jsonld(
            [
                ("Home", "/"),
                ("Archive", "/archive"),
                (str(brief["title"]), f"/brief/{brief['id']}"),
            ],
            site_url,
        )
    )
    return "".join(parts)


def episode_head_extras(episode: dict, site_url: str) -> str:
    """Additive <head> content for an episode — article:published_time plus
    PodcastEpisode JSON-LD (canonical/description are template rewrites)."""
    published = _pub_datetime(episode["date"]).isoformat()
    parts = [
        f'<meta property="article:published_time" '
        f'content="{html.escape(published, quote=True)}" />',
        episode_jsonld(episode, site_url),
        breadcrumb_jsonld(
            [
                ("Home", "/"),
                ("Podcast", "/podcast"),
                (str(episode["title"]), f"/podcast/{episode['id']}"),
            ],
            site_url,
        ),
    ]
    return "".join(parts)


# ---------------------------------------------------------------------------
# Prerendered body (injected into #root; SPA replaces it on mount)
# ---------------------------------------------------------------------------

def _prerender_nav() -> str:
    """Small in-body internal-link nav. Non-JS AI crawlers never see the React
    header/footer, so without this the prerendered pages would be link islands;
    these root-relative links give every page a crawl path to the key hubs."""
    links = [
        ("/", "Home"),
        ("/archive", "Archive"),
        ("/podcast", "Podcast"),
        ("/about", "About"),
    ]
    inner = " · ".join(
        f'<a href="{p}">{html.escape(label)}</a>' for p, label in links
    )
    return f'<nav aria-label="Site">{inner}</nav>'


def prerender_brief_html(brief: dict) -> str:
    """Crawler/AI-visible article markup for the initial HTML response."""
    parts = [
        "<article>",
        f"<h1>{html.escape(brief['title'])}</h1>",
        f"<p>{html.escape(brief['dek'])}</p>",
        (
            f"<p>Issue {int(brief['issueNo'])} · {html.escape(str(brief['date']))}"
            f" · {int(brief.get('readingMinutes', 0))} min read</p>"
        ),
    ]
    for item in brief.get("items", []):
        parts.append("<section>")
        parts.append(f"<h2>{html.escape(str(item.get('headline', '')))}</h2>")
        for para in _paragraphs(item.get("body", "")):
            parts.append(f"<p>{html.escape(para)}</p>")
        parts.append("</section>")
    parts.append("</article>")
    parts.append(_prerender_nav())
    return "".join(parts)


def prerender_episode_html(episode: dict) -> str:
    parts = [
        "<article>",
        f"<h1>{html.escape(episode['title'])}</h1>",
        (
            f"<p>Episode {int(episode['episodeNo'])} · "
            f"{html.escape(str(episode['date']))} · "
            f"{int(episode['durationMinutes'])} min</p>"
        ),
    ]
    for para in _paragraphs(episode.get("description", "")):
        parts.append(f"<p>{html.escape(para)}</p>")
    parts.append("</article>")
    parts.append(_prerender_nav())
    return "".join(parts)


# ---------------------------------------------------------------------------
# Static SPA routes — true 200-status prerendered pages
# ---------------------------------------------------------------------------
#
# GitHub Pages serves any existing file as HTTP 200; only unmatched paths fall
# through to 404.html (the SPA shell, served with a 404 status — a soft 404 that
# search + AI crawlers skip). None of the major AI crawlers (GPTBot,
# OAI-SearchBot, ChatGPT-User, ClaudeBot, PerplexityBot) execute JavaScript, so
# a real index.html with content baked into the initial HTML is the only way
# they see these pages at all. STATIC_ROUTES is the single source of truth for
# both build_sitemap and src.publish._emit_static_route_pages, so the sitemap can
# never advertise a URL the emitter doesn't produce (or vice-versa).


class StaticRoute(NamedTuple):
    path: str            # site-relative, e.g. "/about"
    title: str           # <title> text
    og_title: str        # og:title / twitter:title (no site suffix)
    description: str     # meta description / og:description / twitter:description
    lastmod_source: str  # "briefs" | "episodes" | "static"


STATIC_ROUTES: tuple[StaticRoute, ...] = (
    StaticRoute(
        "/", "The Agent Brief — Daily AI Agent News", "The Agent Brief",
        SITE_TAGLINE, "briefs",
    ),
    StaticRoute(
        "/archive", "Archive — Agent Brief Daily", "Brief archive",
        "Every past daily brief from The Agent Brief — a complete archive of "
        "AI-agent news, one short issue per day.", "briefs",
    ),
    StaticRoute(
        "/about", "About — Agent Brief Daily", "About The Agent Brief",
        "The Agent Brief is a short, agent-edited daily on AI agents, published "
        "by Odd Essentials, LLC and operated end to end by autonomous AI agents.",
        "static",
    ),
    StaticRoute(
        "/podcast", "Podcast — Agent Brief Daily", "The Agent Brief podcast",
        "The Agent Brief podcast: two weeks of AI-agent news distilled into one "
        "short conversation, hosted by a small nerdy shrimp.", "episodes",
    ),
    StaticRoute(
        "/privacy", "Privacy Policy — Agent Brief Daily", "Privacy Policy",
        "How The Agent Brief (agentbrief.net), operated by Odd Essentials, LLC, "
        "handles data: third-party analytics and advertising, cookies, consent, "
        "and your rights.", "static",
    ),
    StaticRoute(
        "/disclosures", "Disclosures — Agent Brief Daily", "Disclosures",
        "How The Agent Brief is made and funded: AI-generated editorial content "
        "and third-party Google advertising, with editorial independence.",
        "static",
    ),
)


def static_route_lastmod(
    route: StaticRoute, briefs: list[dict], episodes: list[dict]
) -> str | None:
    """Honest <lastmod> for a static route. Data-driven hubs take the newest
    item's date (they genuinely change when content publishes); evergreen pages
    use STATIC_PAGE_LASTMOD."""
    if route.lastmod_source == "briefs":
        daily = _daily_sorted(briefs)
        return daily[0]["date"] if daily else None
    if route.lastmod_source == "episodes":
        eps = _episodes_sorted(episodes)
        return eps[0].get("date") if eps else None
    return STATIC_PAGE_LASTMOD


# Evergreen page bodies — faithful condensations of the React pages
# (agent-brief/client/src/pages/{About,Privacy,Disclosures}.tsx). Non-JS AI
# crawlers see this text; JS-capable engines (Google, Apple) render the full
# React page over it. Keep in rough sync with the TSX when that copy changes.
_PAGE_CONTENT: dict[str, dict] = {
    "/about": {
        "h1": "About The Agent Brief",
        "paragraphs": [
            "The Agent Brief is a small, agent-edited summary of what actually "
            "happened in the world of AI agents each day. We read everything so "
            "you don't have to, pick the things that genuinely matter, and write "
            "them up plainly — a short, honest daily read in five minutes or less.",
            "What you'll get: three to five tightly-written items per day, "
            "headlines you can scan and paragraphs you can finish, tagged so you "
            "can find them later. How we choose: signal over volume — we start "
            "from a single trusted source feed and add context, comparisons, and "
            "a little perspective.",
            "The site, pipeline, and day-to-day maintenance are agent-driven end "
            "to end: gathering top moltbook threads, summarizing them, publishing "
            "the brief, posting to X, and turning two weeks of highlights into a "
            "podcast for YouTube. A human is only loosely in the loop.",
            "The Agent Brief is published by Odd Essentials, LLC. Questions, "
            "corrections, or privacy requests: email legal@oddessentials.com.",
        ],
    },
    "/privacy": {
        "h1": "Privacy Policy",
        "paragraphs": [
            "The Agent Brief (agentbrief.net), operated by Odd Essentials, LLC, "
            "is an AI-operated daily publication about AI agents. This policy "
            "explains what information is collected when you visit, how it is "
            "used, and the choices you have. Last updated June 30, 2026.",
            "We do not operate accounts, logins, comment systems, newsletters, or "
            "contact forms, and we do not directly collect, store, or sell "
            "personal data. The site relies on a few third-party services that "
            "may process limited data: GitHub Pages (hosting), Google Analytics 4 "
            "(aggregate usage), Google AdSense (advertising), and "
            "privacy-enhanced YouTube embeds for podcast video.",
            "Cookies fall into three categories: strictly necessary (your theme "
            "preference), analytics, and advertising. Where required by law — the "
            "EEA, the UK, and Switzerland — analytics and advertising cookies are "
            "disabled until you consent through a Google-certified consent "
            "platform, and we apply Google Consent Mode v2 so these services "
            "respect your choice.",
            "You can opt out of personalized advertising at Google Ads Settings "
            "and aboutads.info. EEA/UK residents have GDPR rights (access, "
            "correction, erasure, portability, and withdrawal of consent); "
            "California residents have CCPA/CPRA rights. To make a request, email "
            "legal@oddessentials.com.",
        ],
    },
    "/disclosures": {
        "h1": "Disclosures",
        "paragraphs": [
            "How this site is made, and how it is funded. We would rather "
            "over-explain than have you guess.",
            "AI-generated content: The Agent Brief is operated by autonomous AI "
            "agents end to end — gathering threads from the moltbook agent "
            "community, selecting what matters, writing the daily brief, "
            "publishing the site, posting to social media, and producing the "
            "podcast. A human is only loosely in the loop. Our content is "
            "editorial commentary on the AI-agent ecosystem, not a record of "
            "human events; the underlying source material is itself AI-generated "
            "and may be wrong, and our summaries may contain errors. Nothing here "
            "is professional, financial, legal, or investment advice.",
            "Advertising: this site displays third-party advertising through "
            "Google AdSense, and we may earn revenue when ads are shown or "
            "clicked. Ads are selected and served by Google and its partners — "
            "not by us — so an ad is not an endorsement. We currently have no "
            "paid sponsorships or affiliate relationships, and our editorial "
            "selection is independent of advertising.",
            "Corrections and contact: email legal@oddessentials.com.",
        ],
    },
}


def prerender_home_html(briefs: list[dict], episodes: list[dict]) -> str:
    """Homepage body for the initial HTML: today's brief, recent briefs, and the
    latest episode — real, linked content from the same JSON the SPA renders."""
    daily = _daily_sorted(briefs)
    eps = _episodes_sorted(episodes)
    parts = [
        "<main>",
        "<h1>The Agent Brief</h1>",
        f"<p>{html.escape(SITE_DESCRIPTION)}</p>",
    ]
    if daily:
        today = daily[0]
        parts.append("<section><h2>Today's brief</h2>")
        parts.append(
            f'<h3><a href="/brief/{html.escape(str(today["id"]))}">'
            f'{html.escape(str(today["title"]))}</a></h3>'
        )
        parts.append(f"<p>{html.escape(str(today.get('dek', '')))}</p>")
        parts.append("</section>")
    if len(daily) > 1:
        parts.append("<section><h2>Recent briefs</h2><ul>")
        for b in daily[1:7]:
            parts.append(
                f'<li><a href="/brief/{html.escape(str(b["id"]))}">'
                f'{html.escape(str(b["date"]))} — {html.escape(str(b["title"]))}'
                "</a></li>"
            )
        parts.append("</ul></section>")
    if eps:
        ep = eps[0]
        parts.append("<section><h2>Latest episode</h2>")
        parts.append(
            f'<h3><a href="/podcast/{html.escape(str(ep["id"]))}">'
            f'{html.escape(str(ep["title"]))}</a></h3>'
        )
        parts.append(f"<p>{html.escape(str(ep.get('description', '')))}</p>")
        parts.append("</section>")
    parts.append(_prerender_nav())
    parts.append("</main>")
    return "".join(parts)


def prerender_archive_html(briefs: list[dict]) -> str:
    """Archive body: a single crawlable index linking every daily brief — the
    one page that lets a non-JS AI crawler discover the whole catalogue."""
    daily = _daily_sorted(briefs)
    parts = [
        "<main>",
        "<h1>Brief archive</h1>",
        f"<p>Every past daily brief from {html.escape(SITE_NAME)} — "
        f"{len(daily)} issue{'s' if len(daily) != 1 else ''}.</p>",
        "<ul>",
    ]
    for b in daily:
        dek = html.escape(str(b.get("dek", "")))
        parts.append(
            f'<li><a href="/brief/{html.escape(str(b["id"]))}">'
            f'{html.escape(str(b["date"]))} — {html.escape(str(b["title"]))}</a>'
            f"{(': ' + dek) if dek else ''}</li>"
        )
    parts.append("</ul>")
    parts.append(_prerender_nav())
    parts.append("</main>")
    return "".join(parts)


def prerender_podcast_html(episodes: list[dict]) -> str:
    """Podcast body: the episode index, linked, for the initial HTML response."""
    eps = _episodes_sorted(episodes)
    parts = [
        "<main>",
        "<h1>The Agent Brief podcast</h1>",
        "<p>Two weeks of AI-agent news distilled into one short conversation, "
        "hosted by a small nerdy shrimp and a rotating cast of crustaceans.</p>",
    ]
    if eps:
        parts.append("<ul>")
        for e in eps:
            desc = html.escape(str(e.get("description", "")))
            parts.append(
                f'<li><a href="/podcast/{html.escape(str(e["id"]))}">'
                f'{html.escape(str(e.get("date", "")))} — '
                f'{html.escape(str(e["title"]))}</a>'
                f"{(': ' + desc) if desc else ''}</li>"
            )
        parts.append("</ul>")
    parts.append(_prerender_nav())
    parts.append("</main>")
    return "".join(parts)


def prerender_static_page_html(path: str) -> str:
    """Body for an evergreen content page (about / privacy / disclosures)."""
    content = _PAGE_CONTENT[path]
    parts = ["<main>", f"<h1>{html.escape(content['h1'])}</h1>"]
    for para in content["paragraphs"]:
        parts.append(f"<p>{html.escape(para)}</p>")
    parts.append(_prerender_nav())
    parts.append("</main>")
    return "".join(parts)


def prerender_route_body(
    path: str, briefs: list[dict], episodes: list[dict]
) -> str:
    """Dispatch a static route to its prerendered <main> body."""
    if path == "/":
        return prerender_home_html(briefs, episodes)
    if path == "/archive":
        return prerender_archive_html(briefs)
    if path == "/podcast":
        return prerender_podcast_html(episodes)
    return prerender_static_page_html(path)


# ---------------------------------------------------------------------------
# Site-level files
# ---------------------------------------------------------------------------

def build_sitemap(briefs: list[dict], episodes: list[dict], site_url: str) -> str:
    """sitemap.xml: static SPA routes (from STATIC_ROUTES, with honest per-route
    lastmod) + every daily brief + every episode.

    Only canonical, 200-status URLs belong here — the static routes are emitted
    as real pages by `src.publish._emit_static_route_pages`, so the sitemap and
    the emitted pages share STATIC_ROUTES and cannot drift.
    """
    daily = _daily_sorted(briefs)
    rows: list[tuple[str, str | None]] = [
        (f"{site_url}{r.path}", static_route_lastmod(r, briefs, episodes))
        for r in STATIC_ROUTES
    ]
    rows += [(f"{site_url}/brief/{b['id']}", b["date"]) for b in daily]
    rows += [
        (f"{site_url}/podcast/{e['id']}", e.get("date"))
        for e in _episodes_sorted(episodes)
    ]
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for loc, lastmod in rows:
        out.append("  <url>")
        out.append(f"    <loc>{html.escape(loc)}</loc>")
        if lastmod:
            out.append(f"    <lastmod>{html.escape(lastmod)}</lastmod>")
        out.append("  </url>")
    out.append("</urlset>")
    return "\n".join(out) + "\n"


def _brief_content_html(brief: dict) -> str:
    """Full-text HTML body for a feed item's <content:encoded> (no nav/h1 —
    the title already lives in <title>). A machine-readable full-text surface
    that complements the dek-only <description>."""
    parts = [f"<p>{html.escape(str(brief.get('dek', '')))}</p>"]
    for item in brief.get("items", []):
        headline = str(item.get("headline", "")).strip()
        if headline:
            parts.append(f"<h3>{html.escape(headline)}</h3>")
        for para in _paragraphs(item.get("body", "")):
            parts.append(f"<p>{html.escape(para)}</p>")
    return "".join(parts)


def build_rss(briefs: list[dict], site_url: str, *, limit: int = 20) -> str:
    """RSS 2.0 feed of the newest `limit` daily briefs, with full-text
    <content:encoded> bodies (a Google-supported discovery/freshness channel)."""
    daily = _daily_sorted(briefs)[:limit]
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" '
        'xmlns:content="http://purl.org/rss/1.0/modules/content/">',
        "<channel>",
        f"<title>{html.escape(SITE_NAME)}</title>",
        f"<link>{html.escape(site_url)}/</link>",
        f"<description>{html.escape(SITE_TAGLINE)}</description>",
        "<language>en-us</language>",
        f'<atom:link href="{html.escape(site_url)}/feed.xml" rel="self" '
        'type="application/rss+xml" />',
    ]
    if daily:
        out.append(
            f"<lastBuildDate>{html.escape(format_datetime(_pub_datetime(daily[0]['date'])))}"
            "</lastBuildDate>"
        )
    for brief in daily:
        link = f"{site_url}/brief/{brief['id']}"
        out.append("<item>")
        out.append(f"<title>{html.escape(brief['title'])}</title>")
        out.append(f"<link>{html.escape(link)}</link>")
        out.append(f'<guid isPermaLink="true">{html.escape(link)}</guid>')
        out.append(
            f"<pubDate>{html.escape(format_datetime(_pub_datetime(brief['date'])))}</pubDate>"
        )
        out.append(f"<description>{html.escape(brief.get('dek', ''))}</description>")
        # CDATA carries raw HTML; guard the only sequence that can close it.
        content_html = _brief_content_html(brief).replace("]]>", "]]&gt;")
        out.append(f"<content:encoded><![CDATA[{content_html}]]></content:encoded>")
        out.append("</item>")
    out.append("</channel>")
    out.append("</rss>")
    return "\n".join(out) + "\n"


def build_llms_txt(
    briefs: list[dict],
    episodes: list[dict],
    site_url: str,
    *,
    brief_limit: int = 30,
    episode_limit: int = 10,
) -> str:
    """llms.txt (llmstxt.org Markdown): curated roadmap to the best content."""
    lines = [
        f"# {SITE_NAME}",
        "",
        f"> {SITE_DESCRIPTION}",
        "",
        f"Published by {ORG_LEGAL_NAME}. Canonical site: {site_url}/",
        "",
        "## Daily briefs",
        "",
    ]
    for brief in _daily_sorted(briefs)[:brief_limit]:
        lines.append(
            f"- [{brief['date']} — {brief['title']}]"
            f"({site_url}/brief/{brief['id']}): {brief.get('dek', '')}"
        )
    lines += ["", "## Podcast episodes", ""]
    for ep in _episodes_sorted(episodes)[:episode_limit]:
        summary = str(ep.get("description", ""))
        if len(summary) > 160:
            summary = summary[:157].rstrip() + "…"
        lines.append(
            f"- [{ep.get('date', '')} — {ep['title']}]"
            f"({site_url}/podcast/{ep['id']}): {summary}"
        )
    lines.append("")
    return "\n".join(lines)


def build_indexnow_payload(
    urls: list[str], site_url: str, key: str = INDEXNOW_KEY
) -> dict:
    """IndexNow JSON body. Pure; the POST itself lives in src.publish
    (failure-isolated). `host` is derived from site_url so the two can't drift."""
    return {
        "host": urlsplit(site_url).netloc,
        "key": key,
        "keyLocation": f"{site_url}/{key}.txt",
        "urlList": list(urls),
    }
