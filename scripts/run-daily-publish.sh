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

# Branch guard. The orchestrator commits + pushes on whatever branch is
# checked out. If the operator has a feature branch checked out at
# 05:00, the daily publish lands on that branch — main stays stale,
# GitHub Pages doesn't deploy, the X-post workflow doesn't trigger
# (it watches data/briefs.json on main). Refuse cleanly with a loud
# log line; the operator notices a missed post and finds the cause
# in moltbook-daily.log without scattering chore(publish) commits
# across feature branches.
CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "DETACHED")"
if [ "$CURRENT_BRANCH" != "main" ]; then
    LOG_DIR="$REPO_ROOT/logs"
    mkdir -p "$LOG_DIR"
    {
        echo "----"
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) run-daily-publish.sh refused"
        echo "  current branch: $CURRENT_BRANCH"
        echo "  required branch: main"
        echo "  no publish, no commit, no push. Operator: switch to main and re-run, or wait for tomorrow's window."
    } >>"$LOG_DIR/moltbook-daily.log"
    exit 0
fi

# launchd's default PATH excludes nvm-managed binaries. The orchestrator's
# `pnpm --dir agent-brief build` subprocess inherits this script's PATH,
# so resolve pnpm/node here before invoking Python. Source nvm.sh if
# present; fall back to the highest installed nvm node version.
export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
if [ -s "$NVM_DIR/nvm.sh" ]; then
    # shellcheck disable=SC1091
    . "$NVM_DIR/nvm.sh" >/dev/null 2>&1
fi
if ! command -v pnpm >/dev/null 2>&1; then
    NODE_BIN="$(ls -d "$NVM_DIR/versions/node"/*/bin 2>/dev/null | sort -V | tail -1)"
    [ -n "$NODE_BIN" ] && export PATH="$NODE_BIN:$PATH"
fi
if ! command -v pnpm >/dev/null 2>&1; then
    echo "FATAL: pnpm not on PATH (checked nvm default + latest install)" >&2
    exit 1
fi

# Route Anthropic + Moltbook traffic through the odd-ai-observatory
# mitmproxy so daily synthesis is visible in observability. NO_PROXY
# excludes github.com so `git push` isn't subject to mitmproxy's TLS
# substitution (which would break cert verification).
export HTTPS_PROXY="http://127.0.0.1:8080"
export HTTP_PROXY="http://127.0.0.1:8080"
export NO_PROXY="github.com,api.github.com,*.github.com"

# mitmproxy substitutes the upstream cert with one signed by its CA;
# Python's urllib must verify against a trust store that includes that
# CA. launchd's stripped env has no SSL_CERT_FILE set, so Python falls
# back to a default that doesn't include mitmproxy's CA — point it at
# the bundle directly. (REQUESTS_CA_BUNDLE for any lib that uses requests.)
# All of this script's HTTPS egress is through the proxy except git, and
# git's cert config is independent of these env vars.
MITM_CA="$HOME/.mitmproxy/mitmproxy-ca-cert.pem"
if [ -f "$MITM_CA" ]; then
    export SSL_CERT_FILE="$MITM_CA"
    export REQUESTS_CA_BUNDLE="$MITM_CA"
fi

LOG_DIR="$REPO_ROOT/logs"
mkdir -p "$LOG_DIR"
exec >>"$LOG_DIR/moltbook-daily.log" 2>&1

TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "----"
echo "$TS run-daily-publish.sh start"

"$REPO_ROOT/.venv/bin/python" -m src.publish daily-publish

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) run-daily-publish.sh finished"
