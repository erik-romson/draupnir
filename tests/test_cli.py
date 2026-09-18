from __future__ import annotations

import sys

import pytest
from conftest import Env
from helpers import FakeStdin, sh

from draupnir import __version__
from draupnir.cli import confirm, main
from draupnir.errors import OperationError
from draupnir.ui.app import DraupnirApp


def test_help_lists_every_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0

    out = capsys.readouterr().out
    for name in (
        "ui",
        "init",
        "new",
        "prepare",
        "remove",
        "status",
        "fetch",
        "rebase",
        "push",
        "build",
        "ff-main",
        "pr",
        "open",
    ):
        assert name in out


def test_command_help_describes_the_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["init", "--help"])
    assert exc_info.value.code == 0

    out = capsys.readouterr().out
    assert "Write the workspace files and clone the project into main/." in out
    assert "--main-branch" in out


def test_unknown_subcommand_exits_with_two(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["not-a-real-command"])
    assert exc_info.value.code == 2


def test_version_prints_the_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0

    out = capsys.readouterr().out
    assert __version__ in out


def test_confirm_without_a_terminal_asks_for_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", FakeStdin(is_a_tty=False))

    with pytest.raises(OperationError) as exc_info:
        confirm("Delete the folder?", yes=False)

    assert "--yes" in str(exc_info.value)


def test_confirm_with_yes_does_not_ask(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_input(prompt: str = "") -> str:
        raise AssertionError("confirm must not ask when yes is true")

    monkeypatch.setattr("builtins.input", fail_input)

    assert confirm("Delete the folder?", yes=True) is True


def test_prepare_command_works_on_the_current_clone(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(env.main)

    code = main(["prepare"])

    assert code == 0
    assert (env.main / ".mvn" / "maven.config").is_file()
    out = capsys.readouterr().out
    assert "Maven" in out


def test_command_outside_a_workspace_exits_with_one(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    elsewhere = env.tmp / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    code = main(["prepare"])

    assert code == 1
    assert "draupnir.toml" in capsys.readouterr().err


def _record_app_runs(monkeypatch: pytest.MonkeyPatch) -> list[DraupnirApp]:
    """Replaces DraupnirApp.run, so a test sees which app would start without starting it."""
    started: list[DraupnirApp] = []

    def fake_run(self: DraupnirApp) -> None:
        started.append(self)

    monkeypatch.setattr(DraupnirApp, "run", fake_run)
    return started


def test_chdir_to_a_missing_folder_exits_with_one(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = env.tmp / "missing-folder"

    assert main(["-C", str(missing), "status"]) == 1
    assert str(missing) in capsys.readouterr().err


def test_init_command_takes_the_main_branch(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    other = env.tmp / "pusher-develop"
    sh(env.tmp, "git", "clone", "-q", str(env.remote), str(other))
    sh(other, "git", "push", "-q", "origin", "main:develop")
    monkeypatch.chdir(env.root)

    assert main(["init", "--main-branch", "develop"]) == 0
    assert "main branch is develop" in capsys.readouterr().out


def test_no_subcommand_starts_the_dashboard(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    started = _record_app_runs(monkeypatch)
    monkeypatch.chdir(env.main)

    assert main([]) == 0

    assert len(started) == 1
    assert started[0].ws.root == env.root
    assert started[0].initial_branch is None


def test_ui_command_keeps_the_branch_for_the_new_clone_tab(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = _record_app_runs(monkeypatch)
    monkeypatch.chdir(env.root)

    assert main(["ui", "feature/from-the-command-line"]) == 0

    assert [app.initial_branch for app in started] == ["feature/from-the-command-line"]


def test_ui_with_a_broken_config_stops_before_the_app_starts(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    started = _record_app_runs(monkeypatch)
    (env.root / "draupnir.toml").write_text("[build]\npoll-second = 1\n")
    monkeypatch.chdir(env.root)

    assert main(["ui"]) == 1

    assert started == []
    assert "poll-second" in capsys.readouterr().err


def test_ui_outside_a_workspace_stops_before_the_app_starts(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    started = _record_app_runs(monkeypatch)
    elsewhere = env.tmp / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert main([]) == 1

    assert started == []
    assert "No workspace found" in capsys.readouterr().err
