# PR Radar — Design & Architecture

> This started as a task-by-task TDD build plan for a clipboard "paste-assist"
> tool. It has since evolved into a fully automated Teams poster. This document
> reflects the tool **as it is now**; the original clipboard/voice workflow has
> been removed.

**Goal:** Post the team's ready-for-review pull requests into a Teams channel,
one message per PR so each becomes its own thread that a reviewer claims with 👀.

**Architecture:** A single-file Python script (`pr_radar.py`). Pure functions
(fetch → filter → sort → format) are unit-tested with injected fakes; all I/O is
dependency-injected so it can be tested without network, clipboard, or a browser.
Delivery is done by driving a **dedicated headless Chrome over the Chrome
DevTools Protocol (CDP)** using Playwright.

**Tech stack:** Python 3, the authenticated `gh` CLI (PR data + author names),
Playwright (`connect_over_cdp`) driving a separate Google Chrome instance,
`pytest` for tests, `ruff` for lint.

---

## Context & Constraints

- **Do NOT create any files in the target GitHub repos.** All work lives in
  `python-playground/useful/pr_radar/`.
- `gh` is authenticated already; no auth work needed.
- **Never use `gh search prs` / `gh search repos`** — GitHub's search index lags
  and keyword matching is fuzzy. Always enumerate PRs per-repo with
  `gh pr list --repo <owner>/<repo>`.
- "Ready for review" = an open PR whose `isDraft` is `false`. That is the only
  filter.
- Repo scope and the Teams channel URLs are **not** in the source. They live in a
  git-ignored `config.ini` (template: `config.example.ini`) so company repo names
  and channel/tenant IDs are never committed. `load_config` reads it
  (`configparser`, `interpolation=None` so the `%`-escaped URLs parse).
- **Never run a bare real-channel send while developing** — it broadcasts to the
  team. Verify with `--test` (posts to your own "Notes to self" chat).

## Files

- `pr_radar.py` — the entire tool (config, fetch, format, browser driver, CLI).
- `test_pr_radar.py` — pytest unit tests for the pure functions and injected-I/O.
- `config.example.ini` — commented template; copy to `config.ini` (git-ignored).
- `README.md` — usage.

```bash
cd ~/Desktop/DevData/gitclones/python-playground/useful/pr_radar
../../.venv/bin/python -m pytest test_pr_radar.py -q   # shared venv has pytest + playwright
../../.venv/bin/python -m ruff check pr_radar.py test_pr_radar.py
```

---

## Pipeline

1. **Fetch** — `fetch_ready_prs(repo)` runs `gh pr list --repo … --json …` and
   drops drafts. `collect(REPOS, now)` aggregates across repos and sorts oldest
   first. Age is whole **calendar days** since `createdAt` (via `pr_age_days`, in
   `now`'s local timezone), so a PR opened yesterday reads as 1 day old even if
   that was under 24h ago.
2. **Author names** — `resolve_authors` maps each unique login to the GitHub
   profile `name` (`gh api users/<login>`), falling back to the raw handle (no
   leading `@`, so it can't ping anyone). `humanize_display_name` reformats
   "Surname, Given" → "Given Surname". `effective_author_login` credits the human
   behind a Copilot bot PR (the `U_` GraphQL node-id assignee).
3. **Format** — `format_lead` builds the 📋 header (date, total count, per-repo
   counts + the 👀 claim line); `format_pr_block` builds each PR block (age dot +
   title / `repo #num · Nd old · who` / url). `build_messages` returns
   `[lead, pr1, pr2, …]`.
4. **Deliver** — `drive_chrome` → `_post_into_teams` types and sends each message
   into Teams, confirming each.

## Browser delivery (the core)

- **Dedicated Chrome.** `launch_debug_chrome` starts Chrome with
  `--remote-debugging-port=9222` and a persistent `--user-data-dir`
  (`$HOME/chrome-teams-debug`), headless by default (`--headless=new`). The
  profile keeps the Teams (MSAL) session, so sign-in happens once. Your normal
  Chrome is never touched.
- **Clean slate.** `drive_chrome` closes any existing debug Chrome first
  (`quit_debug_chrome`, matched by profile dir), launches a fresh one, waits for
  CDP (`wait_for_cdp`), drives it, then optionally lingers and closes.
- **Attach & post.** `_post_into_teams` attaches with `connect_over_cdp`, reuses a
  single tab, opens the target URL, and waits for the compose box (a CKEditor
  contenteditable).
- **Type, don't paste.** `_insert_message` uses CDP `insert_text` (clipboard paste
  is a no-op in headless Chrome). Our generated messages never contain a
  mention-triggering `@`; for any `@` inside a PR title, it writes a zero-width
  space after the `@` (`MENTION_GUARD`) **and** issues a real `Space` keypress —
  CDP `insert_text` is an IME-style commit that fires no keydown, so only a true
  keypress dismisses Teams' mention popup before it can swallow the soft line
  break between a PR's title, meta line, and URL.
- **Confirm each send.** Per message: `_insert_and_send` (load box → type → poll
  until the text is present → click Send) then `_confirm_sent` (poll until the box
  clears = posted). Up to 3 attempts; unconfirmed messages are logged and the run
  returns non-zero. No fixed inter-message delay — progress is gated by
  confirmation.
- **Readiness check.** If the compose box never appears, `_teams_not_ready_message`
  distinguishes "not signed in" (login host in the URL, or a visible sign-in
  field) from "Teams didn't load" and prints an actionable message; the run stops.

## CLI (`main`)

- `(no flag)` → post to the real Pull Requests channel (`TEAMS_CHANNEL_URL`),
  headless, send confirmed.
- `--test` → rehearse into the private "Notes to self" chat
  (`TEAMS_SELF_CHAT_URL`) — safe, only you see it.
- `--no-headless` → show the window; required for the one-time Teams sign-in.
- `--check` → run the preflight doctor and exit (posts nothing).

Author names are always resolved via the GitHub API.

## Preflight (prerequisite checks)

Every run verifies its prerequisites **up front and all at once**, so it never
fails halfway through and the user fixes everything in a single pass:

- `check_gh_installed` (gh on PATH), `check_gh_auth` (`gh auth status` exits
  non-zero when logged out), `check_playwright` (package importable via
  `importlib.util.find_spec`), `check_chrome` (Chrome present at `CHROME_APP`).
- Each returns a `CheckResult(name, ok, problem, fix)` — a pure, injectable unit
  (I/O is passed in: `which`, `runner`, `finder`, `exists`), so every branch is
  unit-tested without touching the real environment.
- `preflight` runs them and aggregates all results (skipping only the auth probe
  when gh itself is absent). `format_preflight_report` renders failures-only (for
  startup gating) or every check with ✓/✗ (for `--check`). `run_preflight` prints
  and returns pass/fail.
- `main` gates on `run_preflight()` before doing any work (exit 2 on missing
  prereqs); `--check` prints the full doctor report and exits 0/2.
- Teams sign-in stays a deferred runtime check (`_teams_not_ready_message`) since
  it needs the browser to be up first.

## Robustness notes

- `run_gh` turns a missing `gh` (bare cron `PATH`) into a clear, actionable error
  instead of a raw traceback.
- The script has no working-directory dependency (absolute Chrome path,
  `$HOME`-based profile), so it runs from anywhere — suitable for cron/launchd.
- Compose/Send selectors can't be unit-tested against live Teams, so those code
  paths stay defensive (broad, intentional `except` guards around Playwright
  calls, annotated with `# noqa`).

## Test & lint status

- `pytest test_pr_radar.py` — unit tests for the pure pipeline, `run_gh`,
  author-name resolution, the preflight checks (`check_*`, `preflight`,
  `format_preflight_report`, `run_preflight`) and `main`'s `--check`/gating,
  `chrome_cdp_reachable`, `wait_for_cdp`,
  `launch_debug_chrome`/`quit_debug_chrome`, the `drive_chrome` orchestration
  (with fakes), and `_teams_not_ready_message`.
- `ruff check` — clean.
