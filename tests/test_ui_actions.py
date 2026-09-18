"""Tests for the dashboard actions built in Steps 9 to 12: Open IDE, Remove clone, Rebase, Push
and follow build, Stop waiting, and Fast-forward main (R7, R13, R17, R18, R19, R21)."""

from __future__ import annotations

from conftest import Env
from fake_gh import CheckRun, Poll, running
from helpers import append_config, commit, logger, settle, sh, wait_until

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
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        assert folder.exists()

        await pilot.press("y")
        await settle(app, pilot)

        assert not folder.exists()
        assert "feature-remove-me" not in _row_names(app)
        assert "=== Done: Remove feature-remove-me" in _output_lines(app)


async def test_ui_remove_answered_no_keeps_the_clone(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/keep-me", None, log)

    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        _select(app, "feature-keep-me")
        await pilot.pause()

        await pilot.click("#remove")
        await pilot.pause()
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
        await pilot.pause()
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


def _set_build_timeout(env: Env, seconds: float) -> None:
    """Gives the wait a long timeout, so a slow machine never ends a UI test's wait early."""
    config = env.root / "draupnir.toml"
    text = config.read_text()
    assert "timeout-seconds = 2\n" in text
    config.write_text(text.replace("timeout-seconds = 2\n", f"timeout-seconds = {seconds}\n"))


def _build_cell(app: DraupnirApp, name: str) -> str:
    return app.projects.table.get_cell(name, "build").plain


async def test_ui_push_and_follow_updates_the_build_column_while_waiting(env: Env) -> None:
    _set_build_timeout(env, 120)
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
        # The action still waits, so the column changed while waiting.
        assert app.busy
        assert sh(env.remote, "git", "rev-parse", "feature/follow") == sha
        # The details line shows the jobs of the selected clone's build.
        assert "build: pending" in str(app.projects.details.content)

        env.gh.set_build(sha, [Poll(check_runs=[CheckRun("build")])])
        await settle(app, pilot)

        assert _build_cell(app, "feature-follow") == "success"
        assert app.builds["feature-follow"].sha == sha
        lines = _output_lines(app)
        assert "=== Done: Push feature-follow and follow the build" in lines
        assert "  build: success" in lines


async def test_ui_stop_waiting_ends_the_action_and_keeps_the_last_state(env: Env) -> None:
    _set_build_timeout(env, 120)
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
        assert app.busy
        assert not app.output.stop_button.disabled

        await pilot.click("#stop-waiting")
        await settle(app, pilot)

        assert not app.busy
        assert app.output.stop_button.disabled
        # The column keeps the last state it saw. The build on GitHub was not touched: the fake
        # gh was only asked to read, and the build there still runs.
        assert _build_cell(app, "feature-stop") == "pending"
        assert app.builds["feature-stop"].state == "pending"
        assert all(call.args[0] == "api" for call in env.gh.calls())
        lines = _output_lines(app)
        assert "=== Stopped waiting: Push feature-stop and follow the build" in lines
        assert not any(line.startswith("=== Done: Push feature-stop") for line in lines)
        text = " ".join(lines)
        assert "The build on GitHub keeps running." in text

        # A new action starts once the wait has ended.
        await pilot.click("#build-status")
        await settle(app, pilot)
        assert "=== Done: Build status of feature-stop" in _output_lines(app)


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


def _confirm_question(app: DraupnirApp) -> str:
    screen = app.screen
    assert isinstance(screen, ConfirmScreen)
    return screen.question


async def test_ui_pr_flow_asks_to_remove_the_clone_after_the_merge(env: Env) -> None:
    _set_build_timeout(env, 120)
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
    _set_build_timeout(env, 120)
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
