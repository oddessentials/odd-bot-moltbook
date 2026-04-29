"""ElevenLabs text-to-speech for one segment at a time.

Single function: take text + voice_id + api_key, write MP3 bytes to
out_path. No side effects beyond the file write and the network call.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

from .config import ELEVENLABS_API_BASE, TTS_MODEL


def _tts_request(text: str, voice_id: str, api_key: str) -> bytes:
    body = json.dumps({"text": text, "model_id": TTS_MODEL}).encode()
    req = urllib.request.Request(
        f"{ELEVENLABS_API_BASE}/text-to-speech/{voice_id}",
        data=body,
        method="POST",
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def generate_tts(*, text: str, voice_id: str, out_path: Path, api_key: str) -> Path:
    audio = _tts_request(text, voice_id, api_key)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(audio)
    return out_path
