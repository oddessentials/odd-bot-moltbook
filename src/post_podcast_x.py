"""X.com downstream-consumer for podcast episodes.

Mirrors `src/post_x.py` (daily brief X-post) with episode-specific bits.
Shares the @oddessentials channel + the same X OAuth refresh token, but
runs as its own workflow on its own sidecar so daily-brief tweets and
podcast-episode tweets stay independently auditable.

Discovery semantics:
  - Source of truth: `data/episodes.json` (engine-owned, schema-locked
    EpisodeRecord per Phase 2.1).
  - Push range: parent commit's episodes.json vs after commit's.
  - Eligibility: any id present in after but not in before. The
    EpisodeRecord schema already constrains id to slug-safe characters,
    so no further regex filter is needed at this layer.
  - Sort: episodeNo descending. Newest first.

Idempotency contract is identical to the daily flow:
  - Sidecar `data/podcast-x-posts.jsonl` is the only durable record of
    what's been tweeted; it gates every future run.
  - Tweet runs BEFORE sidecar write — failure leaves no audit row, and
    "manual repair only" is the recovery posture.
  - Latest eligible id posts; older eligible-but-unrecorded ids become
    `skipped_catchup` rows (no replay tweets).

Tweet text shape:
  <joke>\\n<news.oddessentials.ai/podcast/<id>>

The joke synthesizer is a one-shot Claude call with title + description
(EpisodeRecord fields). Same prompt skeleton as the daily flow but the
input fields differ.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional


def discover_new_published_episode_ids(
    before_episodes: list[dict],
    after_episodes: list[dict],
) -> list[str]:
    """Return new episode ids visible in the push range, newest first.

    "New" = present in `after_episodes` AND not present in
    `before_episodes`. Sort: episodeNo descending so the latest episode
    is index 0. Ties broken by id descending for determinism.
    """
    def _record_ids_by_episode_no(eps: list[dict]) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in eps:
            eid = e.get("id")
            ep_no = e.get("episodeNo")
            if isinstance(eid, str) and isinstance(ep_no, int):
                out[eid] = ep_no
        return out

    before_ids = set(_record_ids_by_episode_no(before_episodes).keys())
    after_map = _record_ids_by_episode_no(after_episodes)
    new = [eid for eid in after_map if eid not in before_ids]
    # Sort by episodeNo desc, then id desc as a stable tiebreaker.
    return sorted(new, key=lambda eid: (after_map[eid], eid), reverse=True)


def select_post_target(
    eligible_ids: list[str],
    already_posted: set[str],
) -> tuple[Optional[str], list[str]]:
    """Pick the latest eligible id not yet in the sidecar.

    Same contract as the daily flow's select_post_target (mirroring
    keeps the catch-up policy identical across both pipelines).
    Returns (post_id_or_None, skipped_catchup_ids).
    """
    if not eligible_ids:
        return None, []
    latest = eligible_ids[0]
    older_unrecorded = [x for x in eligible_ids[1:] if x not in already_posted]
    if latest in already_posted:
        return None, older_unrecorded
    return latest, older_unrecorded


def _already_posted_ids(sidecar_path: Path) -> set[str]:
    if not sidecar_path.exists():
        return set()
    posted: set[str] = set()
    for line in sidecar_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        rid = rec.get("id")
        if isinstance(rid, str):
            posted.add(rid)
    return posted


def _append_sidecar(sidecar_path: Path, entry: dict) -> None:
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    with sidecar_path.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def run_post_podcast_x(
    sidecar_path: Path,
    before_episodes: list[dict],
    after_episodes: list[dict],
    *,
    joke_synthesizer: Callable[[dict], str],
    tweet_poster: Callable[[str], str],
    url_prober: Callable[[str], bool],
    article_url_template: str = "https://news.oddessentials.ai/podcast/{id}",
    log: Callable[[str], None] = lambda _msg: None,
) -> int:
    """Episode-side mirror of src/post_x.run_post_x.

    Returns 0 on success or no-op, 1 on URL-probe abort. Propagates
    exceptions from `tweet_poster`. Sidecar is never written before a
    successful tweet — same "manual repair only" invariant as the daily
    pipeline.
    """
    eligible = discover_new_published_episode_ids(before_episodes, after_episodes)
    already_posted = _already_posted_ids(sidecar_path)
    log(
        f"[podcast-x] discovery: {len(eligible)} eligible episode id(s) "
        f"({eligible}); sidecar already_posted={len(already_posted)}"
    )
    post_id, skipped = select_post_target(eligible, already_posted)
    if post_id is None:
        if skipped:
            log(
                f"[podcast-x] no-op: latest already in sidecar; completing "
                f"audit with {len(skipped)} skipped_catchup row(s)"
            )
            now = datetime.now(timezone.utc).isoformat()
            for sk_id in skipped:
                _append_sidecar(sidecar_path, {
                    "id": sk_id,
                    "status": "skipped_catchup",
                    "skipped_at": now,
                })
        else:
            log("[podcast-x] no-op: nothing to do")
        return 0

    log(f"[podcast-x] select: post_id={post_id}, skipped_catchup={skipped}")
    url = article_url_template.format(id=post_id)
    if not url_prober(url):
        log("[podcast-x] abort: asset bundle probe budget exhausted; no tweet, no sidecar mutation")
        return 1

    episode = next((e for e in after_episodes if e.get("id") == post_id), None)
    if episode is None:
        raise RuntimeError(
            f"episode {post_id} eligible but missing from after_episodes",
        )

    joke = joke_synthesizer(episode)
    text = f"{joke}\n{url}"
    log(f"[podcast-x] post: text length={len(text)} chars")
    tweet_id = tweet_poster(text)
    log(f"[podcast-x] post: ok (tweet_id={tweet_id})")

    now = datetime.now(timezone.utc).isoformat()
    _append_sidecar(sidecar_path, {
        "id": post_id,
        "tweet_id": tweet_id,
        "url": url,
        "posted_at": now,
    })
    for sk_id in skipped:
        _append_sidecar(sidecar_path, {
            "id": sk_id,
            "status": "skipped_catchup",
            "skipped_at": now,
        })
    log(f"[podcast-x] sidecar: appended 1 posted row + {len(skipped)} skipped_catchup row(s)")
    return 0


# =============================================================================
# Network helpers + CLI entry point. NOT exercised by the unit suite —
# the orchestrator above is fully testable with injected callables.
# Heavy imports are lazy so test imports stay clean.
# =============================================================================

CLAUDE_MODEL = "claude-opus-4-7"
X_DOMAIN = "https://news.oddessentials.ai"
TOKEN_ENDPOINT = "https://api.x.com/2/oauth2/token"

import re

_BUNDLE_RE = re.compile(r'<script[^>]+\bsrc="(/assets/index-[^"]+\.js)"')

_JOKE_PROMPT = """\
Write a single-line, dry, observational joke that captures the spirit \
of the podcast episode below. Constraints: at most 250 characters, no \
emojis, no hashtags, no surrounding quotes, no preamble. Output the \
joke and nothing else.

Title: {title}
Description: {description}\
"""


def _refresh_access_token(
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> dict:
    """Same confidential-client refresh grant as src/post_x.py.

    Per X docs: `Authorization: Basic <base64(client_id:client_secret)>`
    and `client_id` is NOT in the body. Caller introspects the response
    for `refresh_token` to decide on rotation writeback.
    """
    import base64
    import urllib.parse
    import urllib.request

    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }).encode("utf-8")
    basic = base64.b64encode(
        f"{client_id}:{client_secret}".encode("utf-8"),
    ).decode("ascii")
    req = urllib.request.Request(
        TOKEN_ENDPOINT,
        data=body,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post_tweet(text: str, access_token: str) -> str:
    """tweepy.Client with OAuth 2.0 user-context. user_auth=False is
    REQUIRED — tweepy defaults to OAuth 1.0a which silently 401s
    against an OAuth 2.0 bearer token.
    """
    import tweepy

    client = tweepy.Client(bearer_token=access_token)
    response = client.create_tweet(text=text, user_auth=False)
    return str(response.data["id"])


def _extract_bundle_path(index_html_path: Path) -> str:
    html = index_html_path.read_text()
    m = _BUNDLE_RE.search(html)
    if not m:
        raise RuntimeError(
            f"could not locate Vite asset bundle in {index_html_path}",
        )
    return m.group(1)


_PROBE_USER_AGENT = (
    "odd-bot-moltbook/0.1 podcast-x-post-probe "
    "(+https://github.com/oddessentials/odd-bot-moltbook)"
)


def _probe_asset_bundle(
    asset_url: str,
    *,
    max_retries: int = 10,
    backoff_seconds: int = 30,
) -> bool:
    """HEAD-probe the content-hashed bundle URL until 200 or budget out.

    Same shape as src/post_x._probe_asset_bundle. Probes the bundle
    rather than the article deep-link because the SPA's 404.html
    fallback would 200 on /podcast/<id> regardless of whether the new
    episodes.json is in the deployed bundle.
    """
    import time
    import urllib.error
    import urllib.request

    for attempt in range(max_retries):
        attempt_label = f"attempt {attempt + 1}/{max_retries}"
        try:
            req = urllib.request.Request(
                asset_url,
                method="HEAD",
                headers={"User-Agent": _PROBE_USER_AGENT},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                print(
                    f"[podcast-x] probe {attempt_label}: HTTP {resp.status}",
                    flush=True,
                )
                if 200 <= resp.status < 300:
                    return True
        except urllib.error.HTTPError as e:
            print(
                f"[podcast-x] probe {attempt_label}: HTTPError {e.code}",
                flush=True,
            )
            if e.code != 404:
                return False
        except urllib.error.URLError as e:
            print(
                f"[podcast-x] probe {attempt_label}: URLError "
                f"({type(e).__name__})",
                flush=True,
            )
        if attempt < max_retries - 1:
            time.sleep(backoff_seconds)
    print(
        f"[podcast-x] probe: budget exhausted after {max_retries} attempts",
        flush=True,
    )
    return False


def _synthesize_joke(episode: dict, anthropic_api_key: str) -> str:
    """Generate a one-liner via Claude. Hard-clipped to 250 chars.

    Input fields (title + description) come from the EpisodeRecord
    shape — distinct from the brief flow's (title + dek) but the
    prompt skeleton mirrors it.
    """
    import anthropic

    client = anthropic.Anthropic(api_key=anthropic_api_key)
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": _JOKE_PROMPT.format(
                title=episode.get("title", ""),
                description=episode.get("description", ""),
            ),
        }],
    )
    text = response.content[0].text.strip()
    if len(text) > 250:
        text = text[:247] + "..."
    return text


def _read_episodes_at_ref(ref: str, repo_relative_path: str) -> list[dict]:
    """`git show <ref>:<path>` parsed as JSON.

    Returns [] when the ref resolves but the file didn't exist at that
    revision (legitimate empty parent state for an initial publish).
    Raises if the ref doesn't resolve or the content isn't valid JSON.
    Failing closed on these is deliberate — same rationale as the
    daily flow's _read_briefs_at_ref.
    """
    import subprocess

    result = subprocess.run(
        ["git", "show", f"{ref}:{repo_relative_path}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        file_missing_signals = (
            "does not exist in",
            "exists on disk, but not in",
        )
        if any(sig in stderr for sig in file_missing_signals):
            return []
        raise RuntimeError(
            f"git show failed for ref '{ref}' (path '{repo_relative_path}'): "
            f"{stderr or 'unknown error'}",
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"data at '{ref}:{repo_relative_path}' is not valid JSON: {e}",
        )


def _writeback_refresh_token(new_token: str) -> None:
    import subprocess

    subprocess.run(
        ["gh", "secret", "set", "X_REFRESH_TOKEN", "--body", new_token],
        check=True,
    )


def _is_zero_sha(sha: str) -> bool:
    return bool(sha) and set(sha) == {"0"}


def main() -> int:
    """CLI entry point invoked by .github/workflows/podcast-x-post.yml."""
    import os

    def _log(msg: str) -> None:
        print(msg, flush=True)

    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    _log(f"[podcast-x] starting workflow run {run_id}")

    repo_root = Path(os.environ.get("GITHUB_WORKSPACE", "."))
    sidecar_path = repo_root / "data" / "podcast-x-posts.jsonl"
    episodes_path = repo_root / "data" / "episodes.json"
    index_html_path = repo_root / "docs" / "index.html"

    after_episodes = json.loads(episodes_path.read_text())

    # Defensive validation against the canonical EpisodeRecord schema —
    # matches the daily flow's posture (refuse on malformed source-of-truth).
    from src.podcast.schema import EpisodeRecord
    for entry in after_episodes:
        EpisodeRecord(**entry)

    before_sha = os.environ.get("GITHUB_EVENT_BEFORE", "")
    if not before_sha or _is_zero_sha(before_sha):
        _log("[podcast-x] before_sha empty/zero — initial-push semantics; before_episodes=[]")
        before_episodes: list[dict] = []
    else:
        _log(f"[podcast-x] reading parent episodes.json at before_sha={before_sha[:7]}")
        before_episodes = _read_episodes_at_ref(before_sha, "data/episodes.json")

    bundle_path = _extract_bundle_path(index_html_path)
    bundle_url = f"{X_DOMAIN}{bundle_path}"
    _log(f"[podcast-x] asset bundle probe target: {bundle_url}")

    def url_prober(_article_url: str) -> bool:
        return _probe_asset_bundle(bundle_url)

    x_client_id = os.environ["X_CLIENT_ID"].strip()
    x_client_secret = os.environ["X_CLIENT_SECRET"].strip()
    x_refresh_token = os.environ["X_REFRESH_TOKEN"].strip()
    anthropic_key = os.environ["ANTHROPIC_API_KEY"].strip()

    cached_access_token: dict[str, Optional[str]] = {"value": None}

    def tweet_poster(text: str) -> str:
        if cached_access_token["value"] is None:
            _log("[podcast-x] refresh: requesting new access token")
            response = _refresh_access_token(
                x_client_id,
                x_client_secret,
                x_refresh_token,
            )
            cached_access_token["value"] = response["access_token"]
            new_refresh = response.get("refresh_token")
            rotated = bool(new_refresh and new_refresh != x_refresh_token)
            _log(f"[podcast-x] refresh: ok (rotation={'yes' if rotated else 'no'})")
            if rotated:
                _log("[podcast-x] writeback: gh secret set X_REFRESH_TOKEN")
                _writeback_refresh_token(new_refresh)
                _log("[podcast-x] writeback: ok")
        return _post_tweet(text, cached_access_token["value"])

    def joke_synthesizer(episode: dict) -> str:
        return _synthesize_joke(episode, anthropic_key)

    return run_post_podcast_x(
        sidecar_path,
        before_episodes,
        after_episodes,
        joke_synthesizer=joke_synthesizer,
        tweet_poster=tweet_poster,
        url_prober=url_prober,
        log=_log,
    )


if __name__ == "__main__":
    import sys
    sys.exit(main())
