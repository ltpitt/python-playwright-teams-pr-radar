#!/usr/bin/env python3
"""PR Radar — Teams-paste-ready report of ready-for-review PRs across a curated
list of GitHub repos. Requires the `gh` CLI, authenticated via `gh auth login`."""
from __future__ import annotations

import argparse
import configparser
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from typing import NamedTuple

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "config.ini"
)


class Config(NamedTuple):
    """The delicate, deployment-specific settings loaded from config.ini.

    Kept out of the source (and git-ignored) so company repo names and Teams
    channel URLs are never committed. See config.example.ini for the template.
    `pr_channel_url` is loaded but not posted to yet — kept for future use.
    """

    repos: list
    channel_url: str
    pr_channel_url: str


def _split_repos(raw):
    """Parse the repos value: newline- and/or comma-separated `owner/repo`."""
    return [part.strip() for part in raw.replace(",", "\n").splitlines() if part.strip()]


def load_config(path=CONFIG_PATH):
    """Load settings from `path` (INI). Raise if it's missing or incomplete.

    `interpolation=None` because the Teams URLs contain literal `%` (e.g.
    `Pull%20Requests`) that ConfigParser would otherwise read as interpolation.
    """
    parser = configparser.ConfigParser(interpolation=None)
    if not parser.read(path):
        raise FileNotFoundError(path)
    repos = _split_repos(parser.get("repos", "list", fallback=""))
    channel_url = parser.get("teams", "channel_url", fallback="").strip()
    pr_channel_url = parser.get("teams", "pr_channel_url", fallback="").strip()
    if not repos:
        raise ValueError("no repositories configured under [repos] list")
    if not channel_url:
        raise ValueError("no channel_url configured under [teams]")
    return Config(repos, channel_url, pr_channel_url)



def run_gh(args, runner=subprocess.run):
    """Run a `gh` command and return its JSON stdout parsed as a list."""
    try:
        result = runner(
            ["gh", *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        # cron and other minimal environments start with a bare PATH that omits
        # Homebrew, so `gh` (usually /opt/homebrew/bin or /usr/local/bin) can't be
        # found. Say so plainly instead of crashing with a raw traceback.
        raise RuntimeError(
            "gh not found on PATH. If you're running from cron/launchd, add "
            "gh's directory to PATH (e.g. PATH=/opt/homebrew/bin:/usr/bin:/bin) "
            "or install the GitHub CLI: https://cli.github.com"
        )
    if result.returncode != 0:
        raise RuntimeError(
            f"gh failed: {' '.join(args)}\n{result.stderr.strip()}"
        )
    out = result.stdout.strip()
    return json.loads(out) if out else []


PR_FIELDS = "number,title,isDraft,author,assignees,createdAt,url,reviews,comments"


def fetch_ready_prs(repo, runner=subprocess.run):
    """Return open, non-draft PRs for one `owner/repo`."""
    prs = run_gh(
        [
            "pr", "list", "--repo", repo, "--state", "open",
            "--limit", "100", "--json", PR_FIELDS,
        ],
        runner=runner,
    )
    return [pr for pr in prs if not pr["isDraft"]]


def pr_age_days(created_at, now):
    """Whole calendar days between a PR's `createdAt` and `now`.

    Counts calendar-day boundaries crossed (in `now`'s timezone), so a PR opened
    any time yesterday reads as 1 day old — not 0, as a raw 24-hour elapsed count
    would report for something opened 20-odd hours ago.
    """
    created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    return (now.date() - created.astimezone(now.tzinfo).date()).days


def collect(repos, now, fetcher=fetch_ready_prs):
    """Gather ready PRs across repos as (repo, pr, age) tuples, oldest first."""
    rows = []
    for repo in repos:
        for pr in fetcher(repo):
            age = pr_age_days(pr["createdAt"], now)
            rows.append((repo, pr, age))
    rows.sort(key=lambda row: row[2], reverse=True)
    return rows


def age_marker(age):
    """Traffic-light urgency dot for a PR's age (days).

    Tuned for an AI team's compressed cadence: today is fresh, a day or two is
    warming up, and anything older than two days is overdue.
    """
    if age > 2:
        return "🔴"
    if age >= 1:
        return "🟡"
    return "🟢"


def format_lead(rows, now, reviewing_count=0):
    """Build the lead summary message (header + per-repo counts).

    `reviewing_count` is how many of the ready PRs already have someone engaged
    (reviewing/commenting); when non-zero it's surfaced in the header and a 👥
    legend line is appended.
    """
    if not rows:
        return f"✅ PR Radar · {now:%a %d %b} · All clear — no PRs waiting 🎉"
    counts = {}
    for repo, _pr, _age in rows:
        name = repo.split("/")[1]
        counts[name] = counts.get(name, 0) + 1
    header = f"📋 PR Radar · {now:%a %d %b} · {len(rows)} ready for review"
    if reviewing_count:
        header += f" · {reviewing_count} already being reviewed"
    lines = [header]
    for name, count in counts.items():
        lines.append(f"   • {name}: {count}")
    lines.append("")
    lines.append(
        "👀 React with the eyes emoji on a PR to claim it, "
        "so we don't review the same one twice."
    )
    if reviewing_count:
        lines.append(
            "👥 = someone's already reviewing — grab an unflagged one first."
        )
    return "\n".join(lines)


def format_pr_block(repo, pr, age, author=None, login=None, reviewers=None):
    """Build one per-PR Teams message block.

    `author` is the display string to show; when omitted, the raw `login` (or the
    PR author login) is shown as-is — no leading `@`, which would otherwise trip
    Teams' mention autocomplete. `reviewers` is the list of display strings for
    people already engaged with the PR; when non-empty, an extra `👥 Already
    being reviewed by ...` line is inserted before the URL.
    """
    who = author if author is not None else (login or pr["author"]["login"])
    lines = [
        f"{age_marker(age)} {pr['title']}",
        f"{repo} #{pr['number']} · {age}d old · {who}",
    ]
    if reviewers:
        lines.append(f"👥 Already being reviewed by {', '.join(reviewers)}")
    lines.append(pr["url"])
    return "\n".join(lines)


def effective_author_login(pr):
    """The human login to credit for a PR, seeing past bot authors.

    GitHub's Copilot coding agent opens PRs as a bot (`app/copilot-swe-agent`)
    but assigns the requesting human alongside the `Copilot` bot. When the author
    is a bot we credit the first human assignee instead — GraphQL node ids make
    that unambiguous (`U_` = user, `BOT_` = bot). Falls back to the bot login when
    no human assignee is present.
    """
    author = pr.get("author") or {}
    login = author.get("login", "")
    if not author.get("is_bot") and not login.startswith("app/"):
        return login
    for assignee in pr.get("assignees") or []:
        if str(assignee.get("id", "")).startswith("U_"):
            return assignee["login"]
    return login


# Bots show up in a PR's reviews/comments (e.g. github-actions posts a routing
# note, copilot-pull-request-reviewer leaves a COMMENTED review) but they aren't
# an engineer "working on" the PR. Their author objects in the reviews/comments
# payload only expose `login` (no `is_bot`), so we recognise them by login: the
# GraphQL `[bot]` suffix / `app/` prefix, a small set of known service bots, and
# anything Copilot-flavoured (copilot-swe-agent, copilot-pull-request-reviewer).
KNOWN_BOT_LOGINS = {"github-actions", "dependabot", "codecov"}


def is_bot_login(login):
    """True if `login` looks like a bot rather than a human engineer."""
    login = (login or "").lower()
    if not login:
        return True
    if login.endswith("[bot]") or login.startswith("app/"):
        return True
    if login in KNOWN_BOT_LOGINS:
        return True
    return "copilot" in login


def pr_reviewers(pr):
    """Distinct human logins already engaged with `pr`, first-seen order.

    Someone counts as engaged when they've left a review (formal or an inline
    `COMMENTED` review) or a conversation comment. We skip the PR's own author
    and any bot, so what's left is the people a second reviewer would be
    duplicating.
    """
    author = effective_author_login(pr)
    seen = []
    for source in ("reviews", "comments"):
        for entry in pr.get(source) or []:
            login = (entry.get("author") or {}).get("login", "")
            if not login or login == author or is_bot_login(login):
                continue
            if login not in seen:
                seen.append(login)
    return seen


def build_messages(rows, now, names=None):
    """Return the ordered list of Teams messages (lead first, then per-PR).

    `names` optionally maps a GitHub login to a friendly display name.
    """
    reviewers_by_row = [pr_reviewers(pr) for _repo, pr, _age in rows]
    reviewing_count = sum(1 for revs in reviewers_by_row if revs)
    messages = [format_lead(rows, now, reviewing_count=reviewing_count)]
    for (repo, pr, age), reviewer_logins in zip(rows, reviewers_by_row):
        login = effective_author_login(pr)
        author = names.get(login) if names else None
        reviewers = [
            (names.get(rl) if names else None) or rl for rl in reviewer_logins
        ]
        messages.append(
            format_pr_block(
                repo, pr, age, author=author, login=login, reviewers=reviewers
            )
        )
    return messages


# GitHub logins we will look up: corporate keys are alphanumeric with optional
# hyphens/underscores (e.g. "AB12CD_ing"). Anything else (e.g. "dependabot[bot]")
# is left untouched, which also keeps the `gh api users/<login>` path segment
# free of injectable characters.
AUTHOR_KEY_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_-]*[A-Za-z0-9])?$")
DISPLAY_NAME_RE = re.compile(r"^\s*(?P<sur>[^,]+),\s*[^(]*\((?P<first>[^)]+)\)\s*$")


def humanize_display_name(display):
    """Turn a 'Surname, X. (First)' display name into 'First Surname'.

    Falls back to the trimmed input when it doesn't match that pattern.
    """
    match = DISPLAY_NAME_RE.match(display)
    if match:
        return f"{match.group('first').strip()} {match.group('sur').strip()}"
    return display.strip()


def github_display_name(login, runner=subprocess.run):
    """Resolve a GitHub login to a friendly 'First Surname' via `gh api`.

    Uses the profile's `name` field (populated for IdP-provisioned accounts).
    Returns None when the login isn't a plain corporate key, the API call
    fails (offline, 404, not authed), or the profile has no name set.
    """
    if not AUTHOR_KEY_RE.match(login):
        return None
    try:
        data = run_gh(["api", f"users/{login}"], runner=runner)
    except RuntimeError:
        return None
    name = data.get("name") if isinstance(data, dict) else None
    return humanize_display_name(name) if name else None


def resolve_authors(logins, resolver=github_display_name):
    """Map each unique login to a friendly name, falling back to the raw login.

    We deliberately show the bare handle (no leading `@`) when a name can't be
    resolved: an `@handle` isn't a real Teams mention, it just trips Teams'
    mention autocomplete (which can eat line breaks) and risks pinging people.
    """
    names = {}
    for login in logins:
        if login in names:
            continue
        names[login] = resolver(login) or login
    return names


def unresolved_logins(logins, names):
    """The logins shown as raw handles because no display name was found."""
    return sorted({
        login for login in logins if login and names.get(login) == login
    })


# --- Drive a dedicated debug Chrome over CDP ---------------------------------
# This uses a SEPARATE Chrome instance with its own profile so your normal
# Chrome and its sessions are never touched. Launch it once (quit not required):
#   "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
#       --remote-debugging-port=9222 \
#       --user-data-dir="$HOME/chrome-teams-debug"
# Sign in to Teams in that window once (the login persists in that profile dir).
# CDP-attach reuses that live browser; it never types at the OS level, so no
# macOS Accessibility permission is involved.
CDP_ENDPOINT = "http://127.0.0.1:9222"
CHROME_APP = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME_DEBUG_PROFILE = "$HOME/chrome-teams-debug"

# Your private "Notes to self" chat — safe target for testing (only you see it).
# Generic across all users (the "48:notes" link has no tenant/company data), so
# it stays in code rather than config. Uses the v2 web form (like the channel
# URL) so it lands in the running web app.
TEAMS_SELF_CHAT_URL = (
    "https://teams.microsoft.com/v2/#/l/chat/48:notes/conversations"
    "?context=%7B%22contextType%22%3A%22chat%22%7D"
    "&launchAgent=join_launcher_web"
)


def chrome_cdp_reachable(endpoint=CDP_ENDPOINT, opener=urllib.request.urlopen):
    """True if a CDP-debuggable Chrome is listening at `endpoint`."""
    try:
        with opener(f"{endpoint}/json/version", timeout=2) as resp:
            return resp.getcode() == 200
    except OSError:
        return False


def launch_debug_chrome(
    app=CHROME_APP,
    profile=CHROME_DEBUG_PROFILE,
    port=9222,
    spawner=subprocess.Popen,
    headless=True,
):
    """Start a detached debug Chrome with its own persistent profile.

    The profile dir keeps your Teams (MSAL) session, so you only sign in once.
    Runs headless by default; the persistent profile means the session is reused
    without a visible window. (The very first sign-in must be done with
    --no-headless, since headless can't show the login page.)
    """
    data_dir = os.path.expanduser(os.path.expandvars(profile))
    flags = [
        app,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1280,1000",
    ]
    if headless:
        flags.append("--headless=new")
    spawner(
        flags,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_for_cdp(
    reachable=chrome_cdp_reachable,
    endpoint=CDP_ENDPOINT,
    attempts=30,
    sleeper=time.sleep,
):
    """Poll until the debug Chrome answers on CDP, or give up after `attempts`."""
    for _ in range(attempts):
        if reachable(endpoint):
            return True
        sleeper(1)
    return False


def quit_debug_chrome(profile=CHROME_DEBUG_PROFILE, killer=subprocess.run):
    """Kill only the debug Chrome, matched by its unique profile dir.

    Your normal Chrome uses a different `--user-data-dir`, so it's never hit.
    """
    data_dir = os.path.expanduser(os.path.expandvars(profile))
    killer(
        ["pkill", "-f", data_dir],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_until_gone(
    reachable=chrome_cdp_reachable,
    endpoint=CDP_ENDPOINT,
    attempts=30,
    sleeper=time.sleep,
):
    """Poll until the debug Chrome stops answering on CDP (fully closed)."""
    for _ in range(attempts):
        if not reachable(endpoint):
            return True
        sleeper(1)
    return False


# Slipped in right after every "@", a zero-width space stops Teams from opening
# its mention autocomplete. That popup otherwise swallows the next soft line
# break and glues two lines into one. The space is invisible in the posted text.
MENTION_GUARD = "\u200b"


def _insert_message(page, text):
    """Type `text` into the focused compose box via CDP insertText.

    Not a clipboard paste: headless Chrome can't reach the system clipboard, so a
    Cmd/Ctrl+V lands nothing. CDP's insertText works headless and visible alike.
    We insert line by line with a soft line break (Shift+Enter) between lines, and
    defuse every "@" with a zero-width space so the mention popup never eats a
    break. It's still browser-level input, so the macOS keystroke wall never
    applies.
    """
    for index, line in enumerate(text.split("\n")):
        if index:
            page.keyboard.press("Shift+Enter")
        if not line:
            continue
        page.keyboard.insert_text(line.replace("@", "@" + MENTION_GUARD))
        if "@" in line:
            # An "@handle" can still pop Teams' mention/app suggester when it
            # matches a real entity (e.g. "@app/copilot-swe-agent" — Copilot is a
            # real app). While that popup is open it steals the next Shift+Enter,
            # gluing the following line onto this one. We dismiss it with a real
            # Space *key press* (not insert_text: CDP's insertText is an IME-style
            # commit that never fires the keydown the suggester listens for, so a
            # synthetic space leaves the popup open). The space sits invisibly at
            # end-of-line in the posted message.
            page.keyboard.press("Space")


# Teams' send control, most specific first. The compose box lives next to it.
SEND_BUTTON_SELECTORS = [
    'button[data-tid="sendMessageCommand"]',
    'button[name="send"]',
    'button[aria-label="Send"]',
    'button[title="Send"]',
]


def _click_send(page):
    """Click the Teams Send button. Returns True if one was found and clicked."""
    for selector in SEND_BUTTON_SELECTORS:
        button = page.locator(selector).last
        try:
            button.wait_for(state="visible", timeout=3000)
            button.click()
            return True
        except Exception:  # noqa: BLE001, S112 - Playwright raises varied errors; just try the next selector
            continue
    return False


# Hosts Microsoft bounces you to when the Teams session has expired or was never
# established — our signal that the debug profile needs an interactive sign-in.
_SIGN_IN_HOSTS = (
    "login.microsoftonline.com",
    "login.microsoft.com",
    "login.live.com",
    "login.windows.net",
)


def _teams_not_ready_message(page):
    """Explain why Teams isn't usable, tailored to whether we're signed in.

    The compose box never showing up means one of two things: the debug profile
    isn't signed in (Microsoft parked us on a login page), or Teams simply didn't
    finish loading. We look at the current URL / a visible sign-in field to tell
    the two apart and give an actionable message.
    """
    url = ""
    try:
        url = (page.url or "").lower()
    except Exception:  # noqa: BLE001, S110 - page.url can fail mid-navigation; fall back to no-url
        pass
    on_login_page = any(host in url for host in _SIGN_IN_HOSTS)
    if not on_login_page:
        try:
            email = page.locator('input[type="email"], input[name="loginfmt"]').last
            on_login_page = email.is_visible(timeout=1500)
        except Exception:  # noqa: BLE001 - any locator failure just means no visible sign-in form
            on_login_page = False
    if on_login_page:
        return (
            "Teams isn't signed in on the debug Chrome profile. Run once with "
            "--no-headless to sign in — the session is remembered after that, "
            "so future runs stay headless."
        )
    return (
        "Teams didn't load (no compose box appeared). It may be a slow network "
        "or a changed layout. Try again, or run with --no-headless to see what "
        "the window is showing."
    )


def _first_line(message):
    """First line of a message, stripped — used to spot it in the compose box."""
    return message.split("\n", 1)[0].strip()


class _TeamsComposer:
    """Type, send, and confirm messages in a single Teams compose box.

    Wraps a Playwright `page` and its compose-box `box` locator so the
    type → send → confirm dance is a handful of small, focused methods over the
    shared browser state (and each one is unit-testable with a fake box).
    """

    def __init__(self, page, box):
        self.page = page
        self.box = box

    def load_box(self):
        """Focus the compose box and clear any restored draft, avoiding dupes."""
        self.box.click()
        self.page.keyboard.press("ControlOrMeta+A")
        self.page.keyboard.press("Backspace")

    def box_text(self):
        """Compose-box text with the invisible mention guards stripped out.

        A detached / re-navigating box just reads as empty.
        """
        try:
            return (self.box.inner_text() or "").replace(MENTION_GUARD, "")
        except Exception:  # noqa: BLE001 - a detached/again-navigating box just reads as empty
            return ""

    def wait_for_text(self, message, tries=20, delay=100):
        """True once the compose box holds this message's first line."""
        first = _first_line(message)
        for _ in range(tries):
            if first and first in self.box_text():
                return True
            self.page.wait_for_timeout(delay)
        return False

    def confirm_sent(self, message, tries=12, delay=300):
        """True once the compose box no longer holds this message's text.

        Teams clears the box only on a successful send, so the text
        disappearing from it is our confirmation that it posted.
        """
        first = _first_line(message)
        for _ in range(tries):
            if first and first not in self.box_text():
                return True
            self.page.wait_for_timeout(delay)
        return False

    def insert_and_send(self, message):
        """Type + send one message. Returns True only if the text landed.

        A landed message means the box actually holds our text before we click
        Send, so an empty box afterwards genuinely means it sent.
        """
        self.load_box()
        _insert_message(self.page, message)
        # Poll (not a fixed sleep) until CKEditor has committed the text, so we
        # click Send the instant it's ready instead of guessing a delay.
        landed = self.wait_for_text(message)
        if not _click_send(self.page):
            # Fall back to Enter if the Send button couldn't be located.
            self.page.keyboard.press("Enter")
        return landed


def _post_into_teams(url, messages, endpoint, out, send=False, attempts=3):
    """Attach to the live Chrome, open `url`, and post each message in `messages`.

    Text is typed via CDP insertText (never OS-level typing) and sent by clicking
    the Send button, so every message lands as its own Teams post (its own
    thread). We only advance to the next message once the current one is confirmed
    posted (the compose box clears), so no fixed inter-message delay is needed.
    When `send` is False, only the first message is typed (unsent) so you can
    eyeball it first.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "Playwright isn't installed. Add it to the venv:\n"
            "  ../../.venv/bin/python -m pip install playwright",
            file=out,
        )
        return 1
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(endpoint)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        # Keep exactly one tab: reuse the first, close any extras. This avoids
        # leftover tabs (each with its own unsent text) piling up across runs.
        pages = context.pages
        if pages:
            page = pages[0]
            for extra in pages[1:]:
                extra.close()
        else:
            page = context.new_page()
        print("Opening the Teams conversation in your Chrome...", file=out)
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        # The compose box is a CKEditor contenteditable whose id is generated at
        # runtime, so target it by stable attributes. Its appearance is also our
        # proof that Teams actually loaded and we're signed in — so we wait for it
        # once (combined selector) and treat a timeout as "Teams isn't ready".
        compose_selector = (
            '[data-shortcut-context="compose-field"], '
            '[data-tid="ckeditor"][contenteditable="true"], '
            'div[role="textbox"][contenteditable="true"]'
        )
        box = page.locator(compose_selector).last
        try:
            box.wait_for(state="visible", timeout=30000)
        except Exception:  # noqa: BLE001 - a timeout/any error here means Teams isn't ready
            print(_teams_not_ready_message(page), file=out)
            return 1

        composer = _TeamsComposer(page, box)

        if not send:
            composer.load_box()
            _insert_message(page, messages[0])
            print(
                "Typed the first message into the compose box (not sent). "
                "Review it in Chrome.",
                file=out,
            )
            return 0

        total = len(messages)
        failures = []
        for index, message in enumerate(messages, start=1):
            confirmed = False
            for attempt in range(1, attempts + 1):
                if not composer.insert_and_send(message):
                    print(
                        f"  message {index}/{total}: text didn't land "
                        f"(attempt {attempt}/{attempts}), retrying...",
                        file=out,
                    )
                    continue
                if composer.confirm_sent(message):
                    confirmed = True
                    break
                print(
                    f"  message {index}/{total}: send not confirmed "
                    f"(attempt {attempt}/{attempts}), retrying...",
                    file=out,
                )
            if confirmed:
                print(f"Sent message {index}/{total} (confirmed).", file=out)
            else:
                failures.append(index)
                print(
                    f"Message {index}/{total} could NOT be confirmed after "
                    f"{attempts} attempts — check the chat manually.",
                    file=out,
                )
        if failures:
            print(
                f"Done, but {len(failures)} message(s) were not confirmed: "
                f"{failures}",
                file=out,
            )
            return 1
    return 0


def drive_chrome(
    url=None,
    messages=None,
    endpoint=CDP_ENDPOINT,
    send=False,
    reachable=chrome_cdp_reachable,
    launcher=launch_debug_chrome,
    quitter=quit_debug_chrome,
    waiter=wait_for_cdp,
    gone_waiter=wait_until_gone,
    driver=_post_into_teams,
    linger_seconds=0,
    closer=quit_debug_chrome,
    sleeper=time.sleep,
    out=sys.stdout,
):
    """Open a Teams conversation in a debug Chrome and post `messages`.

    Always starts from a clean slate so runs stay hygienic: if a debug Chrome is
    already up it's closed first, then a fresh one is launched (its own
    persistent profile, so you sign in to Teams only once). This guarantees a
    single window with a single tab. Each message is typed and sent as its own
    Teams post when `send` is True. Your normal Chrome is never touched.

    When `linger_seconds` is set, the debug Chrome is left visible for that long
    after posting so you can eyeball the result, then closed automatically.
    """
    if messages is None:
        messages = ["PR Radar"]

    if reachable(endpoint):
        print(
            "Closing the existing debug Chrome for a clean slate "
            "(your normal Chrome stays untouched)...",
            file=out,
        )
        quitter()
        if not gone_waiter(reachable, endpoint):
            print(
                "Couldn't close the existing debug Chrome. Quit it manually "
                "and retry.",
                file=out,
            )
            return 1
    print(
        "Launching a fresh debug Chrome (your normal Chrome stays untouched)...",
        file=out,
    )
    launcher()
    if not waiter(reachable, endpoint):
        print(
            f"Debug Chrome didn't come up on {endpoint}. Launch it manually:\n"
            f'  "{CHROME_APP}" --remote-debugging-port=9222 --user-data-dir="{CHROME_DEBUG_PROFILE}"',
            file=out,
        )
        return 1
    rc = driver(url, messages, endpoint, out, send)
    if linger_seconds:
        print(
            f"Finishing up ({linger_seconds}s) then closing the debug Chrome...",
            file=out,
        )
        sleeper(linger_seconds)
        closer()
        print("Closed the debug Chrome.", file=out)
    return rc


class CheckResult(NamedTuple):
    """Outcome of a single prerequisite check.

    `problem` and `fix` are only shown when `ok` is False: what's wrong and the
    exact command/URL to put it right.
    """

    name: str
    ok: bool
    problem: str = ""
    fix: str = ""


def check_gh_installed(which=shutil.which):
    """Is the `gh` CLI on PATH?"""
    if which("gh"):
        return CheckResult("GitHub CLI (gh)", True)
    return CheckResult(
        "GitHub CLI (gh)",
        False,
        "not found on PATH",
        "install it — macOS: brew install gh  ·  docs: https://cli.github.com",
    )


def check_gh_auth(runner=subprocess.run):
    """Is `gh` authenticated? (`gh auth status` exits non-zero when logged out.)"""
    try:
        result = runner(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return CheckResult(
            "GitHub CLI auth",
            False,
            "can't check — gh isn't installed",
            "install gh, then run: gh auth login",
        )
    if result.returncode == 0:
        return CheckResult("GitHub CLI auth", True)
    return CheckResult(
        "GitHub CLI auth",
        False,
        "not logged in",
        "run: gh auth login",
    )


def check_playwright(finder=importlib.util.find_spec):
    """Is the Playwright Python package importable?"""
    if finder("playwright") is not None:
        return CheckResult("Playwright (Python)", True)
    return CheckResult(
        "Playwright (Python)",
        False,
        "Python package not installed",
        "install it: python3 -m pip install playwright",
    )


def check_chrome(app=CHROME_APP, exists=os.path.exists):
    """Is Google Chrome present at the path we launch for CDP?"""
    if exists(app):
        return CheckResult("Google Chrome", True)
    return CheckResult(
        "Google Chrome",
        False,
        f"not found at {app}",
        "install Google Chrome: https://www.google.com/chrome/",
    )


def check_config(path=CONFIG_PATH, loader=load_config):
    """Is config.ini present and complete (repos + channel URL)?"""
    try:
        loader(path)
    except FileNotFoundError:
        return CheckResult(
            "Config (config.ini)",
            False,
            "not found",
            f"copy config.example.ini to {path}, then fill in your values",
        )
    except (configparser.Error, ValueError) as exc:
        return CheckResult(
            "Config (config.ini)",
            False,
            f"incomplete or invalid ({exc})",
            "compare your config.ini against config.example.ini",
        )
    return CheckResult("Config (config.ini)", True)


def preflight(
    installed=check_gh_installed,
    authed=check_gh_auth,
    playwright=check_playwright,
    chrome=check_chrome,
    config=check_config,
):
    """Run every prerequisite check and return all results in one pass.

    Checks are independent so the report lists *everything* that's missing at
    once — no fix-one-rerun-hit-the-next. The only dependency: skip the auth probe
    when gh itself is absent (its guidance would just echo the install step).
    """
    gh = installed()
    results = [gh]
    if gh.ok:
        results.append(authed())
    else:
        results.append(
            CheckResult(
                "GitHub CLI auth",
                False,
                "skipped — install gh first",
                "then run: gh auth login",
            )
        )
    results.append(playwright())
    results.append(chrome())
    results.append(config())
    return results


def format_preflight_report(results, show_ok=False):
    """Render preflight `results` as a human report.

    Gating a run: pass `show_ok=False` to list only failures. The `--check`
    doctor: pass `show_ok=True` to show every check with a ✓/✗.
    """
    failed = [r for r in results if not r.ok]
    if not failed and not show_ok:
        return ""
    lines = []
    if show_ok:
        lines.append("PR Radar preflight:")
        for r in results:
            mark = "✓" if r.ok else "✗"
            lines.append(f"  {mark} {r.name}" + ("" if r.ok else f": {r.problem}"))
            if not r.ok:
                lines.append(f"      → {r.fix}")
    else:
        lines.append("PR Radar can't start — missing prerequisites:")
        lines.append("")
        for r in failed:
            lines.append(f"  ✗ {r.name}: {r.problem}")
            lines.append(f"      → {r.fix}")
    lines.append("")
    if failed:
        lines.append("Fix the above and re-run (use --check to re-test).")
    else:
        lines.append("All prerequisites satisfied.")
    return "\n".join(lines)


def run_preflight(out=sys.stdout, show_ok=False, checker=None):
    """Run preflight, print the report, and return True iff all checks pass."""
    if checker is None:
        checker = preflight
    results = checker()
    ok = all(r.ok for r in results)
    report = format_preflight_report(results, show_ok=show_ok)
    if report:
        print(report, file=out)
    return ok


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="pr_radar.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "PR Radar — post ready-for-review PRs into Teams, one message per PR\n"
            "so each becomes its own thread (react 👀 to claim it, no double work)."
        ),
        epilog=(
            "how to use it:\n"
            "  just run it (no flags) — it launches a dedicated debug Chrome\n"
            "  (headless), opens the Pull Requests channel, and posts every\n"
            "  ready PR as its own message (react 👀 to claim one).\n"
            "  First time only: run once with --no-headless to sign in to Teams\n"
            "  in that window; the session is remembered after that.\n"
            "\n"
            "modes (default is the real post to the team channel):\n"
            "  (no flag)            post the PRs to the real Pull Requests channel\n"
            "  --test               rehearse into your private 'Notes to self' chat\n"
            "                       instead (safe — only you see it)\n"
            "  --no-headless        show the Chrome window (needed for first sign-in)\n"
            "  --check              verify prerequisites (gh, auth, Playwright,\n"
            "                       Chrome) and exit — posts nothing\n"
            "\n"
            "author names are resolved to real names via the GitHub API (using your\n"
            "existing `gh` auth).\n"
            "\n"
            "examples:\n"
            "  python3 pr_radar.py                 # post to the real channel (headless)\n"
            "  python3 pr_radar.py --test          # safe rehearsal into your self-chat\n"
            "  python3 pr_radar.py --no-headless   # show the window (first sign-in)\n"
        ),
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Rehearse into your private 'Notes to self' chat instead of the "
             "real channel (safe — only you see it).",
    )
    parser.add_argument(
        "--no-headless",
        dest="headless",
        action="store_false",
        help="Show the Chrome window. Needed for the one-time Teams sign-in; "
             "otherwise the debug Chrome runs headless (the default).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check prerequisites (gh, gh auth, Playwright, Chrome) and exit, "
             "without posting anything. Exits non-zero if any are missing.",
    )
    parser.set_defaults(headless=True)
    args = parser.parse_args(argv)

    # --check: run the doctor and exit (0 = all good, 2 = something's missing).
    if args.check:
        return 0 if run_preflight(show_ok=True) else 2

    # Every run needs gh (+auth), Playwright, Chrome, and a filled-in config.
    # Verify all of it up front and report every gap at once, rather than
    # failing partway through.
    if not run_preflight():
        return 2

    config = load_config()
    now = datetime.now(timezone.utc).astimezone()
    rows = collect(config.repos, now)
    logins = [effective_author_login(pr) for _repo, pr, _age in rows]
    reviewer_logins = [
        rl for _repo, pr, _age in rows for rl in pr_reviewers(pr)
    ]
    names = resolve_authors(logins + reviewer_logins)
    raw = unresolved_logins(logins + reviewer_logins, names)
    if raw:
        print(
            f"Note: no display name for {len(raw)} author(s); "
            f"showing raw GitHub handle: {', '.join(raw)}"
        )
    messages = build_messages(rows, now, names=names)

    def launcher():
        return launch_debug_chrome(headless=args.headless)

    if args.test:
        # Your own chat, so it's safe to rehearse the real send end to end.
        # Headless has nothing to watch, so close right after delivery; a
        # visible window lingers longer so you can eyeball it.
        print(f"Posting {len(messages)} message(s) to your 'Notes to self' chat "
              "(safe test — only you see it)...")
        return drive_chrome(
            url=TEAMS_SELF_CHAT_URL,
            messages=messages,
            send=True,
            launcher=launcher,
            linger_seconds=5 if args.headless else 10,
        )
    # Default: the real thing — post to the team channel and send. Headless
    # closes after delivery; a visible window is left open so you can view it.
    print(f"Posting {len(messages)} message(s) to the Pull Requests channel...")
    return drive_chrome(
        url=config.channel_url,
        messages=messages,
        send=True,
        launcher=launcher,
        linger_seconds=5 if args.headless else 0,
    )


if __name__ == "__main__":
    sys.exit(main())
