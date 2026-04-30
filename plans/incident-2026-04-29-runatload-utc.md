# Incident: 2026-04-29 cadence over-fire (RunAtLoad after UTC rollover)

## Summary

A Mac mini reboot at **22:25 EDT on 2026-04-29** (= `02:25 UTC 2026-04-30`)
caused `com.oddbot.moltbook.daily.plist`'s `RunAtLoad=true` to fire the daily
publisher. The orchestrator computed `today = datetime.now(timezone.utc).date()`
and resolved that to **2026-04-30**, so it took the `d == today` live-API path,
synthesized a brief, committed `6d7d08c chore(publish): 2026-04-30`, and pushed.

The April 30 brief went live at <https://news.oddessentials.ai/brief/2026-04-30>
roughly 6.5 hours before the intended 05:00 EDT editorial window.

The premature artifact was deliberately left in place — reverting the public
surface mid-incident was judged a worse failure mode than the editorial drift.
The fix below ensures it cannot recur on either pipeline.

## Timeline

| UTC                       | Local (EDT)        | Event                                                                 |
| ------------------------- | ------------------ | --------------------------------------------------------------------- |
| `2026-04-29T09:00:01Z`    | 2026-04-29 05:00   | Scheduled daily fire. April 29 brief synthesized + committed.         |
| (same run)                | "                  | Working tree dirty (`Home.tsx`, `Podcast.tsx`, `useEpisodes.ts`) → post-commit cleanliness check raised. Push deferred. |
| `2026-04-30T02:25:32Z`    | 2026-04-29 22:25   | **Reboot.** `RunAtLoad=true` fires LaunchAgent.                       |
| (reconcile phase)         | "                  | April 29 reconciled (draft flipped, run record appended).             |
| (pre-flight push)         | "                  | Pushed the deferred April 29 commit. ✓                                |
| (discovery phase)         | "                  | `today = 2026-04-30` (UTC). `discover_work` returned `[2026-04-30]`. Live-API fetch + synth + commit `6d7d08c` + push. |
| `2026-04-30T02:26:37Z`    | 2026-04-29 22:26   | Site shows April 30 brief.                                            |

## Root cause

`StartCalendarInterval` is interpreted in **local time** (`Hour=5, Minute=0`
= 05:00 EDT = 09:00 UTC), so the scheduled cron path never crosses the UTC
date boundary. The orchestrator's `today` was UTC-derived, however, so any
fire that happened to occur after `~20:00 EDT` saw "tomorrow" as the
editorial date. Only `RunAtLoad` (boot/load triggers) can fire in that window.

The pre-existing idempotency check (already-published id set in
`data/briefs.json`) prevents *re-publishing* the same date, but does not
prevent *publishing the next date early* — the next date isn't yet in the
set.

The same structural risk existed in `com.oddbot.moltbook.podcast.weekly.plist`:
`RunAtLoad=true` plus a `MIN_DAYS=6` cadence guard that compared local
*calendar dates* without enforcing time-of-day. A Saturday late-evening
reboot on the calendar week-6 boundary would have fired the podcast
pipeline in the same shape.

## Fix (PR `fix/editorial-time-guard`)

The publish-eligibility contract is now anchored to **`America/New_York`
local time**, not UTC:

- **Daily window opens at 05:00 America/New_York every calendar day.**
  - `src/publish.py` derives `today` from the local date in
    `America/New_York` (via the new `src/editorial_time.py` helper).
  - The per-date loop calls `is_daily_window_open_for(d, _now())` at
    each iteration — i.e., the window check is **re-evaluated at
    decision time**, not snapshotted at `started`. This closes the
    captured-too-early race where a reboot at 04:59:30 EDT followed by
    a 35-second reconciliation/pre-flight push would otherwise see a
    stale `False` and silently skip today's brief, even though the
    window opened mid-run.
  - Reconciliation + pre-flight push run regardless of window state — those
    are operational catch-up paths and are safe.
- **Weekly podcast window opens at 09:00 America/New_York every Sunday.**
  - `scripts/run-weekly-podcast.sh` invokes `src/editorial_time.py` to
    compute the most-recent Sunday-09:00-local boundary and refuses if the
    latest published episode's date is on/after that boundary (slot already
    filled).
  - Layered on top of the existing `MIN_DAYS=6` cadence guard — both
    must pass.

`RunAtLoad=true` is intentionally **kept** in both plists. It was the
trigger here, but it is also the legitimate recovery path for a missed
scheduled fire (machine asleep at 05:00). The editorial-time guard makes
the trigger safe regardless of when it fires.

## Regression coverage

`tests/test_editorial_time.py` and a new `TestEditorialTimeGuard` class in
`tests/test_publish.py` lock down the failure mode:

- `2026-04-29 22:40 EDT` → daily orchestrator does NOT publish 2026-04-30.
- `2026-04-30 04:59 EDT` → daily orchestrator does NOT publish 2026-04-30.
- `2026-04-30 05:00 EDT` → daily orchestrator MAY publish 2026-04-30.
- Normal scheduled fire at `09:00 UTC` (= 05:00 EDT) still works.
- 2026-04-30 already in `briefs.json` → orchestrator exits cleanly at any
  time of day (idempotency preserved).
- Weekly: Saturday 22:25 EDT reboot one week after a Sunday publish →
  podcast wrapper REFUSES.
- Weekly: Sunday 09:00 EDT scheduled fire → wrapper PROCEEDS.
- Weekly: Sunday 10:00 EDT (overslept the cron) → wrapper PROCEEDS
  (catch-up preserved).
- Captured-too-early race (Codex stop-time finding): `started` captured
  at 04:59:30 EDT, decision moment is 05:00:05 EDT — the per-date loop
  must observe the window-open state at decision time and proceed,
  not skip on the stale `started`-time snapshot.

## What was NOT changed (deliberate)

- The April 30 public artifact is live and stays live. No revert,
  no force-push, no re-run.
- `RunAtLoad=true` retained on both plists. The guard, not the trigger,
  is the load-bearing fix.
- No cadence redesign, no scheduler rewrite, no stale-draft hybrid path.
