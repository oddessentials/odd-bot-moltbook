"""ElevenLabs character pre-flight guard — refuse a run that can't finish TTS.

Sibling of ``src/podcast_hedra_balance_guard.py`` (same stdout contract and
fail-open posture). The weekly wrapper runs both before any spend. An
ElevenLabs exhaustion (HTTP 402/429) mid-episode wastes the Anthropic
script-gen plus every prior segment's TTS and Hedra render, so refusing when
remaining characters can't cover an episode prevents that cascade.

Deliberately standalone — imports only ``requests`` + stdlib, never the
``src.podcast`` package (whose ``__init__`` eagerly pulls heavy SDKs). The
constants mirror ``src/podcast/config.py`` (ELEVENLABS_API_BASE,
PODCAST_KEYS_FILE) and the loader mirrors ``src/podcast/keys.py``
(load_elevenlabs_key) — keep in sync.

Endpoint (verified live 2026-06-30, HTTP 200): GET /v1/user/subscription,
header ``xi-api-key`` -> {..., "character_count": int, "character_limit":
int, ...}. Remaining characters = character_limit - character_count.

FAIL-OPEN by design. Any error reaching or parsing the endpoint yields
PROCEED — an advisory guard must never block a legitimate run on its own
flakiness. The orchestrator's own error and the wrapper's EXIT-trap alert
remain the backstop. (argparse errors / BaseException exit non-zero with
empty stdout; the wrapper's wildcard ``*)`` case treats that as fail-open
too — so the property holds guard+wrapper, not module-alone.)

Single-line stdout (the wrapper parses these prefixes):

    PROCEED:chars:<remaining>:<min>
    REFUSE:chars:<remaining>:<min>
    PROCEED:error:<ExceptionName>

Entrypoint:

    python -m src.podcast_elevenlabs_balance_guard --min 5000
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import requests

ELEVENLABS_API_BASE = "https://api.elevenlabs.io/v1"
SUBSCRIPTION_ENDPOINT = f"{ELEVENLABS_API_BASE}/user/subscription"
# REPO_ROOT/.keys — src/podcast_elevenlabs_balance_guard.py -> src/ -> root.
KEYS_FILE = Path(__file__).resolve().parent.parent / ".keys"
ELEVENLABS_KEY_RE = re.compile(
    r"^Elevenlabs key:\s*(\S+)", re.MULTILINE | re.IGNORECASE
)
REQUEST_TIMEOUT_SEC = 20


def load_elevenlabs_key() -> str:
    """Read the ElevenLabs key from .keys (mirrors src/podcast/keys.py).

    Raises ValueError (NOT SystemExit, unlike the canonical loader):
    main()'s ``except Exception`` must catch a missing/unreadable key to
    fail open. Do NOT replace this with an import of
    src.podcast.keys.load_elevenlabs_key — its SystemExit is a
    BaseException that bypasses fail-open, and the import drags in the
    heavy src.podcast package this guard deliberately avoids.
    """
    m = ELEVENLABS_KEY_RE.search(KEYS_FILE.read_text())
    if not m:
        raise ValueError("ElevenLabs key not found in .keys")
    return m.group(1).strip()


def evaluate(*, remaining: int, min_chars: int) -> str:
    """Pure decision: compare remaining characters against the floor."""
    if remaining < min_chars:
        return f"REFUSE:chars:{remaining}:{min_chars}"
    return f"PROCEED:chars:{remaining}:{min_chars}"


def fetch_remaining_chars() -> int:
    """GET the subscription and return character_limit - character_count.

    Raises on transport error, non-2xx, or an unexpected payload shape;
    ``main`` converts any raise into a fail-open PROCEED.
    """
    resp = requests.get(
        SUBSCRIPTION_ENDPOINT,
        headers={"xi-api-key": load_elevenlabs_key()},
        timeout=REQUEST_TIMEOUT_SEC,
    )
    resp.raise_for_status()
    data = resp.json()
    count = data.get("character_count")
    limit = data.get("character_limit")
    # JSON booleans are ints in Python; reject them explicitly.
    for v in (count, limit):
        if not isinstance(v, int) or isinstance(v, bool):
            raise ValueError(f"unexpected subscription payload: {data!r}")
    # Assumes a fixed-quota plan (the incident case: exhaustion -> hard
    # 402/429). On usage-based/overage billing, count can exceed limit while
    # TTS still works -> negative remaining -> a spurious REFUSE. That is
    # recoverable (FORCE=1 or a lower ELEVENLABS_MIN_CHARS), and the
    # EXIT-trap alert still fires if a real 402 ever slips through.
    return limit - count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min", dest="min_chars", type=int, required=True)
    args = parser.parse_args(argv)

    try:
        remaining = fetch_remaining_chars()
    except Exception as exc:
        # Advisory guard fails open; ExceptionName carries no ':' so the
        # wrapper's prefix parse stays intact.
        sys.stdout.write(f"PROCEED:error:{type(exc).__name__}\n")
        return 0

    sys.stdout.write(
        evaluate(remaining=remaining, min_chars=args.min_chars) + "\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
