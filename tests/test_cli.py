from __future__ import annotations

import re
import shutil
import sys
import time
import unicodedata
from pathlib import Path

import pytest
from conftest import Env
from helpers import (
    FakeStdin,
    append_config,
    cmux_calls,
    sh,
    write_executable,
    write_stub_cmux,
    write_stub_tmux,
)

from draupnir import __version__
from draupnir.cli import confirm, main
from draupnir.cmux import attach_argv, attach_line, session_name
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


def test_attach_ignores_session_false_in_the_config(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    clone = _attach_clone(env)
    append_config(env.root, "\n[cmux]\nsession = false\n")
    calls = write_stub_cmux(env.bin)

    code = main(["-C", str(env.root), "attach", "feature-a"])

    assert code == 0
    assert capsys.readouterr().out == attach_line(clone) + "\n"
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


def test_attach_with_tmux_prints_a_line_that_runs_tmux_on_the_session_socket(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    clone = _attach_clone(env)
    write_stub_cmux(env.bin, sessions={"sessions": [_running_entry(clone)]})
    write_stub_tmux(env.bin)

    code = main(["-C", str(env.root), "attach", "feature-a"])

    assert code == 0
    assert capsys.readouterr().out == (
        f"{env.bin / 'tmux'} -S /tmp/tmux-test/default attach-session -t '=shop-feature-a'\n"
    )


def test_attach_that_starts_the_session_uses_the_socket_of_the_server(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    _attach_clone(env)
    calls = write_stub_cmux(
        env.bin, sessions={"sessions": [], "socket_path": "/tmp/tmux-test/server"}
    )
    write_stub_tmux(env.bin)

    code = main(["-C", str(env.root), "attach", "feature-a"])

    assert code == 0
    assert capsys.readouterr().out.endswith(
        "-S /tmp/tmux-test/server attach-session -t '=shop-feature-a'\n"
    )
    assert len(_starts(calls)) == 1


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


# ----- draupnir new and the terminal session -------------------------------------------------


def _local_tmux_calls(calls: Path) -> list[list[str]]:
    if not calls.exists():
        return []
    return [call for call in cmux_calls(calls) if call[0] == "local-tmux"]


def test_new_starts_the_session_and_prints_the_attach_line(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = write_stub_cmux(env.bin)

    code = main(["-C", str(env.root), "new", "feature-a"])

    out = capsys.readouterr().out
    clone = env.root / "feature-a"
    assert code == 0
    assert clone.is_dir()
    assert cmux_calls(calls)[-2:] == [
        ["local-tmux", "list", "--json"],
        ["local-tmux", "start", "shop-feature-a", "--cwd", str(clone.resolve()), "--detached"],
    ]
    last = out.strip().splitlines()[-1]
    assert last == f"Attach to the terminal session from any terminal with: {attach_line(clone)}"


def test_new_with_tmux_prints_the_line_that_runs_tmux_on_the_server_socket(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    write_stub_cmux(env.bin, sessions={"sessions": [], "socket_path": "/tmp/tmux-test/server"})
    write_stub_tmux(env.bin)

    code = main(["-C", str(env.root), "new", "feature-a"])

    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert code == 0
    assert last == (
        "Attach to the terminal session from any terminal with: "
        f"{env.bin / 'tmux'} -S /tmp/tmux-test/server attach-session -t '=shop-feature-a'"
    )


def test_new_without_cmux_says_nothing_about_sessions(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["-C", str(env.root), "new", "feature-a"])

    out = capsys.readouterr().out
    assert code == 0
    assert (env.root / "feature-a").is_dir()
    assert "session" not in out
    assert "attach" not in out


def test_new_starts_no_session_when_session_is_false(env: Env) -> None:
    append_config(env.root, "\n[cmux]\nsession = false\n")
    calls = write_stub_cmux(env.bin)

    code = main(["-C", str(env.root), "new", "feature-a"])

    assert code == 0
    assert _local_tmux_calls(calls) == []


def test_new_keeps_the_clone_and_explains_when_the_session_cannot_start(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    write_stub_cmux(env.bin, fail=["local-tmux start"])

    code = main(["-C", str(env.root), "new", "feature-a"])

    out = capsys.readouterr().out
    assert code == 0
    assert (env.root / "feature-a").is_dir()
    assert "draupnir could not start the terminal session" in out
    assert "draupnir attach feature-a" in out


def test_new_keeps_the_clone_when_the_session_name_belongs_to_another_folder(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    other = env.tmp / "elsewhere" / "feature-a"
    entry = _running_entry(env.root / "feature-a", cwd=str(other))
    calls = write_stub_cmux(env.bin, sessions={"sessions": [entry]})

    code = main(["-C", str(env.root), "new", "feature-a"])

    out = capsys.readouterr().out
    assert code == 0
    assert (env.root / "feature-a").is_dir()
    assert str(other) in out
    assert _starts(calls) == []


def test_new_with_a_failed_checkout_starts_no_session(env: Env) -> None:
    real_git = shutil.which("git")
    write_executable(
        env.bin / "git",
        f'#!/bin/sh\n[ "$1" = checkout ] && exit 1\nexec {real_git} "$@"\n',
    )
    calls = write_stub_cmux(env.bin)

    code = main(["-C", str(env.root), "new", "feature/broken-checkout"])

    assert code == 1
    assert _local_tmux_calls(calls) == []


def test_new_with_a_failed_prepare_starts_no_session(env: Env) -> None:
    write_executable(env.bin / "mvn", '#!/bin/sh\necho "Apache Maven 3.8.1 (old)"\n')
    calls = write_stub_cmux(env.bin)

    code = main(["-C", str(env.root), "new", "feature/broken-setup"])

    assert code == 1
    assert (env.root / "feature-broken-setup").is_dir()
    assert _local_tmux_calls(calls) == []


def test_new_open_starts_the_ide_when_the_session_cannot_start(env: Env) -> None:
    write_stub_cmux(env.bin, fail=["local-tmux start"])

    code = main(["-C", str(env.root), "new", "feature-a", "--open"])

    assert code == 0
    deadline = time.monotonic() + 10
    while not env.idea_log.is_file() or not env.idea_log.read_text().strip():
        assert time.monotonic() < deadline, "The IDE was not started."
        time.sleep(0.05)
    assert str(env.root / "feature-a") in env.idea_log.read_text()


def test_new_starts_the_session_with_a_suffix_for_a_folder_with_unicode(env: Env) -> None:
    calls = write_stub_cmux(env.bin)

    code = main(["-C", str(env.root), "new", "fiks-baat", "--folder", "fiks-båt"])

    clone = env.root / "fiks-båt"
    name = session_name(clone)
    assert code == 0
    assert re.search(r"-[0-9a-f]{8}$", name)
    assert [call[2] for call in _starts(calls)] == [name]


def test_new_with_a_broken_cmux_command_logs_it_once(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DRAUPNIR_CMUX", '"broken')

    code = main(["-C", str(env.root), "new", "feature-a"])

    out = capsys.readouterr().out
    assert code == 0
    assert (env.root / "feature-a").is_dir()
    assert out.count("could not read the cmux command") == 1


def test_new_survives_an_oserror_while_it_reads_the_sessions(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_stub_cmux(env.bin, sessions={"sessions": [_running_entry(env.root / "feature-a")]})

    def broken(_a: object, _b: object) -> bool:
        raise OSError("the folder went away")

    monkeypatch.setattr("draupnir.cmux.same_folder", broken)

    code = main(["-C", str(env.root), "new", "feature-a"])

    out = capsys.readouterr().out
    assert code == 0
    assert (env.root / "feature-a").is_dir()
    assert out.count("the folder went away") == 1


# ----- draupnir new and the cmux workspace of the clone --------------------------------------

_JSON_FLAGS = ["--json", "--id-format", "both"]
_ATTACH_INTRO = "Attach to the terminal session from any terminal with: "
_NOT_IN_CMUX = (
    "draupnir does not run in a cmux terminal, so it did not open a cmux workspace for feature-a."
)
_UNREADABLE_WORKSPACES = "draupnir could not read the cmux workspaces"

# The first word of each cmux call that is about a workspace, and not about the session.
_WORKSPACE_VERBS = ("tree", "workspace-group", "workspace", "surface", "todo", "split-off")


def _in_cmux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CMUX_WORKSPACE_ID", "workspace-1")


def _workspace_calls(calls: Path) -> list[list[str]]:
    """The calls of the stub cmux that read, open or change a workspace."""
    if not calls.exists():
        return []
    return [call for call in cmux_calls(calls) if call[0] in _WORKSPACE_VERBS]


def _creates(calls: Path) -> list[list[str]]:
    return [call for call in _workspace_calls(calls) if call[:2] == ["workspace", "create"]]


def test_new_in_cmux_opens_a_workspace_that_shows_the_session(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _in_cmux(monkeypatch)
    calls = write_stub_cmux(env.bin)

    code = main(["-C", str(env.root), "new", "feature-a"])

    clone = env.root / "feature-a"
    title = f"{env.root.name} feature-a"
    made = cmux_calls(calls)
    start = ["local-tmux", "start", "shop-feature-a", "--cwd", str(clone.resolve()), "--detached"]
    assert code == 0
    # The workspace does not take the focus, so the user stays where new ran (Q5).
    assert made[made.index(start) + 1 :] == [
        ["tree", "--all", *_JSON_FLAGS],
        ["workspace-group", "list", *_JSON_FLAGS],
        [
            "workspace",
            "create",
            "--name",
            title,
            "--cwd",
            str(clone),
            "--focus",
            "false",
            "--command",
            attach_line(clone),
            *_JSON_FLAGS,
        ],
        [
            "surface",
            "resume",
            "set",
            "--workspace",
            "new-workspace",
            "--surface",
            "new-terminal",
            "--cwd",
            str(clone),
            "--kind",
            "tmux",
            "--name",
            title,
            "--",
            *attach_argv(clone),
        ],
        ["todo", "open", "--workspace", "new-workspace", *_JSON_FLAGS],
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
    assert _ATTACH_INTRO not in capsys.readouterr().out


def test_new_in_cmux_puts_the_workspace_in_the_group_of_the_caller(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _in_cmux(monkeypatch)
    groups = '{"groups": [{"id": "group-shop", "member_workspace_ids": ["workspace-1"]}]}'
    calls = write_stub_cmux(env.bin, groups)

    code = main(["-C", str(env.root), "new", "feature-a"])

    assert code == 0
    [create] = _creates(calls)
    assert create[create.index("--group") :][:6] == [
        "--group",
        "group-shop",
        "--group-placement",
        "afterCurrent",
        "--group-reference",
        "workspace-1",
    ]


def test_new_no_workspace_opens_none_and_prints_the_attach_line(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _in_cmux(monkeypatch)
    calls = write_stub_cmux(env.bin)

    code = main(["-C", str(env.root), "new", "feature-a", "--no-workspace"])

    out = capsys.readouterr().out
    assert code == 0
    assert _workspace_calls(calls) == []
    assert _starts(calls) != []
    assert out.strip().splitlines()[-1] == _ATTACH_INTRO + attach_line(env.root / "feature-a")


def test_new_workspace_overrides_clone_workspace_false(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _in_cmux(monkeypatch)
    append_config(env.root, "\n[cmux]\nclone-workspace = false\n")
    calls = write_stub_cmux(env.bin)

    code = main(["-C", str(env.root), "new", "feature-a", "--workspace"])

    assert code == 0
    assert len(_creates(calls)) == 1


def test_new_with_clone_workspace_false_opens_no_workspace_and_says_nothing(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _in_cmux(monkeypatch)
    append_config(env.root, "\n[cmux]\nclone-workspace = false\n")
    calls = write_stub_cmux(env.bin)

    code = main(["-C", str(env.root), "new", "feature-a"])

    out = capsys.readouterr().out
    assert code == 0
    assert _workspace_calls(calls) == []
    assert "cmux workspace" not in out
    assert _ATTACH_INTRO in out


def test_new_workspace_outside_cmux_warns_and_still_succeeds(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = write_stub_cmux(env.bin)

    code = main(["-C", str(env.root), "new", "feature-a", "--workspace"])

    out = capsys.readouterr().out
    assert code == 0
    assert out.count(_NOT_IN_CMUX) == 1
    assert _workspace_calls(calls) == []
    assert _ATTACH_INTRO in out


def test_new_outside_cmux_says_nothing_about_the_workspace_of_the_config(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    write_stub_cmux(env.bin)

    code = main(["-C", str(env.root), "new", "feature-a"])

    assert code == 0
    assert "does not run in a cmux terminal" not in capsys.readouterr().out


def test_new_keeps_the_session_when_cmux_refuses_the_workspace(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _in_cmux(monkeypatch)
    calls = write_stub_cmux(env.bin, fail=["workspace create"])

    code = main(["-C", str(env.root), "new", "feature-a"])

    out = capsys.readouterr().out
    assert code == 0
    assert out.count("failed with exit code 1") == 1
    assert [call for call in _workspace_calls(calls) if call[0] in ("surface", "todo")] == []
    assert _closes(calls) == []
    assert out.strip().splitlines()[-1] == _ATTACH_INTRO + attach_line(env.root / "feature-a")


def test_new_keeps_the_workspace_when_cmux_cannot_save_its_command(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _in_cmux(monkeypatch)
    calls = write_stub_cmux(env.bin, fail=["surface resume set"])

    code = main(["-C", str(env.root), "new", "feature-a"])

    out = capsys.readouterr().out
    assert code == 0
    assert "will not bring its terminal back after a restart" in out
    assert [call[0] for call in cmux_calls(calls)[-3:]] == ["todo", "split-off", "focus-panel"]
    assert _ATTACH_INTRO not in out


@pytest.mark.parametrize(
    ("tree", "fail"),
    [
        pytest.param(None, "tree", id="the tree fails"),
        pytest.param(
            {
                "windows": [
                    {
                        "workspaces": [
                            {
                                "id": "ws-a",
                                "ref": "workspace:1",
                                "title": "shop feature-a",
                                "panes": [
                                    {"surfaces": [{"id": "t-a", "ref": "t-a", "type": "terminal"}]}
                                ],
                            }
                        ]
                    }
                ]
            },
            "surface resume show",
            id="a binding cannot be read",
        ),
    ],
)
def test_new_opens_no_workspace_when_the_workspaces_cannot_be_read(
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tree: object,
    fail: str,
) -> None:
    """A failed read may hide a workspace of the clone, so new does not open a second one."""
    _in_cmux(monkeypatch)
    calls = write_stub_cmux(env.bin, tree=tree, fail=[fail])

    code = main(["-C", str(env.root), "new", "feature-a"])

    out = capsys.readouterr().out
    assert code == 0
    assert _creates(calls) == []
    assert out.count(_UNREADABLE_WORKSPACES) == 1
    assert _closes(calls) == []
    assert out.strip().splitlines()[-1] == _ATTACH_INTRO + attach_line(env.root / "feature-a")


def test_new_without_a_session_opens_a_plain_workspace(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _in_cmux(monkeypatch)
    append_config(env.root, "\n[cmux]\nsession = false\nclone-workspace = true\n")
    calls = write_stub_cmux(env.bin)

    code = main(["-C", str(env.root), "new", "feature-a"])

    assert code == 0
    assert _local_tmux_calls(calls) == []
    [create] = _creates(calls)
    assert "--command" not in create
    assert create[create.index("--focus") + 1] == "false"
    assert [call for call in cmux_calls(calls) if call[0] in ("tree", "surface")] == []
    assert _ATTACH_INTRO not in capsys.readouterr().out


# ----- draupnir remove and the terminal session ----------------------------------------------

_UNREADABLE_NOTE = (
    "draupnir could not read the terminal sessions, so it cannot say whether the clone has one."
)


def _closes(calls: Path) -> list[list[str]]:
    if not calls.exists():
        return []
    return [call for call in cmux_calls(calls) if call[:2] == ["local-tmux", "close"]]


def _answer_on_a_terminal(monkeypatch: pytest.MonkeyPatch, answer: str) -> list[str]:
    """Makes standard input a terminal that gives answer to every question.

    Returns the list that collects the questions.
    """
    monkeypatch.setattr(sys, "stdin", FakeStdin(is_a_tty=True))
    questions: list[str] = []

    def reply(prompt: str = "") -> str:
        questions.append(prompt)
        return answer

    monkeypatch.setattr("builtins.input", reply)
    return questions


def _remove_question(clone: Path, *lines: str) -> str:
    """The prompt of draupnir remove, with lines under the first line."""
    first = f"Delete {clone} with its .m2repo and local files?"
    return "\n".join([first, *(f"  {line}" for line in lines)]) + " [y/N] "


def test_remove_names_the_session_and_its_program_in_the_question(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = _attach_clone(env)
    write_stub_tmux(env.bin, "zsh\n2.1.301\n")
    calls = write_stub_cmux(env.bin, sessions={"sessions": [_running_entry(clone, clients=1)]})
    questions = _answer_on_a_terminal(monkeypatch, "n")

    code = main(["-C", str(env.root), "remove", "feature-a"])

    assert code == 0
    assert questions == [
        _remove_question(
            clone,
            "Its terminal session shop-feature-a is open, with 1 client attached.",
            "A program still runs in it: 2.1.301.",
            "The session is closed together with the clone.",
        )
    ]
    assert clone.is_dir()
    assert _closes(calls) == []


def test_remove_yes_closes_the_session_and_deletes_the_clone(env: Env) -> None:
    clone = _attach_clone(env)
    calls = write_stub_cmux(env.bin, sessions={"sessions": [_running_entry(clone)]})

    code = main(["-C", str(env.root), "remove", "feature-a", "--yes"])

    assert code == 0
    assert not clone.exists()
    assert _closes(calls) == [["local-tmux", "close", "shop-feature-a"]]


def test_remove_that_is_refused_keeps_the_session(env: Env) -> None:
    clone = _attach_clone(env)
    (clone / "notes.txt").write_text("not committed\n")
    calls = write_stub_cmux(env.bin, sessions={"sessions": [_running_entry(clone)]})

    code = main(["-C", str(env.root), "remove", "feature-a", "--yes"])

    assert code == 1
    assert clone.is_dir()
    assert _closes(calls) == []


def test_remove_force_closes_the_session_before_the_delete(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = _attach_clone(env)
    (clone / "notes.txt").write_text("not committed\n")
    calls = write_stub_cmux(env.bin, sessions={"sessions": [_running_entry(clone)]})
    real_rmtree = shutil.rmtree

    def logged_rmtree(path: Path) -> None:
        with calls.open("a") as handle:
            handle.write("rmtree\n")
        real_rmtree(path)

    monkeypatch.setattr(shutil, "rmtree", logged_rmtree)

    code = main(["-C", str(env.root), "remove", "feature-a", "--yes", "--force"])

    assert code == 0
    assert not clone.exists()
    assert cmux_calls(calls)[-2:] == [["local-tmux", "close", "shop-feature-a"], ["rmtree"]]


def test_remove_deletes_the_clone_when_the_session_cannot_be_closed(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    clone = _attach_clone(env)
    write_stub_cmux(
        env.bin, sessions={"sessions": [_running_entry(clone)]}, fail=["local-tmux close"]
    )

    code = main(["-C", str(env.root), "remove", "feature-a", "--yes"])

    out = capsys.readouterr().out
    assert code == 0
    assert not clone.exists()
    assert out.count("draupnir could not close the terminal session shop-feature-a.") == 1


def test_remove_leaves_the_session_of_another_folder_alone(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = _attach_clone(env)
    other = env.tmp / "elsewhere" / "feature-a"
    calls = write_stub_cmux(env.bin, sessions={"sessions": [_running_entry(clone, cwd=str(other))]})
    questions = _answer_on_a_terminal(monkeypatch, "y")

    code = main(["-C", str(env.root), "remove", "feature-a"])

    assert code == 0
    assert questions == [_remove_question(clone)]
    assert not clone.exists()
    assert _closes(calls) == []


def test_remove_leaves_open_the_session_that_draupnir_runs_in(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clone = _attach_clone(env)
    write_stub_tmux(env.bin)
    entry = _running_entry(clone)
    calls = write_stub_cmux(env.bin, sessions={"sessions": [entry]})
    monkeypatch.setenv("TMUX", f"{entry['socket_path']},4242,0")
    questions = _answer_on_a_terminal(monkeypatch, "y")

    code = main(["-C", str(env.root), "remove", "feature-a"])

    out = capsys.readouterr().out
    assert code == 0
    assert questions == [
        _remove_question(
            clone,
            "Its terminal session shop-feature-a is open, with no client attached.",
            "draupnir runs in this session, so the session stays open.",
        )
    ]
    assert not clone.exists()
    assert _closes(calls) == []
    assert "Type exit" in out


def test_remove_with_a_broken_cmux_command_still_asks_and_deletes(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clone = _attach_clone(env)
    monkeypatch.setenv("DRAUPNIR_CMUX", '"broken')
    questions = _answer_on_a_terminal(monkeypatch, "y")

    code = main(["-C", str(env.root), "remove", "feature-a"])

    out = capsys.readouterr().out
    assert code == 0
    assert questions == [_remove_question(clone, _UNREADABLE_NOTE)]
    assert not clone.exists()
    assert out.count("could not read the cmux command") == 1


def test_remove_with_an_entry_without_live_closes_nothing(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = _attach_clone(env)
    entry = _running_entry(clone)
    del entry["live"]
    calls = write_stub_cmux(env.bin, sessions={"sessions": [entry]})
    questions = _answer_on_a_terminal(monkeypatch, "n")

    assert main(["-C", str(env.root), "remove", "feature-a"]) == 0
    assert questions == [_remove_question(clone, _UNREADABLE_NOTE)]

    code = main(["-C", str(env.root), "remove", "feature-a", "--yes"])

    assert code == 0
    assert not clone.exists()
    assert _closes(calls) == []


def test_remove_survives_an_oserror_while_it_reads_the_sessions(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = _attach_clone(env)
    write_stub_cmux(env.bin, sessions={"sessions": [_running_entry(clone)]})

    def broken(_a: object, _b: object) -> bool:
        raise OSError("the folder went away")

    monkeypatch.setattr("draupnir.cmux.same_folder", broken)

    code = main(["-C", str(env.root), "remove", "feature-a", "--yes"])

    assert code == 0
    assert not clone.exists()


def test_new_parser_reads_the_workspace_flag() -> None:
    from draupnir.cli import build_parser

    parser = build_parser()
    assert parser.parse_args(["new", "x"]).workspace is None
    assert parser.parse_args(["new", "x", "--workspace"]).workspace is True
    assert parser.parse_args(["new", "x", "--no-workspace"]).workspace is False


# ----- draupnir remove closes the workspace of the clone (W5) ------------------------------------

_BOUND_SURFACE = "surface-bound"


def _clone_workspace_fixture(
    clone: Path, *others: tuple[str, str]
) -> tuple[dict[str, object], dict[str, object]]:
    """The tree and the bindings of the workspace that W2 makes, with workspace id ws-b.

    The bound terminal is in one pane, and the todo pane is in a pane below it. Each pair in
    others is the id and the type of a surface in a pane of its own.
    """

    def surface(surface_id: str, kind: str) -> dict[str, object]:
        return {"id": surface_id, "ref": f"ref-{surface_id}", "type": kind}

    panes = [
        {"surfaces": [surface(_BOUND_SURFACE, "terminal")]},
        {"surfaces": [surface("surface-todo", "workspaceTodo")]},
        *({"surfaces": [surface(sid, kind)]} for sid, kind in others),
    ]
    title = f"{clone.parent.name} {clone.name}"
    tree: dict[str, object] = {
        "windows": [
            {"workspaces": [{"id": "ws-b", "ref": "workspace-b", "title": title, "panes": panes}]}
        ]
    }
    binding = {
        "resume_binding": {
            "launch_command": {
                "arguments": ["cmux", "local-tmux", "attach", session_name(clone), "--headless"]
            },
            "cwd": str(clone),
        }
    }
    return tree, {f"ref-{_BOUND_SURFACE}": binding}


def _workspace_closes(calls: Path) -> list[list[str]]:
    if not calls.exists():
        return []
    return [call for call in cmux_calls(calls) if call[:2] == ["workspace", "close"]]


def _log_rmtree(monkeypatch: pytest.MonkeyPatch, calls: Path) -> None:
    real_rmtree = shutil.rmtree

    def logged_rmtree(path: Path) -> None:
        with calls.open("a") as handle:
            handle.write("rmtree\n")
        real_rmtree(path)

    monkeypatch.setattr(shutil, "rmtree", logged_rmtree)


def test_remove_in_cmux_closes_the_workspace_then_the_session_then_deletes(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = _attach_clone(env)
    tree, bindings = _clone_workspace_fixture(clone)
    calls = write_stub_cmux(
        env.bin, sessions={"sessions": [_running_entry(clone)]}, tree=tree, bindings=bindings
    )
    _in_cmux(monkeypatch)
    _log_rmtree(monkeypatch, calls)

    code = main(["-C", str(env.root), "remove", "feature-a", "--yes"])

    assert code == 0
    assert not clone.exists()
    writes = [
        call
        for call in cmux_calls(calls)
        if call[:3] == ["surface", "resume", "clear"]
        or call[:2] in (["workspace", "close"], ["local-tmux", "close"])
        or call == ["rmtree"]
    ]
    assert writes == [
        ["surface", "resume", "clear", "--workspace", "ws-b", "--surface", _BOUND_SURFACE],
        ["workspace", "close", "ws-b"],
        ["local-tmux", "close", "shop-feature-a"],
        ["rmtree"],
    ]


@pytest.mark.parametrize("kind", ["terminal", "browser", "markdown"])
def test_remove_in_cmux_leaves_a_workspace_with_another_tab_open(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    clone = _attach_clone(env)
    tree, bindings = _clone_workspace_fixture(clone, ("surface-other", kind))
    calls = write_stub_cmux(
        env.bin, sessions={"sessions": [_running_entry(clone)]}, tree=tree, bindings=bindings
    )
    _in_cmux(monkeypatch)

    code = main(["-C", str(env.root), "remove", "feature-a", "--yes"])

    out = capsys.readouterr().out
    assert code == 0
    assert _workspace_closes(calls) == []
    assert "has other tabs, so draupnir left it open." in out
    assert _closes(calls) == [["local-tmux", "close", "shop-feature-a"]]
    assert not clone.exists()


def test_remove_in_cmux_with_an_unreadable_workspace_list_still_closes_the_session(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clone = _attach_clone(env)
    tree, bindings = _clone_workspace_fixture(clone)
    calls = write_stub_cmux(
        env.bin,
        sessions={"sessions": [_running_entry(clone)]},
        tree=tree,
        bindings=bindings,
        fail=["tree"],
    )
    _in_cmux(monkeypatch)

    code = main(["-C", str(env.root), "remove", "feature-a", "--yes"])

    out = capsys.readouterr().out
    assert code == 0
    assert _workspace_closes(calls) == []
    assert "so it left any workspace of feature-a open." in out
    assert _closes(calls) == [["local-tmux", "close", "shop-feature-a"]]
    assert not clone.exists()


def test_remove_leaves_the_workspace_that_draupnir_runs_in_open(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clone = _attach_clone(env)
    tree, bindings = _clone_workspace_fixture(clone)
    calls = write_stub_cmux(
        env.bin, sessions={"sessions": [_running_entry(clone)]}, tree=tree, bindings=bindings
    )
    monkeypatch.setenv("CMUX_WORKSPACE_ID", "ws-b")

    code = main(["-C", str(env.root), "remove", "feature-a", "--yes"])

    out = capsys.readouterr().out
    assert code == 0
    assert _workspace_closes(calls) == []
    assert "draupnir runs in the cmux workspace" in out
    assert _closes(calls) == [["local-tmux", "close", "shop-feature-a"]]


def test_remove_closes_neither_when_draupnir_runs_in_the_session(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clone = _attach_clone(env)
    tree, bindings = _clone_workspace_fixture(clone)
    entry = _running_entry(clone)
    calls = write_stub_cmux(env.bin, sessions={"sessions": [entry]}, tree=tree, bindings=bindings)
    _in_cmux(monkeypatch)
    monkeypatch.setenv("TMUX", f"{entry['socket_path']},4242,0")

    code = main(["-C", str(env.root), "remove", "feature-a", "--yes"])

    out = capsys.readouterr().out
    assert code == 0
    assert _workspace_closes(calls) == []
    assert _closes(calls) == []
    assert "so it left the cmux workspace" in out
    assert "Type exit" in out


def test_remove_outside_cmux_closes_the_session_and_says_it_cannot_close_the_workspace(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    clone = _attach_clone(env)
    calls = write_stub_cmux(env.bin, sessions={"sessions": [_running_entry(clone)]})

    code = main(["-C", str(env.root), "remove", "feature-a", "--yes"])

    out = capsys.readouterr().out
    assert code == 0
    assert [call for call in cmux_calls(calls) if call[0] == "tree"] == []
    assert _closes(calls) == [["local-tmux", "close", "shop-feature-a"]]
    assert "so it cannot close a cmux workspace of feature-a." in out


def test_remove_deletes_the_clone_when_the_workspace_cannot_be_closed(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clone = _attach_clone(env)
    tree, bindings = _clone_workspace_fixture(clone)
    calls = write_stub_cmux(
        env.bin,
        sessions={"sessions": [_running_entry(clone)]},
        tree=tree,
        bindings=bindings,
        fail=["workspace close"],
    )
    _in_cmux(monkeypatch)

    code = main(["-C", str(env.root), "remove", "feature-a", "--yes"])

    out = capsys.readouterr().out
    assert code == 0
    assert "draupnir could not close the cmux workspace" in out
    assert _closes(calls) == [["local-tmux", "close", "shop-feature-a"]]
    assert not clone.exists()
