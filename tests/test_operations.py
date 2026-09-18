from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import Env
from fake_gh import CHECK_RUNS, CheckRun, CommitStatus, Poll, running
from helpers import (
    FakeStdin,
    append_config,
    commit,
    logger,
    push_from_other_clone,
    sh,
    write_executable,
)

from draupnir.cli import main
from draupnir.errors import OperationError, RebaseConflict
from draupnir.github import SUCCESS, BuildState
from draupnir.operations import (
    AUTO_MERGE,
    MERGED,
    fast_forward_main,
    fetch_all,
    pr_build_and_merge,
    push,
    rebase_on_main,
    update_main_clone,
)
from draupnir.setup import new_clone, remove_clone, unsaved_work
from draupnir.status import operation_in_progress


def _break_fetch(folder: Path) -> None:
    """Makes `git fetch` fail in folder, without touching its origin url.

    `git remote get-url origin` reads the url key on its own, so a workspace still
    recognizes folder as a clone of the project (see Workspace.project_paths). Only the
    fetch refspec is broken, so `git fetch` itself fails.
    """
    config = folder / ".git" / "config"
    lines = config.read_text().splitlines()
    new_lines = [
        "\tfetch = broken-refspec" if line.strip().startswith("fetch = ") else line
        for line in lines
    ]
    config.write_text("\n".join(new_lines) + "\n")


def test_fetch_all_updates_every_clone_and_names_the_ones_that_failed(env: Env) -> None:
    _, log = logger()
    broken = new_clone(env.ws(), "feature/broken", None, log)
    _break_fetch(broken)

    new_main_sha = push_from_other_clone(env, "main")

    lines, collecting_log = logger()
    with pytest.raises(OperationError) as excinfo:
        fetch_all(env.ws(), collecting_log)

    message = str(excinfo.value)
    assert broken.name in message
    assert env.main.name not in message

    # main/ was fetched even though the other clone's fetch failed.
    assert sh(env.main, "git", "rev-parse", "origin/main") == new_main_sha
    assert any("git fetch" in line for line in lines)


def _push_conflicting_change_to_main(env: Env) -> None:
    """Pushes a commit to main that changes App.java, so a rebase onto it can conflict."""
    other = env.tmp / "other-main-for-conflict"
    sh(env.tmp, "git", "clone", "-q", "--branch", "main", str(env.remote), str(other))
    commit(other, "App.java", "line 1 changed on main\nline 2\nline 3\n", "change on main")
    sh(other, "git", "push", "-q", "origin", "main")


def test_rebase_puts_the_branch_on_top_of_main(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/onto-main", None, log)
    commit(folder, "feature.txt", "feature work\n", "add feature file")
    new_main_sha = push_from_other_clone(env, "main")

    lines, collecting_log = logger()
    rebase_on_main(env.ws(), folder, collecting_log)

    sh(folder, "git", "merge-base", "--is-ancestor", new_main_sha, "HEAD")
    assert any("top of origin/main" in line for line in lines)


def test_rebase_refuses_changed_tracked_files(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/dirty", None, log)
    (folder / "App.java").write_text("changed but not committed\n")

    with pytest.raises(OperationError) as excinfo:
        rebase_on_main(env.ws(), folder, log)

    assert "changed" in str(excinfo.value)


def test_rebase_conflict_stays_in_progress_and_lists_the_files(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/conflict", None, log)
    _push_conflicting_change_to_main(env)
    commit(folder, "App.java", "line 1 changed on branch\nline 2\nline 3\n", "change on branch")

    lines, collecting_log = logger()
    with pytest.raises(RebaseConflict) as excinfo:
        rebase_on_main(env.ws(), folder, collecting_log)

    assert excinfo.value.files == ["App.java"]
    assert any("App.java" in line for line in lines)
    assert operation_in_progress(folder) == "rebase"


def test_actions_refuse_while_a_rebase_is_in_progress(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/blocked", None, log)
    _push_conflicting_change_to_main(env)
    commit(folder, "App.java", "line 1 changed on branch\nline 2\nline 3\n", "change on branch")

    with pytest.raises(RebaseConflict):
        rebase_on_main(env.ws(), folder, log)

    with pytest.raises(OperationError) as excinfo:
        push(env.ws(), folder, log)
    assert "rebase" in str(excinfo.value)

    with pytest.raises(OperationError) as excinfo:
        rebase_on_main(env.ws(), folder, log)
    assert "rebase" in str(excinfo.value)


def test_push_sets_the_upstream_of_a_new_branch(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/first-push", None, log)
    commit(folder, "feature.txt", "feature work\n", "add feature file")

    sha = push(env.ws(), folder, log)

    assert sha == sh(folder, "git", "rev-parse", "HEAD")
    upstream = sh(folder, "git", "rev-parse", "--abbrev-ref", "feature/first-push@{upstream}")
    assert upstream == "origin/feature/first-push"
    assert sh(env.remote, "git", "rev-parse", "feature/first-push") == sha


def test_push_after_rebase_uses_force_with_lease(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/needs-force", None, log)
    commit(folder, "feature.txt", "feature work\n", "add feature file")
    first_sha = push(env.ws(), folder, log)

    push_from_other_clone(env, "main")
    rebase_on_main(env.ws(), folder, log)

    lines, collecting_log = logger()
    sha = push(env.ws(), folder, collecting_log)

    assert sha != first_sha
    assert sh(env.remote, "git", "rev-parse", "feature/needs-force") == sha
    assert any("--force-with-lease" in line and "--force-if-includes" in line for line in lines)


def test_push_of_the_default_branch_never_uses_force(env: Env) -> None:
    lines, collecting_log = logger()
    sha = push(env.ws(), env.main, collecting_log)

    assert sha == sh(env.main, "git", "rev-parse", "HEAD")
    assert not any("--force" in line for line in lines)


def test_push_refuses_when_github_has_new_commits(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/racing", None, log)
    commit(folder, "feature.txt", "feature work\n", "add feature file")
    push(env.ws(), folder, log)

    other_sha = push_from_other_clone(env, "feature/racing")
    rebase_on_main(env.ws(), folder, log)

    with pytest.raises(OperationError) as excinfo:
        push(env.ws(), folder, log)

    assert "not integrated" in str(excinfo.value)
    assert sh(env.remote, "git", "rev-parse", "feature/racing") == other_sha


def test_rebase_command_prints_the_conflicting_files_and_exits_with_one(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/cli-conflict", None, log)
    _push_conflicting_change_to_main(env)
    commit(folder, "App.java", "line 1 changed on branch\nline 2\nline 3\n", "change on branch")
    monkeypatch.chdir(folder)

    code = main(["rebase"])

    assert code == 1
    out = capsys.readouterr().out
    assert "App.java" in out
    assert "draupnir open" in out
    assert "git rebase --continue" in out


def test_build_command_exit_code_follows_the_build_state(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/cli-build", None, log)
    sha = sh(folder, "git", "rev-parse", "HEAD")
    monkeypatch.chdir(folder)

    env.gh.set_build(
        sha,
        [
            Poll(
                check_runs=[CheckRun("unit tests", url="https://ci.example.com/runs/11")],
                statuses=[CommitStatus("jenkins", url="https://jenkins.example.com/job/shop/4")],
            )
        ],
    )
    assert main(["build"]) == 0
    out = capsys.readouterr().out
    assert "success" in out
    assert "unit tests: success https://ci.example.com/runs/11" in out
    assert "jenkins: success https://jenkins.example.com/job/shop/4" in out

    env.gh.set_build(
        sha, [Poll(check_runs=[CheckRun("unit tests", conclusion="failure", url="https://f")])]
    )
    assert main(["build"]) == 3
    out = capsys.readouterr().out
    assert "failure" in out
    assert "https://f" in out

    env.gh.set_build(sha, [Poll(check_runs=[running("unit tests")])])
    assert main(["build"]) == 3
    assert "pending" in capsys.readouterr().out

    env.gh.set_build(sha, [])
    assert main(["build"]) == 3
    assert "none" in capsys.readouterr().out

    # A folder argument works from the workspace root too.
    monkeypatch.chdir(env.root)
    env.gh.set_build(sha, [Poll(check_runs=[CheckRun("unit tests")])])
    assert main(["build", folder.name]) == 0
    capsys.readouterr()

    # A build state that cannot be read is not a build result: the command stops with 1.
    env.gh.set_failing(CHECK_RUNS)
    assert main(["build", folder.name]) == 1
    captured = capsys.readouterr()
    assert "check runs" in captured.err


def test_push_follow_command_prints_job_changes_and_exits_with_the_build_result(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/cli-follow", None, log)
    sha = commit(folder, "feature.txt", "feature work\n", "add feature file")
    monkeypatch.chdir(folder)

    env.gh.set_build(
        sha,
        [
            Poll(check_runs=[running("build")]),
            Poll(check_runs=[CheckRun("build", conclusion="failure", url="https://ci/build/9")]),
        ],
    )
    assert main(["push", "--follow"]) == 3
    out = capsys.readouterr().out
    assert sh(env.remote, "git", "rev-parse", "feature/cli-follow") == sha
    assert "  build: pending" in out
    assert "  build: failure https://ci/build/9" in out
    assert "Failed: build https://ci/build/9" in out

    env.gh.set_build(
        sha, [Poll(check_runs=[running("build")]), Poll(check_runs=[CheckRun("build")])]
    )
    assert main(["push", "--follow"]) == 0
    out = capsys.readouterr().out
    assert "  build: success" in out
    assert "is green" in out


def test_ff_main_pushes_head_to_the_default_branch_and_updates_main(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/ff", None, log)
    sha = commit(folder, "feature.txt", "feature work\n", "add feature file")
    env.gh.set_build(sha, [Poll(check_runs=[CheckRun("build")])])

    lines, collecting_log = logger()
    fast_forward_main(env.ws(), folder, collecting_log)

    assert sh(env.remote, "git", "rev-parse", "main") == sha
    assert sh(env.main, "git", "rev-parse", "HEAD") == sha
    assert (env.main / "feature.txt").is_file()
    assert any("now points" in line for line in lines)
    assert any("now on main" in line for line in lines)


def test_ff_main_refuses_when_not_on_top_of_main(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/behind", None, log)
    commit(folder, "feature.txt", "feature work\n", "add feature file")
    push_from_other_clone(env, "main")

    before = sh(env.remote, "git", "rev-parse", "main")
    with pytest.raises(OperationError) as excinfo:
        fast_forward_main(env.ws(), folder, log)

    assert "on top of" in str(excinfo.value)
    assert sh(env.remote, "git", "rev-parse", "main") == before


def test_ff_main_refuses_without_new_commits(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/no-commits", None, log)

    before = sh(env.remote, "git", "rev-parse", "main")
    with pytest.raises(OperationError) as excinfo:
        fast_forward_main(env.ws(), folder, log)

    assert "nothing to fast-forward" in str(excinfo.value)
    assert sh(env.remote, "git", "rev-parse", "main") == before


def test_ff_main_refuses_a_failed_build_and_names_the_job(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/failed-build", None, log)
    sha = commit(folder, "feature.txt", "feature work\n", "add feature file")
    env.gh.set_build(
        sha,
        [Poll(check_runs=[CheckRun("unit tests", conclusion="failure", url="https://ci/1")])],
    )

    with pytest.raises(OperationError) as excinfo:
        fast_forward_main(env.ws(), folder, log)

    message = str(excinfo.value)
    assert "failed" in message
    assert "unit tests" in message
    assert "https://ci/1" in message
    assert sh(env.remote, "git", "rev-parse", "main") != sha


def test_ff_main_refuses_a_pending_build(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/pending-build", None, log)
    sha = commit(folder, "feature.txt", "feature work\n", "add feature file")
    env.gh.set_build(sha, [Poll(check_runs=[running("unit tests")])])

    with pytest.raises(OperationError) as excinfo:
        fast_forward_main(env.ws(), folder, log)

    assert "still running" in str(excinfo.value)
    assert sh(env.remote, "git", "rev-parse", "main") != sha


def test_ff_main_refuses_when_no_build_exists(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/no-build", None, log)
    sha = commit(folder, "feature.txt", "feature work\n", "add feature file")

    with pytest.raises(OperationError) as excinfo:
        fast_forward_main(env.ws(), folder, log)

    assert "No build has reported" in str(excinfo.value)
    assert sh(env.remote, "git", "rev-parse", "main") != sha


def test_ff_main_refuses_when_the_build_state_is_only_partly_readable(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/partial-read", None, log)
    commit(folder, "feature.txt", "feature work\n", "add feature file")
    env.gh.set_failing(CHECK_RUNS)

    before = sh(env.remote, "git", "rev-parse", "main")
    with pytest.raises(OperationError) as excinfo:
        fast_forward_main(env.ws(), folder, log)

    message = str(excinfo.value)
    assert "could not read the build state" in message
    assert sh(env.remote, "git", "rev-parse", "main") == before


def test_ff_main_without_the_build_check_pushes_anyway(env: Env) -> None:
    append_config(env.root, "\n[actions]\nfast-forward-needs-green-build = false\n")
    _, log = logger()
    folder = new_clone(env.ws(), "feature/skip-build", None, log)
    sha = commit(folder, "feature.txt", "feature work\n", "add feature file")
    # A build that would refuse the fast-forward if it were ever read.
    env.gh.set_failing(CHECK_RUNS)

    fast_forward_main(env.ws(), folder, log)

    assert sh(env.remote, "git", "rev-parse", "main") == sha
    assert env.gh.calls() == []


def test_ff_main_explains_a_push_that_github_refuses(env: Env) -> None:
    write_executable(
        env.remote / "hooks" / "pre-receive",
        "#!/bin/sh\necho 'protected branch hook declined' >&2\nexit 1\n",
    )
    _, log = logger()
    folder = new_clone(env.ws(), "feature/protected", None, log)
    sha = commit(folder, "feature.txt", "feature work\n", "add feature file")
    env.gh.set_build(sha, [Poll(check_runs=[CheckRun("build")])])

    with pytest.raises(OperationError) as excinfo:
        fast_forward_main(env.ws(), folder, log)

    message = str(excinfo.value)
    assert "draupnir pr" in message
    assert "fast-forward-main" in message


def test_ff_main_succeeds_when_main_folder_cannot_be_updated(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/ff-broken-main", None, log)
    sha = commit(folder, "feature.txt", "feature work\n", "add feature file")
    env.gh.set_build(sha, [Poll(check_runs=[CheckRun("build")])])
    _break_fetch(env.main)

    lines, collecting_log = logger()
    fast_forward_main(env.ws(), folder, collecting_log)

    assert sh(env.remote, "git", "rev-parse", "main") == sha
    assert any("could not update" in line for line in lines)


def test_update_main_skips_a_main_folder_with_changes(env: Env) -> None:
    (env.main / "App.java").write_text("changed but not committed\n")
    new_sha = push_from_other_clone(env, "main")

    lines, collecting_log = logger()
    update_main_clone(env.ws(), collecting_log)

    assert sh(env.main, "git", "rev-parse", "HEAD") != new_sha
    assert any("not updated" in line for line in lines)
    assert any("git pull" in line for line in lines)


def test_ff_main_command_refuses_when_disabled_in_config(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    append_config(env.root, "\n[actions]\nfast-forward-main = false\n")
    _, log = logger()
    folder = new_clone(env.ws(), "feature/disabled", None, log)
    monkeypatch.chdir(folder)

    code = main(["ff-main", "--yes"])

    assert code == 1
    err = capsys.readouterr().err
    assert "fast-forward-main" in err
    assert "draupnir pr" in err


# ----- The pull request flow ---------------------------------------------------------------


def _feature_clone(env: Env, branch: str) -> tuple[Path, str]:
    """A new clone of branch with one commit, and the sha of that commit."""
    _, log = logger()
    folder = new_clone(env.ws(), branch, None, log)
    sha = commit(folder, "feature.txt", f"work on {branch}\n", f"add work on {branch}")
    return folder, sha


def _green(env: Env, sha: str) -> None:
    env.gh.set_build(
        sha, [Poll(check_runs=[running("build")]), Poll(check_runs=[CheckRun("build")])]
    )


def _remote_branch(env: Env, branch: str) -> str:
    return sh(env.remote, "git", "branch", "--list", branch)


def _gh_calls(env: Env, *prefix: str) -> list[list[str]]:
    return [call.args for call in env.gh.calls() if call.args[: len(prefix)] == list(prefix)]


def _on_first_update(action: Callable[[], None]) -> Callable[[BuildState], None]:
    """An on_update callback that runs action once, while the flow waits for the build."""
    done: list[bool] = []

    def on_update(_state: BuildState) -> None:
        if not done:
            done.append(True)
            action()

    return on_update


def _pr_number_for(env: Env, branch: str) -> int:
    numbers = [pr.number for pr in env.gh.pull_requests() if pr.head == branch]
    assert len(numbers) == 1, numbers
    return numbers[0]


def test_pr_flow_merges_after_a_green_build_and_updates_main(env: Env) -> None:
    branch = "feature/pr-green"
    folder, sha = _feature_clone(env, branch)
    _green(env, sha)
    main_before = sh(env.remote, "git", "rev-parse", "main")

    lines, log = logger()
    outcome = pr_build_and_merge(env.ws(), folder, log, threading.Event())

    assert outcome.result == MERGED
    assert outcome.build.state == SUCCESS
    assert outcome.build.sha == sha
    pr = env.gh.pull_request(outcome.pr.number)
    assert pr.state == "MERGED"
    assert pr.base == "main"
    assert pr.merged_head_sha == sha
    # GitHub's rebase merge put the change on main as a new commit.
    main_after = sh(env.remote, "git", "rev-parse", "main")
    assert main_after not in (main_before, sha)
    assert sh(env.remote, "git", "show", "main:feature.txt") == f"work on {branch}"
    # main/ is updated, and the tool did not delete the branch on GitHub (D17).
    assert sh(env.main, "git", "rev-parse", "HEAD") == main_after
    assert (env.main / "feature.txt").is_file()
    assert _remote_branch(env, branch) != ""
    merges = _gh_calls(env, "pr", "merge")
    assert merges == [["pr", "merge", str(pr.number), "--rebase", "--match-head-commit", sha]]
    assert not any("--delete-branch" in args for args in merges)
    assert any(f"Created pull request #{pr.number}" in line for line in lines)
    assert f"Pull request #{pr.number} is merged." in lines
    assert any("now on main" in line for line in lines)


def test_pr_flow_keeps_the_pr_open_after_a_red_build(env: Env) -> None:
    folder, sha = _feature_clone(env, "feature/pr-red")
    env.gh.set_build(
        sha, [Poll(check_runs=[CheckRun("unit tests", conclusion="failure", url="https://ci/7")])]
    )
    main_before = sh(env.remote, "git", "rev-parse", "main")

    _, log = logger()
    with pytest.raises(OperationError) as excinfo:
        pr_build_and_merge(env.ws(), folder, log, threading.Event())

    message = str(excinfo.value)
    number = _pr_number_for(env, "feature/pr-red")
    assert "failure" in message
    assert f"pull request #{number}" in message
    assert "stays open" in message
    assert "unit tests https://ci/7" in message
    assert env.gh.pull_request(number).state == "OPEN"
    assert _gh_calls(env, "pr", "merge") == []
    assert sh(env.remote, "git", "rev-parse", "main") == main_before


def test_pr_flow_keeps_the_pr_open_when_the_wait_is_stopped(env: Env) -> None:
    folder, sha = _feature_clone(env, "feature/pr-stopped")
    env.gh.set_build(sha, [Poll(check_runs=[running("build")])])
    cancel = threading.Event()

    _, log = logger()
    with pytest.raises(OperationError) as excinfo:
        pr_build_and_merge(env.ws(), folder, log, cancel, _on_first_update(cancel.set))

    message = str(excinfo.value)
    number = _pr_number_for(env, "feature/pr-stopped")
    assert message.startswith("draupnir stopped waiting for the build")
    assert f"pull request #{number}" in message
    assert env.gh.pull_request(number).state == "OPEN"
    assert _gh_calls(env, "pr", "merge") == []


def test_pr_flow_reuses_an_open_pr(env: Env) -> None:
    branch = "feature/pr-reuse"
    folder, first_sha = _feature_clone(env, branch)
    _, log = logger()
    push(env.ws(), folder, log)
    number = env.gh.add_pr(branch, "main")
    sha = commit(folder, "more.txt", "more work\n", "more work")
    assert sha != first_sha
    _green(env, sha)

    lines, log = logger()
    outcome = pr_build_and_merge(env.ws(), folder, log, threading.Event())

    assert outcome.result == MERGED
    assert outcome.pr.number == number
    assert _gh_calls(env, "pr", "create") == []
    assert len(env.gh.pull_requests()) == 1
    assert env.gh.pull_request(number).state == "MERGED"
    assert any(f"Using the open pull request #{number}" in line for line in lines)


def test_pr_flow_ignores_an_open_pr_for_the_same_branch_into_another_base(env: Env) -> None:
    branch = "feature/pr-two-bases"
    folder, sha = _feature_clone(env, branch)
    _, log = logger()
    push(env.ws(), folder, log)
    release_number = env.gh.add_pr(branch, "release")
    _green(env, sha)

    outcome = pr_build_and_merge(env.ws(), folder, log, threading.Event())

    assert outcome.result == MERGED
    assert outcome.pr.number != release_number
    assert outcome.pr.base == "main"
    assert len(_gh_calls(env, "pr", "create")) == 1
    merged = env.gh.pull_request(outcome.pr.number)
    assert (merged.state, merged.base) == ("MERGED", "main")
    release = env.gh.pull_request(release_number)
    assert (release.state, release.base, release.auto_merge) == ("OPEN", "release", False)
    assert not any(str(release_number) in args for args in _gh_calls(env, "pr", "merge"))
    assert not any(str(release_number) in args for args in _gh_calls(env, "pr", "view"))


def test_pr_flow_stops_when_the_base_changed_during_the_build(env: Env) -> None:
    branch = "feature/pr-new-base"
    folder, sha = _feature_clone(env, branch)
    _green(env, sha)

    def change_base() -> None:
        env.gh.set_pr_base(_pr_number_for(env, branch), "release")

    _, log = logger()
    with pytest.raises(OperationError) as excinfo:
        pr_build_and_merge(env.ws(), folder, log, threading.Event(), _on_first_update(change_base))

    message = str(excinfo.value)
    number = _pr_number_for(env, branch)
    assert f"changed the base of pull request #{number} to release" in message
    assert env.gh.pull_request(number).state == "OPEN"
    assert _gh_calls(env, "pr", "merge") == []


def test_pr_flow_enables_auto_merge_when_a_review_is_required(env: Env) -> None:
    branch = "feature/pr-review"
    folder, sha = _feature_clone(env, branch)
    _green(env, sha)
    env.gh.set_new_pr(merge_states=["BLOCKED"], review_decision="REVIEW_REQUIRED")
    main_before = sh(env.remote, "git", "rev-parse", "main")

    lines, log = logger()
    outcome = pr_build_and_merge(env.ws(), folder, log, threading.Event())

    number = outcome.pr.number
    assert outcome.result == AUTO_MERGE
    assert _gh_calls(env, "pr", "merge") == [
        ["pr", "merge", str(number), "--rebase", "--auto", "--match-head-commit", sha]
    ]
    pr = env.gh.pull_request(number)
    assert (pr.state, pr.auto_merge) == ("OPEN", True)
    expected = f"Auto-merge is on. GitHub merges pull request #{number} after the required review."
    assert outcome.message() == expected
    assert expected in lines
    # Nothing is merged yet, so main/ and main on GitHub stay where they were.
    assert sh(env.remote, "git", "rev-parse", "main") == main_before
    assert sh(env.main, "git", "rev-parse", "HEAD") == main_before


def test_pr_flow_merges_at_once_when_the_review_is_already_given(env: Env) -> None:
    folder, sha = _feature_clone(env, "feature/pr-approved")
    _green(env, sha)
    env.gh.set_new_pr(merge_states=["CLEAN"], review_decision="APPROVED")

    _, log = logger()
    outcome = pr_build_and_merge(env.ws(), folder, log, threading.Event())

    assert outcome.result == MERGED
    merges = _gh_calls(env, "pr", "merge")
    assert len(merges) == 1
    assert "--auto" not in merges[0]
    assert env.gh.pull_request(outcome.pr.number).state == "MERGED"


def test_pr_flow_explains_when_auto_merge_is_turned_off(env: Env) -> None:
    folder, sha = _feature_clone(env, "feature/pr-no-auto")
    _green(env, sha)
    env.gh.set_new_pr(merge_states=["BLOCKED"], review_decision="REVIEW_REQUIRED")
    env.gh.set_allow_auto_merge(False)

    _, log = logger()
    with pytest.raises(OperationError) as excinfo:
        pr_build_and_merge(env.ws(), folder, log, threading.Event())

    message = str(excinfo.value)
    number = _pr_number_for(env, "feature/pr-no-auto")
    assert message.startswith("Auto-merge is turned off for this repository")
    assert f"pull request #{number}" in message
    assert "stays open" in message
    assert "after the required review" in message
    assert _gh_calls(env, "api", "repos/{owner}/{repo}") != []
    pr = env.gh.pull_request(number)
    assert (pr.state, pr.auto_merge) == ("OPEN", False)


def test_pr_flow_stops_when_the_pr_head_changed_during_the_build(env: Env) -> None:
    branch = "feature/pr-new-head"
    folder, sha = _feature_clone(env, branch)
    _green(env, sha)
    pushed: list[str] = []

    def push_from_a_teammate() -> None:
        pushed.append(push_from_other_clone(env, branch))

    _, log = logger()
    with pytest.raises(OperationError) as excinfo:
        pr_build_and_merge(
            env.ws(), folder, log, threading.Event(), _on_first_update(push_from_a_teammate)
        )

    message = str(excinfo.value)
    number = _pr_number_for(env, branch)
    assert message.startswith(f"Someone pushed to {branch}")
    assert pushed[0][:10] in message
    assert sha[:10] in message
    assert env.gh.pull_request(number).state == "OPEN"
    assert _gh_calls(env, "pr", "merge") == []


def test_pr_flow_stops_when_github_wants_the_branch_up_to_date(env: Env) -> None:
    folder, sha = _feature_clone(env, "feature/pr-behind")
    _green(env, sha)
    env.gh.set_new_pr(merge_states=["BEHIND"])

    _, log = logger()
    with pytest.raises(OperationError) as excinfo:
        pr_build_and_merge(env.ws(), folder, log, threading.Event())

    message = str(excinfo.value)
    assert "up to date with main" in message
    assert "Rebase on main" in message
    assert env.gh.pull_request(_pr_number_for(env, "feature/pr-behind")).state == "OPEN"
    assert _gh_calls(env, "pr", "merge") == []


def test_pr_flow_checks_that_rebase_merge_is_allowed_before_pushing(env: Env) -> None:
    folder, sha = _feature_clone(env, "feature/pr-order")
    _green(env, sha)
    env.gh.record_pushes(env.remote)

    _, log = logger()
    outcome = pr_build_and_merge(env.ws(), folder, log, threading.Event())

    assert outcome.result == MERGED
    order = [" ".join(call.args[:2]) for call in env.gh.calls()]
    assert order.count("event push") == 1
    assert order.index("repo view") < order.index("event push") < order.index("pr list")
    repo_view = _gh_calls(env, "repo", "view")
    assert repo_view == [["repo", "view", "--json", "rebaseMergeAllowed"]]


def test_pr_flow_stops_before_pushing_when_rebase_merge_is_not_allowed(env: Env) -> None:
    branch = "feature/pr-no-rebase"
    folder, _ = _feature_clone(env, branch)
    env.gh.set_rebase_merge_allowed(False)
    env.gh.record_pushes(env.remote)

    _, log = logger()
    with pytest.raises(OperationError) as excinfo:
        pr_build_and_merge(env.ws(), folder, log, threading.Event())

    assert str(excinfo.value).startswith(
        "The repository does not allow rebase merges, and draupnir merges only with rebase. "
        "Merge the pull request on GitHub."
    )
    assert _remote_branch(env, branch) == ""
    assert [call.args[0] for call in env.gh.calls()] == ["repo"]


def test_merge_after_rebase_merge_and_branch_deletion_allows_removal(env: Env) -> None:
    branch = "feature/pr-then-remove"
    folder, _ = _feature_clone(env, branch)
    sha = commit(folder, "second.txt", "second\n", "second commit")
    push_from_other_clone(env, "main")
    _green(env, sha)
    env.gh.set_delete_branch_on_merge(True)

    lines, log = logger()
    outcome = pr_build_and_merge(env.ws(), folder, log, threading.Event())

    assert outcome.result == MERGED
    assert any("is not on top of origin/main" in line for line in lines)
    # GitHub rewrote both commits onto main and deleted the branch.
    assert _remote_branch(env, branch) == ""
    local = sh(folder, "git", "rev-list", "origin/main..HEAD").splitlines()
    assert len(local) == 2
    assert sh(env.remote, "git", "show", "main:second.txt") == "second"
    assert unsaved_work(env.ws(), folder, log) == []

    remove_clone(env.ws(), folder, False, log)
    assert not folder.exists()


def test_pr_command_with_remove_deletes_the_clone_after_the_merge(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    folder, sha = _feature_clone(env, "feature/pr-cli-remove")
    _green(env, sha)
    monkeypatch.chdir(folder)
    # A terminal would be asked no question, because --remove answers it.
    monkeypatch.setattr(sys, "stdin", FakeStdin(is_a_tty=True))

    code = main(["pr", "--yes", "--remove"])

    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert not folder.exists()
    assert sh(env.remote, "git", "show", "main:feature.txt") == "work on feature/pr-cli-remove"
    assert "is merged." in captured.out
    assert f"Deleted {folder}" in captured.out


def test_pr_command_without_a_terminal_keeps_the_clone_and_prints_the_command(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    folder, sha = _feature_clone(env, "feature/pr-cli-keep")
    _green(env, sha)
    monkeypatch.chdir(folder)
    monkeypatch.setattr(sys, "stdin", FakeStdin(is_a_tty=False))

    code = main(["pr", "--yes"])

    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert folder.is_dir()
    assert "is merged." in captured.out
    assert f"draupnir remove {folder.name}" in captured.out


def test_pr_flow_reads_an_unknown_merge_state_again(env: Env) -> None:
    folder, sha = _feature_clone(env, "feature/pr-unknown")
    _green(env, sha)
    env.gh.set_new_pr(merge_states=["UNKNOWN", "UNKNOWN", "CLEAN"])

    lines, log = logger()
    outcome = pr_build_and_merge(env.ws(), folder, log, threading.Event())

    assert outcome.result == MERGED
    assert len(_gh_calls(env, "pr", "view")) == 3
    assert sum("still working out" in line for line in lines) == 2


def test_pr_flow_stops_when_the_merge_state_stays_unknown(env: Env) -> None:
    folder, sha = _feature_clone(env, "feature/pr-always-unknown")
    _green(env, sha)
    env.gh.set_new_pr(merge_states=["UNKNOWN"])

    _, log = logger()
    with pytest.raises(OperationError) as excinfo:
        pr_build_and_merge(env.ws(), folder, log, threading.Event())

    assert "did not work out whether pull request" in str(excinfo.value)
    # The first read, and five more.
    assert len(_gh_calls(env, "pr", "view")) == 6
    assert _gh_calls(env, "pr", "merge") == []


def test_pr_flow_stops_for_a_draft_and_for_conflicts(env: Env) -> None:
    folder, sha = _feature_clone(env, "feature/pr-draft")
    _green(env, sha)
    env.gh.set_new_pr(merge_states=["DRAFT"], draft=True)
    _, log = logger()

    with pytest.raises(OperationError) as excinfo:
        pr_build_and_merge(env.ws(), folder, log, threading.Event())
    assert "is a draft" in str(excinfo.value)
    assert "ready for review" in str(excinfo.value)

    other, other_sha = _feature_clone(env, "feature/pr-dirty")
    _green(env, other_sha)
    env.gh.set_new_pr(merge_states=["DIRTY"])

    with pytest.raises(OperationError) as excinfo:
        pr_build_and_merge(env.ws(), other, log, threading.Event())
    assert "has conflicts with main" in str(excinfo.value)
    assert "resolve the conflicts" in str(excinfo.value)
    assert _gh_calls(env, "pr", "merge") == []


def test_pr_flow_refuses_main(env: Env) -> None:
    _, log = logger()
    with pytest.raises(OperationError) as excinfo:
        pr_build_and_merge(env.ws(), env.main, log, threading.Event())

    assert "the default branch" in str(excinfo.value)
    assert env.gh.calls() == []
