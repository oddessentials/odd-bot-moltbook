# CLAUDE.md

Guidance for Claude Code in this repo. **This is a live, automation-sensitive
production system.** Read this before changing anything.

## What this is

An AI-run newsroom covering [moltbook](https://www.moltbook.com/). Two automated
pipelines run on a Mac mini under launchd and publish to
`agentbrief.net` (GitHub Pages) and `@oddessentials` on X:

- **Daily brief** — `com.oddbot.moltbook.daily` (05:00 ET + RunAtLoad) →
  `scripts/run-daily-publish.sh` → `python -m src.publish daily-publish` →
  fetch moltbook, synthesize (LLM via the local OpenClaw gateway — see
  `src/gateway_llm.py`) → `data/briefs.json`
  → `pnpm --dir agent-brief build` → `docs/` → `git push` → Pages + `x-post.yml`.
- **Podcast** — `com.oddbot.moltbook.podcast.weekly` (Sun 09:00 ET + RunAtLoad) →
  `scripts/run-weekly-podcast.sh` → `src.podcast` (Anthropic Sonnet-tier
  script-gen, ElevenLabs TTS, Hedra video, YouTube) → `data/episodes.json` →
  `docs/podcast/` → `podcast-x-post.yml`. Effectively bi-weekly (cadence guard).

## Prime directive

The runtime is **not containerized** — it's the bare Mac mini. Contain every
change's blast radius. Derisk and test before touching load-bearing/architectural
paths. After a live incident, apply the **minimum** fix (+ logging/docs gaps
only); defer tests, deps, and adjacent refactors to a separate session.

## Load-bearing invariants — do not break

1. **Reconcile before spend.** `src.git_sync.reconcile_with_origin` runs first
   every run. After a non-noop reconcile, reload any tracked data already read.
   The bot-owned predicate is strict (author `odd-bot` AND a `chore(...)` subject).
2. **Editorial time is America/New_York** (`src.editorial_time`). Daily window
   opens 05:00 ET; weekly Sunday 09:00 ET. UTC date selection is unsafe.
3. **moltbook `time=day` = the current UTC calendar day**, not a rolling 24h.
   The live API **cannot backfill** past days.
4. **Recovery > backfill.** After any outage (machine off for hours → weeks) the
   system publishes only *today* and skips the missed days. `max_backlog=3`
   bounds the scan; **at most one live fetch+synth per run** (hard invariant,
   asserted in `src/publish.py`). A single missed day is **accepted loss** — do
   not add catch-up/backfill logic. The launchd `StartCalendarInterval`+`RunAtLoad`
   coalesce missed windows into one catch-up run on wake.
5. **Idempotent + atomic publish.** Candidates exclude already-published ids;
   nothing touches public state until the commit pipeline succeeds (deferred
   writes). The commit stages only `data/briefs.json docs/`. `fcntl` locks
   (`data/.run.lock`, `data/.podcast.run.lock`) prevent overlap. Untracked files
   are intentionally *not* a halt condition for the clean-worktree check.
6. **Podcast cadence guard** (`src.podcast_cadence_guard`, `MIN_DAYS=13`).
   Keyed off the single most-recent episode → one episode per return, never a
   backlog. Refuses interim wakes with zero spend.
7. **"Verified" includes the consumer surface.** Before reporting a publish as
   live, grep the *built* artifact (`docs/assets/index-*.js`,
   `docs/brief/<date>/index.html`). Publish-side checks alone are insufficient.

## Data files (tracked on purpose)

`data/*` is gitignored except these cross-run audit files — each run starts from
a fresh checkout and must see prior state through them:

- `data/briefs.json` — publish dedupe + public source of truth (newest-first).
- `data/x-posts.jsonl` / `data/podcast-x-posts.jsonl` — tweet idempotency sidecars.
- `data/episodes.json` — podcast publish gate.

## X-post workflows (outward-facing — careful)

`.github/workflows/x-post.yml` (paths: `data/briefs.json`) and
`podcast-x-post.yml` (paths: `data/episodes.json`) tweet to `@oddessentials`.
They are **path-filtered**, so doc/code commits do **not** tweet. They share one
concurrency group (`x-post-main`) because both rotate the same `X_REFRESH_TOKEN`
secret (single-writer). A red sidecar-push run means a possible duplicate-tweet
risk — read the workflow's error annotation before re-firing.

## Secrets (pointers — never commit values)

- odd-bot API keys → `~/.openclaw/openclaw.json`
- X creds → GitHub repo secrets (the consumer is GH Actions, not local)
- Local `.keys` is gitignored.

## LLM routing

Local pipelines route LLM calls through the OpenClaw gateway
(`src/gateway_llm.py`) — no provider keys in local code paths. Two sanctioned
direct-Anthropic exceptions, documented in that module's docstring: the
GH-Actions X-posters (no local gateway in CI) and `src/podcast/scripting.py`
(needs forced tool-use, which the gateway's compat endpoint can't honor).

## Conventions

- **Python tooling: `uv`** (not pip+venv). The repo uses `uv.lock`.
- **Do not** run `pnpm build` / regenerate `docs/` unilaterally for SPA tweaks —
  edit source under `agent-brief/`, then ask before rebuilding.
- Substantive features land via **feature branch + PR + CI gate**; direct push
  to `main` is reserved for hotfixes/operational repairs.
- Editorial cleanup ≠ cadence hardening — fix the bug, leave the live artifact;
  don't bundle a republish/refresh into a cadence fix unless asked.

## Useful commands

- Daily dry run (read-only; no lock, no fetch, no writes):
  `python -m src.publish daily-publish --dry-run`
- Manual SPA rebuild without LLM/state/push: see memory `reference_standalone_docs_rebuild`.

## Deeper context

`plans/podcast-pipeline.md`, `plans/incident-2026-04-29-runatload-utc.md`, and the
launchd plists under `launchd/`.
