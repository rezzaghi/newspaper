# newspaper

Your own news algorithm, with no clickbait, no outrage feed and no vague posting.

You pick the writers, outlets, aggregators and channels you trust. Your AI model reads everything they publish against your own rules, drops the noise, checks the claims, and hands you a clean page each run: what's worth your time, why it was picked for you, and why everything else was dropped.

It runs inside the agent CLI you already use (Claude Code, Codex, OpenCode, Gemini CLI), on the subscription you already pay for. There are no API keys or servers, and no account.

## Start

```
git clone https://github.com/rezzaghi/newspaper
cd newspaper
claude          # or codex, opencode, gemini
```

Then type `/feed` (`$feed` in Codex, or just "feed").

On the first run the agent asks what you want to read about and whether you have feeds to import (an OPML export from Feedly, Inoreader or a podcast app). If you don't, it finds good sources for you. Then it builds your first paper and opens it in your browser.

The only requirement is Python 3. Everything it uses is in the standard library.

**Codex users:** Codex's sandbox blocks network access by default, and newspaper needs it to fetch your feeds. Approve the fetch when Codex asks, or allow network once in `~/.codex/config.toml`:

```toml
[sandbox_workspace_write]
network_access = true
```

## What you get

- **A ranked page** in tabs: News (ranked by how much it could change your life), Articles, Essays, Videos, and *Might surprise you*, a few great things from outside your usual interests.
- **Every pick explained.** You get a clean title, what it says, why it fits you, the main arguments and conclusion, or for news, whether it changes anything.
- **Claims checked.** News and factual pieces are checked against discussion threads and other coverage, and disputed ones are flagged or dropped.
- **Every drop explained.** The bottom of the page lists everything that was filtered out and why (clickbait, promo, paywall, weak argument…).
- **It learns.** 👍/👎 on any item, and the next run updates your profile. Flag items to keep them in Saved.
- **New sources.** Once a day it suggests feeds you might love, checked for quality and posting rate. Add or skip them from the page.

## How it works

```
your feeds ──► fetch ──► triage ──► deep read + fact check ──► editor ──► your page
              (python)  (titles,    (reads each piece,          (ranks,     (localhost,
                         fast)       searches for coverage)      cuts)       votes saved)
```

- `newspaper.py` does the mechanical work: fetching feeds, batching, rendering and the page server.
- Your agent does the judging, following `AGENTS.md` and the prompts in `prompts/`. CLIs with subagents run the batches in parallel; others work through them one by one.
- The page is served at `http://newspaper.localhost:8765/feed` (any `*.localhost` name points to your own computer, no setup needed) by a tiny local server, which stores your votes, saved items and feed choices in `data/state/`. If the server isn't running when you click, the page keeps your clicks and sends them once it's back.

## Your data

Everything that's yours lives in `data/`, which git ignores:

| File | What it is |
|---|---|
| `data/profile.md` | Your rules: what you want, what you reject, the balance you like. Plain text: edit it, or tell the agent "less US politics". |
| `data/feeds.opml` | The sources you follow. |
| `data/state/` | What you've seen, your votes, saved items and feed suggestions. |

`git pull` updates newspaper without touching your data. To back it up or sync it across computers, make `data/` its own private git repo (`cd data && git init`). The run will then commit and push it for you.

## Cost

newspaper runs on your agent's subscription, so a run costs part of your plan's usage limits and nothing extra. A typical run scans 50 to 150 items and reads 10 to 40 of them in depth.

## License

MIT
