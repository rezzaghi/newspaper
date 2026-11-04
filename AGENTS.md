# Newspaper

A personal newspaper: the reader's own feeds, filtered and edited by you against their profile, published as a local web page. `newspaper.py` does the mechanical steps (fetching, batching, rendering, the page server); you do the judging.

When the user types `/feed`, `$feed`, "feed" or asks for their paper, run **Feed run** below. Keep them posted with one short line per stage. Use `python` (or `python3` if that is what exists). If neither exists, tell the user to install Python 3 from python.org and stop; nothing else needs installing.

Everything the reader owns is in `data/` (gitignored): `profile.md` (what they want), `sections.md` (how their page is organized and how strictly each section is checked), `feeds.opml` and `state/`. Never edit the engine files (`newspaper.py`, `template.html`, `prompts/`) during a run.

## First run: no `data/profile.md` yet

1. Welcome the reader in one line and ask, in one message:
   - What do you want to read about, and what do you never want to see? A few lines in their own words is plenty.
   - Do you already follow feeds somewhere (Feedly, Inoreader, a podcast app)? If so, export an OPML file and give its path.
2. `python newspaper.py init`, or `python newspaper.py init --opml <path>` if they gave one.
3. Rewrite `data/profile.md` from their answers. Keep its sections (Wants, Rejects, Balance, Learned from votes): one bold-labelled line per interest saying what good looks like, keep the default Rejects, and add theirs.
4. Ask how their page should be organized:
   > a) Default: News · Articles · Essays · Videos · Might surprise you
   > b) Your own: describe the sections you want in your own words, e.g. "Brazil news, CS deep dives, weekend long reads, nothing else"
   > c) Not sure: use the default. You can change it anytime.

   For a or c, keep `data/sections.md` as `init` wrote it. For b, rewrite it in the same format (see **Sections** below): one `## Title` per section in their order, a one-line description of what belongs in it, and an `order`. "A few surprises" or similar becomes a `surprise: yes` section; if they don't mention surprises, leave it out. Show the result as a short table (title, what belongs, order) and say that items fitting no section are dropped and listed at the bottom.
5. Ask how strict to be:
   > a) Strict (default): fact-check news, judge arguments, drop weak or disputed
   > b) Light: only cut clickbait, promo and paywalls; no fact-checking (faster)
   > c) Off: everything on-topic gets in, just summarized
   > You can also mix them, e.g. "strict for Brazil, light for Tech".

   Set `checks` on each section accordingly. The reader decides; don't argue them into stricter checks.
6. Show the Wants list and the sections in a few lines and ask if anything is off; one round of fixes, then go on.
7. If they follow fewer than 10 feeds, run the scout (see **Subtasks**) with: "Follow the instructions in prompts/scout.md. This is the reader's first run." List the picks in a few lines (name, format, why), then `python newspaper.py suggest data/state/work/scout_out.json --add`.
8. Continue with the Feed run from step 2.

## Feed run

### 1. Learn from the page

- `python newspaper.py votes` prints the 👍/👎 votes not learned from yet: each has `vote` ("up"/"down"), `title`, `source`, `section`. If there are any, update the **Learned from votes** section of `data/profile.md`. Write generalized preferences ("More long essays on the history of science", "Less US election horse-race coverage"), not a list of titles. Merge with what is there; keep the section under ~15 lines; newer votes win contradictions. A like on a `surprise` item is a new interest worth a line; a single dislike there only means that pick missed. Then `python newspaper.py votes --mark`.
- `python newspaper.py apply-decisions` applies the Add/Skip choices from the page's Suggested feeds tab. Do this before fetching, so added feeds are read this run.

### 2. Fetch

`python newspaper.py fetch`. If it reports 0 new items, say so, run `python newspaper.py open` and stop. Mention failed feeds in one line.

If it stops with `NO NETWORK`, your sandbox is blocking network access; the feeds are fine. Rerun the same command with network access (ask the user to approve running it outside the sandbox). If you can't, stop and tell the user how to allow network for your CLI (Codex: `network_access = true` under `[sandbox_workspace_write]` in `~/.codex/config.toml`). Never report a no-network run as "feeds unreachable" or "no new items".

### 3. Triage

`python newspaper.py batches triage` prints one input path per batch. Each batch is a subtask: "Follow the instructions in prompts/triage.md. Input: `<path>`. Output: `<same path with _in_ replaced by _out_>`."

**Feed discovery, once a day.** If `python newspaper.py discover-due` prints "yes", also run the scout: "Follow the instructions in prompts/scout.md." Before rendering, read `data/state/work/scout_out.json`, drop any pick that is weak or overlaps a current feed, then `python newspaper.py suggest data/state/work/scout_out.json`. If the scout failed, skip discovery this run and say so.

### 4. Deep read and credibility check

`python newspaper.py batches read` prints the read batches (about 6 items each). Each is a subtask: "Follow the instructions in prompts/reader.md. Input: `<path>`. Output: `<same path with _in_ replaced by _out_>`." If a batch fails or leaves no output file, retry it once; if it fails again, leave it out and say so.

### 5. Edit (you)

Read every `data/state/work/read_out_*.json`. Write `data/state/digest.json`:

```json
{"intro": "...", "items": [ {item objects copied from read_out where keep is true} ]}
```

- Every item's `section` must be a key from `data/state/work/sections.json`; `render` drops items with any other key and warns.
- Order within each section by its `order` (an `impact` section puts `high` before `medium`). A section's `max` is a hard cap: keep its best items; `render` cuts the rest.
- Merge stories the readers covered twice. Cut anything that, next to the rest, isn't worth the reader's time; that is your call as editor. Cut items are listed as "cut by editor". In a `checks: off` section, cut only duplicates: the reader asked to see everything on-topic.
- Tighten `summary`, `why_you`, `changes` (news) and `arguments`/`conclusion` (articles, essays) where they are vague. Keep the fields the readers wrote.
- **Surprise section** (`surprise: true`): items there are off-profile on purpose. Include every one that is genuinely good, up to its `max`; order them by how likely they are to delight. They don't count toward the profile's Balance rules, but its "Less" lines still apply. Don't move profile-fitting items into it.
- `intro`: two or three plain sentences: what stands out in this batch and what to read first. No hype.

Then `python newspaper.py render`.

### 6. Open the page and finish

1. `python newspaper.py open` starts the page server in the background if it isn't running, opens the browser and prints the URL. The page's votes, saved items and feed choices are stored in `data/state/` through that server. **Always run this command outside any sandbox** (in Codex, request escalated permissions for it): a browser launched from a sandbox can crash, and a server started there can't save the reader's clicks.
2. `python newspaper.py commit` marks this run's items as seen.
3. If `data/` is a git repository with a remote, commit `data/` with the message "Feed run <date>: <kept> kept of <scanned>" and push it.

Finish with the URL and one line: scanned / kept / dropped. Mention feeds added or skipped this run and new suggestions in the Suggested feeds tab.

## Subtasks

Triage batches, read batches and the scout are independent subtasks, each fully described by its prompt file.

- **If your CLI can run subagents in parallel**, run every batch of a stage at once, and the scout alongside triage. Use a fast, cheap model for triage and a strong one for reading, the scout and editing, if your CLI lets you choose.
- **Otherwise**, do them yourself, one batch at a time: read the prompt file, do exactly what it says, write the output file, move on. It is slower but gives the same result.

Subtasks need web fetch (deep read) and web search (credibility check, scout). Without web search, mark credibility `unverified`, skip the scout and say so once.

## Sections

`data/sections.md` is the reader's page layout (the default one in `examples/sections.md` if it's missing). Each section is a `## Title` heading, a one-line description of what belongs in it, and settings as `- key: value` lines:

- `order`: how to rank items, in plain words (`impact`, `importance to me`, `newest first`, `depth`…). `impact` also groups the section into "Could change your life" and "Worth knowing".
- `checks`: `strict`, `light` or `off` (defaults to `strict`).
- `surprise: yes`: off-profile picks chosen for quality. At most one such section.
- `max`: the most items the section shows.

`python newspaper.py sections` prints the parsed layout; run it after every edit to check it parses.

## Changing preferences

When the reader says what they want more or less of ("less US politics", "more long history essays"), edit `data/profile.md`: Wants, Rejects or Balance for lasting rules. To follow or drop a source, edit `data/feeds.opml`; check a new one with `python newspaper.py probe <url>` first and use the `feed_url` it returns.

When they want to change the page ("add a Podcasts section", "merge Essays into Articles", "turn off Might surprise you", "no fact-checking on Videos", "rename Tech to Deep dives and put it first", "go back to the default layout"), edit `data/sections.md` (copy `examples/sections.md` for the default), run `python newspaper.py sections`, and confirm the change in one line. It applies from the next run.
