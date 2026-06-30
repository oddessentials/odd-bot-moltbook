#!/usr/bin/env bash
# scripts/notify_discord.sh — minimal Discord webhook notifier for the
# automation wrappers. SOURCED (not executed); defines notify_discord().
#
# Reuses the odd-ai-observatory convention: the webhook URL comes from
# $OBSERVATORY_DISCORD_WEBHOOK_URL when set, else from
# ~/.openclaw/keys/observatory-discord-webhook. If neither yields a URL
# the function is a silent no-op — never failing its caller.
#
# Contract (relied on by callers in EXIT traps):
#   - never changes the caller's exit status (always returns 0);
#   - never blocks longer than the curl timeout;
#   - bypasses the mitmproxy (--noproxy '*') so an alert still goes out
#     even when HTTPS_PROXY is exported for Anthropic/Moltbook capture and
#     discord.com is not in NO_PROXY;
#   - posts ONLY the message it is given. Callers MUST NOT pass secrets or
#     raw log tails — the podcast log contains Hedra S3 presigned URLs.
#
# Targets bash 3.2 (macOS /bin/bash, the launchd interpreter).

notify_discord() {
    # $1 = message (plain text; keep it short and secret-free)
    local message="${1:-}"

    local webhook="${OBSERVATORY_DISCORD_WEBHOOK_URL:-}"
    if [ -z "$webhook" ]; then
        # ${HOME:-} not $HOME: under set -u an unset HOME would abort the
        # caller, breaking this function's "always returns 0" contract.
        local webhook_file="${HOME:-}/.openclaw/keys/observatory-discord-webhook"
        if [ -f "$webhook_file" ]; then
            webhook="$(cat "$webhook_file" 2>/dev/null | tr -d '[:space:]' || true)"
        fi
    fi
    if [ -z "$webhook" ]; then
        return 0  # no webhook configured — silent no-op
    fi

    # Build a JSON body by hand: escape backslashes first, then double
    # quotes, then fold control chars (newline/CR/tab) to spaces so the
    # body stays valid JSON. The message is caller-controlled (not
    # untrusted input), but escape anyway for safety.
    local escaped
    escaped="$(printf '%s' "$message" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | tr '\n\r\t' '   ' || true)"

    curl --noproxy '*' --max-time 10 -fsS \
        -H 'Content-Type: application/json' \
        -d "{\"content\": \"${escaped}\"}" \
        "$webhook" >/dev/null 2>&1 || true

    return 0
}
