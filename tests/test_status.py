from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from conftest import Env
from helpers import commit, logger, push_from_other_clone, sh

import draupnir.status as status_module
from draupnir.cli import main
from draupnir.runner import Result
from draupnir.setup import new_clone
from draupnir.status import read_status


def test_status_counts_ahead_and_behind_main(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/ahead-behind-main", None, log)
    commit(folder, "local.txt", "local work\n", "commit ahead of main")
    push_from_other_clone(env, "main")
    sh(folder, "git", "fetch", "-q", "origin")

    status = read_status(env.ws(), folder)

    assert status.ahead_main == 1
    assert status.behind_main == 1
    assert status.error is None


def test_status_counts_ahead_and_behind_remote(env: Env) -> None:
    branch = "feature/ahead-behind-remote"
    _, log = logger()
    folder = new_clone(env.ws(), branch, None, log)
    sh(folder, "git", "push", "-q", "-u", "origin", branch)
    commit(folder, "local.txt", "local work\n", "commit ahead of the upstream")
    push_from_other_clone(env, branch)
    sh(folder, "git", "fetch", "-q", "origin")

    status = read_status(env.ws(), folder)

    assert status.upstream is not None
    assert status.upstream_gone is False
    assert status.ahead_upstream == 1
    assert status.behind_upstream == 1


def test_status_reports_not_pushed_when_there_is_no_upstream(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/never-pushed", None, log)

    status = read_status(env.ws(), folder)

    assert status.upstream is None
    assert status.upstream_gone is False
    assert status.ahead_upstream is None
    assert status.behind_upstream is None


def test_status_reports_an_upstream_that_was_deleted_on_github(env: Env) -> None:
    branch = "feature/deleted-upstream"
    _, log = logger()
    folder = new_clone(env.ws(), branch, None, log)
    sh(folder, "git", "push", "-q", "-u", "origin", branch)
    sh(env.remote, "git", "branch", "-D", branch)
    sh(folder, "git", "fetch", "-q", "--prune", "origin")

    status = read_status(env.ws(), folder)

    assert status.upstream is not None
    assert status.upstream_gone is True
    assert status.ahead_upstream is None
    assert status.behind_upstream is None


def test_status_counts_changed_and_untracked_files(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/dirty", None, log)
    (folder / "App.java").write_text("line 1\nchanged\nline 3\n")
    (folder / "new-file.txt").write_text("new\n")

    status = read_status(env.ws(), folder)

    assert status.changed == 1
    assert status.untracked == 1


def test_status_detects_a_rebase_in_progress(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/rebasing", None, log)
    commit(folder, "work.txt", "work\n", "work to rebase")
    sh(folder, "git", "push", "-q", "-u", "origin", "feature/rebasing")
    rebase = subprocess.run(
        ["git", "rebase", "--force-rebase", "--exec", "false", "origin/main"],
        cwd=folder,
        capture_output=True,
        text=True,
    )
    assert rebase.returncode != 0

    status = read_status(env.ws(), folder)

    assert status.operation == "rebase"


def test_status_detects_a_merge_cherry_pick_revert_and_bisect(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/markers", None, log)
    git_dir = folder / ".git"
    markers = {
        "MERGE_HEAD": "merge",
        "CHERRY_PICK_HEAD": "cherry-pick",
        "REVERT_HEAD": "revert",
        "BISECT_LOG": "bisect",
    }

    for marker, expected_operation in markers.items():
        marker_path = git_dir / marker
        marker_path.write_text("placeholder\n")
        try:
            status = read_status(env.ws(), folder)
            assert status.operation == expected_operation
        finally:
            marker_path.unlink()


def test_status_reports_detached_head(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/detached", None, log)
    sh(folder, "git", "checkout", "-q", "--detach")

    status = read_status(env.ws(), folder)

    assert status.branch is None
    assert status.error is None


def test_status_of_a_broken_clone_is_an_error_row(env: Env) -> None:
    folder = env.root / "not-a-clone"
    folder.mkdir()

    status = read_status(env.ws(), folder)

    assert status.error is not None
    assert status.branch is None
    assert status.changed == 0
    assert status.untracked == 0


def test_status_does_not_touch_the_index(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/no-index-write", None, log)
    index_path = folder / ".git" / "index"
    mtime_before = index_path.stat().st_mtime_ns

    read_status(env.ws(), folder)

    assert index_path.stat().st_mtime_ns == mtime_before


def test_status_command_prints_one_line_per_clone(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    _, log = logger()
    new_clone(env.ws(), "feature/second-project", None, log)

    code = main(["-C", str(env.root), "status"])

    assert code == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line]
    assert len(lines) == 2
    assert any(line.startswith("main") for line in lines)
    assert any(line.startswith("feature-second-project") for line in lines)


# ----- When a clone is finished (R29) ---------------------------------------------------


def _merged_clone(env: Env, branch: str) -> Path:
    """A clone whose branch is pushed and whose commit the main branch already has."""
    _, log = logger()
    folder = new_clone(env.ws(), branch, None, log)
    commit(folder, "work.txt", "work\n", "the work of this clone")
    sh(folder, "git", "push", "-q", "-u", "origin", branch)
    sh(folder, "git", "push", "-q", "origin", f"{branch}:main")
    sh(folder, "git", "fetch", "-q", "origin")
    return folder


def test_a_clone_whose_work_is_on_the_main_branch_is_finished(env: Env) -> None:
    folder = _merged_clone(env, "feature/merged")

    status = read_status(env.ws(), folder)

    assert status.finished is True


def test_a_clone_that_was_never_pushed_is_not_finished(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/fresh", None, log)

    status = read_status(env.ws(), folder)

    # A new clone is ahead of the main branch by nothing either, so only the push tells the
    # two apart.
    assert status.ahead_main == 0
    assert status.upstream is None
    assert status.finished is False


def test_main_is_never_finished(env: Env) -> None:
    status = read_status(env.ws(), env.main)

    assert status.finished is False


def test_a_finished_clone_with_a_changed_file_is_not_finished(env: Env) -> None:
    folder = _merged_clone(env, "feature/merged-then-changed")
    (folder / "App.java").write_text("line 1\nchanged\nline 3\n")

    assert read_status(env.ws(), folder).finished is False


def test_a_finished_clone_with_an_untracked_file_is_not_finished(env: Env) -> None:
    folder = _merged_clone(env, "feature/merged-then-untracked")
    (folder / "scratch.txt").write_text("work in progress\n")

    assert read_status(env.ws(), folder).finished is False


def test_a_finished_clone_with_a_stash_is_not_finished(env: Env) -> None:
    folder = _merged_clone(env, "feature/merged-with-stash")
    (folder / "App.java").write_text("line 1\nstashed\nline 3\n")
    sh(folder, "git", "stash", "push", "-q")

    status = read_status(env.ws(), folder)

    assert status.changed == 0
    assert status.finished is False


def test_a_finished_clone_with_an_operation_in_progress_is_not_finished(env: Env) -> None:
    folder = _merged_clone(env, "feature/merged-then-merging")
    (folder / ".git" / "MERGE_HEAD").write_text("placeholder\n")

    assert read_status(env.ws(), folder).finished is False


def test_a_clone_with_a_commit_of_its_own_is_not_finished(env: Env) -> None:
    branch = "feature/own-commit"
    _, log = logger()
    folder = new_clone(env.ws(), branch, None, log)
    commit(folder, "work.txt", "work\n", "work that is on no other branch")
    sh(folder, "git", "push", "-q", "-u", "origin", branch)

    status = read_status(env.ws(), folder)

    assert status.ahead_main == 1
    assert status.finished is False


def _rebase_merged_clone(env: Env, branch: str) -> Path:
    """A clone whose change the main branch has under another commit, as a rebase merge does."""
    _, log = logger()
    folder = new_clone(env.ws(), branch, None, log)
    commit(folder, "work.txt", "work\n", "the work of this clone")
    sh(folder, "git", "push", "-q", "-u", "origin", branch)
    # The same change, committed again, is the commit the main branch ends up with.
    other = env.tmp / f"merger-{branch.replace('/', '-')}"
    sh(env.tmp, "git", "clone", "-q", str(env.remote), str(other))
    sh(other, "git", "cherry-pick", sh(folder, "git", "rev-parse", "HEAD"))
    sh(other, "git", "commit", "-q", "--amend", "-m", "the work, under another name")
    sh(other, "git", "push", "-q", "origin", "main")
    sh(folder, "git", "fetch", "-q", "origin")
    return folder


def test_a_clone_whose_commits_the_main_branch_has_under_other_names_is_finished(
    env: Env,
) -> None:
    """GitHub's rebase and merge gives the same change a new commit, and that still counts."""
    folder = _rebase_merged_clone(env, "feature/rebased-onto-main")

    status = read_status(env.ws(), folder)

    assert status.ahead_main == 1
    assert status.finished is True


def test_the_comparison_of_the_changes_runs_once_for_each_pair_of_commits(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dashboard reads the status every few seconds, and this is the read that costs."""
    # A clone that is still ahead by a commit, so the cheap count cannot answer on its own.
    folder = _rebase_merged_clone(env, "feature/compared-once")
    comparisons: list[tuple[str, ...]] = []
    real_git = status_module.git

    def counting_git(cwd: Path, *args: str, **named: object) -> Result:
        if "--cherry-pick" in args:
            comparisons.append(args)
        return real_git(cwd, *args, **named)  # type: ignore[arg-type]

    monkeypatch.setattr(status_module, "git", counting_git)

    assert read_status(env.ws(), folder).finished is True
    assert read_status(env.ws(), folder).finished is True

    assert len(comparisons) == 1


def test_a_clone_whose_branch_github_deleted_after_the_merge_is_finished(env: Env) -> None:
    branch = "feature/merged-and-deleted"
    folder = _merged_clone(env, branch)
    sh(env.remote, "git", "branch", "-D", branch)
    sh(folder, "git", "fetch", "-q", "--prune", "origin")

    status = read_status(env.ws(), folder)

    assert status.upstream_gone is True
    assert status.finished is True


def test_a_clone_with_a_commit_that_is_not_pushed_yet_is_not_finished(env: Env) -> None:
    folder = _merged_clone(env, "feature/merged-then-more-work")
    commit(folder, "more.txt", "more\n", "work after the merge")

    status = read_status(env.ws(), folder)

    assert status.ahead_upstream == 1
    assert status.finished is False
