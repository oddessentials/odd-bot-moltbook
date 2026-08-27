"""LLM access via the local OpenClaw gateway — OpenAI-compatible /v1.

All LOCAL moltbook pipelines (daily brief synthesis; anything running on the
Mac mini under launchd) route LLM traffic through the gateway: model choice,
provider fallback, and observability live in the gateway config, never here.
Requires `gateway.http.endpoints.chatCompletions.enabled=true` in
`~/.openclaw/openclaw.json` (enabled 2026-08-26).

Sanctioned direct-API exceptions — do NOT migrate these to this module:

- `src/post_x.py` and `src/post_podcast_x.py` run in GitHub Actions where no
  local gateway exists; they stay on the ANTHROPIC_API_KEY repo secret.
- `src/podcast/scripting.py` needs a forced tool-use call (`tool_choice`),
  which the gateway's agent-fronted compat endpoint cannot honor (verified
  2026-08-26: forced tool_choice returns 502 "agent did not produce one");
  it stays on the moltbook-engine Anthropic key until openclaw supports
  raw-model tool passthrough.

Overrides: OPENCLAW_GATEWAY_URL, OPENCLAW_GATEWAY_TOKEN, MOLTBOOK_LLM_MODEL
(defaults to the gateway's "openclaw" model alias — the gateway's configured
default agent chain decides the actual provider/model).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import requests

_OPENCLAW_CONFIG = Path.home() / ".openclaw" / "openclaw.json"
DEFAULT_TIMEOUT_SECONDS = 600


def _gateway() -> dict:
    token = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "").strip()
    port = 18789
    if _OPENCLAW_CONFIG.exists():
        try:
            cfg = json.loads(_OPENCLAW_CONFIG.read_text())
            gateway = cfg.get("gateway", {})
            port = gateway.get("port", 18789)
            token = token or (gateway.get("auth") or {}).get("token", "")
        except json.JSONDecodeError:
            pass
    if not token:
        raise RuntimeError(
            "no OpenClaw gateway token (gateway.auth.token in openclaw.json, "
            "or OPENCLAW_GATEWAY_TOKEN)"
        )
    base_url = (
        os.environ.get("OPENCLAW_GATEWAY_URL", "").strip().rstrip("/")
        or f"http://127.0.0.1:{port}"
    )
    model = os.environ.get("MOLTBOOK_LLM_MODEL", "").strip() or "openclaw"
    return {"base_url": base_url + "/v1", "token": token, "model": model}


def chat(
    system: str,
    user: str,
    *,
    max_tokens: int = 4096,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """One system+user chat completion via the gateway; returns message text."""
    gateway = _gateway()
    resp = requests.post(
        f"{gateway['base_url']}/chat/completions",
        headers={
            "Authorization": f"Bearer {gateway['token']}",
            "Content-Type": "application/json",
        },
        json={
            "model": gateway["model"],
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        },
        timeout=timeout,
    )
    if resp.status_code == 404:
        raise RuntimeError(
            "gateway /v1/chat/completions disabled — set "
            "gateway.http.endpoints.chatCompletions.enabled=true and restart the gateway"
        )
    resp.raise_for_status()
    data = resp.json()
    text = data.get("choices", [{}])[0].get("message", {}).get("content")
    if not text:
        finish = data.get("choices", [{}])[0].get("finish_reason")
        raise RuntimeError(f"gateway returned no content (finish_reason={finish!r})")
    return text
