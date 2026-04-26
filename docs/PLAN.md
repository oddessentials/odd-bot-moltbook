# odd-bot-moltbook — Plan

**Status:** locked planning, pre-Phase 0
**Last updated:** 2026-04-26
**Canonical Moltbook API spec:** [moltbook.com/skill.md](https://www.moltbook.com/skill.md)

---

## 1. Mission

Once per day, fetch the previous 24h of top Moltbook content from a curated set of submolts, summarize via the existing `claude-api` skill, persist structured artifacts to disk in a shape that matches the `agent-brief` web app's content contract. That is the entire job.

---

## 2. Explicitly out of scope

The engine does **not**:

- Post to Discord, x.com, or any external channel
- Push content to the agent-brief server (it writes a file; agent-brief reads the file)
- Generate voiceovers, audio clips, broadcast overlays, or any other media
- Provide cross-repo hooks for `voice-play`, `clip-maker`, `odd-dj`, `odd-bot-broadcast`, or any other capability
- Perform real-time alerts or keyword monitoring
- Fetch or store comment trees
- Write to Moltbook (no posts, no comments, no votes, no DMs)

Anything that consumes the artifacts produced here is a separate concern. The agent-brief web app is co-located in this repo at `./agent-brief/` but is a logically distinct project with its own `.git` and lifecycle.

---

## 3. API ground truth (verified April 2026)

All read endpoints require `Authorization: Bearer moltbook_sk_...`. There are no public unauthenticated reads. We register one observer agent and use its key.

### Endpoints we will call

| Endpoint | Purpose | Frequency |
|---|---|---|
| `GET /api/v1/submolts?sort=popular&limit=100` | Live community ranking | weekly + Phase 0 |
| `GET /api/v1/posts?sort=top&time=day&limit=100` | Global top of last 24h | daily |
| `GET /api/v1/posts?submolt={name}&sort=top&time=day&limit=100` | Per-submolt top of last 24h | daily, ×N curated submolts |

### Confirmed parameter values

- `sort` ∈ {`hot`, `new`, `top`, `rising`} → we use `top`.
- `time` ∈ {`hour`, `day`, `week`, `month`, `all`} → we use `day`. Ignored when `sort=new`; works with `sort=top`.
- `limit` default 25, max **100** per request.
- `submolt` query param filters by community name (preferred over the path-based `/submolts/{name}/feed` for symmetry).
- Pagination is cursor-based (`cursor` in, `next_cursor` + `has_more` out). Not used in v1 — single page of 100 covers daily top per scope.

### Post object fields returned in list responses

`id`, `title`, `content` (full text), `upvotes`, `downvotes`, `comment_count`, `created_at`, `submolt`, `author`. No per-post detail call needed; comments not fetched.

### Rate limits (canonical)

- Reads: 60 per 60 seconds
- Headers on every response: `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`, `Retry-After` (on 429)

### Daily call budget

- 1 × `/submolts?sort=popular&limit=20` (refresh popularity ranking)
- 1 × `/posts?sort=top&time=day&limit=100` (global top)
- N × `/posts?submolt={name}&sort=top&time=day&limit=100` (per-submolt top)

`N` is configurable (`top_n` in `config/submolts.yaml`, default 5, ceiling 10). Worst-case daily total at `top_n=10` is **12 reads**, still well under the 60/min ceiling.

---

## 4. Submolt selection (adaptive, not hard-coded)

### What changed

The earlier draft listed `philosophy`, `ai-ethics`, and `creative` as candidate submolts. **Those communities do not exist on Moltbook.** I generated those names from the *kind* of content I expected, not from real data. Corrected here.

### Design — adaptive, not pinned

The list of submolts the engine pulls is **recomputed on every run** from `GET /api/v1/submolts?sort=popular&limit=20`. We do not hard-code names that will rot as Moltbook evolves. Configuration lives in `config/submolts.yaml`:

```yaml
# config/submolts.yaml
top_n: 5                  # how many popular submolts to include each run; ceiling 10
exclude:                  # always-skip list (low-signal communities)
  - introductions
  - shitposts
  - crypto
  - offmychest
mandatory:                # always-include list (unioned with top_n after exclude)
  - general               # ~60% of platform volume; pulse of the platform
```

Per-run algorithm:
1. Fetch `/submolts?sort=popular&limit=20` (oversample to absorb exclusions).
2. Filter out anything in `exclude`.
3. Take the first `top_n` survivors → call this the dynamic set.
4. Union with `mandatory` (deduped) → final daily list.
5. Persist the day's chosen list in the run record (`data/runs.jsonl`) for auditability.

`top_n` is a single-line edit. Going from 5 to 10 is one number change, no code changes. The composition naturally shifts week-to-week as platform popularity shifts.

### Volume reference (Hugging Face dataset, snapshot 2026-01-30) — informational only

| Submolt | Posts | Share |
|---|---:|---:|
| `general` | 3,752 | 61% |
| `introductions` | 715 | 12% |
| `ponderings` | 200 | 3% |
| `showandtell` | 102 | 2% |
| `shitposts` | 93 | 2% |
| `todayilearned` | 80 | 1% |
| `infrastructure` | 73 | 1% |
| `crypto` | 54 | 1% |

Source: `huggingface.co/datasets/ronantakizawa/moltbook` (124 communities, 6,105 posts). Academic analysis: top-10 submolts hold ~85% of posts.

This snapshot is from the platform's first week. Moltbook has grown ~25× since (2.5M+ agents, 17K+ submolts as of April 2026), so the dataset informs `exclude` and `mandatory` defaults but is **not** the source of truth for the daily list — the live API is.

### Initial config defaults (Phase 0 will validate against live `/submolts?sort=popular`)

- `top_n: 5`
- `mandatory: [general]`
- `exclude: [introductions, shitposts, crypto, offmychest]`

Rationale for the exclude list — all are high-volume but low signal-to-noise for an editorial brief:
- `introductions` — agent greetings, not news-worthy
- `shitposts` — comedy, doesn't summarize well
- `crypto` — narrow vertical that would dominate output
- `offmychest` — venting noise

Rationale for `top_n: 5` default — the post-volume distribution is heavy-tailed (top-10 = 85% of platform). Picking 5 captures the bulk of signal while keeping the daily Brief readable; raise to 10 when you want broader coverage at the cost of a longer brief.

---

## 5. Daily flow

1. Acquire identity token (`POST /api/v1/agents/me/identity-token`) — short-lived, reserved for any future verified-action context.
2. Fetch global top + each curated submolt top.
3. Dedupe across results on `id`. Persist raw post records to `posts_raw`.
4. Build a single Claude API call (prompt-cached system prompt) that ingests the deduped post set and emits structured JSON conforming to the agent-brief `Brief` contract (see §7).
5. Persist the structured summary to the `summaries` table and write flat artifacts under `data/digests/YYYY-MM-DD/`.
6. Update the rolling `data/briefs.json` consumed by agent-brief.

That ends the run.

---

## 6. Schedule

Two triggers, idempotent wrapper:

- **Scheduled**: launchd `StartCalendarInterval` at **05:00 local**, every day.
- **Boot/login recovery**: launchd `RunAtLoad=true`, with a **30-minute delay** in the wrapper script before the engine runs. This catches the case where the machine was off at 05:00.
- **Idempotency**: wrapper checks `data/digests/$(date +%Y-%m-%d)/summary.json` at start; if it exists, exits cleanly. Both triggers can safely fire on the same day; only the first does work.

Plist lives at `~/Library/LaunchAgents/com.oddbot.moltbook.daily.plist` (managed via repo `launchd/` directory, symlinked or copied at install).

---

## 7. Output contract — the `agent-brief` integration

The agent-brief web app at `./agent-brief/` is the downstream consumer. It declares its content shape in `agent-brief/client/src/data/content.ts`:

```ts
type BriefTag = "Agents" | "Models" | "Tooling" | "Research" | "Industry" | "Open Source";

interface BriefItem { headline: string; body: string; source?: string; }

interface Brief {
  id: string;            // YYYY-MM-DD slug
  issueNo: number;       // monotonically increasing
  date: string;          // ISO 8601
  title: string;
  dek: string;           // 1–2 sentence summary
  readingMinutes: number;
  tags: BriefTag[];
  items: BriefItem[];    // 3–5 highlights per brief
}
```

The engine emits exactly this shape. Tags are inferred by Claude from the content of the day's posts (constrained to the enum). `source` on each item is a Moltbook permalink or submolt name.

### Files written per run

| Path | Purpose |
|---|---|
| `data/moltbook.duckdb:posts_raw` | One row per (run_id, post_id) with full JSON |
| `data/moltbook.duckdb:summaries` | One row per daily run, structured columns |
| `data/digests/YYYY-MM-DD/summary.json` | The single Brief object for the day |
| `data/digests/YYYY-MM-DD/summary.md` | Human-readable prose form |
| `data/digests/YYYY-MM-DD/raw-posts.jsonl` | Input snapshot, replayable |
| `data/briefs.json` | Rolling array of all `Brief` objects, newest first; consumed by agent-brief |

### How agent-brief consumes it

The comment in `content.ts` reads: *"The summarization/extraction pipeline can replace the arrays below (or swap this module for a fetch from a JSON endpoint) without touching any UI code."*

V1 approach: agent-brief's server (Express) is extended with one route, `GET /api/briefs`, that reads `../data/briefs.json` from disk and returns it. The client fetches that route at runtime. No build-time coupling, no engine push.

The agent-brief `Episode` type (podcast) is **out of scope** for this engine — that pipeline does not exist yet.

---

## 8. Observatory integration

The odd-ai-observatory repo monitors all OpenClaw API traffic via a mitmproxy on `127.0.0.1:8080`. Our daily run must show up in that observability layer.

### Required hooks

1. **HTTPS_PROXY in our launchd plist env**: `HTTPS_PROXY=http://127.0.0.1:8080` and `HTTP_PROXY=http://127.0.0.1:8080`. Python `requests`/`httpx` honor these automatically. Adds zero code in our engine.

2. **Register Moltbook in `~/repos/odd-ai-observatory/config/services.yaml`** — one new entry, mirroring the `news_providers` pattern:

   ```yaml
   social_providers:
     moltbook:
       name: "Moltbook"
       url_patterns:
         - "www.moltbook.com"
         - "moltbook.com"
       key_file: "moltbook.key"
       category: "social"
   ```

   This causes mitmproxy to classify our calls, expose them in the Observatory API at `:9292`, and surface them in Grafana. Confirm the addon picks up the new category (it may need an addon code update if `social` is unknown — verify in Phase 0).

3. **Uptime Kuma monitor** for `https://www.moltbook.com/api/v1/agents/me` (HEAD or auth-checked GET) at 300s intervals. Joins the existing 56-monitor fleet.

4. **Sentry init** in `src/poll.py` — match the existing pattern from `~/repos/odd-ai-observatory/collector/sentry_init.py` so any crash in the daily run reports to the same Sentry project.

5. **Run record**: append `{ run_id, started_at, finished_at, posts_in, summary_id, errors }` to `data/runs.jsonl`. The Observatory's Flask bridge (`:9292`) can be extended later with one endpoint to expose this if a daily-job dashboard is wanted; out of scope for v1.

### Do NOT add

- A new launchd supervisor — the existing `ai.openclaw.observatory.plist` supervises observability infra, not jobs. Our daily run is its own LaunchAgent, separate.
- Prometheus exporters — overkill for a once-daily job.

---

## 9. Phase 0 — prerequisites before code

1. ~~Read `moltbook.com/terms`~~ ✅ **Done 2026-04-26.** ToS prohibits automated reading; risk accepted — see §12.
2. **Register one observer agent** at moltbook.com (user-blocked — interactive registration). Receive `moltbook_sk_...` key. Store at `~/.openclaw/keys/moltbook-api-key`, chmod 600.
3. **Live-fire validation**: one call to `GET /api/v1/posts?limit=1` to confirm key works and observe `X-RateLimit-*` headers.
4. **Live submolt ranking sanity check**: call `GET /api/v1/submolts?sort=popular&limit=20` once; verify the response shape, confirm `general` is still #1, confirm the four `exclude` defaults still reflect low-signal communities. Update defaults in `config/submolts.yaml` if reality has shifted. The list is *recomputed every run* — Phase 0 only validates the algorithm's inputs, not a frozen list.
5. ~~Observatory registration: add the `social_providers.moltbook` entry to services.yaml~~ ✅ **Done 2026-04-26**, observatory not yet reloaded — picks up on next supervisor cycle. Smoke test (Step 5b) waits for key.
   5b. **Smoke test through observatory proxy** (after Step 2-4 unblock): `curl --proxy http://127.0.0.1:8080 -H "Authorization: Bearer <key>" https://www.moltbook.com/api/v1/posts?limit=1` — confirm response and that the call appears classified as `social/moltbook` in the Observatory API at `:9292`.

---

## 10. Repo layout (planned)

```
~/repos/odd-bot-moltbook/
├── agent-brief/             # downstream web app POC (own .git, own lifecycle)
│   ├── client/
│   ├── server/
│   └── shared/
├── src/
│   ├── moltbook_client.py   # auth + endpoint wrappers
│   ├── poll.py              # daily fetch + dedupe + persist
│   └── summarize.py         # Claude API call → Brief JSON + flat artifacts
├── data/
│   ├── moltbook.duckdb
│   ├── briefs.json          # rolling array consumed by agent-brief
│   ├── runs.jsonl           # one line per daily run for ops visibility
│   └── digests/YYYY-MM-DD/
│       ├── summary.json     # single Brief object
│       ├── summary.md
│       └── raw-posts.jsonl
├── config/
│   └── submolts.yaml        # locked curated list (Phase 0 output)
├── scripts/
│   └── run-daily.sh         # idempotent wrapper, 30-min defer on RunAtLoad
├── launchd/
│   └── com.oddbot.moltbook.daily.plist
├── docs/
│   ├── PLAN.md              # this file
│   └── IDEAS.md             # explicit deferrals
├── SKILL.md
├── TOOLS.md
└── README.md
```

---

## 11. Hardening (three passes, after first working end-to-end run)

1. **Functional** — golden-path live test, edge cases (empty submolt, 429, expired token, malformed JSON, 0 posts in window, agent-brief fetch with no `briefs.json` yet).
2. **Security** — API key never logged, never echoed; HTTP client host-pinned to `www.moltbook.com`; no path traversal in any file IO; output JSON validated against the `Brief` schema.
3. **Reliability** — idempotent re-runs (same date does not duplicate rows or `Brief` entries); partial-failure tolerance (one submolt erroring does not fail the whole digest); launchd RunAtLoad behavior post-reboot; 30-min defer survives quick crash-and-relaunch.

---

## 12. Risk and security notes

### ToS risk acceptance (2026-04-26)

The Moltbook ToS at `moltbook.com/terms` (last updated 2026-03-15), section "Limitations of Use", verbatim prohibits:

> "use any robot, spider, site search/retrieval application or other automated device, process or means to access, retrieve, scrape or index any portion of our Services or any Content"

> "scrape or otherwise collect any data or other content available on this website"

There is no `/developer-terms` or `/api-terms` page (both 404), and the consumer ToS contains no API carve-out. The platform nevertheless publishes a documented authenticated read API (skill.md) with rate limits and `X-RateLimit-*` headers, indicating the intent that registered agents can consume content programmatically.

**Decision:** proceed as a calculated risk. Accepted by user 2026-04-26.

**Mitigations the engine commits to:**
- **Volume**: ≤12 authenticated reads/day, far below any plausible scraping threshold.
- **Authenticated**: a registered observer agent's API key, not anonymous harvesting.
- **Attributed**: every `Brief.items[].source` field links back to the Moltbook permalink or names the submolt, so derivative use is traceable to source.
- **Read-only**: no POST/PUT/DELETE endpoints used; no votes, comments, or messages.
- **No reproduction at scale**: only a daily summary with bounded item count is published downstream; full post bodies are not republished.
- **Reversible**: if Moltbook or Meta requests we stop, the engine can be disabled in one launchctl unload.

**Accepted exposures:**
- Platform-side termination (key revoked, agent banned).
- Need to migrate to Apify scraper or similar if API access is withdrawn.

### Platform-side security

- Wiz reported a 2026 Moltbook breach exposing ~1.5M API tokens. Mitigations:
  - Single-purpose observer key, not reused for any other capability.
  - Read-only behavior in code; no write paths exist.
  - Rotate the key quarterly.
  - Key file `chmod 600`, owned by macmini1, never committed.
- skill.md warns: *"NEVER send your API key to any domain other than moltbook.com."* HTTP client must hard-pin the host.

---

## 13. Open items remaining

None for the user — all four items from the 2026-04-26 review are resolved:

- ✅ Run schedule: 05:00 + 30-min-deferred boot recovery
- ✅ Observatory monitoring: services.yaml + Uptime Kuma + Sentry hooks documented
- ✅ Agent-brief integration: contract pinned to existing `Brief` interface, file-on-disk consumption pattern
- ✅ Submolt list: adaptive — recomputed every run from live `/submolts?sort=popular`, filtered by `config/submolts.yaml` exclude/mandatory; `top_n` configurable up to 10

Ready to execute Phase 0 when you say go.

---

## 14. Sources

- [moltbook.com/skill.md](https://www.moltbook.com/skill.md) — canonical machine-readable spec
- [moltbook.com/developers](https://www.moltbook.com/developers) — auth flow
- [huggingface.co/datasets/ronantakizawa/moltbook](https://huggingface.co/datasets/ronantakizawa/moltbook) — submolt volume distribution snapshot (2026-01-30)
- [github.com/giordano-demarzo/moltbook-api-crawler](https://github.com/giordano-demarzo/moltbook-api-crawler) — working crawler, MIT, Python; confirms `time` parameter and undocumented `comments` sort
- [github.com/kelkalot/moltbook-observatory](https://github.com/kelkalot/moltbook-observatory) — continuous collector, MIT, Python; proves polling tolerance
- [arxiv.org/html/2602.09270](https://arxiv.org/html/2602.09270) — confirms top-10 submolts hold ~85% of posts
- [moltbook-ai.com — popular submolts and content](https://moltbook-ai.com/posts/moltbook-popular-submolts-content) — qualitative submolt characterization
- [wiz.io — exposed Moltbook database](https://www.wiz.io/blog/exposed-moltbook-database-reveals-millions-of-api-keys) — security caveat source
