"""Tests for opening a branch from GitHub as a new clone, from the Projects tab."""

from __future__ import annotations

from conftest import Env
from helpers import logger, settle, sh

from draupnir.setup import new_clone
from draupnir.ui.app import DraupnirApp
from draupnir.ui.branchpicker import BranchPickerScreen, matching_branches

SIZE = (160, 50)


def test_matching_branches_finds_text_anywhere_in_the_name_ignoring_case() -> None:
    branches = ["feature/vessel-owner", "bugfix/Owner-name", "feature/ports", "main"]

    assert matching_branches(branches, "") == branches
    assert matching_branches(branches, "owner") == ["feature/vessel-owner", "bugfix/Owner-name"]
    assert matching_branches(branches, "sel-own") == ["feature/vessel-owner"]
    assert matching_branches(branches, "nothing") == []


def _push_branches(env: Env, *branches: str) -> None:
    other = env.tmp / "pusher"
    sh(env.tmp, "git", "clone", "-q", str(env.remote), str(other))
    for branch in branches:
        sh(other, "git", "push", "-q", "origin", f"main:{branch}")
    sh(env.main, "git", "fetch", "-q")


def _shown(screen: BranchPickerScreen) -> list[str]:
    options = screen.options
    return [str(options.get_option_at_index(i).id) for i in range(options.option_count)]


async def test_open_branch_filters_while_typing_and_creates_the_clone(env: Env) -> None:
    _push_branches(env, "feature/vessel-owner", "bugfix/owner-name", "feature/taken")
    _, log = logger()
    new_clone(env.ws(), "feature/taken", None, log)

    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)

        await pilot.press("b")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, BranchPickerScreen)
        # A branch that already has a clone is left out, and so is main.
        assert _shown(screen) == ["bugfix/owner-name", "feature/vessel-owner"]

        screen.open_checkbox.value = False
        await pilot.press(*"owner")
        await pilot.pause()
        assert _shown(screen) == ["bugfix/owner-name", "feature/vessel-owner"]

        await pilot.press(*"-n")
        await pilot.pause()
        assert _shown(screen) == ["bugfix/owner-name"]

        await pilot.press("enter")
        await settle(app, pilot)

        assert not isinstance(app.screen, BranchPickerScreen)
        assert (env.root / "bugfix-owner-name").is_dir()
        assert sh(env.root / "bugfix-owner-name", "git", "branch", "--show-current") == (
            "bugfix/owner-name"
        )


async def test_open_branch_moves_with_the_arrow_keys_and_escape_cancels(env: Env) -> None:
    _push_branches(env, "feature/one", "feature/two")

    app = DraupnirApp(env.ws())
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)

        await pilot.press("b")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, BranchPickerScreen)
        await pilot.press(*"feature", "down")
        await pilot.pause()
        assert screen.options.highlighted_option is not None
        assert screen.options.highlighted_option.id == "feature/two"

        await pilot.press("escape")
        await settle(app, pilot)

        assert not isinstance(app.screen, BranchPickerScreen)
        assert not (env.root / "feature-two").exists()
