"""X.com downstream-consumer: orchestrator + network glue + CLI.

The pure functions and `run_post_x` orchestrator are unit-tested in
`tests/test_post_x.py` with injected callables. The network helpers
(OAuth refresh dance, tweepy post, asset-bundle probe, joke
synthesis) and `main()` below are NOT covered by the unit suite —
they're validated empirically in Phase 4 derisking via
`workflow_dispatch` against `X_REFRESH_TOKEN_TEST`. Heavy imports
(tweepy, anthropic, subprocess) are lazy so the orchestrator above
stays import-clean for tests.

See memory `x_post_downstream_plan.md` for the locked contract:
diff range semantics, idempotency invariant, catch-up policy,
ordering rules, conditional-writeback rotation handler, and
asset-bundle probe rationale (the SPA's 404.html fallback makes a
deep-link probe a false positive — we probe the content-hashed JS
bundle instead).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional


_DAILY_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def discover_new_published_daily_ids(
    before_briefs: list[dict],
    after_briefs: list[dict],
) -> list[str]:
    """Return new published-daily ids visible in the push range, latest first.

    "New" means the id is present-and-published in `after_briefs` AND is
    NOT present-and-published in `before_briefs`. Filters by id-shape
    regex `^\\d{4}-\\d{2}-\\d{2}$` — daily slugs only; weeklies ignored.
    """
    def _published_dailies(briefs: list[dict]) -> set[str]:
        return {
            b["id"] for b in briefs
            if b.get("status") == "published"
            and isinstance(b.get("id"), str)
            and _DAILY_ID_RE.match(b["id"])
        }

    new_ids = _published_dailies(after_briefs) - _published_dailies(before_briefs)
    # Daily-slug ids (YYYY-MM-DD) sort lexically == sort chronologically.
    return sorted(new_ids, reverse=True)


def select_post_target(
    eligible_ids: list[str],
    already_posted: set[str],
) -> tuple[Optional[str], list[str]]:
    """Pick the latest eligible id not yet in the sidecar.

    `eligible_ids` arrives latest-first. Returns
    `(post_id_or_None, skipped_catchup_ids)`.

    The latest non-already-posted id is the post target. Any *older*
    eligible ids that are also non-already-posted become
    skipped_catchup rows. If every eligible id is already posted,
    returns `(None, [])`.
    """
    if not eligible_ids:
        return None, []
    latest = eligible_ids[0]
    older_unrecorded = [x for x in eligible_ids[1:] if x not in already_posted]
    if latest in already_posted:
        # Replay of a prior run: latest was tweeted then. Do NOT fall
        # through to next-newest — that's an id the original run
        # classified as skipped_catchup, and posting it would
        # contradict the catch-up policy. Surface still-unrecorded
        # older eligibles so the orchestrator can complete the audit.
        return None, older_unrecorded
    return latest, older_unrecorded


def _already_posted_ids(sidecar_path: Path) -> set[str]:
    """Read previously-recorded ids from the sidecar log.

    Tolerates malformed lines — sidecar is append-only audit, not
    load-bearing for orchestrator decisions beyond "id was seen."
    """
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


def run_post_x(
    sidecar_path: Path,
    before_briefs: list[dict],
    after_briefs: list[dict],
    *,
    joke_synthesizer: Callable[[dict], str],
    tweet_poster: Callable[[str], str],
    url_prober: Callable[[str], bool],
    article_url_template: str = "https://news.oddessentials.ai/brief/{id}",
) -> int:
    """End-to-end X-post orchestrator.

    Returns 0 on success or no-op, non-zero on URL-probe abort.
    Propagates exceptions from `tweet_poster`. The sidecar is never
    written before a successful tweet — failure leaves no audit row,
    matching the "manual repair only" invariant in the locked spec.
    """
    eligible = discover_new_published_daily_ids(before_briefs, after_briefs)
    already_posted = _already_posted_ids(sidecar_path)
    post_id, skipped = select_post_target(eligible, already_posted)
    if post_id is None:
        # Replay: latest already tweeted (or no eligibles at all).
        # Complete audit state by writing any missing skipped_catchup
        # rows, then exit without a tweet.
        if skipped:
            now = datetime.now(timezone.utc).isoformat()
            for sk_id in skipped:
                _append_sidecar(sidecar_path, {
                    "id": sk_id,
                    "status": "skipped_catchup",
                    "skipped_at": now,
                })
        return 0

    url = article_url_template.format(id=post_id)
    if not url_prober(url):
        # Pages deploy not live at the deep-link yet; abort with no
        # tweet and no sidecar mutation. Next run retries.
        return 1

    brief = next((b for b in after_briefs if b.get("id") == post_id), None)
    if brief is None:
        raise RuntimeError(
            f"brief {post_id} eligible but missing from after_briefs",
        )

    joke = joke_synthesizer(brief)
    text = f"{joke}\n{url}"
    # Ordering invariant: tweet_poster runs BEFORE any sidecar write.
    # If it raises, propagation leaves the sidecar untouched.
    tweet_id = tweet_poster(text)

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
    return 0


# =============================================================================
# Network helpers + CLI entry point — Phase 3b. NOT exercised by the unit
# suite; Phase 4 derisking validates these end-to-end via workflow_dispatch
# against X_REFRESH_TOKEN_TEST. Heavy imports are lazy so the orchestrator
# above stays import-clean for tests (no tweepy install required).
# =============================================================================

CLAUDE_MODEL = "claude-opus-4-7"
X_DOMAIN = "https://news.oddessentials.ai"
TOKEN_ENDPOINT = "https://api.x.com/2/oauth2/token"

_BUNDLE_RE = re.compile(r'<script[^>]+\bsrc="(/assets/index-[^"]+\.js)"')

_JOKE_PROMPT = """\
Write a single-line, dry, observational joke that captures the spirit \
of the article below. Constraints: at most 250 characters, no emojis, \
no hashtags, no surrounding quotes, no preamble. Output the joke and \
nothing else.

Title: {title}
Subtitle: {dek}\
"""


def _refresh_access_token(
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> dict:
    """Confidential-client refresh grant. Returns parsed JSON response.

    Per X docs (verified 2026-04-28): `Authorization: Basic
    <base64(client_id:client_secret)>` and `client_id` is NOT in the
    body. Caller introspects the response for `refresh_token` to
    decide on rotation writeback (the rotation behavior itself is
    undocumented by X — see locked spec).
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
    """Post via tweepy with OAuth 2.0 user-context. Returns the tweet id.

    `user_auth=False` is REQUIRED — tweepy's create_tweet defaults to
    `user_auth=True` (OAuth 1.0a) which would silently 401 against an
    OAuth 2.0 user-context bearer token.
    """
    import tweepy

    client = tweepy.Client(bearer_token=access_token)
    response = client.create_tweet(text=text, user_auth=False)
    return str(response.data["id"])


def _extract_bundle_path(index_html_path: Path) -> str:
    """Find Vite's `/assets/index-<hash>.js` path in the SPA's index.html."""
    html = index_html_path.read_text()
    m = _BUNDLE_RE.search(html)
    if not m:
        raise RuntimeError(
            f"could not locate Vite asset bundle in {index_html_path}",
        )
    return m.group(1)


def _probe_asset_bundle(
    asset_url: str,
    *,
    max_retries: int = 10,
    backoff_seconds: int = 30,
) -> bool:
    """HEAD-probe the content-hashed bundle URL until 200 or budget exhausted.

    The new bundle filename only resolves on the live site after the
    Pages deploy completes. 5min total budget (10 * 30s) absorbs
    typical Pages propagation latency without unbounded waiting.
    """
    import time
    import urllib.error
    import urllib.request

    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(asset_url, method="HEAD")
            with urllib.request.urlopen(req, timeout=15) as resp:
                if 200 <= resp.status < 300:
                    return True
        except urllib.error.HTTPError as e:
            if e.code != 404:
                # Surfaced server error — abort, don't burn retries.
                return False
        except urllib.error.URLError:
            pass  # transient network — retry
        if attempt < max_retries - 1:
            time.sleep(backoff_seconds)
    return False


def _synthesize_joke(brief: dict, anthropic_api_key: str) -> str:
    """Generate a one-liner via Claude. Hard-clipped to 250 chars."""
    import anthropic

    client = anthropic.Anthropic(api_key=anthropic_api_key)
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": _JOKE_PROMPT.format(
                title=brief.get("title", ""),
                dek=brief.get("dek", ""),
            ),
        }],
    )
    text = response.content[0].text.strip()
    if len(text) > 250:
        text = text[:247] + "..."
    return text


def _read_briefs_at_ref(ref: str, repo_relative_path: str) -> list[dict]:
    """`git show <ref>:<path>` parsed as JSON.

    Returns `[]` ONLY when the ref resolves cleanly but the file
    didn't exist at that revision (legitimate "no parent state" —
    e.g., the brief tracker hadn't been added yet). Raises if:
      - the ref itself doesn't resolve (bad SHA, deleted branch, typo
        in the workflow_dispatch `before_ref` input)
      - the file content at that ref isn't valid JSON

    Failing closed on these is deliberate. A `before_ref` typo that
    silently coerces into `before_briefs=[]` would surface every
    eligible id as new and could publish unintended tweets — see
    locked spec note about not "failing open" on invalid refs.
    """
    import subprocess

    result = subprocess.run(
        ["git", "show", f"{ref}:{repo_relative_path}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        # `git show` distinguishes file-missing-at-ref from ref-invalid
        # via stderr text. The former is a legitimate empty-state; the
        # latter is operator error and must abort.
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
    """`gh secret set X_REFRESH_TOKEN` — requires GH_TOKEN env (PAT).

    Raises if `gh` returns non-zero. Per the locked spec, writeback
    runs BEFORE the tweet so failure aborts the run with no public
    side effect.
    """
    import subprocess

    subprocess.run(
        ["gh", "secret", "set", "X_REFRESH_TOKEN", "--body", new_token],
        check=True,
    )


def _is_zero_sha(sha: str) -> bool:
    """True for git's all-zero null SHA (initial push or workflow_dispatch)."""
    return bool(sha) and set(sha) == {"0"}


def main() -> int:
    """CLI entry point invoked by `.github/workflows/x-post.yml`.

    Reads env (creds, GitHub event SHAs, repo paths), wires the real
    callables into `run_post_x`, returns its exit code.
    """
    import os

    repo_root = Path(os.environ.get("GITHUB_WORKSPACE", "."))
    sidecar_path = repo_root / "data" / "x-posts.jsonl"
    briefs_path = repo_root / "data" / "briefs.json"
    index_html_path = repo_root / "docs" / "index.html"

    after_briefs = json.loads(briefs_path.read_text())

    # Defensive validation against the canonical Pydantic Brief schema —
    # matches src/publish.py:174-188 posture (refuse to proceed on a
    # malformed source-of-truth).
    from src.summarize import Brief
    for entry in after_briefs:
        Brief(**entry)

    before_sha = os.environ.get("GITHUB_EVENT_BEFORE", "")
    if not before_sha or _is_zero_sha(before_sha):
        # Initial push to main, or workflow_dispatch — no parent state.
        # Discovery surfaces every published-daily id; sidecar gate
        # keeps it idempotent.
        before_briefs: list[dict] = []
    else:
        before_briefs = _read_briefs_at_ref(before_sha, "data/briefs.json")

    bundle_path = _extract_bundle_path(index_html_path)
    bundle_url = f"{X_DOMAIN}{bundle_path}"

    def url_prober(_article_url: str) -> bool:
        # Probe the content-hashed bundle, NOT the article deep-link.
        # The SPA's 404.html fallback would 200 on /brief/<id>
        # regardless of whether the new data is deployed — see locked
        # spec for the rationale.
        return _probe_asset_bundle(bundle_url)

    # Lazy refresh: the access token is acquired ONLY when run_post_x
    # actually invokes tweet_poster. If select_post_target returns
    # (None, ...) — sidecar already contains the latest id — this
    # closure is never called, the refresh endpoint is never hit, and
    # the production refresh_token is never consumed. This is the hard
    # invariant: never call X if the sidecar already has the brief id.
    cached_access_token: dict[str, Optional[str]] = {"value": None}

    def tweet_poster(text: str) -> str:
        if cached_access_token["value"] is None:
            current_refresh = os.environ["X_REFRESH_TOKEN"]
            response = _refresh_access_token(
                os.environ["X_CLIENT_ID"],
                os.environ["X_CLIENT_SECRET"],
                current_refresh,
            )
            cached_access_token["value"] = response["access_token"]
            # Writeback BEFORE posting iff X rotated. If writeback
            # raises (PAT issue, rate limit), the tweet never sends —
            # no public side effect, recoverable by fixing the PAT.
            new_refresh = response.get("refresh_token")
            if new_refresh and new_refresh != current_refresh:
                _writeback_refresh_token(new_refresh)
        return _post_tweet(text, cached_access_token["value"])

    anthropic_key = os.environ["ANTHROPIC_API_KEY"]

    def joke_synthesizer(brief: dict) -> str:
        return _synthesize_joke(brief, anthropic_key)

    return run_post_x(
        sidecar_path,
        before_briefs,
        after_briefs,
        joke_synthesizer=joke_synthesizer,
        tweet_poster=tweet_poster,
        url_prober=url_prober,
    )


if __name__ == "__main__":
    import sys
    sys.exit(main())
