# IDEAS — explicitly deferred

Items considered during planning and explicitly *not* in this repo. Parked here so they don't leak back into the engine.

## Distribution channels

The engine writes `data/briefs.json` (and the per-day `summary.json`) to disk. Anything that distributes that content is downstream and out of scope here.

- **Discord delivery** — removed from spec 2026-04-26.
- **agent-brief web app** — co-located in `./agent-brief/` but logically separate (own `.git`, own lifecycle). Its server reads `../data/briefs.json`. Engine does not push to it.
- **x.com posting** — future separate repo or skill; will consume the same `briefs.json`.
- **Podcast (`Episode` type in agent-brief)** — out of scope; no engine for it exists yet.

## Cross-repo hooks (not in scope)

- `voice-play` reading `voiceover.txt` for an audio bulletin
- `clip-maker` taking quotes for soundboard cuts
- `odd-dj` mood seeds from theme tags
- `odd-bot-broadcast` ticker overlay text

## Engine extensions for later versions

- Comment-tree fetching and summarization (high volume, low signal-to-noise)
- Real-time keyword/agent alerts
- Per-user customization (current scope is one global digest)
- Trend deltas requiring multi-week history (degrades gracefully in v1; meaningful after ~7d of operation)
- Backfill mode for historical date ranges
- Search-driven digests (`/api/v1/search`) for topic-specific monitoring

## Operational extensions for later versions

- Multiple curated submolt sets (e.g., one config per future consumer)
- Dual-write to Postgres for shared analytics
- Webhook out on summary completion (still output-only, not delivery)
