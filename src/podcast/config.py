"""Paths, model identifiers, and validation gates for the podcast engine.

Single source of truth for everything that other modules tune against. No
behavior; importing this module has no side effects beyond defining
constants.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = REPO_ROOT / "data"
BRIEFS_PATH = DATA_DIR / "briefs.json"
EPISODES_DIR = DATA_DIR / "episodes"
EPISODES_PUBLIC_PATH = DATA_DIR / "episodes.json"
LOCK_PATH = DATA_DIR / ".podcast.run.lock"
CAST_CONFIG_PATH = REPO_ROOT / "config" / "podcast-cast.yaml"

DAILY_ID = re.compile(r"^\d{4}-\d{2}-\d{2}$")

SCRIPT_MODEL = "claude-sonnet-4-6"
HEDRA_MODEL = "hedra_character_3"
HEDRA_MODEL_ID = "d1dd37a3-e39a-4854-a298-6510289f9cf2"
TTS_MODEL = "eleven_multilingual_v2"
RESOLUTION = "720p"
ASPECT_RATIO = "16:9"
DEFAULT_VISIBILITY = "unlisted"

ANTHROPIC_KEY_PATH = Path.home() / ".openclaw" / "keys" / "moltbook-engine-anthropic-api-key"
PODCAST_KEYS_FILE = REPO_ROOT / ".keys"
HEDRA_API_BASE = "https://api.hedra.com/web-app/public"
ELEVENLABS_API_BASE = "https://api.elevenlabs.io/v1"

# Canary gates (objective, applied to every segment).
EXPECTED_VIDEO_HEIGHT = 720           # 720p
EXPECTED_ASPECT_RATIO_TOLERANCE = 0.02  # tolerate ±2% on 16:9 = 1.778
EXPECTED_ASPECT_RATIO = 16 / 9
TTS_MIN_BYTES = 1024
TTS_MIN_DURATION_SEC = 2.0
TTS_MAX_DURATION_SEC = 30.0
TTS_MIN_MEAN_VOLUME_DB = -55.0   # mean_volume above this counts as non-silent
CLIP_AUDIO_DURATION_TOLERANCE_SEC = 1.0

EPISODE_DURATION_MIN_SEC = 60.0
EPISODE_DURATION_MAX_SEC = 360.0
STITCH_DURATION_TOLERANCE_SEC = 2.0

YOUTUBE_CATEGORY_ID = "28"           # Science & Technology
YOUTUBE_DEFAULT_LANGUAGE = "en"
YOUTUBE_DEFAULT_TAGS = ["AI agents", "Moltbook", "Odd Essentials"]
YOUTUBE_DISCLAIMER = (
    "\n\n---\nThis is AI-generated editorial commentary on agent-ecosystem "
    "activity. Hosts are synthetic; voices, animations, and narration are "
    "produced from a structured script. Not a record of human events."
)
YOUTUBE_TOKEN_SECTION_HEADER = "# youtube_tokens"
