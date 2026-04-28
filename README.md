# odd-bot-moltbook

Daily auto-publish engine for [moltbook.com](https://www.moltbook.com/) commentary.

**What it does.** Once per day, fetches the past 24h of top Moltbook posts, scrubs them, synthesizes a Brief via Claude, merges into `data/briefs.json`, builds the agent-brief SPA to `/docs/`, commits, and pushes. GitHub Pages auto-deploys to <https://news.oddessentials.ai>.

**How.** `python -m src.publish daily-publish` (wrapped as `scripts/run-daily-publish.sh` under launchd). The orchestrator's contract — lock, pre-flight push, atomic per-date loop, build, commit, push — is documented in `src/publish.py`.

**Future.** X.com posting will plug in as a downstream consumer of `data/briefs.json` and is intentionally out of scope for this repo.
