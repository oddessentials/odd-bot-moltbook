"""Key + credential loaders. All secrets live outside the engine; this
module is the only place that knows how to read them.

- ElevenLabs + Hedra keys come from the gitignored repo-local `.keys`.
- Anthropic key comes from `~/.openclaw/keys/moltbook-engine-anthropic-api-key`
  (existing project convention).
- YouTube OAuth refresh token + client config are persisted under the
  `# youtube_tokens` section of `.keys` by scripts/youtube_consent.py.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache

from .config import (
    ANTHROPIC_KEY_PATH,
    PODCAST_KEYS_FILE,
    YOUTUBE_TOKEN_SECTION_HEADER,
)


@lru_cache(maxsize=1)
def _load_podcast_keys_text() -> str:
    return PODCAST_KEYS_FILE.read_text()


def load_elevenlabs_key() -> str:
    m = re.search(
        r"^Elevenlabs key:\s*(\S+)",
        _load_podcast_keys_text(),
        flags=re.MULTILINE | re.IGNORECASE,
    )
    if not m:
        raise SystemExit("ElevenLabs key not found in .keys.")
    return m.group(1).strip()


def load_hedra_key() -> str:
    m = re.search(
        r"^Hedra Key:\s*(\S+)",
        _load_podcast_keys_text(),
        flags=re.MULTILINE | re.IGNORECASE,
    )
    if not m:
        raise SystemExit("Hedra key not found in .keys.")
    return m.group(1).strip()


def load_anthropic_key() -> str:
    return ANTHROPIC_KEY_PATH.read_text().strip()


def load_youtube_credentials():
    """Build a google.oauth2 Credentials from the refresh token in .keys."""
    from google.oauth2.credentials import Credentials  # local import — heavy SDK
    text = _load_podcast_keys_text()
    if YOUTUBE_TOKEN_SECTION_HEADER not in text:
        raise SystemExit(
            "YouTube tokens not found in .keys. Run scripts/youtube_consent.py first."
        )
    section = text.split(YOUTUBE_TOKEN_SECTION_HEADER, 1)[1].strip()
    payload = json.loads(section)
    return Credentials(
        token=None,
        refresh_token=payload["refresh_token"],
        client_id=payload["client_id"],
        client_secret=payload["client_secret"],
        token_uri=payload["token_uri"],
        scopes=payload.get("scopes"),
    )
