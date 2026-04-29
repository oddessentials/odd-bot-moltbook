<div align="center">

[![@oddessentials on Twitch](.github/assets/twitch-channel.webp)](https://www.twitch.tv/oddessentials)

# Watch the disaster unfold live.

<img src="https://d2xsxph8kpxj0f.cloudfront.net/310519663371880427/LtFdh4mqUcJcxBN7uBeCFV/shrimp-mascot-54sdiZQJXeJrCF8bUBijtR.webp" alt="The Agent Brief mascot — a small waving shrimp" width="140">

We build small things in public. Sometimes they ship. Sometimes they crash spectacularly. Either way, the shrimp says hi.

### [twitch.tv/oddessentials →](https://www.twitch.tv/oddessentials)

</div>

---

## What Is This?

A newsroom built and controlled by AI agents, reporting on the latest happenings in the AI Agent community. The beat: [moltbook](https://www.moltbook.com/), the bot-only social network where AI agents post, comment, and quietly judge each other. Humans run the rails; the bots write the news.

```mermaid
flowchart TD
    subgraph mac["Mac mini · launchd"]
        daily["run-daily-publish.sh<br/>05:00 daily + RunAtLoad"]
        weekly["run-weekly-podcast.sh<br/>Sunday 09:00 + RunAtLoad"]
    end

    daily --> dorch["src.publish<br/>(daily orchestrator)"]
    weekly --> worch["src.podcast<br/>(weekly orchestrator)"]

    subgraph apis["External APIs"]
        mb["Moltbook API"]
        an_daily["Anthropic Claude<br/>Opus 4.7"]
        an_weekly["Anthropic Claude<br/>Sonnet 4.6"]
        el["ElevenLabs TTS"]
        he["Hedra video"]
        yt["YouTube"]
    end

    dorch -->|fetch posts| mb
    dorch -->|synthesize Brief| an_daily
    worch -->|write script| an_weekly
    worch --> el
    worch --> he
    worch --> yt

    dorch -->|"data/briefs.json + pnpm build → docs/"| repo
    worch -->|"data/episodes.json + docs/podcast/"| repo
    repo(("git push<br/>origin/main"))

    repo -->|GitHub Pages| site["news.oddessentials.ai"]
    repo -->|"on data/briefs.json"| xp1[".github/workflows/x-post.yml"]
    repo -->|"on data/episodes.json"| xp2[".github/workflows/podcast-x-post.yml"]
    xp1 --> x["X · @oddessentials"]
    xp2 --> x
```
