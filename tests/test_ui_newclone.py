"""Tests for the New clone tab (R12, R14)."""

from __future__ import annotations

import pytest
from conftest import Env
from helpers import cmux_calls, settle, write_stub_cmux
from textual.pilot import Pilot
from textual.widgets import TabbedContent

from draupnir.ui.app import DraupnirApp

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


def _row_names(app: DraupnirApp) -> list[str]:
    table = app.projects.table
    return [table.get_row_at(index)[0].plain for index in range(table.row_count)]


async def test_new_clone_tab_creates_a_clone_and_the_table_shows_it(env: Env) -> None:
    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)

        await pilot.press("n")
        await pilot.pause()
        assert app.query_one("#tabs", TabbedContent).active == "new-clone"

        app.newclone.branch_input.value = "feature/from-ui"
        await pilot.click("#create")
        await settle(app, pilot)

        assert "feature-from-ui" in _row_names(app)
        assert (env.root / "feature-from-ui").is_dir()


async def test_new_clone_tab_creates_the_clone_in_the_folder_it_is_given(env: Env) -> None:
    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)

        await pilot.press("n")
        await pilot.pause()
        app.newclone.branch_input.value = "feature/from-ui"
        await pilot.pause()
        assert app.newclone.folder_input.placeholder == "feature-from-ui"

        app.newclone.folder_input.value = "short"
        await pilot.click("#create")
        await settle(app, pilot)

        assert "short" in _row_names(app)
        assert (env.root / "short").is_dir()
        assert not (env.root / "feature-from-ui").exists()


async def test_branch_argument_fills_in_the_form_and_opens_the_tab(env: Env) -> None:
    app = DraupnirApp(env.ws(), initial_branch="feature/typed-in")
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)

        assert app.query_one("#tabs", TabbedContent).active == "new-clone"
        assert app.newclone.branch_input.value == "feature/typed-in"
        assert app.newclone.branch_input.has_focus


async def test_new_clone_tab_shows_a_message_for_an_empty_name(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        notifications = _record_notifications(app, monkeypatch)

        await pilot.press("n")
        await pilot.pause()
        assert app.newclone.branch_input.value == ""

        await pilot.click("#create")
        await pilot.pause()

        assert not app.busy
        assert len(app.workers) == 0
        assert any(severity == "warning" for _, _, severity in notifications.shown)


def _output_text(app: DraupnirApp) -> str:
    return "\n".join(strip.text.rstrip() for strip in app.output.log_view.lines)


async def _create_clone(app: DraupnirApp, pilot: Pilot[None], branch: str) -> str:
    await settle(app, pilot)
    await pilot.press("n")
    await pilot.pause()
    app.newclone.branch_input.value = branch
    await pilot.click("#create")
    await settle(app, pilot)
    return _output_text(app)


async def test_new_clone_tab_starts_the_terminal_session(env: Env) -> None:
    calls = write_stub_cmux(env.bin)
    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        text = await _create_clone(app, pilot, "feature-a")

    assert "=== Done: Create a clone for feature-a" in text
    starts = [call for call in cmux_calls(calls) if call[:2] == ["local-tmux", "start"]]
    assert [call[2] for call in starts] == ["shop-feature-a"]


async def test_new_clone_tab_ends_done_when_the_session_cannot_start(env: Env) -> None:
    write_stub_cmux(env.bin, fail=["local-tmux start"])
    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        text = await _create_clone(app, pilot, "feature-a")

    assert "=== Done: Create a clone for feature-a" in text
    assert "draupnir could not start the terminal session" in text
    assert (env.root / "feature-a").is_dir()


async def test_new_clone_tab_ends_done_with_a_broken_cmux_command(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DRAUPNIR_CMUX", '"broken')
    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        text = await _create_clone(app, pilot, "feature-a")

    assert "=== Done: Create a clone for feature-a" in text
    assert text.count("could not read the cmux command") == 1
