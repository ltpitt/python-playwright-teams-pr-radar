import io
from datetime import datetime, timezone

import pytest

import pr_radar


class FakeCompleted:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_run_gh_parses_json_list():
    def fake_runner(cmd, capture_output, text, check):
        assert cmd[0] == "gh"
        return FakeCompleted(stdout='[{"number": 1}]')

    result = pr_radar.run_gh(["pr", "list"], runner=fake_runner)
    assert result == [{"number": 1}]


def test_run_gh_returns_empty_list_on_blank_output():
    def fake_runner(cmd, capture_output, text, check):
        return FakeCompleted(stdout="")

    assert pr_radar.run_gh(["pr", "list"], runner=fake_runner) == []


def test_run_gh_raises_on_nonzero_exit():
    def fake_runner(cmd, capture_output, text, check):
        return FakeCompleted(stderr="boom", returncode=1)

    try:
        pr_radar.run_gh(["pr", "list"], runner=fake_runner)
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "boom" in str(exc)


def test_run_gh_explains_when_gh_missing_from_path():
    def fake_runner(cmd, capture_output, text, check):
        raise FileNotFoundError("gh")

    try:
        pr_radar.run_gh(["pr", "list"], runner=fake_runner)
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "PATH" in str(exc)
        assert "gh" in str(exc)


def test_fetch_ready_prs_excludes_drafts():
    payload = (
        '[{"number": 1, "isDraft": false, "title": "ready"},'
        ' {"number": 2, "isDraft": true, "title": "draft"}]'
    )

    def fake_runner(cmd, capture_output, text, check):
        assert "--repo" in cmd and "owner/repo" in cmd
        return FakeCompleted(stdout=payload)

    prs = pr_radar.fetch_ready_prs("owner/repo", runner=fake_runner)
    assert [pr["number"] for pr in prs] == [1]


def test_pr_age_days_counts_whole_days():
    now = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)
    assert pr_radar.pr_age_days("2026-07-28T12:00:00Z", now) == 2


def test_pr_age_days_same_day_is_zero():
    now = datetime(2026, 7, 30, 23, 0, 0, tzinfo=timezone.utc)
    assert pr_radar.pr_age_days("2026-07-30T01:00:00Z", now) == 0


def test_pr_age_days_yesterday_is_one_even_under_24h():
    # Opened ~21h ago but on the previous calendar day -> 1, not 0.
    now = datetime(2026, 7, 31, 11, 0, 0, tzinfo=timezone.utc)
    assert pr_radar.pr_age_days("2026-07-30T14:00:00Z", now) == 1


def test_collect_sorts_oldest_first_across_repos():
    now = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)
    fake_data = {
        "org/a": [
            {"number": 1, "title": "new", "author": {"login": "x"},
             "createdAt": "2026-07-29T12:00:00Z", "url": "u1"},
        ],
        "org/b": [
            {"number": 2, "title": "old", "author": {"login": "y"},
             "createdAt": "2026-07-20T12:00:00Z", "url": "u2"},
        ],
    }

    def fake_fetcher(repo):
        return fake_data[repo]

    rows = pr_radar.collect(["org/a", "org/b"], now, fetcher=fake_fetcher)
    assert [(repo, pr["number"], age) for repo, pr, age in rows] == [
        ("org/b", 2, 10),
        ("org/a", 1, 1),
    ]


def _row(repo, number, age):
    pr = {"number": number, "title": "t", "author": {"login": "a"}, "url": "u"}
    return (repo, pr, age)


def test_format_lead_with_prs_has_header_and_repo_counts():
    now = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)
    rows = [_row("org/a", 1, 5), _row("org/a", 2, 3), _row("org/b", 3, 1)]
    lead = pr_radar.format_lead(rows, now)
    assert "3 ready for review" in lead
    assert "oldest" not in lead
    assert "🔴" not in lead
    assert "• a: 2" in lead
    assert "• b: 1" in lead
    assert "👀" in lead
    assert "claim it" in lead


def test_format_lead_empty_is_all_clear():
    now = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)
    lead = pr_radar.format_lead([], now)
    assert "All clear" in lead
    assert "🎉" in lead


def test_format_pr_block_contains_key_facts():
    pr = {
        "number": 42, "title": "Fix the thing",
        "author": {"login": "dev1"}, "url": "https://example/pull/42",
    }
    block = pr_radar.format_pr_block("org/repo", pr, 7)
    assert "Fix the thing" in block
    assert "org/repo #42" in block
    assert "7d old" in block
    assert "dev1" in block
    assert "@dev1" not in block  # no mention-triggering @ in our output
    assert "https://example/pull/42" in block
    # No decorative bullet or per-block claim line — that lives in the lead now.
    assert "🔸" not in block
    assert "👀" not in block
    # Age-urgency dot leads the title line (7d → overdue red).
    assert block.splitlines()[0] == "🔴 Fix the thing"


def test_format_pr_block_uses_supplied_author_name():
    pr = {
        "number": 7, "title": "t",
        "author": {"login": "AB12CD_ing"}, "url": "u",
    }
    block = pr_radar.format_pr_block("org/repo", pr, 1, author="Jane Doe")
    assert "Jane Doe" in block
    assert "AB12CD_ing" not in block


def test_build_messages_lead_first_then_one_per_pr():
    now = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)
    rows = [_row("org/a", 1, 5), _row("org/b", 2, 1)]
    messages = pr_radar.build_messages(rows, now)
    assert len(messages) == 3
    assert "ready for review" in messages[0]
    assert "org/a #1" in messages[1]
    assert "org/b #2" in messages[2]


def test_build_messages_maps_logins_to_names():
    now = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)
    rows = [_row("org/a", 1, 5)]
    rows[0][1]["author"]["login"] = "AB12CD_ing"
    messages = pr_radar.build_messages(
        rows, now, names={"AB12CD_ing": "Jane Doe"}
    )
    assert "Jane Doe" in messages[1]


def test_effective_author_login_returns_human_author_directly():
    pr = {"author": {"login": "dev1", "is_bot": False}, "assignees": []}
    assert pr_radar.effective_author_login(pr) == "dev1"


def test_effective_author_login_credits_human_assignee_for_bot_author():
    pr = {
        "author": {"login": "app/copilot-swe-agent", "is_bot": True},
        "assignees": [
            {"login": "Copilot", "id": "BOT_kgDOabc"},
            {"login": "AB12CD_ing", "id": "U_kgDOxyz"},
        ],
    }
    assert pr_radar.effective_author_login(pr) == "AB12CD_ing"


def test_effective_author_login_keeps_bot_when_no_human_assignee():
    pr = {
        "author": {"login": "app/copilot-swe-agent", "is_bot": True},
        "assignees": [{"login": "Copilot", "id": "BOT_kgDOabc"}],
    }
    assert pr_radar.effective_author_login(pr) == "app/copilot-swe-agent"


def test_build_messages_credits_human_behind_copilot_pr():
    now = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)
    pr = {
        "number": 52, "title": "wire flags", "url": "u",
        "author": {"login": "app/copilot-swe-agent", "is_bot": True},
        "assignees": [
            {"login": "Copilot", "id": "BOT_x"},
            {"login": "AB12CD_ing", "id": "U_y"},
        ],
    }
    rows = [("org/a", pr, 0)]
    messages = pr_radar.build_messages(
        rows, now, names={"AB12CD_ing": "Jane Doe"}
    )
    assert "Jane Doe" in messages[1]
    assert "copilot-swe-agent" not in messages[1]


def test_build_messages_empty_is_lead_only():
    now = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)
    messages = pr_radar.build_messages([], now)
    assert len(messages) == 1
    assert "All clear" in messages[0]


def test_is_bot_login_flags_bots_and_passes_humans():
    for bot in ("github-actions", "dependabot[bot]", "app/copilot-swe-agent",
                "copilot-pull-request-reviewer", ""):
        assert pr_radar.is_bot_login(bot) is True
    for human in ("AB12CD_ing", "sergiou87", "williammartin"):
        assert pr_radar.is_bot_login(human) is False


def test_pr_reviewers_collects_reviews_and_comments_no_dupes():
    pr = {
        "author": {"login": "dev1"},
        "reviews": [
            {"author": {"login": "alice"}, "state": "COMMENTED"},
            {"author": {"login": "alice"}, "state": "APPROVED"},
        ],
        "comments": [{"author": {"login": "bob"}}],
    }
    assert pr_radar.pr_reviewers(pr) == ["alice", "bob"]


def test_pr_reviewers_skips_author_and_bots():
    pr = {
        "author": {"login": "dev1"},
        "reviews": [
            {"author": {"login": "copilot-pull-request-reviewer"}},
            {"author": {"login": "dev1"}},
        ],
        "comments": [{"author": {"login": "github-actions"}}],
    }
    assert pr_radar.pr_reviewers(pr) == []


def test_pr_reviewers_credits_humans_behind_copilot_author():
    pr = {
        "author": {"login": "app/copilot-swe-agent", "is_bot": True},
        "assignees": [{"login": "AB12CD_ing", "id": "U_y"}],
        "reviews": [{"author": {"login": "carol"}}],
        "comments": [],
    }
    assert pr_radar.pr_reviewers(pr) == ["carol"]


def test_format_pr_block_lists_reviewers_before_url():
    pr = {
        "number": 42, "title": "Fix the thing",
        "author": {"login": "dev1"}, "url": "https://example/pull/42",
    }
    block = pr_radar.format_pr_block(
        "org/repo", pr, 7, reviewers=["John Smith", "Priya Patel"]
    )
    lines = block.splitlines()
    assert lines[2] == "👥 Already being reviewed by John Smith, Priya Patel"
    assert lines[3] == "https://example/pull/42"


def test_format_pr_block_without_reviewers_has_no_flag_line():
    pr = {
        "number": 42, "title": "t",
        "author": {"login": "dev1"}, "url": "u",
    }
    assert "👥" not in pr_radar.format_pr_block("org/repo", pr, 1)


def test_format_lead_surfaces_reviewing_count_and_legend():
    now = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)
    rows = [_row("org/a", 1, 5), _row("org/a", 2, 3)]
    lead = pr_radar.format_lead(rows, now, reviewing_count=1)
    assert "1 already being reviewed" in lead
    assert "👥 = someone's already reviewing" in lead


def test_format_lead_no_reviewing_count_omits_legend():
    now = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)
    lead = pr_radar.format_lead([_row("org/a", 1, 5)], now)
    assert "already being reviewed" not in lead
    assert "👥" not in lead


def test_build_messages_flags_reviewed_pr_and_counts_it():
    now = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)
    reviewed = {
        "number": 1, "title": "busy", "url": "u1",
        "author": {"login": "dev1"},
        "reviews": [{"author": {"login": "AB12CD_ing"}}],
        "comments": [{"author": {"login": "bob"}}],
    }
    quiet = {
        "number": 2, "title": "quiet", "url": "u2",
        "author": {"login": "dev2"}, "reviews": [], "comments": [],
    }
    rows = [("org/a", reviewed, 5), ("org/b", quiet, 1)]
    messages = pr_radar.build_messages(
        rows, now, names={"AB12CD_ing": "Jane Doe"}
    )
    assert "1 already being reviewed" in messages[0]
    assert "👥 Already being reviewed by Jane Doe, bob" in messages[1]
    assert "👥" not in messages[2]


class _FakeResp:
    def __init__(self, code):
        self._code = code

    def getcode(self):
        return self._code

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_chrome_cdp_reachable_true_on_200():
    def fake_opener(url, timeout):
        assert url.endswith("/json/version")
        return _FakeResp(200)

    assert pr_radar.chrome_cdp_reachable(opener=fake_opener) is True


def test_chrome_cdp_reachable_false_when_connection_refused():
    def fake_opener(url, timeout):
        raise OSError("connection refused")

    assert pr_radar.chrome_cdp_reachable(opener=fake_opener) is False


class _FakeElement:
    def __init__(self, visible):
        self._visible = visible

    def is_visible(self, timeout=None):
        return self._visible


class _FakeLocator:
    def __init__(self, element):
        self.last = element


class _FakePage:
    def __init__(self, url, email_visible=False):
        self.url = url
        self._email = _FakeElement(email_visible)

    def locator(self, selector):
        return _FakeLocator(self._email)


def test_teams_not_ready_message_flags_signin_when_on_login_host():
    page = _FakePage("https://login.microsoftonline.com/common/oauth2/authorize")
    msg = pr_radar._teams_not_ready_message(page)
    assert "--no-headless" in msg
    assert "signed in" in msg.lower()


def test_teams_not_ready_message_flags_signin_when_login_form_visible():
    page = _FakePage("https://teams.microsoft.com/v2/", email_visible=True)
    msg = pr_radar._teams_not_ready_message(page)
    assert "--no-headless" in msg
    assert "signed in" in msg.lower()


def test_teams_not_ready_message_reports_load_failure_when_signed_in():
    page = _FakePage("https://teams.microsoft.com/v2/", email_visible=False)
    msg = pr_radar._teams_not_ready_message(page)
    assert "didn't load" in msg.lower()
    assert "signed in" not in msg.lower()



def test_drive_chrome_gives_up_with_message_when_launch_fails():
    out = io.StringIO()

    def never_called(*args, **kwargs):
        raise AssertionError("driver must not run when Chrome never comes up")

    rc = pr_radar.drive_chrome(
        reachable=lambda _endpoint: False,
        launcher=lambda: None,
        waiter=lambda *a, **k: False,
        driver=never_called,
        out=out,
    )
    assert rc == 1
    assert "remote-debugging-port=9222" in out.getvalue()


def test_drive_chrome_autolaunches_then_drives_when_chrome_absent():
    events = []

    def fake_driver(url, messages, endpoint, out, send):
        events.append("drive")
        return 0

    rc = pr_radar.drive_chrome(
        reachable=lambda _endpoint: False,
        launcher=lambda: events.append("launch"),
        quitter=lambda: events.append("quit"),
        waiter=lambda *a, **k: True,
        gone_waiter=lambda *a, **k: True,
        driver=fake_driver,
        out=io.StringIO(),
    )
    assert rc == 0
    # No debug Chrome was up, so nothing to close — just launch then drive.
    assert events == ["launch", "drive"]


def test_drive_chrome_closes_existing_then_relaunches_when_chrome_open():
    events = []
    seen = {}

    def fake_driver(url, messages, endpoint, out, send):
        seen["url"] = url
        seen["messages"] = messages
        seen["send"] = send
        events.append("drive")
        return 0

    rc = pr_radar.drive_chrome(
        url="https://self-chat",
        messages=["one", "two"],
        send=True,
        reachable=lambda _endpoint: True,
        quitter=lambda: events.append("quit"),
        gone_waiter=lambda *a, **k: True,
        launcher=lambda: events.append("launch"),
        waiter=lambda *a, **k: True,
        driver=fake_driver,
        out=io.StringIO(),
    )
    assert rc == 0
    # Existing debug Chrome is closed first, then a fresh one is launched.
    assert events == ["quit", "launch", "drive"]
    assert seen["url"] == "https://self-chat"
    assert seen["messages"] == ["one", "two"]
    assert seen["send"] is True


def test_drive_chrome_gives_up_when_existing_chrome_wont_close():
    def never_called(*args, **kwargs):
        raise AssertionError("must not launch when the old Chrome won't close")

    out = io.StringIO()
    rc = pr_radar.drive_chrome(
        reachable=lambda _endpoint: True,
        quitter=lambda: None,
        gone_waiter=lambda *a, **k: False,
        launcher=never_called,
        driver=never_called,
        out=out,
    )
    assert rc == 1
    assert "Couldn't close" in out.getvalue()


def test_drive_chrome_lingers_then_closes_when_asked():
    events = []

    rc = pr_radar.drive_chrome(
        reachable=lambda _endpoint: False,
        launcher=lambda: events.append("launch"),
        quitter=lambda: events.append("quit"),
        waiter=lambda *a, **k: True,
        gone_waiter=lambda *a, **k: True,
        driver=lambda *a, **k: events.append("drive") or 0,
        linger_seconds=10,
        closer=lambda: events.append("close"),
        sleeper=lambda secs: events.append(("slept", secs)),
        out=io.StringIO(),
    )
    assert rc == 0
    # Type first, then wait the requested time, then close.
    assert events == ["launch", "drive", ("slept", 10), "close"]


def test_drive_chrome_does_not_close_when_no_linger():
    events = []

    rc = pr_radar.drive_chrome(
        reachable=lambda _endpoint: False,
        launcher=lambda: events.append("launch"),
        waiter=lambda *a, **k: True,
        gone_waiter=lambda *a, **k: True,
        driver=lambda *a, **k: events.append("drive") or 0,
        closer=lambda: events.append("close"),
        sleeper=lambda secs: events.append("slept"),
        out=io.StringIO(),
    )
    assert rc == 0
    assert "close" not in events
    assert "slept" not in events



def test_wait_for_cdp_returns_true_once_reachable():
    calls = {"n": 0}

    def reachable(_endpoint):
        calls["n"] += 1
        return calls["n"] >= 3

    assert pr_radar.wait_for_cdp(
        reachable=reachable, attempts=5, sleeper=lambda _s: None
    ) is True
    assert calls["n"] == 3


def test_wait_for_cdp_gives_up_after_attempts():
    assert pr_radar.wait_for_cdp(
        reachable=lambda _endpoint: False, attempts=4, sleeper=lambda _s: None
    ) is False


def test_launch_debug_chrome_spawns_with_profile_and_port():
    spawned = {}

    def fake_spawner(cmd, stdout=None, stderr=None):
        spawned["cmd"] = cmd

    pr_radar.launch_debug_chrome(
        app="/chrome", profile="$HOME/x", port=9222, spawner=fake_spawner
    )
    cmd = spawned["cmd"]
    assert cmd[0] == "/chrome"
    assert "--remote-debugging-port=9222" in cmd
    assert any(a.startswith("--user-data-dir=") and "$HOME" not in a for a in cmd)


def test_launch_debug_chrome_headless_by_default():
    spawned = {}

    def fake_spawner(cmd, stdout=None, stderr=None):
        spawned["cmd"] = cmd

    pr_radar.launch_debug_chrome(
        app="/chrome", profile="$HOME/x", spawner=fake_spawner
    )
    assert "--headless=new" in spawned["cmd"]


def test_launch_debug_chrome_visible_when_headless_false():
    spawned = {}

    def fake_spawner(cmd, stdout=None, stderr=None):
        spawned["cmd"] = cmd

    pr_radar.launch_debug_chrome(
        app="/chrome", profile="$HOME/x", spawner=fake_spawner, headless=False
    )
    assert "--headless=new" not in spawned["cmd"]


def test_quit_debug_chrome_kills_only_the_profile_process():
    killed = {}

    def fake_killer(cmd, stdout=None, stderr=None):
        killed["cmd"] = cmd

    pr_radar.quit_debug_chrome(profile="$HOME/x", killer=fake_killer)
    cmd = killed["cmd"]
    assert cmd[0] == "pkill"
    assert cmd[1] == "-f"
    # Matches the expanded, unique profile dir so normal Chrome is never hit.
    assert cmd[2].endswith("/x")
    assert "$HOME" not in cmd[2]



def test_humanize_display_name_reformats_surname_first():
    assert pr_radar.humanize_display_name("Doe, J. (Jane)") == "Jane Doe"
    assert (
        pr_radar.humanize_display_name("Smith, A.B.C. (Alice)")
        == "Alice Smith"
    )


def test_humanize_display_name_passes_through_unknown_format():
    assert pr_radar.humanize_display_name("Just A Name") == "Just A Name"


def test_github_display_name_reads_name_field():
    def fake_runner(cmd, capture_output, text, check):
        assert cmd[:3] == ["gh", "api", "users/AB12CD_ing"]
        return FakeCompleted(stdout='{"login": "AB12CD_ing", "name": "Doe, J. (Jane)"}')

    assert pr_radar.github_display_name("AB12CD_ing", runner=fake_runner) == "Jane Doe"


def test_github_display_name_skips_non_key_logins():
    calls = []

    def fake_runner(cmd, capture_output, text, check):
        calls.append(cmd)
        return FakeCompleted(stdout="{}")

    assert pr_radar.github_display_name("dependabot[bot]", runner=fake_runner) is None
    assert calls == []  # never hits the API for bracketed bot logins


def test_github_display_name_none_when_no_name_set():
    def fake_runner(cmd, capture_output, text, check):
        return FakeCompleted(stdout='{"login": "ghost", "name": null}')

    assert pr_radar.github_display_name("ghost", runner=fake_runner) is None


def test_resolve_authors_caches_and_falls_back_to_login():
    lookups = []

    def fake_resolver(login):
        lookups.append(login)
        return "Real Person" if login == "known" else None

    names = pr_radar.resolve_authors(
        ["known", "known", "unknown"], resolver=fake_resolver
    )
    assert names == {"known": "Real Person", "unknown": "unknown"}
    assert lookups == ["known", "unknown"]  # deduped


def test_unresolved_logins_lists_raw_handles_only():
    names = {"known": "Real Person", "unknown": "unknown", "": ""}
    assert pr_radar.unresolved_logins(
        ["known", "unknown", "unknown", ""], names
    ) == ["unknown"]


def test_load_config_reads_repos_and_urls(tmp_path):
    cfg = tmp_path / "config.ini"
    # A real-shaped channel URL with % escapes and a # fragment, to prove
    # neither trips ConfigParser (interpolation off, inline comments off).
    cfg.write_text(
        "[repos]\n"
        "list =\n"
        "    org/one\n"
        "    org/two\n"
        "\n"
        "[teams]\n"
        "channel_url = https://teams.microsoft.com/v2/?tenantId=abc"
        "#/l/channel/19:xyz@thread.tacv2/Pull%20Requests?groupId=ghi\n"
        "pr_channel_url = https://example/second\n"
    )
    config = pr_radar.load_config(str(cfg))
    assert config.repos == ["org/one", "org/two"]
    assert "Pull%20Requests" in config.channel_url
    assert config.channel_url.count("#") == 1  # fragment kept, not comment-eaten
    assert config.pr_channel_url == "https://example/second"


def test_load_config_accepts_comma_separated_repos(tmp_path):
    cfg = tmp_path / "config.ini"
    cfg.write_text(
        "[repos]\nlist = org/one, org/two , org/three\n"
        "[teams]\nchannel_url = https://x\n"
    )
    assert pr_radar.load_config(str(cfg)).repos == [
        "org/one", "org/two", "org/three"
    ]


def test_load_config_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        pr_radar.load_config(str(tmp_path / "nope.ini"))


def test_load_config_rejects_empty_repos(tmp_path):
    cfg = tmp_path / "config.ini"
    cfg.write_text("[repos]\nlist =\n[teams]\nchannel_url = https://x\n")
    with pytest.raises(ValueError):
        pr_radar.load_config(str(cfg))


def test_load_config_rejects_missing_channel_url(tmp_path):
    cfg = tmp_path / "config.ini"
    cfg.write_text("[repos]\nlist = org/one\n[teams]\nchannel_url =\n")
    with pytest.raises(ValueError):
        pr_radar.load_config(str(cfg))


def test_check_config_ok_when_loader_succeeds():
    result = pr_radar.check_config(
        path="whatever", loader=lambda path: pr_radar.Config(["org/a"], "u", "")
    )
    assert result.ok


def test_check_config_reports_missing_file():
    def loader(path):
        raise FileNotFoundError(path)

    result = pr_radar.check_config(path="/x/config.ini", loader=loader)
    assert not result.ok
    assert "config.example.ini" in result.fix


def test_check_config_reports_invalid_config():
    def loader(path):
        raise ValueError("no channel_url")

    result = pr_radar.check_config(path="/x/config.ini", loader=loader)
    assert not result.ok
    assert "invalid" in result.problem or "incomplete" in result.problem



def test_first_line_is_stripped_first_line():
    assert pr_radar._first_line("  hi there \nsecond\nthird") == "hi there"


class _ComposerBox:
    def __init__(self, text):
        self.text = text
        self.clicked = 0

    def click(self):
        self.clicked += 1

    def inner_text(self):
        return self.text


class _ComposerPage:
    def wait_for_timeout(self, _delay):
        pass


def test_composer_box_text_strips_mention_guard():
    box = _ComposerBox("hi " + pr_radar.MENTION_GUARD + "@bot")
    composer = pr_radar._TeamsComposer(_ComposerPage(), box)
    assert composer.box_text() == "hi @bot"


def test_composer_wait_for_text_true_when_first_line_present():
    box = _ComposerBox("PR title\nrepo #1")
    composer = pr_radar._TeamsComposer(_ComposerPage(), box)
    assert composer.wait_for_text("PR title\nrepo #1", tries=1) is True


def test_composer_wait_for_text_false_when_box_empty():
    box = _ComposerBox("")
    composer = pr_radar._TeamsComposer(_ComposerPage(), box)
    assert composer.wait_for_text("PR title", tries=2) is False


def test_composer_confirm_sent_true_when_box_cleared():
    box = _ComposerBox("")
    composer = pr_radar._TeamsComposer(_ComposerPage(), box)
    assert composer.confirm_sent("PR title\nrepo #1", tries=1) is True


def test_composer_confirm_sent_false_while_text_lingers():
    box = _ComposerBox("PR title\nrepo #1")
    composer = pr_radar._TeamsComposer(_ComposerPage(), box)
    assert composer.confirm_sent("PR title\nrepo #1", tries=2) is False


def test_age_marker_thresholds():
    assert pr_radar.age_marker(0) == "🟢"
    assert pr_radar.age_marker(1) == "🟡"
    assert pr_radar.age_marker(2) == "🟡"
    assert pr_radar.age_marker(3) == "🔴"
    assert pr_radar.age_marker(10) == "🔴"


class _RecordingKeyboard:
    def __init__(self):
        self.events = []

    def insert_text(self, text):
        self.events.append(("insert", text))

    def press(self, key):
        self.events.append(("press", key))


class _RecordingPage:
    def __init__(self):
        self.keyboard = _RecordingKeyboard()


def test_insert_message_dismisses_mention_popup_after_at_lines():
    page = _RecordingPage()
    pr_radar._insert_message(page, "title\n#7 · @app/copilot-swe-agent\nhttps://x")
    events = page.keyboard.events
    # The soft break before the URL line is only reliable if the mention popup
    # was dismissed first, and only a real Space key press (not insert_text)
    # fires the keydown Teams closes the suggester on.
    at_line = f"#7 · @{pr_radar.MENTION_GUARD}app/copilot-swe-agent"
    idx = events.index(("insert", at_line))
    assert events[idx + 1] == ("press", "Space")
    assert events[idx + 2] == ("press", "Shift+Enter")
    assert events[idx + 3] == ("insert", "https://x")


def test_insert_message_no_dismiss_space_without_at():
    page = _RecordingPage()
    pr_radar._insert_message(page, "plain title\nhttps://x")
    assert ("press", "Space") not in page.keyboard.events


# --- Preflight / prerequisite checks -------------------------------------


def test_check_gh_installed_ok_when_on_path():
    result = pr_radar.check_gh_installed(which=lambda name: "/usr/bin/gh")
    assert result.ok
    assert result.name == "GitHub CLI (gh)"


def test_check_gh_installed_fails_when_missing():
    result = pr_radar.check_gh_installed(which=lambda name: None)
    assert not result.ok
    assert result.problem
    assert result.fix


def test_check_gh_auth_ok_on_zero_exit():
    def fake_runner(cmd, capture_output, text, check):
        assert cmd == ["gh", "auth", "status"]
        return FakeCompleted(returncode=0)

    assert pr_radar.check_gh_auth(runner=fake_runner).ok


def test_check_gh_auth_fails_when_logged_out():
    def fake_runner(cmd, capture_output, text, check):
        return FakeCompleted(returncode=1, stderr="not logged in")

    result = pr_radar.check_gh_auth(runner=fake_runner)
    assert not result.ok
    assert "gh auth login" in result.fix


def test_check_gh_auth_handles_missing_gh():
    def fake_runner(cmd, capture_output, text, check):
        raise FileNotFoundError

    result = pr_radar.check_gh_auth(runner=fake_runner)
    assert not result.ok


def test_check_playwright_ok_when_importable():
    result = pr_radar.check_playwright(finder=lambda name: object())
    assert result.ok


def test_check_playwright_fails_when_absent():
    result = pr_radar.check_playwright(finder=lambda name: None)
    assert not result.ok
    assert "pip install playwright" in result.fix


def test_check_chrome_ok_when_present():
    result = pr_radar.check_chrome(app="/x/Chrome", exists=lambda p: True)
    assert result.ok


def test_check_chrome_fails_when_absent():
    result = pr_radar.check_chrome(app="/x/Chrome", exists=lambda p: False)
    assert not result.ok
    assert "/x/Chrome" in result.problem


def _ok(name):
    return pr_radar.CheckResult(name, True)


def _bad(name):
    return pr_radar.CheckResult(name, False, "broken", "fix it")


def test_preflight_runs_all_checks_when_gh_present():
    results = pr_radar.preflight(
        installed=lambda: _ok("gh"),
        authed=lambda: _ok("auth"),
        playwright=lambda: _ok("pw"),
        chrome=lambda: _ok("chrome"),
        config=lambda: _ok("config"),
    )
    assert [r.name for r in results] == ["gh", "auth", "pw", "chrome", "config"]
    assert all(r.ok for r in results)


def test_preflight_skips_auth_probe_when_gh_missing():
    called = []

    def auth_probe():
        called.append(True)
        return _ok("auth")

    results = pr_radar.preflight(
        installed=lambda: _bad("gh"),
        authed=auth_probe,
        playwright=lambda: _ok("pw"),
        chrome=lambda: _ok("chrome"),
        config=lambda: _ok("config"),
    )
    # The real auth probe must not run when gh isn't installed.
    assert called == []
    auth = next(r for r in results if r.name == "GitHub CLI auth")
    assert not auth.ok


def test_preflight_aggregates_all_failures():
    results = pr_radar.preflight(
        installed=lambda: _ok("gh"),
        authed=lambda: _bad("auth"),
        playwright=lambda: _bad("pw"),
        chrome=lambda: _bad("chrome"),
        config=lambda: _bad("config"),
    )
    failed = [r.name for r in results if not r.ok]
    assert failed == ["auth", "pw", "chrome", "config"]


def test_format_preflight_report_failures_only_is_empty_when_all_ok():
    results = [_ok("a"), _ok("b")]
    assert pr_radar.format_preflight_report(results, show_ok=False) == ""


def test_format_preflight_report_lists_each_failure_with_fix():
    results = [_ok("gh"), _bad("auth")]
    report = pr_radar.format_preflight_report(results, show_ok=False)
    assert "can't start" in report
    assert "auth" in report
    assert "fix it" in report
    assert "gh" not in report.split("\n")[1]  # passing checks aren't listed


def test_format_preflight_report_show_ok_lists_every_check():
    results = [_ok("gh"), _bad("auth")]
    report = pr_radar.format_preflight_report(results, show_ok=True)
    assert "✓ gh" in report
    assert "✗ auth" in report


def test_run_preflight_returns_false_and_prints_on_failure():
    out = io.StringIO()
    ok = pr_radar.run_preflight(
        out=out, checker=lambda: [_ok("gh"), _bad("auth")]
    )
    assert ok is False
    assert "auth" in out.getvalue()


def test_run_preflight_returns_true_when_all_ok():
    out = io.StringIO()
    ok = pr_radar.run_preflight(
        out=out, show_ok=False, checker=lambda: [_ok("gh"), _ok("auth")]
    )
    assert ok is True
    assert out.getvalue() == ""  # nothing printed when gating and all-clear


def test_main_check_flag_exits_zero_when_all_ok(monkeypatch):
    monkeypatch.setattr(
        pr_radar, "preflight", lambda: [_ok("gh"), _ok("auth")]
    )
    assert pr_radar.main(["--check"]) == 0


def test_main_check_flag_exits_two_when_missing(monkeypatch):
    monkeypatch.setattr(pr_radar, "preflight", lambda: [_bad("gh")])
    assert pr_radar.main(["--check"]) == 2


def test_main_aborts_with_two_when_prereqs_missing(monkeypatch):
    monkeypatch.setattr(pr_radar, "preflight", lambda: [_bad("gh")])
    # collect must never run if preflight fails.
    monkeypatch.setattr(
        pr_radar,
        "collect",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran work")),
    )
    assert pr_radar.main([]) == 2


