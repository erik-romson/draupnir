"""Talks to the cmux terminal: new workspaces, and the dashboard's status in its sidebar.

It also names the terminal session of a clone, builds the line that attaches to it, and reads,
starts and closes that session with cmux local-tmux.
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
    """Starts the terminal session of a clone that `new` just made. It never raises (rule B1).

    It runs only after the clone exists, so a failure here never undoes the clone. Without a
    cmux command line tool it says nothing. With `[cmux] session = true` it makes sure that the
    session exists, and then logs the line that attaches to it. A session that cannot start is
    logged together with what to do next, and the exit code of `new` stays 0.

    workspace_flag is the --workspace/--no-workspace flag, or None when it was not given. The
    workspace part of `new` (rule W3) reads it: the flag when given, else
    cmux_config.clone_workspace. Nothing reads it yet.
    """
    del workspace_flag
    try:
        if cmux_command() is None:
            return
        if not cmux_config.session:
            return
        try:
            ensure_session(folder, log)
        except OperationError as exc:
            log(str(exc))
            log(
                f"The clone is ready. Run draupnir attach {folder.name} to try again, or set "
                "session = false under [cmux] in draupnir.toml."
            )
            return
        log(f"Attach to the terminal session from any terminal with: {attach_line(folder)}")
    # The catch is not wider on purpose. A TypeError or a KeyError here would be a bug in
    # draupnir, and the tests must see it. These three are what the outside world can cause: a
    # cmux that refuses, a folder that went away, or output that does not parse.
    except (OperationError, OSError, ValueError) as exc:
        log(_first_line(str(exc)))


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
