"""Talks to the cmux terminal: new workspaces, and the dashboard's status in its sidebar.

It also names the terminal session of a clone, and builds the line that attaches to it.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from draupnir.errors import OperationError
from draupnir.github import FAILURE, BuildState
from draupnir.ide import project_name
from draupnir.runner import CMUX_TIMEOUT_SECONDS, LogFn, Result, run
from draupnir.status import ProjectStatus

# cmux sets this variable in every terminal it starts.
_CMUX_VARIABLE = "CMUX_WORKSPACE_ID"

# cmux sets this variable to the path of its own command line tool.
_CLI_VARIABLE = "CMUX_BUNDLED_CLI_PATH"

# The variable that names the cmux command, like the IDE variables of ide.py.
_COMMAND_VARIABLE = "DRAUPNIR_CMUX"

# Where the macOS app keeps the command line tool, when it is not on the PATH.
_BUNDLED_COMMAND = Path("/Applications/cmux.app/Contents/Resources/bin/cmux")

# A status in the sidebar has a key, so the dashboard can write its own line again and clear it.
STATUS_KEY = "draupnir"

# The icon of the status, so the line is draupnir's at a glance, the way cmux marks its own agent
# status with an icon of its own. It names a symbol of the system, and cmux does not check the
# name, so a wrong one draws nothing.
STATUS_ICON = "arrow.triangle.branch"

# The three levels of the sidebar status. Red means that a clone needs the user before work can
# go on. Yellow means unfinished work. Green means that every clone is clean and in sync. cmux
# sorts the statuses of a workspace by priority, so the worst level comes first.
RED = "#cf222e"
YELLOW = "#d29922"
GREEN = "#3fb950"
_PRIORITIES = {RED: 30, YELLOW: 20, GREEN: 10}

# How many parts the status line holds. The parts are sorted worst first, so a sidebar that is
# too narrow for the line still shows the part that matters most.
_STATUS_PARTS = 3


@dataclass(frozen=True)
class SidebarStatus:
    """One short line about the clones of a workspace, for the sidebar of cmux."""

    text: str
    color: str

    @property
    def priority(self) -> int:
        return _PRIORITIES[self.color]


def cmux_command() -> list[str] | None:
    """The command that runs the cmux command line tool, or None when it is not installed.

    The order is: the DRAUPNIR_CMUX environment variable, then `cmux` on the PATH, then the
    path in CMUX_BUNDLED_CLI_PATH, which cmux sets in every terminal it starts, then the tool
    in the macOS app bundle. The PATH comes before the variable from cmux, so a cmux that a
    test or a user put on the PATH is still the one that runs.
    """
    configured = os.environ.get(_COMMAND_VARIABLE)
    if configured:
        try:
            return shlex.split(configured)
        except ValueError as exc:
            raise OperationError(
                f"draupnir could not read the cmux command {configured!r}: {exc}. Fix the "
                f"quotes in {_COMMAND_VARIABLE}."
            ) from exc

    on_path = shutil.which("cmux")
    if on_path:
        return [on_path]

    from_cmux = os.environ.get(_CLI_VARIABLE)
    candidates = [Path(from_cmux)] if from_cmux else []
    candidates.append(_BUNDLED_COMMAND)
    for candidate in candidates:
        if os.access(candidate, os.X_OK):
            return [str(candidate)]
    return None


def _argv(args: Sequence[str | Path]) -> list[str]:
    """The full command line for a cmux call.

    Raises OperationError when the cmux command line tool is not installed.
    """
    command = cmux_command()
    if command is None:
        raise OperationError(
            "draupnir found no cmux command line tool. Open cmux once, so that it installs the "
            f"tool, or set {_COMMAND_VARIABLE} to the command that runs it."
        )
    return [*command, *(str(arg) for arg in args)]


# cmux accepts a session name of 1 to 128 characters from A-Z, a-z, 0-9, _ and -.
_SESSION_NAME_LIMIT = 128

# The readable part is cut here when a suffix follows: 119 characters, "-" and 8 hex characters.
_SESSION_SUFFIXED_LIMIT = _SESSION_NAME_LIMIT - 9

_SESSION_CHARACTERS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _session_part(text: str) -> tuple[str, bool]:
    """Maps the name of one folder to session characters, and says whether it changed any."""
    mapped: list[str] = []
    changed = False
    for char in text:
        if char in _SESSION_CHARACTERS:
            mapped.append(char)
        elif char in ".:":
            # tmux turns these two into "_" as well.
            mapped.append("_")
            changed = True
        else:
            mapped.append("-")
            changed = True
    return "".join(mapped), changed


def session_name(folder: Path) -> str:
    """The name of the terminal session of a clone, such as "shop-feature-login".

    The readable part is the project name of the IDE, mapped to the characters that cmux accepts.
    A name that kept every character and fits is used as it is, so common names stay readable.
    Every other name gets "-" and 8 hex characters of the hash of the clone's path. Without the
    suffix, `fiks-båt` and `fiks-bøt` would both be `fiks-b-t`. The text is normalized to NFC
    first, so a folder that macOS stores in NFD gets the same name as the spelling a user types.
    """
    resolved = folder.resolve()
    # The two folders are mapped on their own, so the one space that project_name puts between
    # them is a separator and not a replaced character.
    parent, parent_changed = _session_part(_nfc(resolved.parent.name))
    child, child_changed = _session_part(_nfc(resolved.name))
    joined = f"{parent}-{child}"
    readable = joined.lstrip("-")
    # A dropped leading "-" counts as a change, because the name would read as an option.
    exact = (
        not parent_changed
        and not child_changed
        and len(readable) == len(joined)
        and len(readable) <= _SESSION_NAME_LIMIT
    )
    if exact:
        return readable
    digest = hashlib.sha256(_nfc(str(resolved)).encode("utf-8")).hexdigest()[:8]
    cut = readable[:_SESSION_SUFFIXED_LIMIT].rstrip("-")
    return f"{cut}-{digest}" if cut else f"clone-{digest}"


def same_folder(a: Path | str, b: Path | str) -> bool:
    """True when two paths are the same folder.

    It uses os.path.samefile when both paths exist, which also sees through symbolic links. In
    every other case, and when samefile fails, it compares the NFC forms of the resolved paths.
    """
    first = Path(a).resolve()
    second = Path(b).resolve()
    if first.exists() and second.exists():
        try:
            return os.path.samefile(first, second)
        except OSError:
            pass
    return _nfc(str(first)) == _nfc(str(second))


def attach_argv(folder: Path) -> list[str]:
    """The command that attaches a terminal to the session of a clone, without a cmux socket.

    Raises OperationError when the cmux command line tool is not installed.
    """
    return _argv(["local-tmux", "attach", session_name(folder), "--headless"])


def attach_line(folder: Path) -> str:
    """attach_argv as one line that a shell can run."""
    return shlex.join(attach_argv(folder))


def in_cmux() -> bool:
    """True when draupnir runs in a cmux terminal, so it can open cmux workspaces."""
    return bool(os.environ.get(_CMUX_VARIABLE))


def _cmux_json(args: list[str | Path], folder: Path, log: LogFn) -> dict[str, object]:
    """Runs a cmux command that answers with JSON, and returns the object it prints.

    The command line goes to log. The JSON is read from stdout alone, because cmux prints
    notices on stderr.

    Raises OperationError when the command fails or does not print a JSON object.
    """
    command = _argv([*args, "--json", "--id-format", "both"])
    log(f"$ {shlex.join(command)}")
    result = run(command, folder, timeout=CMUX_TIMEOUT_SECONDS)
    try:
        parsed: object = json.loads(result.stdout)
    except json.JSONDecodeError:
        parsed = None
    if not isinstance(parsed, dict):
        raise OperationError(
            f"{shlex.join(command)} did not answer with JSON. Check that cmux is up to date, "
            "and try again."
        )
    return cast("dict[str, object]", parsed)


def _text(fields: dict[str, object], key: str) -> str | None:
    value = fields.get(key)
    return value if isinstance(value, str) else None


def _cmux_group(workspace_id: str, folder: Path, log: LogFn) -> str | None:
    """The id of the sidebar group that holds the cmux workspace, or None when it has none.

    When cmux cannot list the groups, the workspace counts as ungrouped, because a new
    workspace outside the group is still useful.
    """
    try:
        listed = _cmux_json(["workspace-group", "list"], folder, log)
    except OperationError:
        return None
    groups = listed.get("groups")
    for group in cast("list[object]", groups) if isinstance(groups, list) else []:
        if not isinstance(group, dict):
            continue
        fields = cast("dict[str, object]", group)
        members = fields.get("member_workspace_ids")
        if isinstance(members, list) and workspace_id in members:
            return _text(fields, "id")
    return None


def _add_todo_pane(workspace: str, terminal: str, folder: Path, log: LogFn) -> None:
    """Opens the todo list of the cmux workspace in a pane below its terminal.

    cmux opens the todo list as a tab next to the terminal, so it is split off into its own
    pane. Then the terminal gets the focus back.
    """
    opened = _cmux_json(["todo", "open", "--workspace", workspace], folder, log)
    todo = _text(opened, "surface_id")
    if todo is None:
        return
    for command in (
        ["split-off", "--surface", todo, "down", "--workspace", workspace, "--focus", "false"],
        ["focus-panel", "--panel", terminal, "--workspace", workspace],
    ):
        run(_argv(command), folder, log=log, timeout=CMUX_TIMEOUT_SECONDS)


def open_cmux_workspace(folder: Path, log: LogFn) -> None:
    """Opens a new cmux workspace with a shell in folder and its todo list below, and switches
    to it.

    The workspace gets the name from project_name. When the workspace that draupnir runs in
    belongs to a sidebar group, the new workspace joins that group, right after it. Otherwise
    cmux adds it to the window that draupnir runs in. When the todo list cannot be added, the
    workspace stays open without it.

    Raises OperationError when cmux is not found or refuses the workspace.
    """
    command: list[str | Path] = [
        "workspace",
        "create",
        "--name",
        project_name(folder),
        "--cwd",
        folder,
        "--focus",
        "true",
    ]
    caller = os.environ.get(_CMUX_VARIABLE)
    group = _cmux_group(caller, folder, log) if caller else None
    if caller and group:
        command += [
            "--group",
            group,
            "--group-placement",
            "afterCurrent",
            "--group-reference",
            caller,
        ]
    created = _cmux_json(command, folder, log)
    workspace = _text(created, "workspace_id")
    terminal = _text(created, "surface_id")
    if workspace is None or terminal is None:
        return
    try:
        _add_todo_pane(workspace, terminal, folder, log)
    except OperationError as exc:
        log(
            "The workspace is open, but draupnir could not add the todo list below the "
            f"terminal: {str(exc).splitlines()[0]}"
        )


def _one_or_count(names: Sequence[str], one: str, many: str) -> list[str]:
    """One part naming the single clone, or one part counting them, or no part at all.

    `one` holds `{name}`, and `many` holds `{count}`.
    """
    if not names:
        return []
    if len(names) == 1:
        return [one.format(name=names[0])]
    return [many.format(count=len(names))]


def _failed_builds(
    statuses: Sequence[ProjectStatus], builds: Mapping[str, BuildState]
) -> list[str]:
    """The clones whose current commit has a failed build.

    A build of an older commit is left out, because the clone has moved on since.
    """
    heads = {status.name: status.head for status in statuses}
    return [
        name
        for name, build in builds.items()
        if build.state == FAILURE and name in heads and build.sha == heads[name]
    ]


def sidebar_status(
    statuses: Sequence[ProjectStatus], builds: Mapping[str, BuildState]
) -> SidebarStatus | None:
    """One line about the clones for the sidebar of cmux, or None when there is nothing to say.

    Red covers what stops work: an operation in progress, a failed build of the current commit,
    and a clone that draupnir cannot read. Yellow covers unfinished work: local changes, commits
    that are not on GitHub, and a branch that GitHub no longer has. Green means that every clone
    is clean and in sync.
    """
    if not statuses:
        return None

    red: list[str] = []
    red += _one_or_count(
        [f"{status.operation} in {status.name}" for status in statuses if status.operation],
        "{name}",
        "{count} operations in progress",
    )
    red += _one_or_count(
        _failed_builds(statuses, builds), "build failed in {name}", "{count} builds failed"
    )
    red += _one_or_count(
        [status.name for status in statuses if status.error],
        "{name} cannot be read",
        "{count} clones cannot be read",
    )

    yellow: list[str] = []
    yellow += _one_or_count(
        [status.name for status in statuses if status.changed or status.untracked],
        "{name} is changed",
        "{count} clones are changed",
    )
    yellow += _one_or_count(
        [
            status.name
            for status in statuses
            if status.branch is not None
            and not status.error
            and (status.upstream is None or bool(status.ahead_upstream))
        ],
        "{name} is not pushed",
        "{count} clones are not pushed",
    )
    yellow += _one_or_count(
        [status.name for status in statuses if status.upstream_gone],
        "{name} is deleted on GitHub",
        "{count} branches are deleted on GitHub",
    )

    parts = red + yellow
    if not parts:
        return SidebarStatus(text="clean", color=GREEN)
    return SidebarStatus(text=" · ".join(parts[:_STATUS_PARTS]), color=RED if red else YELLOW)


def set_sidebar_status(status: SidebarStatus, folder: Path) -> Result:
    """Writes status into the sidebar row of the cmux workspace that draupnir runs in.

    The workspace is named, so the status stays on the dashboard's own row when the user has
    switched to another workspace. Returns the result instead of raising, because the status
    is a convenience: a dashboard whose status cannot be written still works.

    Raises OperationError only when the cmux command line tool is not installed.
    """
    workspace = os.environ.get(_CMUX_VARIABLE, "")
    command = _argv(
        [
            "set-status",
            STATUS_KEY,
            status.text,
            "--icon",
            STATUS_ICON,
            "--color",
            status.color,
            "--priority",
            str(status.priority),
            "--workspace",
            workspace,
        ]
    )
    return run(command, folder, check=False, timeout=CMUX_TIMEOUT_SECONDS)


def clear_sidebar_status(folder: Path) -> Result:
    """Removes the dashboard's line from the sidebar row of its cmux workspace.

    Returns the result instead of raising, for the same reason as set_sidebar_status.
    """
    workspace = os.environ.get(_CMUX_VARIABLE, "")
    command = _argv(["clear-status", STATUS_KEY, "--workspace", workspace])
    return run(command, folder, check=False, timeout=CMUX_TIMEOUT_SECONDS)
