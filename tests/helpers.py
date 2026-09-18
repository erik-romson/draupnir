"""Typed helpers shared by the test suite."""

from __future__ import annotations

import os
import stat
import subprocess
import time
from collections.abc import Callable
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
