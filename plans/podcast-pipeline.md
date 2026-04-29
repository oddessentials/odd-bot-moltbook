# Podcast pipeline — locked plan

**Status:** Locked 2026-04-28. Canonical source of truth for the weekly AI-news video podcast feature.
**Scope:** Greenlights the build. Phase sequence below is the agreed order.
**Decision boundary:** "Locked decisions" are NOT to be re-litigated in build sessions. "Open questions" ARE load-bearing for Phase 0 and must be resolved during build.

---

## Greenlight + framing

The architecture is greenlit. The first milestone is an **automation-first Episode 1 proof**, not a deferred or partially manual spike.

The first podcast episode is generated from **all existing eligible published daily brief content** in the repo at kickoff time, not blocked on having seven daily briefs. The "seven-day aggregation" rule becomes the steady-state weekly behavior later. Episode 1 uses the current corpus immediately so we validate the full pipeline with real content now.

The pipeline is **100% automated from the start**. No manual upload steps, manual stitching, manual YouTube publishing, manual metadata edits, manual public/private flips, or manual X-posting. Credentials, host identities, voice IDs, image assets, captions, video generation calls, FFmpeg composition, YouTube upload, validation gates, and publish-event writes all flow through the same automated control path the production system uses.

The pre-build validation is not to postpone Episode 1. The goal is to remove unknowns **before integrating into the repo's live automation path**.

---

## Locked decisions

### Episode 1 corpus

- **Source:** all currently eligible published daily briefs at kickoff time. Filter: `status == "published"` AND id matches `^\d{4}-\d{2}-\d{2}$`.
- **At plan-lock (2026-04-28):** the eligible corpus is `2026-04-27` and `2026-04-28` only.
- **Excluded:** the grandfathered weekly artifact `2026-W18` is excluded from Episode 1 and from the steady-state ingest.
- **Editorial thinness is acceptable** — Episode 1's purpose is to prove the full automated production path, not to be the editorial ideal.

### Automation contract

- 100% automated. Zero manual steps anywhere in the chain.
- The same automated control path that produces Episode 1 is the path that runs weekly later. No "Phase 0 happens off-tree" carve-outs.

### Cast assets — establishment contract

- A **separate small PR** establishes the cast before any recurring engine code lands.
- That PR commits a non-secret `config/podcast-cast.yaml` (or equivalent) with:
  - Stable host IDs and display names (anchor "Shrimp" + rotating guest crustacean cast).
  - Stable ElevenLabs voice IDs.
  - Stable Hedra image / asset / character IDs.
- Only stable IDs and public/non-secret identity metadata are stored in the repo. Secrets stay in `~/.openclaw/keys/` (per existing key-storage convention).
- The recurring engine **never** creates, mutates, re-uploads, or re-clones host identities at runtime. Identity-mapping stability is the load-bearing property for week-over-week consistency.

### YouTube destination

- Live `@odd_essentials` channel: <https://www.youtube.com/@odd_essentials>.
- Phase 0 uploads as **unlisted**.
- A video flips public **only through the automated publish-event path**, never via a manual step.

### Phase 0 branch behavior

- Phase 0 runs from a feature branch on the Mac mini using production credentials.
- The branch may push to origin so CI runs on each commit.
- The branch must NOT merge to `main` and must NOT touch the live publish path until the integrated engine and publish contract are ready (Phase 1+).

### Separation of concerns (mandatory)

- Existing daily brief ingestion / finalization remains **untouched**.
- Existing daily publish flow (`scripts/run-daily-publish.sh`, `src/publish.py`, `com.oddbot.moltbook.daily.plist`) remains **untouched**.
- Existing X-post workflow (`x-post.yml`, `src/post_x.py`) remains **untouched**.
- Podcast generation gets its own module, config, work directory, manifest, wrapper, launchd job, and downstream workflow.
- `data/episodes/` is gitignored / internal. Manifests, intermediate audio, intermediate clips, and stitched MP4s never leave the work tree.
- `data/episodes.json` is the **only** public podcast publish event. Written only after successful validation + verified YouTube `videoId`.
- Podcast X-posting uses a separate `.github/workflows/podcast-x-post.yml` and dedicated sidecar; the locked `x-post.yml` for daily briefs stays untouched.

### Pipeline-level invariants

- **Identity-mapping stability:** `voice_id` + `image_id` (or Hedra `character_id`) are stable inputs to the engine, never regenerated at runtime.
- **Segment-level retry, not episode-level:** the unit of retry is a script segment. Episode-level retry is forbidden (cost + idempotency).
- **Validation before publish:** automated gates pass before any public artifact is written or YouTube visibility flips.
- **Publish event contract:** an `Episode` record matching the SPA's TS interface (`agent-brief/client/src/data/content.ts:61-70`) is the single output that downstream consumers (SPA build, X-post workflow) read.

---

## Phase plan

### Phase 0a — Cast-asset PR (small)

**Status:** in progress — credentials staged, asset establishment + refresh token pending.

Pre-req progress (2026-04-28):
- Hedra API key staged in `/.keys` (gitignored).
- ElevenLabs API key staged in `/.keys`.
- Google OAuth Desktop client created in GCP project `oddbot-483603`; consent screen **Published to production** (avoids the 7-day refresh-token expiry that affects Testing-mode apps with restricted scopes).
- YouTube refresh token: **not yet generated** — one-shot consent script handles this next session.

PR scope (unchanged):
- Create or confirm Shrimp + guest-host visual assets.
- Create or confirm stable ElevenLabs voice IDs.
- Create or confirm stable Hedra image / asset / character IDs.
- Commit the non-secret `config/podcast-cast.yaml` cast contract.
- No engine code lands here.

### Phase 0b — Automated Episode 1 proof

**Status:** not started. Blocked on Phase 0a.

The proof exercises the complete automated path on a feature branch using production credentials.

Required behavior:

- Read all currently eligible published daily briefs.
- Generate a structured two-host script using the locked cast.
- Generate TTS per segment using stable voice IDs.
- Generate avatar video clips using stable Hedra image / asset IDs.
- Stitch the final MP4 deterministically with FFmpeg.
- Generate captions.
- Upload the video to the live `@odd_essentials` YouTube channel as **unlisted**.
- Verify the returned YouTube `videoId` via a `videos.list` call.
- Generate Episode metadata matching the SPA `Episode` shape.
- Keep all intermediate artifacts gitignored under `data/episodes/<id>/`.
- Make zero changes to the live daily brief path.

Exit criteria:

- [ ] Episode 1 is generated automatically from current eligible content.
- [ ] Cast identity is stable and contract-driven.
- [ ] No host identity is created or changed by the recurring engine during the run.
- [ ] Final MP4 passes FFprobe validation (duration ∈ bounds, video + audio streams present).
- [ ] YouTube upload returns a `videoId`; `videos.list` confirms it.
- [ ] Episode metadata is valid against the SPA `Episode` shape.
- [ ] No existing live system behavior changes.

### Phase 1 — Recurring engine integration

**Status:** not started. Blocked on Phase 0b.

Land the podcast engine in the repo behind clean separation:

- `src/podcast.py` — orchestrator mirroring the `src/publish.py` shape (lock, idempotency, deferred state writes).
- Podcast config loading.
- Manifest / resume behavior — segment-level state machine in `data/episodes/<id>/manifest.json`.
- Segment-level retry with bounded attempts.
- Validation gates (script word count, segment count, audio/clip duration, FFprobe checks).
- Internal work-directory handling (gitignored).
- CLI entry point for automated generation.

This phase integrates the automation path **without** publishing public podcast artifacts from `main` until Phase 2.

### Phase 2 — Public podcast artifact

**Status:** not started. Blocked on Phase 1.

- `data/episodes.json` write path (engine-owned; only completed and verified episodes; written only after YouTube `videoId` verification).
- Per-episode OG pages: `docs/podcast/<id>/index.html` mirroring the per-brief OG pattern from PR #2 (`_render_per_brief_html` in `src/publish.py`).
- SPA flips its data source from the static `episodes[]` in `agent-brief/client/src/data/content.ts` to engine-owned JSON. Mirrors the same pattern the briefs already use.
- YouTube unlisted → public flip wired to the publish event.

The public artifact contains only completed published episodes. Draft / status / audit state stays internal in the gitignored manifest.

### Phase 3 — Downstream podcast X-post

**Status:** not started. Blocked on Phase 2.

- New workflow `.github/workflows/podcast-x-post.yml` triggered only on `data/episodes.json` push.
- Mirrors the locked x-post pattern: push-range diff, sidecar dedupe at `data/podcast-x-posts.jsonl`, `[skip ci]` commit-back, latest-id-only posting with `skipped_catchup` rows for older eligible ids.
- Tweet shape: `<joke>\n<youtube-url>` (or `news.oddessentials.ai/podcast/<id>`). Joke prompt input = Episode `title + description`.
- The existing daily brief X-post workflow stays untouched.

### Phase 4 — Weekly launchd cadence

**Status:** not started. Blocked on Phase 3.

- `launchd/com.oddbot.moltbook.podcast.weekly.plist` mirroring the daily plist shape.
- `scripts/run-weekly-podcast.sh` wrapper mirroring `run-daily-publish.sh` (mitmproxy env, nvm resolution, log redirection).
- Different fire hour from the daily 05:00 to avoid lock contention.
- Steady-state cadence may use the latest weekly window of published daily briefs. Episode 1 remains based on all currently eligible daily content and is NOT blocked on a seven-brief requirement.
- Lock path distinct from the daily lock at `data/.run.lock` so a daily run and a weekly podcast run can interleave safely.

---

## Open questions (resolve during Phase 0)

These are NOT plan-lock blockers but ARE load-bearing for build. Each must be resolved during Phase 0a/0b.

- **Hedra Creator-tier sizing.** $30/mo ≈ 11 min of 720p; 4 weekly × 3 min = 12 min already over budget before retry slack. Verify against current Hedra pricing during Phase 0; tier-up may be required.
- **Hedra custom-character support at the chosen tier.** Verify during Phase 0a (during cast-asset establishment) before committing the cast IDs.
- **ElevenLabs voice choice.** PVCs (paid, custom) vs pre-built voices (free, less unique). Decide as part of Phase 0a.
- **YouTube OAuth credential storage location.** Likely `~/.openclaw/keys/youtube-*.json` to mirror the existing key-storage pattern. Recurring engine runs on the Mac mini, so local file storage is consistent. Token-rotation handler needed.
- **Captions delivery.** SRT burn-in via FFmpeg (deterministic) vs YouTube auto-CC (free, lower quality) vs API-uploaded caption track. Decide in Phase 0b.
- **Hedra clip-duration cap → segment word budget.** Empirically calibrate during Phase 0b (script segment text → TTS duration → clip duration). Tighten the script-validation gates after one real run.

---

## Existing repo infrastructure to reuse (NOT rebuild)

- **Episode TS interface (locked schema):** `agent-brief/client/src/data/content.ts:61-70`. Engine output must match.
- **/podcast SPA route already wired:** `agent-brief/client/src/App.tsx:33-34`, `agent-brief/client/src/pages/Podcast.tsx`, `agent-brief/client/src/components/EpisodeCard.tsx`. Currently consumes static `episodes[]` from `content.ts`. Phase 2 flips the data source.
- **Source content:** `data/briefs.json` (filter by daily shape + published status).
- **X-post pattern as model:** `src/post_x.py`, `.github/workflows/x-post.yml`, and the architectural decisions captured in the auto-memory `x_post_downstream_plan.md`. Replicate the diff-range / sidecar-dedupe / `[skip ci]` pattern in `podcast-x-post.yml`.
- **Runtime wrapper pattern:** `scripts/run-daily-publish.sh` + `launchd/com.oddbot.moltbook.daily.plist`. Mirror for the weekly podcast.
- **Per-brief OG card pattern:** `_render_per_brief_html` in `src/publish.py` (introduced in PR #2). Mirror for per-episode OG pages in Phase 2.

---

## Operational reminders

- mitmproxy at `127.0.0.1:8080` is load-bearing under launchd's stripped env. The weekly wrapper inherits the same constraint as the daily.
- `NO_PROXY` must include any external API hostnames the engine calls (ElevenLabs, Hedra, YouTube) IF mitmproxy's TLS substitution interferes with their auth. Default policy: keep them in-proxy for observability; verify each works during Phase 0.
- `git push` under launchd uses `osxkeychain` credential helper (proven for daily). Same applies for weekly.
- `~/.openclaw/keys/` is the established secret-storage location.
- Mac mini bare runtime (not containerized) — every decision's blast radius must be contained.
