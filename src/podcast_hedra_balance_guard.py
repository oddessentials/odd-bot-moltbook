"""Hedra credit pre-flight guard — refuse a podcast run that can't finish.

Mirror of ``src/podcast_cadence_guard.py``: a thin, cheap pre-flight the
weekly wrapper (``scripts/run-weekly-podcast.sh``) runs AFTER the cadence
guard PROCEEDs and BEFORE any Anthropic / ElevenLabs / Hedra spend.

The 2026-06-28 incident this prevents: Hedra held partial credit, so the
run generated the full script, all TTS, and 13 of 16 video clips before a
mid-render ``402 Payment Required`` aborted it — no episode, real spend,
and no signal. This guard queries Hedra's public billing endpoint and
refuses when remaining credits fall below a floor large enough for an
episode.

Deliberately standalone — imports only ``requests`` + stdlib, never the
``src.podcast`` package (whose ``__init__`` eagerly pulls the Anthropic /
Google SDKs). Like the cadence guard, the pre-flight stays cheap and
independent of pipeline import health. The constants below mirror
``src/podcast/config.py`` (HEDRA_API_BASE, PODCAST_KEYS_FILE) and the
loader mirrors ``src/podcast/keys.py`` (load_hedra_key) — keep in sync.

FAIL-OPEN by design. Any error reaching or parsing the billing endpoint
yields PROCEED — an advisory guard must never block a legitimate run on
its own flakiness. The orchestrator's own 402 and the wrapper's EXIT-trap
alert remain the backstop. (argparse errors / BaseException exit non-zero
with empty stdout; the wrapper's wildcard ``*)`` case treats that as
fail-open too — so the property holds guard+wrapper, not module-alone.)

Single-line stdout (the wrapper parses these prefixes):

    PROCEED:credits:<remaining>:<min>
    REFUSE:credits:<remaining>:<min>
    PROCEED:error:<ExceptionName>

Entrypoint:

    python -m src.podcast_hedra_balance_guard --min 1800
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import requests

# Documented at https://www.hedra.com/docs/api-reference/public/get-credits:
# GET, header X-API-Key, 200 -> {"remaining": int, "used": int,
# "expiring": int, "workspace_credits": {...}|null}.
HEDRA_API_BASE = "https://api.hedra.com/web-app/public"
CREDITS_ENDPOINT = f"{HEDRA_API_BASE}/billing/credits"
# REPO_ROOT/.keys — src/podcast_hedra_balance_guard.py -> src/ -> repo root.
KEYS_FILE = Path(__file__).resolve().parent.parent / ".keys"
HEDRA_KEY_RE = re.compile(r"^Hedra Key:\s*(\S+)", re.MULTILINE | re.IGNORECASE)
REQUEST_TIMEOUT_SEC = 20


def load_hedra_key() -> str:
    """Read the Hedra key from .keys (mirrors src/podcast/keys.py).

    Raises ValueError (NOT SystemExit, unlike the canonical loader in
    src/podcast/keys.py): main()'s ``except Exception`` must catch a
    missing/unreadable key to fail open. Do NOT replace this with an
    import of src.podcast.keys.load_hedra_key — its SystemExit is a
    BaseException that bypasses fail-open, and the import drags in the
    heavy src.podcast package this guard deliberately avoids.
    """
    m = HEDRA_KEY_RE.search(KEYS_FILE.read_text())
    if not m:
        raise ValueError("Hedra key not found in .keys")
    return m.group(1).strip()


def evaluate(*, remaining: int, min_credits: int) -> str:
    """Pure decision: compare remaining credits against the floor."""
    if remaining < min_credits:
        return f"REFUSE:credits:{remaining}:{min_credits}"
    return f"PROCEED:credits:{remaining}:{min_credits}"


def fetch_remaining_credits() -> int:
    """GET Hedra's billing/credits and return the ``remaining`` integer.

    Raises on transport error, non-2xx, or an unexpected payload shape;
    ``main`` converts any raise into a fail-open PROCEED.
    """
    resp = requests.get(
        CREDITS_ENDPOINT,
        headers={"x-api-key": load_hedra_key()},
        timeout=REQUEST_TIMEOUT_SEC,
    )
    resp.raise_for_status()
    data = resp.json()
    remaining = data.get("remaining")
    # JSON booleans are ints in Python; reject them so a stray `true`
    # can't read as 1 credit.
    if not isinstance(remaining, int) or isinstance(remaining, bool):
        raise ValueError(f"unexpected billing payload: {data!r}")
    return remaining


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min", dest="min_credits", type=int, required=True)
    args = parser.parse_args(argv)

    try:
        remaining = fetch_remaining_credits()
    except Exception as exc:
        # Advisory guard fails open: a billing-endpoint hiccup must never
        # block a legitimate run. ExceptionName carries no ':' so the
        # wrapper's prefix parse stays intact.
        sys.stdout.write(f"PROCEED:error:{type(exc).__name__}\n")
        return 0

    sys.stdout.write(
        evaluate(remaining=remaining, min_credits=args.min_credits) + "\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
