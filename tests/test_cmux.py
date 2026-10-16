"""Tests for src/draupnir/cmux.py: finding the cmux command, the line for its sidebar, and the
terminal session of a clone."""

from __future__ import annotations

import os
import re
import shutil
import stat
import unicodedata
from pathlib import Path

import pytest
from conftest import Env
from helpers import cmux_calls, logger, write_stub_cmux, write_stub_tmux

from draupnir.cmux import (
    GREEN,
    RED,
    STATUS_ICON,
    STATUS_KEY,
    YELLOW,
    SidebarStatus,
    TerminalSession,
    attach_line,
    clear_sidebar_status,
    close_session,
    cmux_command,
    ensure_session,
    read_session,
    runs_in_session,
    same_folder,
    session_environment,
    session_name,
    session_programs,
    set_sidebar_status,
    sidebar_status,
)
from draupnir.errors import OperationError
from draupnir.github import FAILURE, SUCCESS, BuildState
from draupnir.status import ProjectStatus

HEAD = "a" * 40


def _status(name: str, **changes: object) -> ProjectStatus:
    """A clean clone called name, with the given fields changed."""
    fields: dict[str, object] = {
        "folder": Path("/workspace") / name,
        "branch": "main",
        "ahead_main": 0,
        "behind_main": 0,
        "upstream": f"origin/{name}",
        "upstream_gone": False,
        "ahead_upstream": 0,
        "behind_upstream": 0,
        "changed": 0,
        "untracked": 0,
        "operation": None,
        "error": None,
        "head": HEAD,
    }
    fields.update(changes)
    return ProjectStatus(**fields)  # type: ignore[arg-type]


def _executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


# ----- The line for the sidebar ---------------------------------------------------------


def test_sidebar_status_is_green_when_every_clone_is_clean() -> None:
    status = sidebar_status([_status("main"), _status("feature-a")], {})

    assert status is not None
    assert (status.text, status.color, status.priority) == ("clean", GREEN, 10)


def test_sidebar_status_is_none_without_clones() -> None:
    """Before the first read there is nothing to say, so the sidebar keeps its last line."""
    assert sidebar_status([], {}) is None


def test_sidebar_status_names_the_clone_that_has_an_operation_in_progress() -> None:
    statuses = [_status("main"), _status("feature-a", operation="rebase")]

    status = sidebar_status(statuses, {})

    assert status is not None
    assert (status.text, status.color, status.priority) == ("rebase in feature-a", RED, 30)


def test_sidebar_status_counts_clones_that_have_local_changes() -> None:
    statuses = [
        _status("main", changed=2),
        _status("feature-a", untracked=1),
        _status("feature-b"),
    ]

    status = sidebar_status(statuses, {})

    assert status is not None
    assert (status.text, status.color) == ("2 clones are changed", YELLOW)


def test_sidebar_status_reports_commits_that_are_not_on_github() -> None:
    statuses = [_status("main"), _status("feature-a", upstream=None, ahead_upstream=None)]

    status = sidebar_status(statuses, {})

    assert status is not None
    assert (status.text, status.color) == ("feature-a is not pushed", YELLOW)


def test_sidebar_status_reports_a_branch_that_github_no_longer_has() -> None:
    """A merged branch asks for a cleanup, so it is yellow and not red."""
    statuses = [
        _status("main"),
        _status("feature-a", upstream_gone=True, ahead_upstream=None, behind_upstream=None),
    ]

    status = sidebar_status(statuses, {})

    assert status is not None
    assert (status.text, status.color) == ("feature-a is deleted on GitHub", YELLOW)


def test_sidebar_status_reports_a_failed_build_of_the_current_commit() -> None:
    statuses = [_status("main"), _status("feature-a")]
    builds = {"feature-a": BuildState(sha=HEAD, state=FAILURE)}

    status = sidebar_status(statuses, builds)

    assert status is not None
    assert (status.text, status.color) == ("build failed in feature-a", RED)


def test_sidebar_status_ignores_a_failed_build_of_an_older_commit() -> None:
    """The clone has moved on since that build, so the failure says nothing about it now."""
    statuses = [_status("main"), _status("feature-a")]
    builds = {"feature-a": BuildState(sha="b" * 40, state=FAILURE)}

    status = sidebar_status(statuses, builds)

    assert status is not None
    assert (status.text, status.color) == ("clean", GREEN)


def test_sidebar_status_ignores_a_build_that_did_not_fail() -> None:
    builds = {"main": BuildState(sha=HEAD, state=SUCCESS)}

    status = sidebar_status([_status("main")], builds)

    assert status is not None
    assert (status.text, status.color) == ("clean", GREEN)


def test_sidebar_status_reports_a_clone_that_cannot_be_read() -> None:
    statuses = [_status("main"), _status("broken", error="fatal: not a git repository")]

    status = sidebar_status(statuses, {})

    assert status is not None
    assert (status.text, status.color) == ("broken cannot be read", RED)


def test_sidebar_status_puts_the_worst_part_first_and_keeps_three() -> None:
    """A narrow sidebar cuts the line off, so the part that matters most comes first."""
    statuses = [
        _status("main", changed=1),
        _status("feature-a", operation="merge"),
        _status("feature-b", upstream=None, ahead_upstream=None),
        _status("feature-c", upstream_gone=True, ahead_upstream=None, behind_upstream=None),
    ]
    builds = {"feature-a": BuildState(sha=HEAD, state=FAILURE)}

    status = sidebar_status(statuses, builds)

    assert status is not None
    assert status.color == RED
    assert status.text == "merge in feature-a · build failed in feature-a · main is changed"


# ----- Writing the line into the sidebar ------------------------------------------------


def test_set_sidebar_status_names_the_key_the_icon_the_colour_and_the_workspace(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cmux marks its own agent status with an icon, so draupnir marks its line the same way.

    The workspace is named in the call, so the line stays on the dashboard's own row after the
    user has switched to another workspace.
    """
    calls = write_stub_cmux(env.bin)
    monkeypatch.setenv("CMUX_WORKSPACE_ID", "workspace-1")

    set_sidebar_status(SidebarStatus(text="clean", color=GREEN), env.main)

    assert cmux_calls(calls) == [
        [
            "set-status",
            STATUS_KEY,
            "clean",
            "--icon",
            STATUS_ICON,
            "--color",
            GREEN,
            "--priority",
            "10",
            "--workspace",
            "workspace-1",
        ]
    ]


def test_clear_sidebar_status_takes_the_line_off_the_workspace_row(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = write_stub_cmux(env.bin)
    monkeypatch.setenv("CMUX_WORKSPACE_ID", "workspace-1")

    clear_sidebar_status(env.main)

    assert cmux_calls(calls) == [["clear-status", STATUS_KEY, "--workspace", "workspace-1"]]


# ----- Finding the cmux command ---------------------------------------------------------


def test_no_test_reaches_the_cmux_of_the_desktop(env: Env) -> None:
    """A suite that runs inside a cmux terminal must not call the user's own cmux.

    cmux puts its command line tool on the PATH of every terminal it starts, so the fixture
    builds the PATH from its own stubs and the system folders instead of inheriting it. A test
    that needs a cmux writes a stub of its own.
    """
    assert shutil.which("cmux") is None
    assert cmux_command() is None


def test_cmux_command_reads_the_variable_first(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRAUPNIR_CMUX", "'/opt/my cmux/cmux' --verbose")

    assert cmux_command() == ["/opt/my cmux/cmux", "--verbose"]


def test_cmux_command_takes_the_tool_on_the_path(env: Env) -> None:
    """The stub cmux of the tests sits on the PATH, so it is the one that runs."""
    stub = _executable(env.bin / "cmux")

    assert cmux_command() == [str(stub)]


def test_cmux_command_falls_back_to_the_path_cmux_gives(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Outside a cmux terminal the tool is not on the PATH, but cmux names it in its own
    variable."""
    bundled = _executable(env.tmp / "bundled-cmux")
    monkeypatch.setenv("CMUX_BUNDLED_CLI_PATH", str(bundled))

    assert cmux_command() == [str(bundled)]


def test_cmux_command_is_none_when_cmux_is_not_installed(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("draupnir.cmux._BUNDLED_COMMAND", env.tmp / "no-cmux-here")

    assert cmux_command() is None


def test_cmux_command_skips_a_path_that_cannot_be_run(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cmux may name a tool that an update has removed, and a folder is not a tool either."""
    monkeypatch.setenv("CMUX_BUNDLED_CLI_PATH", str(env.tmp / "gone"))
    bundled = _executable(env.tmp / "bundled-cmux")
    monkeypatch.setattr("draupnir.cmux._BUNDLED_COMMAND", bundled)

    assert cmux_command() == [str(bundled)]
    assert not os.access(env.tmp / "gone", os.X_OK)


# ----- The session name, the folder comparison and the attach line -----------------------

_VALID_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,127}$")
_HASH = r"[0-9a-f]{8}"


def _name(folder: Path | str) -> str:
    name = session_name(Path(folder))
    assert _VALID_NAME.match(name), name
    assert len(name) <= 128
    return name


def test_session_name_of_a_plain_clone_is_the_readable_name() -> None:
    assert _name("/w/shop/feature-login") == "shop-feature-login"
    assert _name("/w/shop/main") == "shop-main"


@pytest.mark.parametrize(
    ("folder", "pattern"),
    [
        ("/w/shop/release-1.2.3", rf"shop-release-1_2_3-{_HASH}"),
        ("/w/my shop/feat 1", rf"my-shop-feat-1-{_HASH}"),
        ("/w/-x/feat", rf"x-feat-{_HASH}"),
        ("/w/shop/fiks-båt", rf"shop-fiks-b-t-{_HASH}"),
        ("/w/shop/語", rf"shop-{_HASH}"),
        ("/w/日本/語", rf"clone-{_HASH}"),
    ],
)
def test_session_name_of_a_changed_name_ends_with_a_hash(folder: str, pattern: str) -> None:
    assert re.fullmatch(pattern, _name(folder))


def test_session_name_hash_is_eight_hex_characters_of_the_path() -> None:
    import hashlib

    folder = Path("/w/shop/release-1.2.3")
    digest = hashlib.sha256(b"/w/shop/release-1.2.3").hexdigest()[:8]

    assert _name(folder) == f"shop-release-1_2_3-{digest}"


def test_session_name_keeps_folders_with_the_same_readable_part_apart() -> None:
    assert _name("/w/shop/fiks-båt") != _name("/w/shop/fiks-bøt")


def test_session_name_of_very_long_folders_has_128_characters_and_differs() -> None:
    first = _name("/w/shop/" + "a" * 200 + "b" * 100)
    second = _name("/w/shop/" + "a" * 200 + "c" * 100)

    assert len(first) == 128
    assert len(second) == 128
    assert first != second


def test_session_name_is_exact_up_to_128_characters_and_suffixed_above() -> None:
    exact = _name("/w/shop/" + "a" * 123)
    suffixed = _name("/w/shop/" + "a" * 124)

    assert exact == "shop-" + "a" * 123
    assert len(exact) == 128
    assert len(suffixed) == 128
    assert re.fullmatch(rf"shop-a{{114}}-{_HASH}", suffixed)


def test_session_name_tells_a_dot_from_an_underscore() -> None:
    assert _name("/w/shop/release-1.2.3") != _name("/w/shop/release-1_2_3")


def test_session_name_is_the_same_for_nfc_and_nfd() -> None:
    nfc = unicodedata.normalize("NFC", "fiks-båt")
    nfd = unicodedata.normalize("NFD", "fiks-båt")
    assert nfc != nfd

    assert _name(f"/w/shop/{nfc}") == _name(f"/w/shop/{nfd}")


def test_session_name_of_a_folder_without_ascii_does_not_raise() -> None:
    assert re.fullmatch(rf"clone-{_HASH}", _name("/w/日本/語"))


def test_session_name_keeps_the_case() -> None:
    assert _name("/w/Shop/main") != _name("/w/shop/main")
    assert _name("/w/Shop/main") == "Shop-main"


def test_session_name_of_a_symlinked_workspace_is_that_of_its_target(tmp_path: Path) -> None:
    target = tmp_path / "real shop"
    (target / "feat.1").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(target)

    assert _name(link / "feat.1") == _name(target / "feat.1")


def test_session_name_is_stable() -> None:
    assert _name("/w/shop/release-1.2.3") == _name("/w/shop/release-1.2.3")


def test_same_folder_sees_through_a_symlink(tmp_path: Path) -> None:
    folder = tmp_path / "shop"
    folder.mkdir()
    link = tmp_path / "link"
    link.symlink_to(folder)

    assert same_folder(folder, link)
    assert not same_folder(folder, tmp_path)


def test_same_folder_compares_missing_paths_by_their_nfc_strings() -> None:
    nfc = unicodedata.normalize("NFC", "fiks-båt")
    nfd = unicodedata.normalize("NFD", "fiks-båt")

    assert same_folder(f"/w/shop/{nfc}", Path(f"/w/shop/{nfd}"))
    assert not same_folder("/w/shop/a", "/w/shop/b")


def test_same_folder_falls_back_to_the_strings_when_samefile_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "shop"
    folder.mkdir()
    link = tmp_path / "link"
    link.symlink_to(folder)

    def fail(a: object, b: object) -> bool:
        raise OSError("gone")

    monkeypatch.setattr("os.path.samefile", fail)

    assert same_folder(folder, link)
    assert not same_folder(folder, tmp_path)


def test_attach_line_attaches_headless_to_the_session_of_the_clone(env: Env) -> None:
    write_stub_cmux(env.bin)

    line = attach_line(env.root / "feature-login")

    assert line == f"{env.bin}/cmux local-tmux attach shop-feature-login --headless"


def test_attach_line_quotes_nothing_for_a_workspace_with_a_space(env: Env) -> None:
    write_stub_cmux(env.bin)
    folder = env.tmp / "my shop" / "feat"

    line = attach_line(folder)

    assert re.fullmatch(
        rf"{re.escape(str(env.bin))}/cmux local-tmux attach my-shop-feat-{_HASH} --headless", line
    )


# ----- Reading the terminal session ------------------------------------------------------

_SOCKET = "/private/tmp/cmux-local-tmux/shop.sock"
_LIST = ["local-tmux", "list", "--json"]
_UNREADABLE = (
    "draupnir could not read the terminal sessions with cmux local-tmux list --json. Check "
    "that cmux is up to date and that tmux is installed."
)
_CLOSE_UNREADABLE = (
    "draupnir could not read the terminal sessions, so it did not close one. Check with cmux "
    "local-tmux list."
)


def _clone(env: Env, name: str = "feature-a") -> Path:
    """An empty folder next to main/, which is all that the session code needs of a clone."""
    folder = env.root / name
    folder.mkdir()
    return folder


def _entry(folder: Path, **changes: object) -> dict[str, object]:
    """The entry of cmux local-tmux list --json for the running session of folder.

    It holds every field that draupnir reads, and the given fields are changed.
    """
    fields: dict[str, object] = {
        "session_name": session_name(folder),
        "cwd": str(folder.resolve()),
        "live": True,
        "socket_path": _SOCKET,
        "session_id": "$0",
        "clients": 1,
    }
    fields.update(changes)
    return fields


def _sessions(*entries: dict[str, object]) -> dict[str, object]:
    return {"sessions": list(entries)}


def _verbs(calls: Path) -> list[list[str]]:
    """The first two arguments of each call to the stub cmux, such as local-tmux close."""
    return [call[:2] for call in cmux_calls(calls)]


def _warnings(lines: list[str]) -> list[str]:
    """The logged lines that are not command lines."""
    return [line for line in lines if not line.startswith("$ ")]


def _start(folder: Path) -> list[str]:
    return [
        "local-tmux",
        "start",
        session_name(folder),
        "--cwd",
        str(folder.resolve()),
        "--detached",
    ]


def test_read_session_finds_the_running_session_of_the_clone(env: Env) -> None:
    folder = _clone(env)
    write_stub_cmux(env.bin, sessions=_sessions(_entry(folder, clients=2)))
    _lines, log = logger()

    session = read_session(folder, log)

    assert session == TerminalSession(
        name="shop-feature-a",
        state="running",
        cwd=str(folder.resolve()),
        socket_path=_SOCKET,
        session_id="$0",
        clients=2,
    )


def test_read_session_lists_the_sessions_without_an_id_format(env: Env) -> None:
    folder = _clone(env)
    calls = write_stub_cmux(env.bin)
    _lines, log = logger()

    read_session(folder, log)

    assert cmux_calls(calls) == [_LIST]


def test_read_session_of_a_session_that_does_not_run_is_stopped(env: Env) -> None:
    folder = _clone(env)
    write_stub_cmux(env.bin, sessions=_sessions(_entry(folder, live=False)))
    _lines, log = logger()

    assert read_session(folder, log).state == "stopped"


def test_read_session_of_a_stopped_session_needs_no_socket_and_no_id(env: Env) -> None:
    folder = _clone(env)
    entry = _entry(folder, live=False)
    del entry["socket_path"]
    del entry["session_id"]
    write_stub_cmux(env.bin, sessions=_sessions(entry))
    _lines, log = logger()

    session = read_session(folder, log)

    assert (session.state, session.socket_path, session.session_id) == ("stopped", None, None)


def test_read_session_of_a_session_in_another_folder_is_foreign(env: Env) -> None:
    folder = _clone(env)
    other = env.tmp / "elsewhere" / "feature-a"
    write_stub_cmux(env.bin, sessions=_sessions(_entry(folder, cwd=str(other))))
    _lines, log = logger()

    session = read_session(folder, log)

    assert (session.state, session.cwd) == ("foreign", str(other))


def test_read_session_matches_the_name_exactly(env: Env) -> None:
    """tmux matches a name by prefix, but the session shop-feat-2 is not the session of feat."""
    folder = _clone(env, "feat")
    other = _clone(env, "feat-2")
    write_stub_cmux(env.bin, sessions=_sessions(_entry(other)))
    _lines, log = logger()

    assert read_session(folder, log).state == "absent"


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param("", id="empty"),
        pytest.param("not json", id="not json"),
        pytest.param('{"sessions": {}}', id="sessions is no list"),
    ],
)
def test_read_session_of_an_answer_without_a_list_is_unreadable(env: Env, answer: str) -> None:
    folder = _clone(env)
    write_stub_cmux(env.bin, sessions=answer)
    _lines, log = logger()

    session = read_session(folder, log)

    assert session.state == "unreadable"
    assert session.failure


def test_read_session_of_a_list_that_fails_is_unreadable(env: Env) -> None:
    folder = _clone(env)
    write_stub_cmux(env.bin, sessions=_sessions(_entry(folder)), fail=["local-tmux list"])
    _lines, log = logger()

    session = read_session(folder, log)

    assert session.state == "unreadable"
    assert session.failure is not None
    assert "failed with exit code 1" in session.failure


def test_read_session_of_a_list_that_runs_too_long_is_unreadable(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _clone(env)
    write_stub_cmux(env.bin, sessions=_sessions(_entry(folder)), slow=["local-tmux list"])
    monkeypatch.setattr("draupnir.cmux.LOCAL_TMUX_READ_TIMEOUT_SECONDS", 0.2)
    _lines, log = logger()

    session = read_session(folder, log)

    assert session.state == "unreadable"
    assert session.failure is not None
    assert "took longer than 0.2 seconds" in session.failure


def test_read_session_gives_no_count_for_clients_that_are_not_a_number(env: Env) -> None:
    folder = _clone(env)
    without = _entry(folder)
    del without["clients"]
    _lines, log = logger()

    for entry in (_entry(folder, clients=True), without):
        write_stub_cmux(env.bin, sessions=_sessions(entry))
        session = read_session(folder, log)

        assert (session.state, session.clients) == ("running", None)


def _check_unreadable(env: Env, folder: Path, entries: list[dict[str, object]]) -> None:
    """Checks that the entries read as unreadable, and that they let draupnir neither start nor
    close a session."""
    calls = write_stub_cmux(env.bin, sessions={"sessions": entries})
    lines, log = logger()

    assert read_session(folder, log).state == "unreadable"
    with pytest.raises(OperationError) as raised:
        ensure_session(folder, log)
    close_session(folder, log)

    assert str(raised.value).splitlines()[0] == _UNREADABLE
    assert ["local-tmux", "start"] not in _verbs(calls)
    assert ["local-tmux", "close"] not in _verbs(calls)
    assert _CLOSE_UNREADABLE in lines


@pytest.mark.parametrize(
    ("dropped", "changes"),
    [
        pytest.param(("live",), {}, id="no live"),
        pytest.param((), {"live": "true"}, id="live is a string"),
        pytest.param((), {"live": 1}, id="live is a number"),
        pytest.param((), {"live": None}, id="live is null"),
        pytest.param(("cwd",), {}, id="no cwd"),
        pytest.param((), {"cwd": ""}, id="empty cwd"),
        pytest.param((), {"cwd": 5}, id="cwd is a number"),
        pytest.param(("socket_path",), {}, id="running without a socket"),
        pytest.param(("session_id",), {}, id="running without an id"),
    ],
)
def test_an_entry_with_a_field_draupnir_cannot_use_is_unreadable(
    env: Env, dropped: tuple[str, ...], changes: dict[str, object]
) -> None:
    """A missing field or a field of another type must never allow a start or a close."""
    folder = _clone(env)
    entry = _entry(folder, **changes)
    for key in dropped:
        del entry[key]

    _check_unreadable(env, folder, [entry])


def test_two_entries_with_the_name_are_unreadable(env: Env) -> None:
    folder = _clone(env)

    _check_unreadable(env, folder, [_entry(folder), _entry(folder, session_id="$1")])


# ----- Starting the terminal session -----------------------------------------------------


def _start_environment(calls: Path) -> dict[str, str]:
    """The environment that the stub cmux got for local-tmux start."""
    text = (calls.parent / "cmux-start-env.txt").read_text()
    return {name: value for name, _, value in (line.partition("=") for line in text.splitlines())}


def test_ensure_session_starts_a_missing_session_in_the_clone(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The session must not get the runner's variables or the virtual environment of uv run,
    because the tmux server hands its first environment to every pane."""
    folder = _clone(env)
    calls = write_stub_cmux(env.bin)
    venv = env.tmp / "venv"
    # The stub cmux stays on the PATH, after the bin folder of the virtual environment.
    monkeypatch.setenv(
        "PATH", os.pathsep.join([str(venv / "bin"), str(env.bin), "/usr/bin", "/bin"])
    )
    monkeypatch.setenv("VIRTUAL_ENV", str(venv))
    monkeypatch.setenv("CMUX_SURFACE_ID", "surface-1")
    # The runner adds these two, so the test makes sure that they do not come from elsewhere.
    for name in ("GIT_EDITOR", "NO_COLOR"):
        monkeypatch.delenv(name, raising=False)
    lines, log = logger()

    session = ensure_session(folder, log)

    assert cmux_calls(calls) == [_LIST, _start(folder)]
    assert (session.name, session.state) == ("shop-feature-a", "running")
    assert any(line.startswith("$ ") and "local-tmux start" in line for line in lines)
    started = _start_environment(calls)
    for name in ("GIT_EDITOR", "NO_COLOR", "VIRTUAL_ENV"):
        assert name not in started
    assert started["PATH"] == os.pathsep.join([str(env.bin), "/usr/bin", "/bin"])
    assert started["CMUX_SURFACE_ID"] == "surface-1"


def test_session_environment_keeps_the_path_without_a_virtual_environment(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", os.pathsep.join([str(env.bin), "/usr/bin"]))

    environment = session_environment()

    assert environment["PATH"] == os.pathsep.join([str(env.bin), "/usr/bin"])
    assert "VIRTUAL_ENV" not in environment


def test_ensure_session_leaves_a_running_session_as_it_is(env: Env) -> None:
    folder = _clone(env)
    calls = write_stub_cmux(env.bin, sessions=_sessions(_entry(folder)))
    _lines, log = logger()

    session = ensure_session(folder, log)

    assert (session.state, session.session_id) == ("running", "$0")
    assert cmux_calls(calls) == [_LIST]


def test_ensure_session_closes_a_stopped_session_before_it_starts_it_again(env: Env) -> None:
    """cmux 0.64.25 keeps the record of a dead session, and that record blocks the name."""
    folder = _clone(env)
    calls = write_stub_cmux(env.bin, sessions=_sessions(_entry(folder, live=False)))
    _lines, log = logger()

    ensure_session(folder, log)

    assert cmux_calls(calls) == [_LIST, ["local-tmux", "close", "shop-feature-a"], _start(folder)]


def test_ensure_session_refuses_the_session_of_another_folder(env: Env) -> None:
    folder = _clone(env)
    other = env.tmp / "elsewhere" / "feature-a"
    calls = write_stub_cmux(env.bin, sessions=_sessions(_entry(folder, cwd=str(other))))
    _lines, log = logger()

    with pytest.raises(OperationError) as raised:
        ensure_session(folder, log)

    first = str(raised.value).splitlines()[0]
    assert first.startswith(f"The terminal session shop-feature-a belongs to {other}, so ")
    assert ["local-tmux", "start"] not in _verbs(calls)
    assert ["local-tmux", "close"] not in _verbs(calls)


def test_ensure_session_says_which_session_it_could_not_start(env: Env) -> None:
    folder = _clone(env)
    write_stub_cmux(env.bin, fail=["local-tmux start"])
    _lines, log = logger()

    with pytest.raises(OperationError) as raised:
        ensure_session(folder, log)

    lines = str(raised.value).splitlines()
    assert lines[0] == "draupnir could not start the terminal session shop-feature-a for feature-a."
    assert "cmux: the stub was told to fail this call" in lines


def test_ensure_session_without_cmux_says_that_cmux_is_missing(env: Env) -> None:
    folder = _clone(env)
    _lines, log = logger()

    with pytest.raises(OperationError, match="found no cmux command line tool"):
        ensure_session(folder, log)


# ----- Closing the terminal session ------------------------------------------------------


@pytest.mark.parametrize("live", [True, False])
def test_close_session_closes_the_session_of_the_clone(env: Env, live: bool) -> None:
    folder = _clone(env)
    calls = write_stub_cmux(env.bin, sessions=_sessions(_entry(folder, live=live)))
    lines, log = logger()

    close_session(folder, log)

    assert cmux_calls(calls) == [_LIST, ["local-tmux", "close", "shop-feature-a"]]
    assert _warnings(lines) == []


def test_close_session_of_a_clone_without_a_session_does_nothing(env: Env) -> None:
    folder = _clone(env)
    calls = write_stub_cmux(env.bin)
    lines, log = logger()

    close_session(folder, log)

    assert cmux_calls(calls) == [_LIST]
    assert _warnings(lines) == []


def test_close_session_counts_a_failed_close_as_closed_when_the_session_is_gone(
    env: Env,
) -> None:
    folder = _clone(env)
    calls = write_stub_cmux(
        env.bin, sessions=_sessions(_entry(folder)), fail_after=["local-tmux close"]
    )
    lines, log = logger()

    close_session(folder, log)

    assert cmux_calls(calls) == [_LIST, ["local-tmux", "close", "shop-feature-a"], _LIST]
    assert _warnings(lines) == []


def test_close_session_warns_when_the_session_stays(env: Env) -> None:
    folder = _clone(env)
    calls = write_stub_cmux(env.bin, sessions=_sessions(_entry(folder)), fail=["local-tmux close"])
    lines, log = logger()

    close_session(folder, log)

    assert cmux_calls(calls) == [_LIST, ["local-tmux", "close", "shop-feature-a"], _LIST]
    assert [line for line in lines if "could not close" in line] == [
        "draupnir could not close the terminal session shop-feature-a. Close it with cmux "
        "local-tmux close shop-feature-a."
    ]
    assert "cmux: the stub was told to fail this call" in lines


def test_close_session_leaves_the_session_of_another_folder_open(env: Env) -> None:
    folder = _clone(env)
    other = env.tmp / "elsewhere" / "feature-a"
    calls = write_stub_cmux(env.bin, sessions=_sessions(_entry(folder, cwd=str(other))))
    lines, log = logger()

    close_session(folder, log)

    assert ["local-tmux", "close"] not in _verbs(calls)
    assert _warnings(lines) == [
        f"The terminal session shop-feature-a belongs to {other}, so draupnir left it open."
    ]


def test_close_session_leaves_the_session_open_that_draupnir_runs_in(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _clone(env)
    calls = write_stub_cmux(env.bin, sessions=_sessions(_entry(folder)))
    monkeypatch.setenv("TMUX", f"{_SOCKET},123,0")
    lines, log = logger()

    close_session(folder, log)

    assert ["local-tmux", "close"] not in _verbs(calls)
    assert _warnings(lines) == [
        "draupnir runs in the terminal session shop-feature-a, so it left the session open. "
        "Type exit there to close it."
    ]


def test_close_session_closes_the_session_when_draupnir_runs_in_another(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _clone(env)
    calls = write_stub_cmux(env.bin, sessions=_sessions(_entry(folder)))
    monkeypatch.setenv("TMUX", "/private/tmp/tmux-501/default,123,0")
    _lines, log = logger()

    close_session(folder, log)

    assert ["local-tmux", "close"] in _verbs(calls)


def test_close_session_logs_a_cmux_command_it_cannot_read_and_does_not_raise(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _clone(env)
    monkeypatch.setenv("DRAUPNIR_CMUX", '"broken')
    lines, log = logger()

    close_session(folder, log)

    assert len(lines) == 1
    assert lines[0].startswith("draupnir could not read the cmux command")


@pytest.mark.parametrize(
    ("tmux", "inside"),
    [
        pytest.param(f"{_SOCKET},123,0", True, id="this session"),
        pytest.param(f"{_SOCKET},123,1", False, id="another session on the socket"),
        pytest.param("/private/tmp/tmux-501/default,123,0", False, id="another socket"),
        pytest.param("", False, id="no tmux"),
    ],
)
def test_runs_in_session_compares_the_socket_and_the_session_id(
    monkeypatch: pytest.MonkeyPatch, tmux: str, inside: bool
) -> None:
    monkeypatch.setenv("TMUX", tmux)
    session = TerminalSession(
        name="shop-feature-a",
        state="running",
        cwd="/w/shop/feature-a",
        socket_path=_SOCKET,
        session_id="$0",
    )

    assert runs_in_session(session) is inside


# ----- The programs in the terminal session ----------------------------------------------


def _running(folder: Path) -> TerminalSession:
    return TerminalSession(
        name=session_name(folder),
        state="running",
        cwd=str(folder.resolve()),
        socket_path=_SOCKET,
        session_id="$0",
        clients=0,
    )


def test_session_programs_names_what_runs_besides_the_shells(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _clone(env)
    monkeypatch.setenv("SHELL", "/bin/zsh")
    calls = write_stub_tmux(env.bin, "zsh\n-zsh\n2.1.301\nvim\n2.1.301\n")

    assert session_programs(_running(folder)) == ["2.1.301", "vim"]
    # The = makes the target exact, because tmux matches a bare name by prefix.
    assert cmux_calls(calls) == [
        [
            "-S",
            _SOCKET,
            "list-panes",
            "-s",
            "-t",
            "=shop-feature-a",
            "-F",
            "#{pane_current_command}",
        ]
    ]


def test_session_programs_of_a_session_with_only_shells_is_empty(env: Env) -> None:
    folder = _clone(env)
    write_stub_tmux(env.bin, "zsh\n-bash\nlogin\n")

    assert session_programs(_running(folder)) == []


def test_session_programs_leaves_out_the_shell_of_the_user(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _clone(env)
    monkeypatch.setenv("SHELL", "/opt/shells/xonsh")
    write_stub_tmux(env.bin, "xonsh\nvim\n")

    assert session_programs(_running(folder)) == ["vim"]


def test_session_programs_uses_the_tmux_that_cmux_names(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _clone(env)
    elsewhere = env.tmp / "tmux-bin"
    calls = write_stub_tmux(elsewhere, "vim\n")
    monkeypatch.setenv("CMUX_LOCAL_TMUX_BIN", str(elsewhere / "tmux"))

    assert session_programs(_running(folder)) == ["vim"]
    assert len(cmux_calls(calls)) == 1


def test_session_programs_without_tmux_is_unknown(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _clone(env)
    # A PATH with only the stub folder, so a tmux of the machine cannot answer.
    monkeypatch.setenv("PATH", str(env.bin))

    assert session_programs(_running(folder)) is None


def test_session_programs_of_a_tmux_that_fails_is_unknown(env: Env) -> None:
    folder = _clone(env)
    write_stub_tmux(env.bin, "vim\n", code=1)

    assert session_programs(_running(folder)) is None
