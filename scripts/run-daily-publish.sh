#!/usr/bin/env bash
# scripts/run-daily-publish.sh — single entry point for daily auto-publish.
#
# Invoked by launchd via com.oddbot.moltbook.daily.plist at 05:00 local
# (+ RunAtLoad for boot recovery). Local + automation parity — the
# operator's manual command is exactly this script.
#
# All behavior — locking, pre-flight push, catch-up scan, atomic per-date
# publish, build, commit, push, run-state telemetry — lives in
# `src.publish.run_daily_publish`. This wrapper is intentionally dumb:
# it only sets cwd, the observatory proxy, and log redirection.
#
# Auth/keys: never set in env. Python loads keys lazily from
# ~/.openclaw/keys/ inside the synthesis path. The lock at
# data/.run.lock and run-state at data/.last-run-state.json are managed
# by Python; this wrapper does not touch them.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Route Anthropic + Moltbook traffic through the odd-ai-observatory
# mitmproxy so daily synthesis is visible in observability. NO_PROXY
# excludes github.com so `git push` isn't subject to mitmproxy's TLS
# substitution (which would break cert verification).
export HTTPS_PROXY="http://127.0.0.1:8080"
export HTTP_PROXY="http://127.0.0.1:8080"
export NO_PROXY="github.com,api.github.com,*.github.com"

LOG_DIR="$REPO_ROOT/logs"
mkdir -p "$LOG_DIR"
exec >>"$LOG_DIR/moltbook-daily.log" 2>&1

TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "----"
echo "$TS run-daily-publish.sh start"

"$REPO_ROOT/.venv/bin/python" -m src.publish daily-publish

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) run-daily-publish.sh finished"
