from __future__ import annotations

import threading
from collections.abc import Sequence
from pathlib import Path

import pytest
from conftest import Env
from helpers import commit, logger, push_from_other_clone, settle, sh, wait_until
from textual.widgets import TabbedContent

import draupnir.ui.app as app_module
from draupnir import runner
from draupnir.errors import OperationError
from draupnir.runner import LogFn, Result, git
from draupnir.setup import new_clone
from draupnir.status import ProjectStatus
from draupnir.ui.app import DraupnirApp
from draupnir.ui.projects import ACTION_BUTTONS
from draupnir.workspace import Workspace

SIZE = (160, 50)


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


def _record_notifications(app: DraupnirApp, monkeypatch: pytest.MonkeyPatch) -> Notifications:
    notifications = Notifications()
    monkeypatch.setattr(app, "notify", notifications.notify)
    return notifications


def _set_refresh_seconds(env: Env, seconds: float) -> None:
    config = env.root / "draupnir.toml"
    text = config.read_text()
    assert "refresh-seconds = 0\n" in text
    config.write_text(text.replace("refresh-seconds = 0\n", f"refresh-seconds = {seconds}\n"))


def _row_names(app: DraupnirApp) -> list[str]:
    table = app.projects.table
    return [table.get_row_at(index)[0].plain for index in range(table.row_count)]


def _row(app: DraupnirApp, name: str) -> list[str]:
    return [cell.plain for cell in app.projects.table.get_row(name)]


def _output_lines(app: DraupnirApp) -> list[str]:
    return [strip.text.rstrip() for strip in app.output.log_view.lines]


def _select(app: DraupnirApp, name: str) -> None:
    app.projects.table.move_cursor(row=_row_names(app).index(name))


async def test_projects_tab_lists_main_and_every_clone(env: Env) -> None:
    _, log = logger()
    new_clone(env.ws(), "feature/one", None, log)
    new_clone(env.ws(), "feature/two", None, log)
    # A clone of another repository and a plain folder are not clones of this project.
    other_source = env.tmp / "other-source"
    other_source.mkdir()
    sh(other_source, "git", "init", "-q")
    commit(other_source, "README.md", "other\n", "other project")
    sh(env.root, "git", "clone", "-q", str(other_source), "other-project")
    (env.root / "notes").mkdir()

    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)

        assert app.query_one("#tabs", TabbedContent).active == "projects"
        assert _row_names(app) == ["main", "feature-one", "feature-two"]
        assert [status.name for status in app.statuses] == ["main", "feature-one", "feature-two"]


async def test_table_shows_every_column_from_r16(env: Env) -> None:
    _, log = logger()
    busy_branch = "feature/busy"
    busy = new_clone(env.ws(), busy_branch, None, log)
    sh(busy, "git", "push", "-q", "-u", "origin", busy_branch)
    commit(busy, "local.txt", "local work\n", "commit that is not pushed")
    push_from_other_clone(env, busy_branch)
    push_from_other_clone(env, "main")
    sh(busy, "git", "fetch", "-q", "origin")
    (busy / "App.java").write_text("line 1\nchanged\nline 3\n")
    (busy / "new-file.txt").write_text("new\n")
    (busy / ".git" / "MERGE_HEAD").write_text(sh(busy, "git", "rev-parse", "HEAD") + "\n")

    new_clone(env.ws(), "feature/never-pushed", None, log)

    gone_branch = "feature/gone"
    gone = new_clone(env.ws(), gone_branch, None, log)
    sh(gone, "git", "push", "-q", "-u", "origin", gone_branch)
    sh(env.remote, "git", "branch", "-D", gone_branch)
    sh(gone, "git", "fetch", "-q", "--prune", "origin")

    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)

        labels = [column.label.plain for column in app.projects.table.ordered_columns]
        assert labels == [
            "Folder",
            "Branch",
            "vs origin/main",
            "vs upstream",
            "Local changes",
            "In progress",
            "Build",
        ]
        assert _row(app, "feature-busy") == [
            "feature-busy",
            "feature/busy",
            "↑1 ↓1",
            "↑1 ↓1",
            "1 changed, 1 untracked",
            "merge",
            "",
        ]
        assert _row(app, "feature-never-pushed") == [
            "feature-never-pushed",
            "feature/never-pushed",
            "↑0 ↓0",
            "not pushed",
            "clean",
            "",
            "",
        ]
        assert _row(app, "feature-gone")[3] == "deleted on GitHub"
        assert _row(app, "main")[:2] == ["main", "main"]


async def test_a_broken_clone_shows_its_error_in_its_row(env: Env) -> None:
    _, log = logger()
    broken = new_clone(env.ws(), "feature/broken", None, log)
    # git status needs the index, but git remote get-url does not, so the folder is still
    # found as a clone of the project.
    (broken / ".git" / "index").write_text("this is not an index\n")

    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)

        row = _row(app, "feature-broken")
        assert row[1] == "error"
        assert row[4] != ""
        _select(app, "feature-broken")
        await pilot.pause()
        assert "could not read this clone" in str(app.projects.details.content)


async def test_refresh_shows_new_commits(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/refresh", None, log)

    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        assert _row(app, "feature-refresh")[2] == "↑0 ↓0"

        commit(folder, "one.txt", "one\n", "first new commit")
        await pilot.click("#refresh")
        await settle(app, pilot)
        assert _row(app, "feature-refresh")[2] == "↑1 ↓0"

        commit(folder, "two.txt", "two\n", "second new commit")
        await pilot.press("r")
        await settle(app, pilot)
        assert _row(app, "feature-refresh")[2] == "↑2 ↓0"


async def test_fetch_all_updates_behind_counts(env: Env) -> None:
    _, log = logger()
    new_clone(env.ws(), "feature/fetch", None, log)

    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        push_from_other_clone(env, "main")

        # Refresh only reads, so the new commit on GitHub is not visible yet.
        await pilot.press("r")
        await settle(app, pilot)
        assert _row(app, "main")[2] == "↑0 ↓0"
        assert _row(app, "feature-fetch")[2] == "↑0 ↓0"

        await pilot.press("f")
        await settle(app, pilot)
        assert _row(app, "main")[2] == "↑0 ↓1"
        assert _row(app, "feature-fetch")[2] == "↑0 ↓1"
        assert "=== Done: Fetch all clones" in _output_lines(app)


async def test_output_area_shows_the_command_and_its_output(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        notifications = _record_notifications(app, monkeypatch)

        def show_last_commit(log: LogFn, _cancel: threading.Event) -> None:
            git(env.main, "log", "-1", "--format=subject %s", log=log)

        assert app.start("Show the last commit", show_last_commit)
        await settle(app, pilot)

        lines = _output_lines(app)
        assert "=== Show the last commit" in lines
        assert "$ git log -1 '--format=subject %s'" in lines
        assert "subject initial" in lines
        assert "=== Done: Show the last commit" in lines
        assert ("Done: Show the last commit", "", "information") in notifications.shown

        def fail(log: LogFn, _cancel: threading.Event) -> None:
            log("working on it")
            raise OperationError("The example action stopped.\nThis line gives more details.")

        assert app.start("Fail on purpose", fail)
        await settle(app, pilot)

        lines = _output_lines(app)
        assert "working on it" in lines
        assert "The example action stopped." in lines
        assert "This line gives more details." in lines
        assert "=== Stopped: Fail on purpose" in lines
        # The notification shows only the first line of the message.
        assert (
            "The example action stopped.",
            "Stopped: Fail on purpose",
            "error",
        ) in notifications.shown

        # Success is green and failure is red.
        strips = app.output.log_view.lines
        done = next(strip for strip in strips if strip.text.startswith("=== Done:"))
        stopped = next(strip for strip in strips if strip.text.startswith("=== Stopped:"))
        assert any(
            seg.style and seg.style.color and seg.style.color.name == "green" for seg in done
        )
        assert any(
            seg.style and seg.style.color and seg.style.color.name == "red" for seg in stopped
        )


async def test_only_one_action_runs_at_a_time(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    fetches: list[Path] = []

    def recording_fetch_all(ws: Workspace, log: LogFn) -> None:
        fetches.append(ws.root)

    monkeypatch.setattr(app_module, "fetch_all", recording_fetch_all)

    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        notifications = _record_notifications(app, monkeypatch)

        started = threading.Event()
        release = threading.Event()
        second_ran = threading.Event()

        def first(log: LogFn, _cancel: threading.Event) -> None:
            started.set()
            release.wait(timeout=30)

        def second(log: LogFn, _cancel: threading.Event) -> None:
            second_ran.set()

        try:
            assert app.start("First action", first)
            await wait_until(pilot, started.is_set, "the first action runs")

            assert app.busy
            assert app.start("Second action", second) is False
            await pilot.press("f")
            await pilot.pause()

            lines = _output_lines(app)
            assert '"Second action" did not start, because "First action" is still running.' in (
                "\n".join(lines)
            )
            assert '"Fetch all clones" did not start, because "First action" is still running.' in (
                "\n".join(lines)
            )
            assert any(severity == "warning" for _, _, severity in notifications.shown)
            assert app.projects.buttons["fetch-all"].disabled
            assert app.projects.buttons["refresh"].disabled
            assert not app.output.stop_button.disabled
        finally:
            release.set()

        await settle(app, pilot)
        assert not app.busy
        assert not second_ran.is_set()
        assert fetches == []
        assert not app.projects.buttons["fetch-all"].disabled
        assert app.output.stop_button.disabled

        # Once the first action has ended, a new action starts.
        assert app.start("Second action", second)
        await settle(app, pilot)
        assert second_ran.is_set()


async def test_every_action_button_has_a_tooltip(env: Env) -> None:
    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        for spec in ACTION_BUTTONS:
            tooltip = app.query_one(f"#{spec.name}").tooltip
            assert isinstance(tooltip, str)
            assert tooltip
            assert "{main}" not in tooltip


async def test_feature_actions_are_disabled_for_main(env: Env) -> None:
    _, log = logger()
    new_clone(env.ws(), "feature/actions", None, log)
    # A second clone of the project that is on the default branch.
    sh(env.root, "git", "clone", "-q", str(env.remote), "second-main")

    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        assert sorted(_row_names(app)) == ["feature-actions", "main", "second-main"]

        buttons = app.projects.buttons
        assert [str(buttons[spec.name].label) for spec in ACTION_BUTTONS] == [
            "Fetch all",
            "Refresh",
            "Open branch",
            "Open IDE",
            "Rebase on main",
            "Push and follow build",
            "Fast-forward main",
            "Pull request and merge",
            "Build status",
            "Remove clone",
        ]
        feature_actions = ["ff-main", "pr-merge", "remove"]
        other_actions = ["open-ide", "rebase", "push-follow", "build-status"]

        for name in ("main", "second-main"):
            _select(app, name)
            await pilot.pause()
            assert all(buttons[action].disabled for action in feature_actions), name
            assert not any(buttons[action].disabled for action in other_actions), name

        _select(app, "feature-actions")
        await pilot.pause()
        assert not any(buttons[action].disabled for action in feature_actions)
        assert not any(buttons[action].disabled for action in other_actions)

        # A new read of the status keeps the selection, and so the buttons stay the same.
        await pilot.press("r")
        await settle(app, pilot)
        selected = app.projects.selected_status()
        assert selected is not None and selected.name == "feature-actions"
        assert not any(buttons[action].disabled for action in feature_actions)


def _record_commands(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    commands: list[list[str]] = []
    original_run = runner.run

    def recording_run(
        args: Sequence[str | Path],
        cwd: Path,
        *,
        check: bool = True,
        log: LogFn | None = None,
        timeout: float | None = None,
    ) -> Result:
        commands.append([str(arg) for arg in args])
        return original_run(args, cwd, check=check, log=log, timeout=timeout)

    monkeypatch.setattr(runner, "run", recording_run)
    return commands


async def test_timer_reads_the_status_again_without_fetching(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_refresh_seconds(env, 0.05)
    _, log = logger()
    folder = new_clone(env.ws(), "feature/timer", None, log)
    commands = _record_commands(monkeypatch)

    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        assert _row(app, "feature-timer")[2] == "↑0 ↓0"

        pushed = push_from_other_clone(env, "main")
        commit(folder, "timer.txt", "timer\n", "commit seen by the timer")

        await wait_until(
            pilot,
            lambda: _row(app, "feature-timer")[2] == "↑1 ↓0",
            "the timer shows the new local commit",
        )
        await settle(app, pilot)

        # The commit pushed to GitHub is not seen, because the timer never fetches.
        assert _row(app, "feature-timer")[2] == "↑1 ↓0"
        assert _row(app, "main")[2] == "↑0 ↓0"

    assert any(command[:2] == ["git", "status"] for command in commands)
    assert not any("fetch" in command for command in commands)
    assert sh(folder, "git", "rev-parse", "origin/main") != pushed
    assert sh(env.main, "git", "rev-parse", "origin/main") != pushed


async def test_timer_does_not_read_while_an_action_runs(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_refresh_seconds(env, 0.05)

    ticks: list[None] = []
    original_tick = DraupnirApp.refresh_on_timer

    def counting_tick(self: DraupnirApp) -> None:
        ticks.append(None)
        original_tick(self)

    monkeypatch.setattr(DraupnirApp, "refresh_on_timer", counting_tick)

    reads: list[Path] = []
    original_read_status = app_module.read_status

    def counting_read_status(ws: Workspace, path: Path) -> ProjectStatus:
        reads.append(path)
        return original_read_status(ws, path)

    monkeypatch.setattr(app_module, "read_status", counting_read_status)

    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await wait_until(pilot, lambda: len(ticks) >= 2, "the timer ticks")
        await settle(app, pilot)

        started = threading.Event()
        release = threading.Event()

        def wait_for_release(log: LogFn, _cancel: threading.Event) -> None:
            started.set()
            release.wait(timeout=30)

        # No await between settle and start, so no timer tick can start a read in between.
        reads_before = len(reads)
        assert app.start("Wait for the test", wait_for_release)
        try:
            await wait_until(pilot, started.is_set, "the action runs")
            ticks_before = len(ticks)
            await wait_until(
                pilot, lambda: len(ticks) >= ticks_before + 3, "the timer ticks during the action"
            )

            assert len(reads) == reads_before
            assert not app.reading
        finally:
            release.set()

        await settle(app, pilot)
        # The status is read again after the action.
        assert len(reads) > reads_before
