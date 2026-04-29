#!/usr/bin/env python3
"""ElevenLabs auth + TTS smoke test.

Verifies the staged ElevenLabs API key works and surveys available voices.
Generates one short TTS line with a default pre-built voice and writes it to
data/_smoke/elevenlabs.mp3 for manual listen-back.

Outputs:
  - Subscription tier + character quota (informs PVC vs pre-built tradeoff)
  - First N pre-built voices the account has access to
  - Any PVC voices the account already owns
  - One generated mp3 at data/_smoke/elevenlabs.mp3
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import urllib.request
import urllib.error

REPO_ROOT = Path(__file__).resolve().parent.parent
KEYS_FILE = REPO_ROOT / ".keys"
OUT_DIR = REPO_ROOT / "data" / "_smoke"
OUT_FILE = OUT_DIR / "elevenlabs.mp3"

API_BASE = "https://api.elevenlabs.io/v1"
DEFAULT_TEXT = (
    "This is Shrimp from Odd Essentials. We're proving the audio path before we "
    "lock in a voice. If you can hear this, we're in business."
)
DEFAULT_MODEL = "eleven_multilingual_v2"


def load_api_key() -> str:
    text = KEYS_FILE.read_text()
    m = re.search(r"^Elevenlabs key:\s*(\S+)", text, flags=re.MULTILINE | re.IGNORECASE)
    if not m:
        raise SystemExit("ElevenLabs key not found in .keys (looking for 'Elevenlabs key:' line).")
    return m.group(1).strip()


def http_get_json(url: str, api_key: str) -> dict:
    req = urllib.request.Request(url, headers={"xi-api-key": api_key, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def http_post(url: str, api_key: str, body: dict, accept: str = "audio/mpeg") -> bytes:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": accept,
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def main() -> int:
    api_key = load_api_key()

    try:
        sub = http_get_json(f"{API_BASE}/user/subscription", api_key)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise SystemExit(f"Auth check failed: HTTP {e.code} {e.reason}\n{body}")
    tier = sub.get("tier")
    chars_used = sub.get("character_count")
    chars_limit = sub.get("character_limit")
    voice_slots_used = sub.get("voice_slots_used")
    voice_limit = sub.get("voice_limit")
    pvc_used = sub.get("professional_voice_slots_used")
    pvc_limit = sub.get("professional_voice_limit")
    print(f"Tier: {tier}")
    print(f"Characters: {chars_used}/{chars_limit} used this period")
    print(f"Voice slots: {voice_slots_used}/{voice_limit} used (custom + cloned)")
    print(f"PVC slots: {pvc_used}/{pvc_limit} used (Professional Voice Clones)")

    voices = http_get_json(f"{API_BASE}/voices", api_key).get("voices", [])
    pvc = [v for v in voices if v.get("category") == "professional"]
    cloned = [v for v in voices if v.get("category") == "cloned"]
    premade = [v for v in voices if v.get("category") == "premade"]
    generated = [v for v in voices if v.get("category") == "generated"]
    print()
    print(f"Voice library: {len(voices)} total — premade={len(premade)} pvc={len(pvc)} "
          f"cloned={len(cloned)} generated={len(generated)}")
    if pvc:
        print("  PVCs already in this account:")
        for v in pvc:
            print(f"    - {v['voice_id']} {v['name']!r} labels={v.get('labels')}")
    if cloned:
        print("  Cloned voices already in this account:")
        for v in cloned[:5]:
            print(f"    - {v['voice_id']} {v['name']!r} labels={v.get('labels')}")
    print("  First 6 premade voices:")
    for v in premade[:6]:
        labels = v.get("labels") or {}
        descriptor = " | ".join(f"{k}={labels[k]}" for k in ("gender", "age", "accent", "use_case") if k in labels)
        print(f"    - {v['voice_id']} {v['name']!r} {descriptor}")

    # Pick the first premade voice deterministically for smoke
    if not premade:
        raise SystemExit("No premade voices available — unexpected.")
    voice_id = premade[0]["voice_id"]
    voice_name = premade[0]["name"]
    print()
    print(f"Generating TTS with premade voice {voice_name!r} ({voice_id})...")
    audio = http_post(
        f"{API_BASE}/text-to-speech/{voice_id}",
        api_key,
        {"text": DEFAULT_TEXT, "model_id": DEFAULT_MODEL},
    )
    if not audio.startswith(b"ID3") and not audio.startswith(b"\xff\xfb") and not audio.startswith(b"\xff\xf3"):
        raise SystemExit(f"Response did not look like MP3 (first bytes: {audio[:8]!r})")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_bytes(audio)
    print(f"Wrote {len(audio)} bytes to {OUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
