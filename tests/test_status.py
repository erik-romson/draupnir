from __future__ import annotations

import subprocess

import pytest
from conftest import Env
from helpers import commit, logger, push_from_other_clone, sh

from draupnir.cli import main
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
