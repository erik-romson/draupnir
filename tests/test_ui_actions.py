"""Tests for the dashboard actions built in Steps 9 to 12: Open IDE, Remove clone, Rebase, Push
and follow build, Stop waiting, Fast-forward main and Edit config (R7, R13, R17, R18, R19, R21,
R25)."""

from __future__ import annotations

import contextlib
from pathlib import Path

import pytest
from conftest import Env
from fake_gh import CheckRun, Poll, running
from helpers import (
    append_config,
    cmux_calls,
    commit,
    logger,
    settle,
    sh,
    wait_until,
    write_executable,
    write_stub_cmux,
    write_stub_tmux,
)
from textual.content import Content
from textual.pilot import Pilot
from textual.widgets import Static

from draupnir.cmux import GREEN, YELLOW, attach_line, session_name
from draupnir.operations import push
from draupnir.setup import new_clone, unsaved_work
from draupnir.ui.app import DraupnirApp
from draupnir.ui.confirm import ConfirmScreen

SIZE = (160, 50)


def _row_names(app: DraupnirApp) -> list[str]:
    table = app.projects.table
    return [table.get_row_at(index)[0].plain for index in range(table.row_count)]


def _select(app: DraupnirApp, name: str) -> None:
    app.projects.table.move_cursor(row=_row_names(app).index(name))


def _output_lines(app: DraupnirApp) -> list[str]:
    return [strip.text.rstrip() for strip in app.output.log_view.lines]


async def _wait_for_question(app: DraupnirApp, pilot: Pilot[None]) -> None:
    """Waits for the remove question, which opens after a worker has read the session."""
    await wait_until(pilot, lambda: isinstance(app.screen, ConfirmScreen), "the remove question")


async def test_ui_remove_asks_for_confirmation_and_deletes_the_clone(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/remove-me", None, log)
    assert unsaved_work(env.ws(), folder, log) == []

    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        _select(app, "feature-remove-me")
        await pilot.pause()

        await pilot.click("#remove")
        await _wait_for_question(app, pilot)
        assert isinstance(app.screen, ConfirmScreen)
        assert folder.exists()

        await pilot.press("y")
        await settle(app, pilot)

        assert not folder.exists()
        assert "feature-remove-me" not in _row_names(app)
        assert "=== Done: Remove feature-remove-me" in _output_lines(app)


async def test_ui_remove_deletes_a_clone_on_the_default_branch(env: Env) -> None:
    """A clone that is on the default branch is removable too, only main/ is not (D47)."""
    sh(env.root, "git", "clone", "-q", str(env.remote), "merged-work")
    folder = env.root / "merged-work"

    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        _select(app, "merged-work")
        await pilot.pause()
        assert not app.projects.buttons["remove"].disabled

        await pilot.click("#remove")
        await _wait_for_question(app, pilot)
        assert isinstance(app.screen, ConfirmScreen)

        await pilot.press("y")
        await settle(app, pilot)

        assert not folder.exists()
        assert "merged-work" not in _row_names(app)

        # main/ itself stays out of reach.
        _select(app, "main")
        await pilot.pause()
        assert app.projects.buttons["remove"].disabled


async def test_ui_remove_answered_no_keeps_the_clone(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/keep-me", None, log)

    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        _select(app, "feature-keep-me")
        await pilot.pause()

        await pilot.click("#remove")
        await _wait_for_question(app, pilot)
        assert isinstance(app.screen, ConfirmScreen)

        await pilot.press("escape")
        await pilot.pause()

        assert folder.is_dir()
        assert not app.busy
        assert "feature-keep-me" in _row_names(app)


async def test_ui_remove_refusal_shows_every_reason(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/many-reasons", None, log)
    commit(folder, "unpushed.txt", "not pushed\n", "commit that is not pushed")
    (folder / "App.java").write_text("stashed change\n")
    sh(folder, "git", "stash", "-q")
    (folder / "notes.txt").write_text("not committed\n")

    reasons = unsaved_work(env.ws(), folder, log)
    assert len(reasons) == 3

    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        _select(app, "feature-many-reasons")
        await pilot.pause()

        await pilot.click("#remove")
        await _wait_for_question(app, pilot)
        await pilot.press("y")
        await settle(app, pilot)

        assert folder.is_dir()
        assert not app.busy
        # The output area wraps long lines, so a sentence can be split over several lines.
        # Joining with a space instead of a newline reconstructs it for the substring check.
        text = " ".join(_output_lines(app))
        for reason in reasons:
            assert reason in text
        assert "--force" in text


# ----- The remove question and the terminal session -----------------------------------------

_UNREADABLE_NOTE = (
    "draupnir could not read the terminal sessions, so it cannot say whether the clone has one."
)


def _running_session(folder: Path) -> dict[str, object]:
    """An entry of cmux local-tmux list for the running session of the clone in folder."""
    return {
        "session_name": session_name(folder),
        "cwd": str(folder.resolve()),
        "live": True,
        "socket_path": "/tmp/tmux-test/default",
        "session_id": "$0",
        "clients": 0,
    }


def _closes(calls: Path) -> list[list[str]]:
    if not calls.exists():
        return []
    return [call for call in cmux_calls(calls) if call[:2] == ["local-tmux", "close"]]


async def _ask_to_remove(app: DraupnirApp, pilot: Pilot[None], name: str) -> str:
    """Presses Remove on the clone called name, and returns the question once it is shown."""
    await settle(app, pilot)
    _select(app, name)
    await pilot.pause()
    await pilot.click("#remove")
    await _wait_for_question(app, pilot)
    return _confirm_question(app)


async def test_ui_remove_names_the_session_and_closes_it(env: Env) -> None:
    folder = new_clone(env.ws(), "feature-a", None, logger()[1])
    write_stub_tmux(env.bin)
    calls = write_stub_cmux(env.bin, sessions={"sessions": [_running_session(folder)]})

    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        question = await _ask_to_remove(app, pilot, "feature-a")

        assert question.startswith("Delete the folder feature-a?\n\n")
        assert "Its terminal session shop-feature-a is open, with no client attached." in question
        assert "The session is closed together with the clone." in question
        assert folder.is_dir()
        assert _closes(calls) == []

        await pilot.press("y")
        await settle(app, pilot)

        assert not folder.exists()
        assert _closes(calls) == [["local-tmux", "close", "shop-feature-a"]]
        assert "=== Done: Remove feature-a" in _output_lines(app)


async def test_ui_remove_answered_no_keeps_the_session(env: Env) -> None:
    folder = new_clone(env.ws(), "feature-a", None, logger()[1])
    write_stub_tmux(env.bin)
    calls = write_stub_cmux(env.bin, sessions={"sessions": [_running_session(folder)]})

    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        await _ask_to_remove(app, pilot, "feature-a")
        await pilot.press("escape")
        await settle(app, pilot)

        assert folder.is_dir()
        assert _closes(calls) == []


async def test_ui_remove_question_appears_when_the_sessions_cannot_be_read_in_time(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = new_clone(env.ws(), "feature-a", None, logger()[1])
    write_stub_cmux(
        env.bin, sessions={"sessions": [_running_session(folder)]}, slow=["local-tmux list"]
    )
    monkeypatch.setattr("draupnir.cmux.LOCAL_TMUX_READ_TIMEOUT_SECONDS", 0.2)

    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        question = await _ask_to_remove(app, pilot, "feature-a")

        assert question.startswith("Delete the folder feature-a?")
        assert question.endswith(f"\n\n{_UNREADABLE_NOTE}")

        await pilot.press("escape")
        await settle(app, pilot)
        assert folder.is_dir()


async def test_ui_remove_question_shows_a_program_name_with_brackets_as_it_is(env: Env) -> None:
    folder = new_clone(env.ws(), "feature-a", None, logger()[1])
    write_stub_tmux(env.bin, "zsh\n[x]\n")
    write_stub_cmux(env.bin, sessions={"sessions": [_running_session(folder)]})

    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        await _ask_to_remove(app, pilot, "feature-a")

        shown = app.screen.query_one(Static).visual
        assert isinstance(shown, Content)
        assert "A program still runs in it: [x]." in shown.plain
        assert "Its terminal session shop-feature-a is open" in shown.plain

        await pilot.press("escape")
        await settle(app, pilot)


async def test_ui_remove_with_a_broken_cmux_command_still_asks_and_deletes(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = new_clone(env.ws(), "feature-a", None, logger()[1])
    monkeypatch.setenv("DRAUPNIR_CMUX", '"broken')

    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        question = await _ask_to_remove(app, pilot, "feature-a")

        assert question.endswith(f"\n\n{_UNREADABLE_NOTE}")

        await pilot.press("y")
        await settle(app, pilot)

        assert not folder.exists()
        lines = _output_lines(app)
        assert "=== Done: Remove feature-a" in lines
        assert sum("could not read the cmux command" in line for line in lines) == 1


def _make_rebase_conflict(env: Env, folder: str) -> None:
    """Pushes a commit to main that changes App.java, so folder's rebase conflicts."""
    other = env.tmp / "other-main-for-ui-conflict"
    sh(env.tmp, "git", "clone", "-q", "--branch", "main", str(env.remote), str(other))
    commit(other, "App.java", "line 1 changed on main\nline 2\nline 3\n", "change on main")
    sh(other, "git", "push", "-q", "origin", "main")
    commit(
        env.root / folder,
        "App.java",
        "line 1 changed on branch\nline 2\nline 3\n",
        "change on branch",
    )


async def test_ui_rebase_conflict_offers_the_ide_for_the_rebased_clone(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/conflict", None, log)
    new_clone(env.ws(), "feature/other", None, log)
    _make_rebase_conflict(env, "feature-conflict")

    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        _select(app, "feature-conflict")
        await pilot.pause()

        await pilot.click("#rebase")
        # The selection moves to another clone before the rebase ends, so the dialog must
        # still remember feature-conflict, the clone the action was started on.
        _select(app, "feature-other")
        await settle(app, pilot)

        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("y")
        await settle(app, pilot)

        await wait_until(
            pilot,
            lambda: env.idea_log.is_file() and env.idea_log.read_text().strip() != "",
            "the IDE starts",
        )
        assert env.idea_log.read_text().strip() == str(folder)


def _build_cell(app: DraupnirApp, name: str) -> str:
    """The build state in the Build cell, without the padding and the link arrow of the button."""
    return app.projects.table.get_cell(name, "build").plain.strip().removesuffix(" ↗")


def _record_opened_urls(app: DraupnirApp, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Makes the app record the URLs it opens, instead of starting a browser."""
    opened: list[str] = []

    def open_url(url: str, *, new_tab: bool = True) -> None:
        opened.append(url)

    monkeypatch.setattr(app, "open_url", open_url)
    return opened


def _build_cell_offset(app: DraupnirApp, name: str) -> tuple[int, int]:
    """Where the Build cell of the clone called name is, relative to the table."""
    table = app.projects.table
    build_column = table.get_column_index("build")
    x = sum(column.get_render_width(table) for column in table.ordered_columns[:build_column])
    return x + 2, 1 + _row_names(app).index(name)


async def test_ui_push_and_follow_updates_the_build_column_while_waiting(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/follow", None, log)
    sha = commit(folder, "feature.txt", "feature work\n", "add feature file")
    # The build keeps running until the test lets it end.
    env.gh.set_build(sha, [Poll(check_runs=[running("build")])])

    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        _select(app, "feature-follow")
        await pilot.pause()
        assert _build_cell(app, "feature-follow") == ""

        await pilot.click("#push-follow")
        await wait_until(
            pilot,
            lambda: _build_cell(app, "feature-follow") == "pending",
            "the Build column shows the running build",
        )
        # The push has ended, and the build is followed in the background (D61).
        assert not app.busy
        assert "feature-follow" in app.watches
        assert sh(env.remote, "git", "rev-parse", "feature/follow") == sha
        # The details line shows the jobs of the selected clone's build, and the wait.
        details = str(app.projects.details.content)
        assert "build: pending" in details
        assert "draupnir is following this build." in details

        env.gh.set_build(sha, [Poll(check_runs=[CheckRun("build")])])
        await settle(app, pilot)

        assert _build_cell(app, "feature-follow") == "success"
        assert app.builds["feature-follow"].sha == sha
        assert app.watches == {}
        lines = _output_lines(app)
        assert "=== Done: Push feature-follow" in lines
        assert any(line.startswith("feature-follow: ") for line in lines)


async def test_ui_another_action_runs_while_a_build_is_followed(env: Env) -> None:
    """The dashboard is free while it follows a build, so another clone can be worked on (D61)."""
    _, log = logger()
    folder = new_clone(env.ws(), "feature/followed", None, log)
    other = new_clone(env.ws(), "feature/other", None, log)
    sha = commit(folder, "feature.txt", "feature work\n", "add feature file")
    other_sha = commit(other, "other.txt", "other work\n", "add other file")
    env.gh.set_build(sha, [Poll(check_runs=[running("build")])])
    env.gh.set_build(other_sha, [Poll(check_runs=[CheckRun("build")])])

    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        _select(app, "feature-followed")
        await pilot.pause()

        await pilot.click("#push-follow")
        await wait_until(
            pilot,
            lambda: "feature-followed" in app.watches,
            "the build of the pushed clone is followed",
        )
        # Every button is enabled again, although the build is still running.
        assert not app.busy
        assert not app.projects.buttons["build-status"].disabled
        assert not app.output.stop_button.disabled

        _select(app, "feature-other")
        await pilot.pause()
        await pilot.click("#build-status")
        await wait_until(
            pilot,
            lambda: "=== Done: Build status of feature-other" in _output_lines(app),
            "the build status of the other clone",
        )
        # The first clone is still followed while the second action ran.
        assert "feature-followed" in app.watches
        assert _build_cell(app, "feature-other") == "success"

        env.gh.set_build(sha, [Poll(check_runs=[CheckRun("build")])])
        await settle(app, pilot)
        assert _build_cell(app, "feature-followed") == "success"


async def test_ui_stop_waiting_ends_the_action_and_keeps_the_last_state(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/stop", None, log)
    sha = commit(folder, "feature.txt", "feature work\n", "add feature file")
    env.gh.set_build(sha, [Poll(check_runs=[running("build")])])

    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        _select(app, "feature-stop")
        await pilot.pause()

        await pilot.click("#push-follow")
        await wait_until(
            pilot,
            lambda: _build_cell(app, "feature-stop") == "pending",
            "the Build column shows the running build",
        )
        assert "feature-stop" in app.watches
        assert not app.output.stop_button.disabled

        await pilot.click("#stop-waiting")
        await settle(app, pilot)

        assert app.watches == {}
        assert app.output.stop_button.disabled
        # The column keeps the last state it saw. The build on GitHub was not touched: the fake
        # gh was only asked to read, and the build there still runs.
        assert _build_cell(app, "feature-stop") == "pending"
        assert app.builds["feature-stop"].state == "pending"
        assert all(call.args[0] == "api" for call in env.gh.calls())
        lines = _output_lines(app)
        assert "feature-stop: stopped following the build. It keeps running on GitHub." in lines
        assert not any(line.startswith("feature-stop: The build of commit") for line in lines)

        # A new action starts once the wait has ended.
        await pilot.click("#build-status")
        await settle(app, pilot)
        assert "=== Done: Build status of feature-stop" in _output_lines(app)


async def test_a_click_on_the_build_cell_opens_the_failed_job(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/red", None, log)
    new_clone(env.ws(), "feature/other", None, log)
    sha = commit(folder, "feature.txt", "feature work\n", "add feature file")
    failed_url = "https://ci.example.com/runs/lint"
    env.gh.set_build(
        sha,
        [Poll(check_runs=[CheckRun("build"), CheckRun("lint", "failure", url=failed_url)])],
    )

    app = DraupnirApp(env.ws())
    opened = _record_opened_urls(app, monkeypatch)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        _select(app, "feature-red")
        await pilot.click("#build-status")
        await settle(app, pilot)
        assert _build_cell(app, "feature-red") == "failure"

        # The click lands on a row that is not selected. It opens the build at once.
        _select(app, "feature-other")
        await pilot.pause()
        await pilot.click("#table", offset=_build_cell_offset(app, "feature-red"))
        await pilot.pause()

        assert opened == [failed_url]
        selected = app.projects.selected_status()
        assert selected is not None
        assert selected.name == "feature-red"


async def test_open_build_key_and_job_links_open_the_build(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/green", None, log)
    sha = commit(folder, "feature.txt", "feature work\n", "add feature file")
    build_url = "https://ci.example.com/runs/build"
    lint_url = "https://ci.example.com/runs/lint"
    env.gh.set_build(
        sha, [Poll(check_runs=[CheckRun("build", url=build_url), CheckRun("lint", url=lint_url)])]
    )

    app = DraupnirApp(env.ws())
    opened = _record_opened_urls(app, monkeypatch)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        _select(app, "feature-green")
        await pilot.pause()

        # No build is known yet, so the key opens nothing.
        await pilot.press("g")
        await pilot.pause()
        assert opened == []

        await pilot.click("#build-status")
        await settle(app, pilot)
        await pilot.press("g")
        await pilot.pause()
        assert opened == [build_url]

        # Each job in the details line is a link to its own page.
        lines = app.projects.details.render_lines(app.projects.details.region.reset_offset)
        lint_line = next(index for index, line in enumerate(lines) if "lint: success" in line.text)
        await pilot.click("#details", offset=(4, lint_line))
        await pilot.pause()
        assert opened == [build_url, lint_url]


async def test_ff_main_is_hidden_when_disabled_in_config(env: Env) -> None:
    append_config(env.root, "\n[actions]\nfast-forward-main = false\n")
    _, log = logger()
    new_clone(env.ws(), "feature/hidden", None, log)

    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        _select(app, "feature-hidden")
        await pilot.pause()

        assert app.projects.buttons["ff-main"].display is False


async def test_ui_ff_main_asks_for_confirmation(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/ff-ui", None, log)
    sha = commit(folder, "feature.txt", "feature work\n", "add feature file")
    push(env.ws(), folder, log)
    env.gh.set_build(sha, [Poll(check_runs=[CheckRun("build")])])

    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        _select(app, "feature-ff-ui")
        await pilot.pause()
        assert app.projects.buttons["ff-main"].display is True

        await pilot.click("#ff-main")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        # main/ has not moved yet: the dialog only asked, it did not act.
        assert sh(env.remote, "git", "rev-parse", "main") != sha

        await pilot.press("y")
        await settle(app, pilot)

        assert sh(env.remote, "git", "rev-parse", "main") == sha
        assert "=== Done: Fast-forward main to feature-ff-ui" in _output_lines(app)


def _use_stub_editor(env: Env, monkeypatch: pytest.MonkeyPatch, script: str) -> None:
    """Makes the Edit config button run script instead of a real editor.

    The dashboard cannot be suspended in a test, so suspend does nothing here. The editor
    itself is a real command that draupnir starts, and it gets the path of the file.
    """
    editor = env.root / "stub-editor"
    write_executable(editor, script)
    monkeypatch.setenv("DRAUPNIR_EDITOR", str(editor))

    def no_suspend(_app: DraupnirApp) -> contextlib.AbstractContextManager[None]:
        return contextlib.nullcontext()

    monkeypatch.setattr(DraupnirApp, "suspend", no_suspend)


async def test_ui_edit_config_shows_the_file_when_no_editor_is_set(env: Env) -> None:
    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)

        await pilot.click("#config")
        await settle(app, pilot)

        # The output wraps a long path, so only the end of the first line is checked.
        lines = _output_lines(app)
        assert any("draupnir.toml:" in line for line in lines)
        assert any("[workspace]" in line for line in lines)
        assert any("DRAUPNIR_EDITOR" in line for line in lines)


async def test_ui_edit_config_starts_the_editor_and_reads_the_config_again(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_stub_editor(
        env, monkeypatch, '#!/bin/sh\nprintf \'\\n[ide]\\njava = "my-idea"\\n\' >> "$1"\n'
    )
    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        assert app.ws.config.ide.java is None

        await pilot.click("#config")
        await settle(app, pilot)

        assert app.ws.config.ide.java == "my-idea"


async def test_ui_edit_config_reports_a_file_that_cannot_be_read(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_stub_editor(env, monkeypatch, "#!/bin/sh\nprintf 'not toml at all\\n' > \"$1\"\n")
    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        before = app.ws.config

        await pilot.click("#config")
        await settle(app, pilot)

        assert app.ws.config is before
        assert any("draupnir.toml" in line for line in _output_lines(app))


def _confirm_question(app: DraupnirApp) -> str:
    screen = app.screen
    assert isinstance(screen, ConfirmScreen)
    return screen.question


async def test_ui_pr_flow_asks_to_remove_the_clone_after_the_merge(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/pr-ui", None, log)
    sha = commit(folder, "feature.txt", "feature work\n", "add feature file")
    env.gh.set_build(
        sha, [Poll(check_runs=[running("build")]), Poll(check_runs=[CheckRun("build")])]
    )
    env.gh.set_delete_branch_on_merge(True)

    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        _select(app, "feature-pr-ui")
        await pilot.pause()
        assert app.projects.buttons["pr-merge"].display is True

        await pilot.click("#pr-merge")
        await pilot.pause()
        question = _confirm_question(app)
        assert "Pull request flow for feature/pr-ui" in question
        assert "Wait for the build" in question
        # The dialog only asked. Nothing was pushed yet.
        assert sh(env.remote, "git", "branch", "--list", "feature/pr-ui") == ""

        await pilot.press("y")
        await settle(app, pilot)

        # The action has finished, and the app asks about the removal as a new question.
        assert not app.busy
        assert "=== Done: Pull request for feature/pr-ui" in _output_lines(app)
        assert _build_cell(app, "feature-pr-ui") == "success"
        assert [pr.state for pr in env.gh.pull_requests()] == ["MERGED"]
        assert sh(env.remote, "git", "show", "main:feature.txt") == "feature work"
        assert (env.main / "feature.txt").is_file()
        assert _confirm_question(app).startswith(
            "The branch is merged. Remove the clone feature-pr-ui?"
        )
        assert folder.is_dir()

        await pilot.press("y")
        await settle(app, pilot)

        assert not folder.exists()
        assert "feature-pr-ui" not in _row_names(app)
        assert "=== Done: Remove feature-pr-ui" in _output_lines(app)


async def test_ui_pr_flow_does_not_offer_removal_after_auto_merge(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/pr-ui-auto", None, log)
    sha = commit(folder, "feature.txt", "feature work\n", "add feature file")
    env.gh.set_build(sha, [Poll(check_runs=[CheckRun("build")])])
    env.gh.set_new_pr(merge_states=["BLOCKED"], review_decision="REVIEW_REQUIRED")

    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        _select(app, "feature-pr-ui-auto")
        await pilot.pause()

        await pilot.click("#pr-merge")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("y")
        await settle(app, pilot)

        assert not app.busy
        assert not isinstance(app.screen, ConfirmScreen)
        assert folder.is_dir()
        assert "feature-pr-ui-auto" in _row_names(app)
        [pr] = env.gh.pull_requests()
        assert (pr.state, pr.auto_merge) == ("OPEN", True)
        lines = _output_lines(app)
        assert "=== Done: Pull request for feature/pr-ui-auto" in lines
        text = " ".join(lines)
        assert f"Auto-merge is on. GitHub merges pull request #{pr.number} after the" in text
        assert not any(line.startswith("=== Done: Remove") for line in lines)


async def test_ui_cmux_workspace_is_hidden_outside_cmux(env: Env) -> None:
    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)

        assert not app.projects.buttons["cmux-workspace"].display
        assert app.check_action("open_cmux_workspace", ()) is False


async def test_ui_cmux_workspace_opens_the_selected_clone(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a session, w starts it first, and the new workspace attaches to it (W4)."""
    calls = write_stub_cmux(env.bin)
    monkeypatch.setenv("CMUX_WORKSPACE_ID", "workspace-1")
    feature = new_clone(env.ws(), "feature-cmux", None, logger()[1])

    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        assert app.projects.buttons["cmux-workspace"].display
        assert str(app.projects.buttons["cmux-workspace"].label) == "cmux workspace"
        _select(app, "feature-cmux")
        await pilot.pause()

        await pilot.press("w")
        await settle(app, pilot)

        verbs = [call[:2] for call in _cmux_calls_of_w(calls)]
        assert verbs.index(["local-tmux", "start"]) < verbs.index(["workspace", "create"])
        create = next(call for call in cmux_calls(calls) if call[:2] == ["workspace", "create"])
        assert create[create.index("--cwd") + 1] == str(feature)
        assert create[create.index("--focus") + 1] == "true"
        assert create[create.index("--command") + 1] == attach_line(feature)


class Notifications:
    """Records what the app shows as notifications."""

    def __init__(self) -> None:
        self.shown: list[tuple[str, str, str]] = []

    def notify(
        self,
        message: str,
        *,
        title: str = "",
        severity: str = "information",
        timeout: float | None = None,
        markup: bool = True,
    ) -> None:
        self.shown.append((message, title, severity))

    def errors(self) -> list[str]:
        return [message for message, _title, severity in self.shown if severity == "error"]


def _cmux_calls_of_w(calls: Path) -> list[list[str]]:
    """The calls of the stub cmux, without those that write the sidebar status."""
    return [call for call in cmux_calls(calls) if call[0] not in ("set-status", "clear-status")]


async def _press_w_on(
    app: DraupnirApp, pilot: Pilot[None], name: str, monkeypatch: pytest.MonkeyPatch
) -> Notifications:
    """Presses w on the clone called name, and returns the notifications of the action."""
    await settle(app, pilot)
    _select(app, name)
    await pilot.pause()
    notifications = Notifications()
    monkeypatch.setattr(app, "notify", notifications.notify)
    await pilot.press("w")
    await settle(app, pilot)
    return notifications


async def test_ui_cmux_workspace_goes_to_the_workspace_that_the_clone_has(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    feature = new_clone(env.ws(), "feature-cmux", None, logger()[1])
    terminal = {"id": "t-b-id", "ref": "t-b", "type": "terminal"}
    tree = {
        "windows": [
            {
                "workspaces": [
                    {
                        "id": "ws-b",
                        "ref": "workspace:2",
                        "title": f"{env.root.name} feature-cmux",
                        "panes": [{"surfaces": [terminal]}],
                    }
                ]
            }
        ]
    }
    arguments = ["cmux", "local-tmux", "attach", session_name(feature), "--headless"]
    binding = {"resume_binding": {"launch_command": {"arguments": arguments}, "cwd": str(feature)}}
    calls = write_stub_cmux(
        env.bin,
        sessions={"sessions": [_running_session(feature)]},
        tree=tree,
        bindings={"t-b": binding},
    )
    monkeypatch.setenv("CMUX_WORKSPACE_ID", "workspace-1")

    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        notifications = await _press_w_on(app, pilot, "feature-cmux", monkeypatch)

        made = _cmux_calls_of_w(calls)
        assert ["workspace", "select", "ws-b"] in made
        assert not [call for call in made if call[:2] == ["workspace", "create"]]
        assert notifications.errors() == []


async def test_ui_cmux_workspace_without_a_session_opens_a_plain_workspace(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With session = false, w makes the calls it made before sessions existed."""
    append_config(env.root, "\n[cmux]\nsession = false\n")
    feature = new_clone(env.ws(), "feature-cmux", None, logger()[1])
    calls = write_stub_cmux(env.bin)
    monkeypatch.setenv("CMUX_WORKSPACE_ID", "workspace-1")

    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        await _press_w_on(app, pilot, "feature-cmux", monkeypatch)

        json_flags = ["--json", "--id-format", "both"]
        assert _cmux_calls_of_w(calls) == [
            ["workspace-group", "list", *json_flags],
            [
                "workspace",
                "create",
                "--name",
                f"{env.root.name} feature-cmux",
                "--cwd",
                str(feature),
                "--focus",
                "true",
                *json_flags,
            ],
            ["todo", "open", "--workspace", "new-workspace", *json_flags],
            [
                "split-off",
                "--surface",
                "new-todo",
                "down",
                "--workspace",
                "new-workspace",
                "--focus",
                "false",
            ],
            ["focus-panel", "--panel", "new-terminal", "--workspace", "new-workspace"],
        ]


async def test_ui_cmux_workspace_stops_when_the_session_belongs_to_another_folder(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    feature = new_clone(env.ws(), "feature-cmux", None, logger()[1])
    other = env.tmp / "elsewhere" / "feature-cmux"
    entry = {**_running_session(feature), "cwd": str(other)}
    calls = write_stub_cmux(env.bin, sessions={"sessions": [entry]})
    monkeypatch.setenv("CMUX_WORKSPACE_ID", "workspace-1")

    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        notifications = await _press_w_on(app, pilot, "feature-cmux", monkeypatch)

        [error] = notifications.errors()
        assert error.startswith(f"The terminal session {session_name(feature)} belongs to {other}")
        assert [call[:2] for call in _cmux_calls_of_w(calls)] == [["local-tmux", "list"]]


async def test_ui_cmux_workspace_opens_none_when_the_workspaces_cannot_be_read(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second press after a failed read must not open a second workspace (A22)."""
    new_clone(env.ws(), "feature-cmux", None, logger()[1])
    calls = write_stub_cmux(env.bin, fail=["tree"])
    monkeypatch.setenv("CMUX_WORKSPACE_ID", "workspace-1")

    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        notifications = await _press_w_on(app, pilot, "feature-cmux", monkeypatch)

        [error] = notifications.errors()
        assert error.startswith(
            "draupnir could not read the cmux workspaces, so it did not open one for feature-cmux"
        )
        assert not [call for call in _cmux_calls_of_w(calls) if call[:2] == ["workspace", "create"]]


async def test_ui_writes_the_status_of_the_clones_into_the_cmux_sidebar(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dashboard publishes one line per workspace into the sidebar of cmux (D52)."""
    calls = write_stub_cmux(env.bin)
    monkeypatch.setenv("CMUX_WORKSPACE_ID", "workspace-1")

    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)

        [clean] = [call for call in cmux_calls(calls) if call[0] == "set-status"]
        assert clean[1:3] == ["draupnir", "clean"]
        assert clean[clean.index("--color") + 1] == GREEN
        assert clean[clean.index("--workspace") + 1] == "workspace-1"

        (env.main / "notes.txt").write_text("a local file\n")
        await pilot.press("r")
        await settle(app, pilot)

        changed = [call for call in cmux_calls(calls) if call[0] == "set-status"][-1]
        assert changed[1:3] == ["draupnir", "main is changed"]
        assert changed[changed.index("--color") + 1] == YELLOW

    # The dashboard takes its line away when it quits.
    assert [call for call in cmux_calls(calls) if call[0] == "clear-status"] == [
        ["clear-status", "draupnir", "--workspace", "workspace-1"]
    ]


async def test_ui_writes_the_sidebar_status_once_while_nothing_changes(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = write_stub_cmux(env.bin)
    monkeypatch.setenv("CMUX_WORKSPACE_ID", "workspace-1")

    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        for _ in range(3):
            await pilot.press("r")
            await settle(app, pilot)

        assert len([call for call in cmux_calls(calls) if call[0] == "set-status"]) == 1


async def test_ui_writes_no_sidebar_status_outside_cmux(env: Env) -> None:
    calls = write_stub_cmux(env.bin)

    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)

    assert not calls.exists()


async def test_ui_sidebar_status_can_be_turned_off(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    append_config(env.root, "cmux-status = false\n")
    calls = write_stub_cmux(env.bin)
    monkeypatch.setenv("CMUX_WORKSPACE_ID", "workspace-1")

    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        assert not app.sidebar_enabled

    assert not calls.exists()


async def test_ui_stops_writing_the_sidebar_status_after_cmux_refuses_it(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One failed write turns the status off, so cmux is not called again on every refresh."""
    calls = env.tmp / "cmux-calls.txt"
    write_executable(
        env.bin / "cmux",
        "#!/bin/sh\n"
        f'printf \'%s\\t\' "$@" >> "{calls}"\n'
        f'echo >> "{calls}"\n'
        'echo "no workspace with that id" >&2\n'
        "exit 1\n",
    )
    monkeypatch.setenv("CMUX_WORKSPACE_ID", "workspace-1")

    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await pilot.press("r")
        await settle(app, pilot)

        assert not app.sidebar_enabled
        assert len(cmux_calls(calls)) == 1
        text = " ".join(_output_lines(app))
        assert "cmux refused the status of this workspace: no workspace with that id." in text
        assert "draupnir does not write the status again." in text
