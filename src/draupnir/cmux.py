"""Talks to the cmux terminal: new workspaces, and the dashboard's status in its sidebar.

It also names the terminal session of a clone, builds the line that attaches to it, and reads,
starts and closes that session with cmux local-tmux. It finds and opens the clone's own cmux
workspace, which shows that session. It writes the lines about the session that the remove
question shows.
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
from typing import Literal, cast

from draupnir.config import CmuxConfig
from draupnir.errors import OperationError
from draupnir.github import FAILURE, BuildState
from draupnir.ide import project_name
from draupnir.runner import (
    CMUX_TIMEOUT_SECONDS,
    LOCAL_TMUX_READ_TIMEOUT_SECONDS,
    CommandError,
    LogFn,
    Result,
    run,
)
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


def _cmux_json(
    args: list[str | Path],
    folder: Path,
    log: LogFn,
    *,
    id_format: bool = True,
    timeout: float = CMUX_TIMEOUT_SECONDS,
) -> dict[str, object]:
    """Runs a cmux command that answers with JSON, and returns the object it prints.

    The command gets --json, and with id_format also --id-format both, so the answer holds both
    the ids and the refs. With id_format false it gets only --json, for a command whose plain
    answer draupnir reads, such as cmux local-tmux list. The command line goes to log. The JSON
    is read from stdout alone, because cmux prints notices on stderr.

    Raises OperationError when the command fails or does not print a JSON object.
    """
    flags = ["--json", "--id-format", "both"] if id_format else ["--json"]
    command = _argv([*args, *flags])
    log(f"$ {shlex.join(command)}")
    result = run(command, folder, timeout=timeout)
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


@dataclass(frozen=True)
class CreatedWorkspace:
    """The ids that cmux gave to a workspace that draupnir has just created."""

    workspace_id: str
    surface_id: str


def _create_cmux_workspace(
    folder: Path, log: LogFn, *, command: str | None, focus: bool
) -> CreatedWorkspace | None:
    """Creates the cmux workspace of folder, and returns its ids.

    The workspace gets the name from project_name. When the workspace that draupnir runs in
    belongs to a sidebar group, the new workspace joins that group, right after it. Otherwise
    cmux adds it to the window that draupnir runs in. It returns None when cmux answers without
    both ids.

    Raises OperationError when cmux is not found or refuses the workspace.
    """
    arguments: list[str | Path] = [
        "workspace",
        "create",
        "--name",
        project_name(folder),
        "--cwd",
        folder,
        "--focus",
        "true" if focus else "false",
    ]
    if command is not None:
        arguments += ["--command", command]
    caller = os.environ.get(_CMUX_VARIABLE)
    group = _cmux_group(caller, folder, log) if caller else None
    if caller and group:
        arguments += [
            "--group",
            group,
            "--group-placement",
            "afterCurrent",
            "--group-reference",
            caller,
        ]
    created = _cmux_json(arguments, folder, log)
    workspace = _text(created, "workspace_id")
    terminal = _text(created, "surface_id")
    if workspace is None or terminal is None:
        return None
    return CreatedWorkspace(workspace_id=workspace, surface_id=terminal)


def _add_todo_pane_or_log(created: CreatedWorkspace, folder: Path, log: LogFn) -> None:
    """Adds the todo list below the terminal of a new workspace, and logs when it cannot."""
    try:
        _add_todo_pane(created.workspace_id, created.surface_id, folder, log)
    except OperationError as exc:
        log(
            "The workspace is open, but draupnir could not add the todo list below the "
            f"terminal: {str(exc).splitlines()[0]}"
        )


def open_cmux_workspace(
    folder: Path, log: LogFn, *, command: str | None = None, focus: bool = True
) -> CreatedWorkspace | None:
    """Opens a new cmux workspace with a shell in folder and its todo list below.

    The workspace gets the name from project_name and joins the sidebar group of the workspace
    that draupnir runs in, if it has one. With focus the view switches to the new workspace. With
    a command the terminal runs that line first. When the todo list cannot be added, the
    workspace stays open without it. It returns the ids of the workspace, or None when cmux
    answered without them.

    Raises OperationError when cmux is not found or refuses the workspace.
    """
    created = _create_cmux_workspace(folder, log, command=command, focus=focus)
    if created is None:
        return None
    # The clone's workspace runs `cmux surface resume set` at this place, between the create and
    # the todo pane, so it needs the ids first. show_clone_workspace calls
    # _create_cmux_workspace and _add_todo_pane_or_log itself, with that call in between.
    _add_todo_pane_or_log(created, folder, log)
    return created


# ----- The workspace of a clone -------------------------------------------------------------

WorkspaceOutcome = Literal["found", "absent", "unreadable"]


@dataclass(frozen=True)
class CloneWorkspace:
    """Whether the clone has a cmux workspace, as find_clone_workspace read it.

    The outcome is found, absent or unreadable. Only absent allows draupnir to create a
    workspace. A found workspace has its id, its title, the id of its bound terminal and the
    number of other tabs in it. An unreadable lookup has the first line of the failure.
    """

    outcome: WorkspaceOutcome
    workspace_id: str | None = None
    title: str | None = None
    surface_id: str | None = None
    other_tabs: int = 0
    failure: str | None = None


# The surface of the todo list that draupnir adds to a workspace. It is not another tab.
_TODO_SURFACE_TYPE = "workspaceTodo"


def _objects(value: object) -> list[dict[str, object]] | None:
    """The items of a JSON list when all are objects, and else None."""
    if not isinstance(value, list):
        return None
    items = cast("list[object]", value)
    if not all(isinstance(item, dict) for item in items):
        return None
    return cast("list[dict[str, object]]", items)


def _tree_workspaces(tree: dict[str, object]) -> list[dict[str, object]] | None:
    """The workspaces of windows[].workspaces[] of cmux tree, or None for another shape."""
    windows = _objects(tree.get("windows"))
    if windows is None:
        return None
    found: list[dict[str, object]] = []
    for window in windows:
        workspaces = _objects(window.get("workspaces"))
        if workspaces is None:
            return None
        for workspace in workspaces:
            if any(_text(workspace, key) is None for key in ("id", "ref", "title")):
                return None
            found.append(workspace)
    return found


def _workspace_surfaces(workspace: dict[str, object]) -> list[dict[str, object]] | None:
    """The surfaces of panes[].surfaces[] of a workspace, or None for another shape."""
    panes = _objects(workspace.get("panes"))
    if panes is None:
        return None
    surfaces: list[dict[str, object]] = []
    for pane in panes:
        in_pane = _objects(pane.get("surfaces"))
        if in_pane is None:
            return None
        for surface in in_pane:
            if any(_text(surface, key) is None for key in ("id", "ref", "type")):
                return None
            surfaces.append(surface)
    return surfaces


def _binding(shown: dict[str, object]) -> tuple[list[str], str | None]:
    """The command and the cwd of the resume binding of a terminal, read as bin/cmux-resume does.

    A terminal without a binding gives an empty command. A command line that shlex cannot split
    is not a binding of draupnir, because draupnir always writes the arguments.
    """
    raw = shown.get("resume_binding")
    if not isinstance(raw, dict):
        return [], None
    binding = cast("dict[str, object]", raw)
    cwd = _text(binding, "cwd")
    launch = binding.get("launch_command")
    if isinstance(launch, dict):
        arguments = cast("dict[str, object]", launch).get("arguments")
        if isinstance(arguments, list) and arguments:
            items = cast("list[object]", arguments)
            if all(isinstance(item, str) for item in items):
                return cast("list[str]", items), cwd
    command = _text(binding, "command")
    if command:
        try:
            return shlex.split(command), cwd
        except ValueError:
            return [], cwd
    return [], cwd


def find_clone_workspace(folder: Path, log: LogFn) -> CloneWorkspace:
    """Finds the cmux workspace of the clone in folder.

    Titles are not unique, and the workspace id in a registry is only a hint. So the candidates
    are the workspaces with the title of the clone, and the clone's own is the one with a
    terminal whose resume binding is the attach line of the clone's session, in the clone's
    folder. draupnir writes that binding when it creates the workspace.

    Every read must succeed for absent, because a failed read may hide the clone's workspace, and
    a second workspace would be a duplicate. So a read that fails gives unreadable, unless a
    workspace was found before it. The first line of the failure goes to log. The caller must
    have checked in_cmux.
    """
    try:
        tree = _cmux_json(["tree", "--all"], folder.parent, log)
        workspaces = _tree_workspaces(tree)
        if workspaces is None:
            return _unreadable_workspace(
                "cmux tree --all --json did not print windows with workspaces.", log
            )
        title = project_name(folder)
        wanted = ["local-tmux", "attach", session_name(folder), "--headless"]
        for workspace in workspaces:
            if workspace["title"] != title:
                continue
            surfaces = _workspace_surfaces(workspace)
            if surfaces is None:
                return _unreadable_workspace(
                    "cmux tree --all --json did not print panes with surfaces.", log
                )
            for surface in surfaces:
                if surface["type"] != "terminal":
                    continue
                shown = _cmux_json(
                    [
                        "surface",
                        "resume",
                        "show",
                        "--workspace",
                        str(workspace["id"]),
                        "--surface",
                        str(surface["ref"]),
                    ],
                    folder.parent,
                    log,
                    id_format=False,
                )
                arguments, cwd = _binding(shown)
                if arguments[-4:] != wanted or cwd is None or not same_folder(cwd, folder):
                    continue
                bound = str(surface["id"])
                others = sum(
                    1
                    for other in surfaces
                    if other["id"] != bound and other["type"] != _TODO_SURFACE_TYPE
                )
                return CloneWorkspace(
                    outcome="found",
                    workspace_id=str(workspace["id"]),
                    title=title,
                    surface_id=bound,
                    other_tabs=others,
                )
    except OperationError as exc:
        return _unreadable_workspace(_first_line(str(exc)), log)
    return CloneWorkspace(outcome="absent")


def _unreadable_workspace(failure: str, log: LogFn) -> CloneWorkspace:
    log(failure)
    return CloneWorkspace(outcome="unreadable", failure=failure)


# What show_clone_workspace did: it found the clone's workspace, or it opened a new one.
ShownWorkspace = Literal["found", "opened"]


def show_clone_workspace(folder: Path, focus: bool, log: LogFn) -> ShownWorkspace:
    """Goes to the cmux workspace of the clone, or opens it with the clone's terminal session.

    The caller has made sure that the session runs, and that draupnir runs in cmux. When the
    clone has its workspace, cmux selects it with focus, and without focus the log says that it
    is there. Only when the lookup found no workspace does draupnir create one. Its terminal
    runs the attach line, and cmux saves the same line as the command that brings the terminal
    back after a restart. The todo list goes below the terminal, as in every new workspace.

    Raises OperationError when the workspaces cannot be read, because a new workspace could be a
    second one for the clone. It also raises when cmux refuses to select or create the
    workspace. A failed save of the command is only logged, because the workspace still works.
    """
    found = find_clone_workspace(folder, log)
    if found.outcome == "found":
        if focus:
            assert found.workspace_id is not None  # A found workspace always has its id.
            _run_cmux(["workspace", "select", found.workspace_id], folder, log)
        else:
            log(f"The clone already has the cmux workspace {found.title}.")
        return "found"
    if found.outcome == "unreadable":
        # The first line makes sense alone. The failure follows for the output of the command.
        lines = [
            "draupnir could not read the cmux workspaces, so it did not open one for "
            f"{folder.name}, because the clone may have one already. Try again in a moment."
        ]
        if found.failure:
            lines.append(found.failure)
        raise OperationError("\n".join(lines))

    created = _create_cmux_workspace(folder, log, command=attach_line(folder), focus=focus)
    title = project_name(folder)
    lost = (
        f"The cmux workspace {title} is open, but cmux will not bring its terminal back after "
        "a restart."
    )
    if created is None:
        # Without the ids, draupnir cannot save the command that brings the terminal back.
        log(lost)
        return "opened"
    try:
        _run_cmux(
            [
                "surface",
                "resume",
                "set",
                "--workspace",
                created.workspace_id,
                "--surface",
                created.surface_id,
                "--cwd",
                folder,
                "--kind",
                "tmux",
                "--name",
                title,
                "--",
                *attach_argv(folder),
            ],
            folder,
            log,
        )
    except OperationError as exc:
        log(lost)
        log(_first_line(str(exc)))
    _add_todo_pane_or_log(created, folder, log)
    return "opened"


# ----- The terminal session of a clone ------------------------------------------------------

# What cmux local-tmux list says about the session of a clone.
SessionState = Literal["absent", "running", "stopped", "foreign", "unreadable"]

_LIST_COMMAND = "cmux local-tmux list --json"

# tmux sets this variable in every pane it runs, as <socket>,<pid>,<session number>.
_TMUX_VARIABLE = "TMUX"

# Where cmux local-tmux finds its tmux, when it is not the tmux on the PATH.
_TMUX_COMMAND_VARIABLE = "CMUX_LOCAL_TMUX_BIN"

# A pane that runs one of these runs only a shell, so the remove question does not name it. The
# basename of SHELL counts too.
_SHELLS = frozenset({"sh", "bash", "zsh", "fish", "dash", "ksh", "tcsh", "csh", "nu", "login"})

_UNREADABLE_SESSIONS = (
    "draupnir could not read the terminal sessions with cmux local-tmux list --json. Check "
    "that cmux is up to date and that tmux is installed."
)


@dataclass(frozen=True)
class TerminalSession:
    """What cmux local-tmux list --json says about the terminal session of a clone.

    The state is one of:

    - absent: no session has the name;
    - running: the clone's own session, and it runs;
    - stopped: the clone's own session, which cmux knows but which does not run;
    - foreign: a session with the name whose cwd is another folder;
    - unreadable: the list could not be read, or its entry for the name is not as expected.

    A session that read_session found running always has socket_path and session_id. A session
    that ensure_session has just started has neither, because it is not read again. clients is
    None when the list gives no count. failure holds the first line of what went wrong when the
    state is unreadable.
    """

    name: str
    state: SessionState
    cwd: str | None = None
    socket_path: str | None = None
    session_id: str | None = None
    clients: int | None = None
    failure: str | None = None


def _first_line(text: str) -> str:
    stripped = text.strip()
    return stripped.splitlines()[0] if stripped else ""


def _unreadable(name: str, failure: str) -> TerminalSession:
    return TerminalSession(name=name, state="unreadable", failure=failure)


def _unusable_field(name: str, field: str) -> str:
    return f"The entry of {name} in {_LIST_COMMAND} has no {field} that draupnir can use."


def read_session(folder: Path, log: LogFn) -> TerminalSession:
    """Reads the state of the terminal session of the clone in folder.

    It runs cmux local-tmux list --json in the workspace root, with a short timeout, because
    the remove question waits for it. A command that fails, times out or prints something else
    than a list of sessions gives unreadable. So does an entry for the name whose fields are
    missing or have another type: every field that decides whether draupnir may close or start
    the session must be there, so a missing field never allows either.

    Raises OperationError only when the cmux command line tool is not installed, or when
    DRAUPNIR_CMUX cannot be read. Those two keep their own messages, because they say what to
    fix.
    """
    name = session_name(folder)
    # A missing cmux and a DRAUPNIR_CMUX that cannot be split raise here, before the try, so
    # they do not turn into an unreadable list.
    _argv([])
    try:
        listed = _cmux_json(
            ["local-tmux", "list"],
            folder.parent,
            log,
            id_format=False,
            timeout=LOCAL_TMUX_READ_TIMEOUT_SECONDS,
        )
    except OperationError as exc:
        return _unreadable(name, _first_line(str(exc)))
    sessions = listed.get("sessions")
    if not isinstance(sessions, list):
        return _unreadable(name, f"{_LIST_COMMAND} printed no list of sessions.")

    # The name is compared exactly. A prefix would let shop-feat find the session shop-feat-2.
    matching: list[dict[str, object]] = []
    for entry in cast("list[object]", sessions):
        if isinstance(entry, dict):
            fields = cast("dict[str, object]", entry)
            if fields.get("session_name") == name:
                matching.append(fields)
    if not matching:
        return TerminalSession(name=name, state="absent")
    if len(matching) > 1:
        return _unreadable(name, f"{_LIST_COMMAND} lists the session {name} more than once.")
    entry = matching[0]

    cwd = entry.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return _unreadable(name, _unusable_field(name, "cwd"))
    if not same_folder(cwd, folder):
        return TerminalSession(name=name, state="foreign", cwd=cwd)
    # Only a JSON boolean counts. "true", 1 or null could be a change in cmux, and draupnir must
    # not guess whether the session runs.
    live = entry.get("live")
    if not isinstance(live, bool):
        return _unreadable(name, _unusable_field(name, "live"))
    socket_path = _text(entry, "socket_path")
    session_id = _text(entry, "session_id")
    clients = entry.get("clients")
    count = clients if isinstance(clients, int) and not isinstance(clients, bool) else None
    if not live:
        return TerminalSession(
            name=name,
            state="stopped",
            cwd=cwd,
            socket_path=socket_path,
            session_id=session_id,
            clients=count,
        )
    # Without the socket and the id, draupnir cannot tell whether it runs inside the session,
    # and it must not close the session it runs in.
    if socket_path is None:
        return _unreadable(name, _unusable_field(name, "socket_path"))
    if session_id is None:
        return _unreadable(name, _unusable_field(name, "session_id"))
    return TerminalSession(
        name=name,
        state="running",
        cwd=cwd,
        socket_path=socket_path,
        session_id=session_id,
        clients=count,
    )


def session_environment() -> dict[str, str]:
    """The environment a new terminal session starts with.

    It is the environment of draupnir without VIRTUAL_ENV, and without the bin folder of that
    virtual environment on the PATH. `uv run draupnir` sets both. A tmux server takes its global
    environment from the process that starts it, so the first session after a boot would hand
    them to every pane of every session. The CMUX_ variables stay. Passed as env to run, it also
    keeps out the variables that run adds to keep git from asking questions.
    """
    environment = dict(os.environ)
    venv = environment.pop("VIRTUAL_ENV", None)
    path = environment.get("PATH")
    if venv and path is not None:
        venv_bin = os.path.normpath(os.path.join(venv, "bin"))
        environment["PATH"] = os.pathsep.join(
            entry
            for entry in path.split(os.pathsep)
            if not entry or os.path.normpath(entry) != venv_bin
        )
    return environment


def _run_cmux(
    args: list[str | Path], cwd: Path, log: LogFn, *, env: Mapping[str, str] | None = None
) -> Result:
    """Runs a cmux command after its command line went to log.

    Raises CommandError when the command fails or runs too long.
    """
    command = _argv(args)
    log(f"$ {shlex.join(command)}")
    return run(command, cwd, timeout=CMUX_TIMEOUT_SECONDS, env=env)


def ensure_session(folder: Path, log: LogFn) -> TerminalSession:
    """Makes sure that the clone in folder has a terminal session that runs, and returns it.

    A session that runs is used as it is. A missing session is started in the clone folder,
    with the user's login shell. A session that cmux knows but that does not run is closed, and
    then started again. The command lines go to log.

    Raises OperationError when the name belongs to the session of another folder, when the
    sessions cannot be read, or when the session cannot be started.
    """
    session = read_session(folder, log)
    if session.state == "running":
        return session
    if session.state == "foreign":
        raise OperationError(
            f"The terminal session {session.name} belongs to {session.cwd}, so draupnir did not "
            f"use it for {folder}. Close it with cmux local-tmux close {session.name} when it "
            "is no longer needed, or give the clone another folder name."
        )
    if session.state == "unreadable":
        # The first line makes sense alone. The failure follows for the output of the command.
        lines = [_UNREADABLE_SESSIONS]
        if session.failure:
            lines.append(session.failure)
        raise OperationError("\n".join(lines))

    resolved = folder.resolve()
    try:
        if session.state == "stopped":
            # cmux 0.64.25 keeps the record of a session whose tmux session died, and that record
            # blocks the name for good (cmux issue #17269, fixed in 0.65.1). Closing the record
            # frees the name for the start below.
            _run_cmux(["local-tmux", "close", session.name], folder.parent, log)
        # --detached starts the session without a tab. Without it, start attaches a tab.
        _run_cmux(
            ["local-tmux", "start", session.name, "--cwd", resolved, "--detached"],
            folder.parent,
            log,
            env=session_environment(),
        )
    except CommandError as exc:
        raise OperationError(
            f"draupnir could not start the terminal session {session.name} for "
            f"{folder.name}.\n{exc}"
        ) from exc
    return TerminalSession(name=session.name, state="running", cwd=str(resolved))


def open_clone_terminal(
    folder: Path, cmux_config: CmuxConfig, workspace_flag: bool | None, log: LogFn
) -> None:
    """Starts the terminal session and the cmux workspace of a clone that `new` just made.

    It never raises (rule B1). It runs only after the clone exists, so a failure here never
    undoes the clone. Without a cmux command line tool it says nothing. With
    `[cmux] session = true` it makes sure that the session exists. A session that cannot start
    is logged together with what to do next, and the exit code of `new` stays 0.

    workspace_flag is the --workspace/--no-workspace flag, or None when it was not given. The
    flag decides whether the clone gets a cmux workspace, and without it
    cmux_config.clone_workspace decides. When the session runs and the clone got no workspace,
    the log ends with the line that attaches to the session.
    """
    try:
        if cmux_command() is None:
            return
        if cmux_config.session:
            try:
                ensure_session(folder, log)
            except OperationError as exc:
                log(str(exc))
                log(
                    f"The clone is ready. Run draupnir attach {folder.name} to try again, or "
                    "set session = false under [cmux] in draupnir.toml."
                )
                return
        shown = _open_new_clone_workspace(folder, cmux_config, workspace_flag, log)
        if cmux_config.session and not shown:
            log(f"Attach to the terminal session from any terminal with: {attach_line(folder)}")
    # The catch is not wider on purpose. A TypeError or a KeyError here would be a bug in
    # draupnir, and the tests must see it. These three are what the outside world can cause: a
    # cmux that refuses, a folder that went away, or output that does not parse.
    except (OperationError, OSError, ValueError) as exc:
        log(_first_line(str(exc)))


def _open_new_clone_workspace(
    folder: Path, cmux_config: CmuxConfig, workspace_flag: bool | None, log: LogFn
) -> bool:
    """Opens the cmux workspace of a new clone when the flag or the config asks for one (W3).

    Returns True when the clone has a workspace now, found or opened. The workspace does not
    take the focus, so the user stays where `new` ran. Outside cmux there is no workspace to
    open, and only a flag that was given makes that worth a line. The first failure ends this
    part, so a cmux that refuses or hangs costs one timeout. A lookup that cannot be read opens
    nothing, because the clone may have a workspace already.
    """
    wish = workspace_flag if workspace_flag is not None else cmux_config.clone_workspace
    if not wish:
        return False
    if not in_cmux():
        if workspace_flag is not None:
            log(
                "draupnir does not run in a cmux terminal, so it did not open a cmux workspace "
                f"for {folder.name}."
            )
        return False
    try:
        if cmux_config.session:
            show_clone_workspace(folder, False, log)
        else:
            # Without a session, the workspace is a plain shell in the clone, as w opens it.
            open_cmux_workspace(folder, log, focus=False)
    except OperationError as exc:
        log(str(exc))
        return False
    return True


def open_clone_workspace_for_user(folder: Path, cmux_config: CmuxConfig, log: LogFn) -> None:
    """Goes to the cmux workspace of a clone, for the w key of the dashboard (rule W4).

    With `[cmux] session = true` it makes sure that the clone's terminal session runs, and then
    selects the clone's workspace, or opens it with the session when the clone has none. With
    session = false it opens a new workspace with a shell in the clone, as before. The caller
    has made sure that draupnir runs in cmux.

    Raises OperationError when the session cannot start, when the workspaces cannot be read, or
    when cmux refuses the workspace. So a second press after a failed read never opens a second
    workspace.
    """
    if not cmux_config.session:
        open_cmux_workspace(folder, log)
        return
    ensure_session(folder, log)
    show_clone_workspace(folder, True, log)


def runs_in_session(session: TerminalSession) -> bool:
    """True when draupnir runs in a pane of session.

    tmux sets TMUX to <socket>,<pid>,<n> in every pane, and $<n> is the id of the session. A
    session without a socket path or an id never matches.
    """
    if session.socket_path is None or session.session_id is None:
        return False
    # The socket path may hold a comma, so the two numbers are split off from the right.
    parts = os.environ.get(_TMUX_VARIABLE, "").rsplit(",", 2)
    if len(parts) != 3:
        return False
    socket, _pid, number = parts
    return socket == session.socket_path and f"${number}" == session.session_id


def close_session(folder: Path, log: LogFn) -> None:
    """Closes the terminal session of the clone in folder, and logs what it could not close.

    It leaves alone the session of another folder, and the session that draupnir runs in. The
    commands run in the workspace root, because the clone folder is about to go. It never raises
    an OperationError: a session that stays open must not stop the removal of the clone.
    """
    try:
        _close_session(folder, log)
    except OperationError as exc:
        log(_first_line(str(exc)))


def _close_session(folder: Path, log: LogFn) -> None:
    session = read_session(folder, log)
    if session.state == "absent":
        return
    if session.state == "foreign":
        log(
            f"The terminal session {session.name} belongs to {session.cwd}, so draupnir left it "
            "open."
        )
        return
    if session.state == "unreadable":
        log(
            "draupnir could not read the terminal sessions, so it did not close one. Check "
            "with cmux local-tmux list."
        )
        if session.failure:
            log(session.failure)
        return
    if runs_in_session(session):
        log(
            f"draupnir runs in the terminal session {session.name}, so it left the session "
            "open. Type exit there to close it."
        )
        return
    try:
        _run_cmux(["local-tmux", "close", session.name], folder.parent, log)
    except CommandError as exc:
        failure = _first_line(exc.output) or _first_line(str(exc))
    else:
        return
    # A close that failed may still have closed the session, so the list decides.
    if read_session(folder, log).state == "absent":
        return
    log(
        f"draupnir could not close the terminal session {session.name}. Close it with cmux "
        f"local-tmux close {session.name}."
    )
    log(failure)


def session_programs(session: TerminalSession) -> list[str] | None:
    """The programs that run in the panes of session, other than shells, or None when unknown.

    The names are those that tmux gives (#{pane_current_command}), in their order and without
    duplicates. Some programs show as their version number, such as 2.1.301. No tmux, a tmux
    that fails and a tmux that runs too long all give None, so a session is never called idle
    by mistake.
    """
    if session.socket_path is None or session.cwd is None:
        return None
    tmux = os.environ.get(_TMUX_COMMAND_VARIABLE) or shutil.which("tmux")
    if not tmux:
        return None
    command = [
        tmux,
        "-S",
        session.socket_path,
        "list-panes",
        "-s",
        # tmux matches a bare session name by prefix. The = asks for exactly this session.
        "-t",
        f"={session.name}",
        "-F",
        "#{pane_current_command}",
    ]
    try:
        # The parent of the clone folder is the workspace root, which stays when the clone goes.
        result = run(
            command,
            Path(session.cwd).parent,
            check=False,
            timeout=LOCAL_TMUX_READ_TIMEOUT_SECONDS,
        )
    except CommandError:
        return None
    if result.code != 0:
        return None
    shells = _SHELLS | {os.path.basename(os.environ.get("SHELL", ""))}
    programs: list[str] = []
    for line in result.stdout.splitlines():
        # A login shell shows with a leading -, such as -zsh.
        program = line.strip().removeprefix("-")
        if program and program not in shells and program not in programs:
            programs.append(program)
    return programs


_UNREADABLE_NOTE = (
    "draupnir could not read the terminal sessions, so it cannot say whether the clone has one."
)


def _ignore_log(_line: str) -> None:
    """A log for the reads behind the remove question, which shows no command lines."""


def _clients_part(clients: int | None) -> str:
    if clients is None:
        return ""
    if clients == 0:
        return ", with no client attached"
    if clients == 1:
        return ", with 1 client attached"
    return f", with {clients} clients attached"


def _note_lines(session: TerminalSession) -> list[str]:
    if session.state in ("absent", "foreign"):
        # The session of another folder is not the clone's, so the question does not name it.
        return []
    if session.state == "unreadable":
        return [_UNREADABLE_NOTE]
    if session.state == "stopped":
        return [
            f"Its terminal session {session.name} is registered, but it does not run. It is "
            "removed together with the clone."
        ]
    lines = [f"Its terminal session {session.name} is open{_clients_part(session.clients)}."]
    programs = session_programs(session)
    if programs:
        lines.append(f"A program still runs in it: {', '.join(programs)}.")
    if runs_in_session(session):
        lines.append("draupnir runs in this session, so the session stays open.")
    else:
        lines.append("The session is closed together with the clone.")
    return lines


def session_note(folder: Path) -> list[str]:
    """The lines about the clone's terminal session that go under the remove question (rule C3).

    It returns no lines without a cmux command line tool, when the clone has no session, and when
    the session with the name belongs to another folder. It never raises (rule B1): what it
    cannot read gives the line that says so, because the question must still be asked.
    """
    try:
        if cmux_command() is None:
            return []
        return _note_lines(read_session(folder, _ignore_log))
    # The catch is not wider on purpose. A TypeError or a KeyError here would be a bug in
    # draupnir, and the tests must see it. These three are what the outside world can cause: a
    # cmux that refuses, a folder that went away, or output that does not parse.
    except (OperationError, OSError, ValueError):
        return [_UNREADABLE_NOTE]


def close_clone_terminal(folder: Path, log: LogFn) -> None:
    """Closes the terminal session of a clone before `remove` deletes it. It never raises (B1).

    remove_clone calls it after the checks passed and before the delete, so a program in the
    session cannot write into the folder while it goes. Without a cmux command line tool it says
    nothing. What it cannot close is logged, and the removal goes on.
    """
    try:
        if cmux_command() is None:
            return
        close_session(folder, log)
    # The catch is not wider on purpose. A TypeError or a KeyError here would be a bug in
    # draupnir, and the tests must see it. These three are what the outside world can cause: a
    # cmux that refuses, a folder that went away, or output that does not parse.
    except (OperationError, OSError, ValueError) as exc:
        log(_first_line(str(exc)))


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
