from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path

import pytest
from conftest import Env
from helpers import FakeStdin, cmux_calls, sh, write_stub_cmux

from draupnir import __version__
from draupnir.cli import confirm, main
from draupnir.cmux import attach_line, session_name
from draupnir.errors import OperationError
from draupnir.setup import new_clone
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
        "attach",
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


# ----- draupnir attach ----------------------------------------------------------------------


def _attach_clone(env: Env, branch: str = "feature-a", folder: str | None = None) -> Path:
    return new_clone(env.ws(), branch, None, lambda _line: None, folder)


def _running_entry(folder: Path, **changes: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "session_name": session_name(folder),
        "cwd": str(folder.resolve()),
        "live": True,
        "socket_path": "/tmp/tmux-test/default",
        "session_id": "$0",
        "clients": 0,
    }
    fields.update(changes)
    return fields


def _starts(calls: Path) -> list[list[str]]:
    # The stub writes its log with the first call, so a run that never called cmux has none.
    if not calls.exists():
        return []
    return [call for call in cmux_calls(calls) if call[:2] == ["local-tmux", "start"]]


def test_attach_starts_the_session_and_prints_the_line(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    clone = _attach_clone(env)
    calls = write_stub_cmux(env.bin)

    code = main(["-C", str(clone), "attach", "feature-a"])

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == attach_line(clone) + "\n"
    assert "local-tmux start" in captured.err
    assert _starts(calls) == [
        ["local-tmux", "start", "shop-feature-a", "--cwd", str(clone.resolve()), "--detached"]
    ]


def test_attach_does_not_start_a_session_that_runs(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    clone = _attach_clone(env)
    calls = write_stub_cmux(env.bin, sessions={"sessions": [_running_entry(clone)]})

    code = main(["-C", str(env.root), "attach", "feature-a"])

    assert code == 0
    assert capsys.readouterr().out == attach_line(clone) + "\n"
    assert _starts(calls) == []


def test_attach_without_a_folder_uses_the_clone_that_holds_the_current_folder(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clone = _attach_clone(env)
    write_stub_cmux(env.bin)
    inside = clone / "src"
    inside.mkdir()
    monkeypatch.chdir(inside)

    code = main(["attach"])

    assert code == 0
    assert capsys.readouterr().out == attach_line(clone) + "\n"


def test_attach_to_a_folder_that_is_no_clone_exits_with_one(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    _attach_clone(env)
    calls = write_stub_cmux(env.bin)

    code = main(["-C", str(env.root), "attach", "typo"])

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert "typo" in captured.err
    assert "is not a clone of this workspace" in captured.err
    assert _starts(calls) == []


def test_attach_to_main_uses_the_session_name_of_main(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    write_stub_cmux(env.bin)

    code = main(["-C", str(env.root), "attach", "main"])

    out = capsys.readouterr().out
    assert code == 0
    assert out == attach_line(env.main) + "\n"
    assert out.strip().endswith("local-tmux attach shop-main --headless")


def test_attach_finds_a_folder_that_macos_stores_in_another_unicode_form(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    nfd = unicodedata.normalize("NFD", "fiks-båt")
    nfc = unicodedata.normalize("NFC", "fiks-båt")
    assert nfd != nfc
    clone = _attach_clone(env, "fiks-baat", folder=nfd)
    write_stub_cmux(env.bin)

    code = main(["-C", str(env.root), "attach", nfc])

    out = capsys.readouterr().out
    assert code == 0
    assert out == attach_line(clone) + "\n"
    assert session_name(clone) in out
    assert re.search(r"-[0-9a-f]{8} ", out)


def test_attach_without_cmux_exits_with_one(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    _attach_clone(env)

    code = main(["-C", str(env.root), "attach", "feature-a"])

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert "found no cmux command line tool" in captured.err


def test_attach_with_a_broken_cmux_command_exits_with_one(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _attach_clone(env)
    monkeypatch.setenv("DRAUPNIR_CMUX", '"broken')

    code = main(["-C", str(env.root), "attach", "feature-a"])

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert "could not read the cmux command" in captured.err


def test_attach_to_a_foreign_session_exits_with_one(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    clone = _attach_clone(env)
    other = env.tmp / "elsewhere" / "feature-a"
    calls = write_stub_cmux(env.bin, sessions={"sessions": [_running_entry(clone, cwd=str(other))]})

    code = main(["-C", str(env.root), "attach", "feature-a"])

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert str(other) in captured.err
    assert _starts(calls) == []


def test_attach_with_a_failed_start_exits_with_one(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    _attach_clone(env)
    write_stub_cmux(env.bin, fail=["local-tmux start"])

    code = main(["-C", str(env.root), "attach", "feature-a"])

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert "could not start the terminal session" in captured.err
