# PR Radar

Posts your team's **ready-for-review** pull requests into Teams — **one message
per PR**, so each becomes its own thread. Teammates react with 👀 on a PR's
message to claim it, so no two people review the same PR.

It drives a **dedicated headless Chrome** (its own profile; your normal Chrome is
never touched), types each PR into the Teams web app, sends it, and **confirms
each message actually posted** — retrying any that don't land.

## Prerequisites

- `gh` CLI installed and authenticated: `gh auth status` (run `gh auth login` if not).
- Python 3 with [Playwright](https://playwright.dev/python/):
  `pip install playwright` (the browser-automation driver).
- Google Chrome, macOS.
- A filled-in `config.ini` (see [Configure](#configure)).

Not sure what's missing? Run the built-in check — it verifies all of the above at
once and tells you exactly how to fix anything that's absent, without posting:

```bash
python3 pr_radar.py --check
```

Every normal run does this check first too, and stops with the same actionable
report (exit code 2) if a prerequisite is missing — so it never fails halfway
through.

## Configure

All deployment-specific values live in a git-ignored `config.ini` (repo list and
Teams channel URLs) — they're never committed. Copy the template and fill it in:

```bash
cp config.example.ini config.ini
```

Then edit `config.ini`:

```ini
[repos]
list =
    your-org/your-first-repo
    your-org/your-second-repo

[teams]
# Teams → channel → “…” → Get link to channel
channel_url = https://teams.microsoft.com/v2/?tenantId=...#/l/channel/19:...@thread.tacv2/...&launchAgent=join_launcher_web
```

> **Important:** keep the `&launchAgent=join_launcher_web` parameter at the end of
> the channel URL. It forces Teams to open the channel **directly in the browser**;
> without it the web app redirects to the "open in the desktop app / download"
> interstitial, and the headless automation never reaches the channel. "Get link
> to channel" usually includes it — if yours doesn't, append it manually.

`config.example.ini` is fully commented. Run `python3 pr_radar.py --check` after
editing to validate it.

## First-time setup (one sign-in)

The dedicated Chrome keeps its own Teams session in a persistent profile. Sign in
once with a visible window; it's remembered afterwards, so every later run is
headless:

```bash
python3 pr_radar.py --no-headless --test
```

Sign in to Teams in the window that opens. Once you can see the chat, you're set.

## Use

**Default — post to the real channel (headless):**

```bash
python3 pr_radar.py
```

Launches a fresh headless Chrome, opens the Pull Requests channel, posts every
ready PR as its own confirmed message, then closes itself.

**Safe rehearsal into your private "Notes to self" chat** (only you see it):

```bash
python3 pr_radar.py --test
```

**Show the Chrome window** (needed for the first sign-in, handy for debugging):

```bash
python3 pr_radar.py --no-headless
```

**Check prerequisites only** (gh, gh auth, Playwright, Chrome) and exit:

```bash
python3 pr_radar.py --check
```

Run `python3 pr_radar.py --help` for the short guide.

## How it works

- **One dedicated Chrome.** Started with its own `--user-data-dir`, so your normal
  Chrome and its sessions are untouched. Each run starts clean (any previous debug
  Chrome is closed first) so there's exactly one window, one tab.
- **Types, never pastes.** Text is inserted via the Chrome DevTools Protocol
  (works headless, where clipboard paste doesn't). Our messages never contain a
  mention-triggering `@`; for any `@` that shows up inside a PR title, an
  invisible zero-width space plus a real `Space` keypress dismiss Teams' mention
  popup so it can't eat the soft line breaks between a PR's title, meta line, and
  URL.
- **Confirmed sends.** For each message: type → send → confirm the compose box
  cleared (proof it posted) → retry up to 3× → log any it can't confirm. No blind
  fixed delays; progress is gated by real confirmation.
- **Readiness check.** If the compose box never appears, it stops with a clear
  message — telling you whether Teams isn't signed in (run `--no-headless` once)
  or simply didn't load.

## Message format

A **lead summary** posts first (date, total count, and a per-repo tally), then
**one message per PR**, oldest first. Each PR block leads with an age-urgency dot
tuned for a fast-moving team, based on how many **calendar days** ago the PR was
opened (so anything opened yesterday counts as 1 day, even if that was under 24h
ago):

- 🟢 today · 🟡 1–2 days · 🔴 more than 2 days old

When nothing is waiting for review, it posts a single all-clear line instead.

## Author names

Each PR shows the author's real name (e.g. `Jane Doe`) instead of the raw
corporate login. Names come from the GitHub profile's `name` field via
`gh api users/<login>` — using your existing `gh` auth, no Azure or extra
permissions.

- **Bot-authored PRs get the human.** GitHub's Copilot coding agent opens PRs as
  a bot (`app/copilot-swe-agent`) but assigns the requesting human alongside the
  `Copilot` bot; PR Radar credits that human (identified by the `U_` GraphQL node
  id) rather than the bot.
- **Fallback.** If a name can't be resolved (offline, no name set, or a bot with
  no human assignee), it shows the raw GitHub handle — with **no leading `@`**, so
  it never trips Teams' mention autocomplete or accidentally pings anyone. A note
  is logged listing any handles shown this way.

## Scheduling (cron / launchd)

The script is working-directory independent (absolute paths, `$HOME`-based
profile), so it runs from anywhere. Under cron, set `PATH` so `gh` and Chrome
resolve:

```cron
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
0 9 * * 1-5 /path/to/.venv/bin/python /path/to/pr_radar.py >> /tmp/pr_radar.log 2>&1
```

On modern macOS a launchd **LaunchAgent** is more reliable than cron (it runs in
your GUI session with your permissions). Either way, do the one-time
`--no-headless` sign-in first; if the session ever expires, the readiness check
stops with the "not signed in" message instead of failing silently.

## Test

```bash
python3 -m pytest test_pr_radar.py -v
ruff check pr_radar.py test_pr_radar.py
```
