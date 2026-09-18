"""Tests for reading build states from GitHub and waiting for builds, with the fake gh."""

from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path

import pytest
from conftest import Env
from fake_gh import CHECK_RUNS, STATUS, CheckRun, CommitStatus, Poll, running
from helpers import commit, logger, sh, write_executable

from draupnir.errors import OperationError
from draupnir.github import (
    CANCELLED,
    FAILURE,
    MAX_FAILED_POLLS,
    MAX_POLL_SECONDS,
    NONE,
    PENDING,
    SUCCESS,
    TIMEOUT,
    BuildItem,
    BuildState,
    BuildTiming,
    PrMergeState,
    auto_merge_allowed,
    build_state,
    create_pr,
    estimate_build_seconds,
    find_open_pr,
    poll_delay,
    pr_merge_state,
    rebase_merge_allowed,
    require_gh,
    wait_for_build,
)
from draupnir.operations import check_build
from draupnir.setup import new_clone

# Waits that are not about the grace period or the timeout get long values for both, so a slow
# machine never ends them early. The polls themselves are fast.
FAST = BuildTiming(poll_seconds=0.01, grace_seconds=30, timeout_seconds=30)

GREEN_POLL = Poll(check_runs=[CheckRun("build")], statuses=[CommitStatus("jenkins")])


def _head(env: Env) -> str:
    return sh(env.main, "git", "rev-parse", "HEAD")


def _states(updates: list[BuildState]) -> list[str]:
    return [update.state for update in updates]


def test_build_state_combines_check_runs_and_commit_statuses(env: Env) -> None:
    sha = _head(env)
    env.gh.set_build(
        sha,
        [
            Poll(
                check_runs=[CheckRun("unit tests", url="https://ci.example.com/runs/1")],
                statuses=[CommitStatus("jenkins", url="https://jenkins.example.com/job/shop/7")],
            )
        ],
    )

    state = build_state(env.main, sha)

    assert state.sha == sha
    assert state.state == SUCCESS
    assert state.items == (
        BuildItem("unit tests", SUCCESS, "https://ci.example.com/runs/1"),
        BuildItem("jenkins", SUCCESS, "https://jenkins.example.com/job/shop/7"),
    )

    # A commit status that still runs keeps the whole build pending, even when every check run
    # is green, and the other way round.
    env.gh.set_build(
        sha, [Poll(check_runs=[CheckRun("build")], statuses=[CommitStatus("jenkins", "pending")])]
    )
    assert build_state(env.main, sha).state == PENDING

    env.gh.set_build(
        sha, [Poll(check_runs=[running("build")], statuses=[CommitStatus("jenkins", "success")])]
    )
    assert build_state(env.main, sha).state == PENDING


def test_build_state_is_failure_when_any_item_fails(env: Env) -> None:
    sha = _head(env)
    cases = [
        Poll(check_runs=[CheckRun("build"), CheckRun("lint", conclusion="failure")]),
        Poll(check_runs=[CheckRun("build", conclusion="timed_out")]),
        Poll(check_runs=[CheckRun("build", conclusion="cancelled")]),
        Poll(check_runs=[CheckRun("build")], statuses=[CommitStatus("jenkins", "failure")]),
        Poll(check_runs=[CheckRun("build")], statuses=[CommitStatus("jenkins", "error")]),
        # A failed job wins over a job that still runs.
        Poll(check_runs=[running("build")], statuses=[CommitStatus("jenkins", "failure")]),
    ]
    for poll in cases:
        env.gh.set_build(sha, [poll])
        state = build_state(env.main, sha)
        assert state.state == FAILURE, poll
        assert len(state.failed_items()) == 1, poll


def test_build_state_reads_every_page(env: Env) -> None:
    sha = _head(env)
    green_runs = [CheckRun(f"build-{n}") for n in range(150)]
    green_statuses = [CommitStatus(f"jenkins-{n}") for n in range(150)]
    cases = [
        Poll(check_runs=[*green_runs, CheckRun("lint", conclusion="failure")]),
        Poll(statuses=[*green_statuses, CommitStatus("deploy", "failure")]),
    ]
    for poll in cases:
        env.gh.set_build(sha, [poll])
        state = build_state(env.main, sha)
        assert state.state == FAILURE
        assert len(state.items) == 151


def test_build_state_counts_neutral_and_skipped_as_success(env: Env) -> None:
    sha = _head(env)
    env.gh.set_build(
        sha,
        [
            Poll(
                check_runs=[
                    CheckRun("build"),
                    CheckRun("optional checks", conclusion="neutral"),
                    CheckRun("deploy", conclusion="skipped"),
                ]
            )
        ],
    )

    state = build_state(env.main, sha)

    assert state.state == SUCCESS
    assert [item.state for item in state.items] == [SUCCESS, SUCCESS, SUCCESS]


def test_build_state_is_none_without_items(env: Env) -> None:
    sha = _head(env)

    state = build_state(env.main, sha)

    assert state.state == NONE
    assert state.items == ()


def test_build_state_raises_when_check_runs_fail_and_statuses_are_green(env: Env) -> None:
    sha = _head(env)
    env.gh.set_build(sha, [GREEN_POLL])
    env.gh.set_failing(CHECK_RUNS)

    with pytest.raises(OperationError) as excinfo:
        build_state(env.main, sha)

    message = str(excinfo.value)
    first_line = message.splitlines()[0]
    assert "check runs" in first_line
    assert "commit statuses" not in first_line
    assert sha[:10] in first_line
    assert "unknown" in first_line


def test_build_state_raises_when_statuses_fail_and_check_runs_are_green(env: Env) -> None:
    sha = _head(env)
    env.gh.set_build(sha, [GREEN_POLL])
    env.gh.set_failing(STATUS)

    with pytest.raises(OperationError) as excinfo:
        build_state(env.main, sha)

    first_line = str(excinfo.value).splitlines()[0]
    assert "commit statuses" in first_line
    assert "check runs" not in first_line
    assert "unknown" in first_line


def test_wait_treats_a_partial_read_as_a_failed_poll(env: Env) -> None:
    sha = _head(env)
    # The first poll has green check runs, but its commit statuses cannot be read. Used as a
    # state, it would look like a green build. The second poll shows that Jenkins failed.
    env.gh.set_build(
        sha,
        [
            Poll(check_runs=[CheckRun("build")], fail=[STATUS]),
            Poll(check_runs=[CheckRun("build")], statuses=[CommitStatus("jenkins", "failure")]),
        ],
    )
    lines, log = logger()
    updates: list[BuildState] = []

    state = wait_for_build(env.main, sha, log, threading.Event(), FAST, updates.append)

    assert state.state == FAILURE
    assert _states(updates) == [FAILURE]
    assert any("commit statuses" in line for line in lines)

    # When only one read keeps failing, the wait stops with the error, and never as success.
    env.gh.set_build(sha, [GREEN_POLL])
    env.gh.set_failing(STATUS)
    updates.clear()

    with pytest.raises(OperationError) as excinfo:
        wait_for_build(env.main, sha, log, threading.Event(), FAST, updates.append)

    assert updates == []
    assert str(MAX_FAILED_POLLS) in str(excinfo.value).splitlines()[0]
    assert env.gh.reads(sha, CHECK_RUNS) == MAX_FAILED_POLLS


def test_wait_ends_on_success_and_on_first_failure(env: Env) -> None:
    sha = _head(env)
    env.gh.set_build(
        sha,
        [
            Poll(check_runs=[running("build")]),
            Poll(check_runs=[running("build")], statuses=[CommitStatus("jenkins", "pending")]),
            GREEN_POLL,
        ],
    )
    lines, log = logger()
    updates: list[BuildState] = []

    state = wait_for_build(env.main, sha, log, threading.Event(), FAST, updates.append)

    assert state.state == SUCCESS
    assert _states(updates) == [PENDING, PENDING, SUCCESS]
    assert env.gh.reads(sha) == 3
    assert "  build: pending" in lines
    assert "  jenkins: pending" in lines
    assert "  build: success" in lines

    # The wait ends at the first failed job, while another job still runs.
    env.gh.set_build(
        sha,
        [
            Poll(check_runs=[running("build"), running("lint")]),
            Poll(
                check_runs=[
                    running("build"),
                    CheckRun("lint", conclusion="failure", url="https://ci.example.com/lint"),
                ]
            ),
            Poll(check_runs=[CheckRun("build"), CheckRun("lint", conclusion="failure")]),
        ],
    )
    lines.clear()
    updates.clear()

    state = wait_for_build(env.main, sha, log, threading.Event(), FAST, updates.append)

    assert state.state == FAILURE
    assert _states(updates) == [PENDING, FAILURE]
    assert env.gh.reads(sha) == 2
    assert "  lint: failure https://ci.example.com/lint" in lines


def test_wait_gives_none_after_the_grace_period(env: Env) -> None:
    sha = _head(env)
    lines, log = logger()
    updates: list[BuildState] = []
    timing = BuildTiming(poll_seconds=0.01, grace_seconds=0.05, timeout_seconds=30)

    state = wait_for_build(env.main, sha, log, threading.Event(), timing, updates.append)

    assert state.state == NONE
    assert _states(updates) == [NONE]
    assert any("No build has reported" in line for line in lines)

    # A build that reports within the grace period is followed to its end.
    env.gh.set_build(sha, [Poll(), Poll(), GREEN_POLL])

    state = wait_for_build(env.main, sha, log, threading.Event(), FAST)

    assert state.state == SUCCESS


def test_wait_gives_timeout_after_the_timeout(env: Env) -> None:
    sha = _head(env)
    env.gh.set_build(sha, [Poll(check_runs=[running("build")])])
    lines, log = logger()
    updates: list[BuildState] = []
    timing = BuildTiming(poll_seconds=0.01, grace_seconds=0.01, timeout_seconds=0.05)

    state = wait_for_build(env.main, sha, log, threading.Event(), timing, updates.append)

    assert state.state == TIMEOUT
    assert state.items == (BuildItem("build", PENDING, "https://ci.example.com/check-runs/build"),)
    # The dashboard keeps the last state it read, so a timeout is not sent as an update.
    assert set(_states(updates)) == {PENDING}
    assert any("keeps running" in line for line in lines)


def test_wait_can_be_stopped(env: Env) -> None:
    sha = _head(env)
    env.gh.set_build(sha, [Poll(check_runs=[running("build")])])
    lines, log = logger()
    cancel = threading.Event()
    updates: list[BuildState] = []

    def stop_at_the_first_state(state: BuildState) -> None:
        updates.append(state)
        cancel.set()

    timing = BuildTiming(poll_seconds=30, grace_seconds=30, timeout_seconds=60)
    state = wait_for_build(env.main, sha, log, cancel, timing, stop_at_the_first_state)

    # poll_seconds is 30, so the wait only ends this fast because the event stops it at once.
    assert state.state == CANCELLED
    assert state.items == updates[0].items
    assert _states(updates) == [PENDING]
    assert env.gh.reads(sha) == 1
    assert any("Stopped waiting" in line for line in lines)


def test_wait_survives_a_failed_poll(env: Env) -> None:
    sha = _head(env)
    failed = Poll(fail=[CHECK_RUNS, STATUS])
    # Two failed polls, a good one that resets the count, two more failed polls, then green.
    env.gh.set_build(
        sha,
        [failed, Poll(fail=[CHECK_RUNS]), Poll(check_runs=[running("build")]), failed, failed]
        + [GREEN_POLL],
    )
    lines, log = logger()
    updates: list[BuildState] = []

    state = wait_for_build(env.main, sha, log, threading.Event(), FAST, updates.append)

    assert state.state == SUCCESS
    assert _states(updates) == [PENDING, SUCCESS]
    assert env.gh.reads(sha) == 6
    assert sum("Reading the build state failed" in line for line in lines) == 4


def _path_without_gh(env: Env) -> str:
    """A PATH that still finds git, but not the fake gh or a gh installed on this machine."""
    git = shutil.which("git")
    assert git is not None
    only_git = env.tmp / "bin-without-gh"
    only_git.mkdir()
    (only_git / "git").symlink_to(git)
    return str(only_git)


# ----- Polling without hitting the rate limit ----------------------------------------------


class RecordingEvent(threading.Event):
    """A cancel event that records how long each wait would be, and returns at once."""

    def __init__(self) -> None:
        super().__init__()
        self.waits: list[float] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout or 0.0)
        return super().wait(0)


def _two_commits(env: Env) -> tuple[str, str]:
    """Adds two commits to main/, and returns the older and the newer sha."""
    older = commit(env.main, "one.txt", "one\n", "one")
    newer = commit(env.main, "two.txt", "two\n", "two")
    return older, newer


def _finished_build(minutes: int) -> Poll:
    return Poll(
        check_runs=[
            CheckRun(
                "build",
                started_at="2026-09-17T10:00:00Z",
                completed_at=f"2026-09-17T10:{minutes:02d}:00Z",
            )
        ]
    )


def test_poll_delay_waits_for_the_expected_end_then_polls_more_often() -> None:
    # No build yet: poll often, so the grace period works.
    assert poll_delay(0, 600, 15, build_seen=False) == 15
    # With an estimate: one wait until 80% of it, then a twentieth of it.
    assert poll_delay(0, 600, 15, build_seen=True) == 480
    assert poll_delay(100, 600, 15, build_seen=True) == 380
    assert poll_delay(500, 600, 15, build_seen=True) == 30
    assert poll_delay(6000, 7200, 15, build_seen=True) == MAX_POLL_SECONDS
    # Without an estimate: the pause grows with the time waited.
    assert poll_delay(30, None, 15, build_seen=True) == 15
    assert poll_delay(600, None, 15, build_seen=True) == 60
    assert poll_delay(5000, None, 15, build_seen=True) == MAX_POLL_SECONDS


def test_estimate_comes_from_the_finished_build_of_an_earlier_commit(env: Env) -> None:
    older, newer = _two_commits(env)
    env.gh.set_build(older, [_finished_build(10)])

    assert estimate_build_seconds(env.main, newer) == 600


def test_estimate_reads_commit_statuses_and_skips_an_unfinished_build(env: Env) -> None:
    oldest = commit(env.main, "zero.txt", "zero\n", "zero")
    older, newer = _two_commits(env)
    env.gh.set_build(older, [Poll(check_runs=[running("build")])])
    env.gh.set_build(
        oldest,
        [
            Poll(
                statuses=[
                    CommitStatus("jenkins", "pending", created_at="2026-09-17T10:00:00Z"),
                    CommitStatus("jenkins", "success", created_at="2026-09-17T10:05:00Z"),
                ]
            )
        ],
    )

    assert estimate_build_seconds(env.main, newer) == 300


def test_wait_sleeps_until_the_expected_end_of_the_build(env: Env) -> None:
    older, newer = _two_commits(env)
    env.gh.set_build(older, [_finished_build(10)])
    env.gh.set_build(newer, [Poll(check_runs=[running("build")]), GREEN_POLL])
    cancel = RecordingEvent()
    timing = BuildTiming(poll_seconds=0.01, grace_seconds=30, timeout_seconds=3600)
    lines, log = logger()

    state = wait_for_build(env.main, newer, log, cancel, timing)

    assert state.state == SUCCESS
    assert len(cancel.waits) == 1
    assert 470 < cancel.waits[0] <= 480
    assert env.gh.reads(newer) == 2
    assert any("about 10 minutes" in line for line in lines)


def test_wait_pauses_until_the_rate_limit_resets(env: Env) -> None:
    sha = _head(env)
    env.gh.set_build(sha, [GREEN_POLL])
    env.gh.set_rate_limit(remaining=10, reset=int(time.time()) + 300)
    cancel = RecordingEvent()
    timing = BuildTiming(poll_seconds=0.01, grace_seconds=30, timeout_seconds=3600)
    lines, log = logger()

    state = wait_for_build(env.main, sha, log, cancel, timing)

    assert state.state == SUCCESS
    assert 290 < cancel.waits[0] <= 302
    assert any("rate limit" in line for line in lines)


def test_missing_gh_gives_install_and_login_steps(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", _path_without_gh(env))

    with pytest.raises(OperationError) as excinfo:
        require_gh(env.main)

    message = str(excinfo.value)
    assert "gh was not found" in message
    assert "Install" in message
    assert "gh auth login" in message
    assert "--hostname" in message

    _, log = logger()
    with pytest.raises(OperationError) as excinfo:
        check_build(env.ws(), env.main, log)
    assert "gh auth login" in str(excinfo.value)


def test_missing_gh_login_names_the_host(env: Env) -> None:
    repo = env.tmp / "enterprise-clone"
    repo.mkdir()
    sh(repo, "git", "init", "-q")
    sh(repo, "git", "remote", "add", "origin", "https://ghe.example.com/org/repo.git")
    env.gh.set_logged_in(False)

    with pytest.raises(OperationError) as excinfo:
        require_gh(repo)

    assert str(excinfo.value) == (
        "gh is not logged in to ghe.example.com. Run gh auth login --hostname ghe.example.com "
        "and try again."
    )
    assert [call.args for call in env.gh.calls()] == [
        ["auth", "status", "--hostname", "ghe.example.com"]
    ]

    # Once gh is logged in, the check passes, and it runs only once per host per process.
    env.gh.set_logged_in(True)
    require_gh(repo)
    require_gh(repo)
    assert len(env.gh.calls()) == 2


def test_gh_runs_inside_the_clone(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/where-gh-runs", None, log)
    sha = sh(folder, "git", "rev-parse", "HEAD")
    env.gh.set_build(sha, [GREEN_POLL])
    # draupnir runs from another folder, so the working folder of gh must come from draupnir.
    monkeypatch.chdir(env.tmp)
    assert Path(os.getcwd()).resolve() != folder.resolve()

    state = check_build(env.ws(), folder, log)

    assert state.state == SUCCESS
    calls = env.gh.calls()
    assert [call.args for call in calls] == [
        ["api", f"repos/{{owner}}/{{repo}}/commits/{sha}/check-runs?per_page=100&page=1"],
        ["api", f"repos/{{owner}}/{{repo}}/commits/{sha}/status?per_page=100&page=1"],
    ]
    assert all(call.cwd.resolve() == folder.resolve() for call in calls)


# ----- Pull requests ---------------------------------------------------------------------


def _replace_gh(env: Env, script: str) -> None:
    """Puts a gh on PATH that runs script instead of the fake, for answers the fake never gives."""
    write_executable(env.bin / "gh", "#!/bin/sh\n" + script)


def test_find_open_pr_filters_on_head_and_base(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/two-prs", None, log)
    sh(folder, "git", "push", "-q", "-u", "origin", "feature/two-prs")
    env.gh.add_pr("feature/two-prs", "release")
    env.gh.add_pr("feature/other", "main")

    assert find_open_pr(folder, "feature/two-prs", "main") is None

    created = create_pr(folder, "feature/two-prs", "main", log)

    assert created.base == "main"
    assert created.head_sha == sh(folder, "git", "rev-parse", "HEAD")
    assert find_open_pr(folder, "feature/two-prs", "main") == created
    release = find_open_pr(folder, "feature/two-prs", "release")
    assert release is not None and release.number != created.number


def test_find_open_pr_refuses_a_pr_into_another_base(env: Env) -> None:
    _replace_gh(
        env,
        'echo \'[{"number": 4, "url": "https://github.example.com/acme/shop/pull/4", '
        '"headRefOid": "abc", "baseRefName": "release"}]\'\n',
    )

    with pytest.raises(OperationError) as excinfo:
        find_open_pr(env.main, "feature/x", "main")

    message = str(excinfo.value)
    assert "#4" in message
    assert "release" in message
    assert "does not reuse or merge it" in message


def test_missing_json_field_names_the_field_and_the_gh_version(env: Env) -> None:
    _replace_gh(
        env,
        'if [ "$1" = "--version" ]; then echo "gh version 1.0.0 (old)"; exit 0; fi\necho "{}"\n',
    )

    with pytest.raises(OperationError) as excinfo:
        rebase_merge_allowed(env.main)
    message = str(excinfo.value)
    assert "'rebaseMergeAllowed'" in message
    assert "gh version 1.0.0 (old)" in message

    with pytest.raises(OperationError) as excinfo:
        pr_merge_state(env.main, 3)
    message = str(excinfo.value)
    named = [
        field for field in ("state", "reviewDecision", "headRefOid") if f"'{field}'" in message
    ]
    assert len(named) == 1, message
    assert "answered without the field" in message
    assert "gh version 1.0.0 (old)" in message


def test_pr_merge_state_reads_every_field(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/view", None, log)
    sh(folder, "git", "push", "-q", "-u", "origin", "feature/view")
    number = env.gh.add_pr(
        "feature/view", "main", merge_states=["BLOCKED"], review_decision="REVIEW_REQUIRED"
    )

    state = pr_merge_state(folder, number)

    assert state == PrMergeState(
        state="OPEN",
        is_draft=False,
        merge_state_status="BLOCKED",
        review_decision="REVIEW_REQUIRED",
        head_sha=sh(folder, "git", "rev-parse", "HEAD"),
        base="main",
        auto_merge=False,
    )


def test_auto_merge_allowed_is_unknown_when_github_leaves_the_field_out(env: Env) -> None:
    assert auto_merge_allowed(env.main) is True
    env.gh.set_allow_auto_merge(False)
    assert auto_merge_allowed(env.main) is False
    env.gh.set_allow_auto_merge(None)
    assert auto_merge_allowed(env.main) is None
