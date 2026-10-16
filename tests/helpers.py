"""Typed helpers shared by the test suite."""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from fake_gh import rebase_merge

from draupnir.runner import LogFn

if TYPE_CHECKING:
    from conftest import Env
    from textual.pilot import Pilot

    from draupnir.ui.app import DraupnirApp

# How long a UI test waits for the dashboard before it fails. The wait ends as soon as the
# condition holds, so a passing test never waits this long.
UI_WAIT_SECONDS = 30.0


def sh(cwd: Path, *args: str, extra_env: dict[str, str] | None = None) -> str:
    """Runs a plain command for test setup, outside the runner draupnir uses itself."""
    env = {**os.environ, **extra_env} if extra_env else None
    return subprocess.run(
        args, cwd=cwd, check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


class FakeStdin:
    """Stands in for sys.stdin, so a test can decide whether it is a terminal."""

    def __init__(self, is_a_tty: bool) -> None:
        self._is_a_tty = is_a_tty

    def isatty(self) -> bool:
        return self._is_a_tty


def write_executable(path: Path, content: str) -> None:
    """Writes content to path and makes it executable, for a stub mvn, mvnw or hook."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


# The stub cmux is a Python script, so it can read and edit JSON. A small shell file named cmux
# in the bin folder runs it, the way tests/conftest.py starts the fake gh. The script lives next to
# the bin folder, so nothing else lands on the PATH. It finds its data files next to itself.
_STUB_CMUX = r"""
import json
import os
import sys
import time
from pathlib import Path

here = Path(__file__).parent
args = sys.argv[1:]

with (here / "cmux-calls.txt").open("a") as handle:
    handle.write("".join(f"{arg}\t" for arg in args) + "\n")

rules = json.loads((here / "cmux-rules.json").read_text())
sessions_file = here / "cmux-sessions.json"


def matches(prefixes):
    return any(args[: len(prefix.split())] == prefix.split() for prefix in prefixes)


def option(name):
    return args[args.index(name) + 1] if name in args[:-1] else ""


def close_session():
    sessions = json.loads(sessions_file.read_text())
    sessions["sessions"] = [
        entry for entry in sessions["sessions"] if entry.get("session_name") != args[2]
    ]
    sessions_file.write_text(json.dumps(sessions))


def fail():
    print("cmux: the stub was told to fail this call", file=sys.stderr)
    sys.exit(1)


if matches(rules["slow"]):
    time.sleep(1)
if matches(rules["fail"]):
    fail()

if args[:2] == ["local-tmux", "close"] and len(args) > 2:
    close_session()
elif args[:2] == ["local-tmux", "start"]:
    lines = [f"{name}={value}" for name, value in os.environ.items()]
    (here / "cmux-start-env.txt").write_text("\n".join(lines) + "\n")

if matches(rules["fail_after"]):
    fail()

if args[:1] == ["workspace-group"]:
    print((here / "cmux-groups.json").read_text(), end="")
elif args[:2] == ["local-tmux", "list"]:
    print(sessions_file.read_text(), end="")
elif args[:1] == ["tree"]:
    print((here / "cmux-tree.json").read_text(), end="")
elif args[:3] == ["surface", "resume", "show"]:
    bindings = json.loads((here / "cmux-bindings.json").read_text())
    print(json.dumps(bindings.get(option("--surface"), {})))
elif args[:1] == ["workspace"]:
    print('{"workspace_id": "new-workspace", "surface_id": "new-terminal"}')
elif args[:1] == ["todo"]:
    print('{"surface_id": "new-todo"}')
"""


def _json_text(value: object) -> str:
    """A JSON value as text: a str is already JSON text, and anything else is dumped."""
    return value if isinstance(value, str) else json.dumps(value)


def write_stub_cmux(
    bin_dir: Path,
    groups: str = '{"groups": []}',
    *,
    sessions: object = None,
    tree: object = None,
    bindings: Mapping[str, object] | None = None,
    fail: Sequence[str] = (),
    fail_after: Sequence[str] = (),
    slow: Sequence[str] = (),
) -> Path:
    """Writes a stub cmux into bin_dir, and returns the file that gets its calls.

    Each call appends one line to the file, with each argument followed by a tab. Failed and
    slow calls are logged too. The stub answers:

    - `workspace-group list` with groups, which is JSON text;
    - `workspace ...` with the workspace new-workspace and the terminal new-terminal;
    - `todo ...` with the surface new-todo;
    - `local-tmux list` with sessions, read from a file on every call. A value is JSON text or a
      Python object, and the default is `{"sessions": []}`. `local-tmux close <name>` removes the
      entry with that name from the file. `local-tmux start` adds no entry. It writes its
      environment to `cmux-start-env.txt` next to the calls file, one `NAME=value` line per
      variable;
    - `tree ...` with tree, as JSON text or a Python object. The default is `{"windows": []}`;
    - `surface resume show ... --surface <ref>` with bindings[ref]. The bindings map a surface
      ref to JSON text or a Python object. A surface that is not in it gets `{}`.

    Every other call prints nothing and exits 0. The keywords fail, fail_after and slow are lists
    of argument prefixes, such as "local-tmux start". A prefix matches word by word, from the
    first argument. A call that matches fail prints a line on stderr and exits 1 before it has any
    effect. A call that matches fail_after does the same after its effect, so a `local-tmux
    close` removes its entry first. A call that matches slow sleeps one second before it answers.
    """
    root = bin_dir.parent
    calls = root / "cmux-calls.txt"
    calls.unlink(missing_ok=True)
    (root / "cmux-start-env.txt").unlink(missing_ok=True)
    (root / "cmux-groups.json").write_text(groups)
    (root / "cmux-sessions.json").write_text(
        _json_text(sessions if sessions is not None else {"sessions": []})
    )
    (root / "cmux-tree.json").write_text(_json_text(tree if tree is not None else {"windows": []}))
    parsed = {ref: json.loads(_json_text(value)) for ref, value in (bindings or {}).items()}
    (root / "cmux-bindings.json").write_text(json.dumps(parsed))
    (root / "cmux-rules.json").write_text(
        json.dumps({"fail": list(fail), "fail_after": list(fail_after), "slow": list(slow)})
    )
    script = root / "cmux-stub.py"
    script.write_text(_STUB_CMUX)
    write_executable(
        bin_dir / "cmux",
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -I -S {shlex.quote(str(script))} "$@"\n',
    )
    return calls


def write_stub_tmux(bin_dir: Path, panes: str = "zsh\n", *, code: int = 0) -> Path:
    """Writes a stub tmux into bin_dir, and returns the file that gets its calls.

    Each call appends one line to the file, with each argument followed by a tab, as the stub
    cmux does. The stub prints panes, which stands for the `#{pane_current_command}` lines, and
    exits with code.
    """
    calls = bin_dir.parent / "tmux-calls.txt"
    calls.unlink(missing_ok=True)
    panes_file = bin_dir.parent / "tmux-panes.txt"
    panes_file.write_text(panes)
    write_executable(
        bin_dir / "tmux",
        "#!/bin/sh\n"
        f'printf \'%s\\t\' "$@" >> "{calls}"\n'
        f'echo >> "{calls}"\n'
        f'cat "{panes_file}"\n'
        f"exit {code}\n",
    )
    return calls


def cmux_calls(calls: Path) -> list[list[str]]:
    """The calls that the stub cmux from write_stub_cmux got, as lists of arguments."""
    return [line.rstrip("\t").split("\t") for line in calls.read_text().splitlines()]


def append_config(root: Path, text: str) -> None:
    """Appends text to root/draupnir.toml, for a test that needs a non-default config value."""
    path = root / "draupnir.toml"
    with path.open("a") as handle:
        handle.write(text)


def commit(repo: Path, name: str, text: str, message: str) -> str:
    """Writes a file, commits it, and returns the new commit's sha."""
    (repo / name).write_text(text)
    sh(repo, "git", "add", name)
    sh(repo, "git", "commit", "-q", "-m", message)
    return sh(repo, "git", "rev-parse", "HEAD")


def push_from_other_clone(env: Env, branch: str) -> str:
    """A second, throwaway clone of the bare repository pushes a new commit to branch.

    This stands in for a teammate who pushed to GitHub while the clone under test has not
    fetched yet, and so cannot see the new commit.
    """
    other = env.tmp / f"other-clone-{branch.replace('/', '-')}"
    sh(env.tmp, "git", "clone", "-q", "--branch", branch, str(env.remote), str(other))
    sha = commit(other, "other.txt", f"from another clone on {branch}\n", f"update {branch}")
    sh(other, "git", "push", "-q", "origin", branch)
    return sha


def rebase_merge_on_github(env: Env, branch: str, base: str = "main") -> list[str]:
    """Does on the bare repository what GitHub's "rebase and merge" does, and deletes branch.

    GitHub replays every commit of the branch onto base as a new commit with a new committer
    date, also when the branch is already on top of base. So base gets commits with the same
    changes as the branch, but with other shas. Then GitHub deletes the branch. Returns the
    shas of the new commits on base, oldest first. The fake gh uses the same function when it
    merges a pull request.
    """
    return rebase_merge(env.remote, env.tmp, branch, base, delete_branch=True)


def logger() -> tuple[list[str], LogFn]:
    """A list that collects log lines, and a LogFn that appends to it."""
    lines: list[str] = []
    return lines, lines.append


async def settle(app: DraupnirApp, pilot: Pilot[None]) -> None:
    """Waits until no action and no status read runs, and the app has handled its messages.

    An action ends with a new status read, which starts after the action's worker is done. So
    one `wait_for_complete` is not enough, and this waits again until nothing runs.
    """
    deadline = time.monotonic() + UI_WAIT_SECONDS
    while True:
        # Textual types the worker manager with workers of any result type.
        await app.workers.wait_for_complete()  # pyright: ignore[reportUnknownMemberType]
        await pilot.pause()
        if not app.busy and not app.reading and len(app.workers) == 0:
            return
        if time.monotonic() > deadline:
            raise AssertionError("The dashboard still had work running after the wait.")


async def wait_until(pilot: Pilot[None], condition: Callable[[], bool], what: str) -> None:
    """Lets the app run until condition is true. Fails with a message that names what."""
    deadline = time.monotonic() + UI_WAIT_SECONDS
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"The dashboard did not get to this state in time: {what}.")
        await pilot.pause()
