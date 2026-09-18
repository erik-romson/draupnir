"""Tests for the New clone tab (R12, R14)."""

from __future__ import annotations

import pytest
from conftest import Env
from helpers import settle
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
