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
    return "".join(parts)


def episode_head_extras(episode: dict, site_url: str) -> str:
    """Additive <head> content for an episode — article:published_time plus
    PodcastEpisode JSON-LD (canonical/description are template rewrites)."""
    published = _pub_datetime(episode["date"]).isoformat()
    parts = [
        f'<meta property="article:published_time" '
        f'content="{html.escape(published, quote=True)}" />',
        episode_jsonld(episode, site_url),
    ]
    return "".join(parts)


# ---------------------------------------------------------------------------
# Prerendered body (injected into #root; SPA replaces it on mount)
# ---------------------------------------------------------------------------

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
    return "".join(parts)


# ---------------------------------------------------------------------------
# Site-level files
# ---------------------------------------------------------------------------

def build_sitemap(
    briefs: list[dict],
    episodes: list[dict],
    site_url: str,
    *,
    static_paths: tuple[str, ...] = (
        "/",
        "/archive",
        "/about",
        "/podcast",
        "/privacy",
        "/disclosures",
    ),
) -> str:
    """sitemap.xml: static SPA routes + every daily brief + every episode."""
    daily = _daily_sorted(briefs)
    newest = daily[0]["date"] if daily else None
    rows: list[tuple[str, str | None]] = [(f"{site_url}{p}", newest) for p in static_paths]
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


def build_rss(briefs: list[dict], site_url: str, *, limit: int = 20) -> str:
    """RSS 2.0 feed of the newest `limit` daily briefs."""
    daily = _daily_sorted(briefs)[:limit]
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
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
