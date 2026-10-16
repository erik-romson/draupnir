"""Tests for src/draupnir/cmux.py: finding the cmux command, and the line for its sidebar."""

from __future__ import annotations

import os
import re
import shutil
import stat
import unicodedata
from pathlib import Path

import pytest
from conftest import Env
from helpers import cmux_calls, write_stub_cmux

from draupnir.cmux import (
    GREEN,
    RED,
    STATUS_ICON,
    STATUS_KEY,
    YELLOW,
    SidebarStatus,
    attach_line,
    clear_sidebar_status,
    cmux_command,
    same_folder,
    session_name,
    set_sidebar_status,
    sidebar_status,
)
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
