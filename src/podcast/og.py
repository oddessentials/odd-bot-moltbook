"""Per-episode OG page generator.

Emits `docs/podcast/<id>/index.html` for each published episode. The page
is a copy of the SPA template (`docs/index.html`) with the same seven
OG/Twitter meta tags rewritten that `src/publish.py:_render_per_brief_html`
rewrites for daily briefs. Static brand tags (og:image, og:site_name,
twitter:image, all favicon/theme tags) are preserved untouched.

Mirroring the per-brief pattern is deliberate: X.com's Card crawler hits
`/podcast/<id>` and reads episode-specific og:title + og:description from
this static page rather than the SPA shell. The SPA's wouter route still
resolves `/podcast/<id>` client-side via the 404.html fallback for
human visitors; this file just gives crawlers and X cards something
concrete.

Sandbox: output is anchored on `docs/podcast/<episode_id>/index.html`
where `episode_id` comes from `manifest_path.parent.name` — operator-
supplied filesystem location, never `manifest["id"]`. The shared
`resolve_inside_dir` helper enforces the boundary.

The published file's relative path is recorded back in the manifest as
`og_html_path`. Phase 2.1's publish gate G5 reads that field; until this
generator runs, G5 fail-closes every publish (intentional).
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from pathlib import Path
from typing import Any

from .config import (
    DOCS_INDEX_PATH,
    PODCAST_OG_DIR,
    REPO_ROOT,
    SITE_URL,
)
from .manifest import (
    EpisodeBoundaryError,
    advance_validation_status,
    atomic_write_text,
    manifest_path_for,
    read_manifest,
    resolve_inside_dir,
    write_manifest,
)
from .schema import EpisodeRecord


# Pre-compiled patterns for the seven meta tags the renderer rewrites.
# Attribute-order-insensitive; tolerates `>` vs ` />` self-closing styles.
# Targets the SPA template at agent-brief/client/index.html — a non-1
# match count raises (drift detection so a future template change that
# strips/duplicates Card meta surfaces here, not silently as broken
# X cards).
_TITLE_TAG_RE = re.compile(r"<title[^>]*>[^<]*</title>", re.IGNORECASE)
_OG_TITLE_RE = re.compile(r'<meta[^>]*\bproperty="og:title"[^>]*/?>', re.IGNORECASE)
_OG_DESC_RE = re.compile(r'<meta[^>]*\bproperty="og:description"[^>]*/?>', re.IGNORECASE)
_OG_URL_RE = re.compile(r'<meta[^>]*\bproperty="og:url"[^>]*/?>', re.IGNORECASE)
_OG_TYPE_RE = re.compile(r'<meta[^>]*\bproperty="og:type"[^>]*/?>', re.IGNORECASE)
_TW_TITLE_RE = re.compile(r'<meta[^>]*\bname="twitter:title"[^>]*/?>', re.IGNORECASE)
_TW_DESC_RE = re.compile(r'<meta[^>]*\bname="twitter:description"[^>]*/?>', re.IGNORECASE)


def render_episode_og_html(template_html: str, record: EpisodeRecord) -> str:
    """Rewrite the SPA template with episode-specific OG/Twitter meta.

    Pure (no I/O). Returns a new HTML string; `template_html` is not
    mutated. Raises RuntimeError if any of the seven targeted tags
    doesn't match exactly once in the template (template drift).

    Per-episode rewrites:
      <title>          → "{title} — Agent Brief Daily"
      og:title         → record.title (HTML-escaped)
      og:description   → "Episode {episodeNo} · {description}" (HTML-escaped)
      og:url           → SITE_URL/podcast/{id}
      og:type          → "article" (parity with daily briefs)
      twitter:title    → record.title (HTML-escaped)
      twitter:description → "Episode {episodeNo} · {description}" (HTML-escaped)

    Static across all episodes (NOT touched here):
      og:image, og:image:width, og:image:height, og:site_name,
      twitter:card, twitter:image, all favicon/theme tags.
    """
    title = html.escape(record.title, quote=True)
    description_escaped = html.escape(record.description, quote=True)
    description = f"Episode {record.episodeNo} · {description_escaped}"
    # record.id is schema-constrained to slug-safe chars (see
    # EpisodeRecord.id pattern); HTML-escape is defense in depth for the
    # case where a future caller bypasses the schema or for diagnostic
    # output that prints the URL alongside other content.
    canonical_url = html.escape(
        f"{SITE_URL}/podcast/{record.id}", quote=True,
    )

    rewrites: list[tuple[re.Pattern[str], str, str]] = [
        (_TITLE_TAG_RE,
         f"<title>{title} — Agent Brief Daily</title>",
         "<title>"),
        (_OG_TITLE_RE,
         f'<meta property="og:title" content="{title}" />',
         'meta property="og:title"'),
        (_OG_DESC_RE,
         f'<meta property="og:description" content="{description}" />',
         'meta property="og:description"'),
        (_OG_URL_RE,
         f'<meta property="og:url" content="{canonical_url}" />',
         'meta property="og:url"'),
        (_OG_TYPE_RE,
         '<meta property="og:type" content="article" />',
         'meta property="og:type"'),
        (_TW_TITLE_RE,
         f'<meta name="twitter:title" content="{title}" />',
         'meta name="twitter:title"'),
        (_TW_DESC_RE,
         f'<meta name="twitter:description" content="{description}" />',
         'meta name="twitter:description"'),
    ]

    out = template_html
    for pattern, replacement, label in rewrites:
        new_out, count = pattern.subn(replacement, out)
        if count != 1:
            raise RuntimeError(
                f"per-episode HTML render: expected exactly one {label} tag "
                f"in template; got {count}. SPA template may have drifted — "
                "check agent-brief/client/index.html"
            )
        out = new_out
    return out


def generate_episode_og(*, manifest_path: Path) -> Path:
    """Render and write the per-episode OG page; return the absolute Path.

    Reads the SPA template from `docs/index.html` (built by the daily
    brief pipeline; this generator depends on it existing). Renders
    against the episode's `episode_record` from the manifest. Writes
    to `docs/podcast/<episode_id>/index.html` where `episode_id` comes
    from `manifest_path.parent.name`.

    Persists the resulting relative path back to the manifest as
    `og_html_path`. Re-running on an existing manifest re-renders the
    file (idempotent — same inputs produce same output bytes).
    """
    if not DOCS_INDEX_PATH.exists():
        raise RuntimeError(
            f"SPA template not found at {DOCS_INDEX_PATH}. The podcast OG "
            "generator depends on the daily brief pipeline having built "
            "the SPA at least once. Run `pnpm --dir agent-brief build` "
            "or trigger a daily publish first."
        )

    manifest = read_manifest(manifest_path)
    record_payload = manifest.get("episode_record")
    if not record_payload:
        raise RuntimeError(
            "manifest.episode_record missing — run `upload` before `og`."
        )
    record = EpisodeRecord.model_validate(record_payload)

    episode_id = manifest_path.parent.name  # filesystem-derived
    out_dir = PODCAST_OG_DIR / episode_id
    out_path = out_dir / "index.html"

    # Sanity-check the output path against the docs/podcast/ sandbox.
    # episode_id is filesystem-derived so this is a defense-in-depth
    # check rather than the primary guard, but it catches a future bug
    # where an upstream caller manages to slip a tampered episode_id
    # past `manifest_path.parent.name`.
    boundary = PODCAST_OG_DIR / episode_id
    try:
        resolve_inside_dir(
            boundary=boundary,
            recorded_rel=str(out_path.relative_to(REPO_ROOT)),
        )
    except EpisodeBoundaryError as e:
        raise RuntimeError(f"OG output path escapes sandbox: {e}") from e

    template_html = DOCS_INDEX_PATH.read_text()
    rendered = render_episode_og_html(template_html, record)
    out_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(out_path, rendered)

    # Record the relative path so publish gate G5 can find + re-validate.
    manifest = read_manifest(manifest_path)
    manifest["og_html_path"] = str(out_path.relative_to(REPO_ROOT))
    write_manifest(manifest_path, manifest)
    return out_path


def cmd_og(args: argparse.Namespace) -> int:
    eid = args.episode_id
    mpath = manifest_path_for(eid)
    if not mpath.exists():
        print(f"manifest missing at {mpath}", file=sys.stderr)
        return 2
    try:
        out_path = generate_episode_og(manifest_path=mpath)
    except RuntimeError as e:
        print(f"og generation failed: {e}", file=sys.stderr)
        return 2
    advance_validation_status(mpath, "og_generated")
    rel = out_path.relative_to(REPO_ROOT)
    print(f"OG page rendered at {rel}")
    return 0
