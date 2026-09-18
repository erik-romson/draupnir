"""Runs external commands: git, gh and other tools, without ever waiting for input."""

from __future__ import annotations

import contextlib
import os
import re
import shlex
import signal
import subprocess
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from draupnir.errors import OperationError

LogFn = Callable[[str], None]

# Commands must never wait for input, because nobody can type into them (rule 1).
_EXTRA_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_EDITOR": "true",
    "GIT_SEQUENCE_EDITOR": "true",
    "GCM_INTERACTIVE": "never",
    "SSH_ASKPASS_REQUIRE": "never",
    "GH_PROMPT_DISABLED": "1",
    "GH_NO_UPDATE_NOTIFIER": "1",
    "NO_COLOR": "1",
    "CLICOLOR": "0",
}

# How long a command may run before it is stopped (rule 5). git clone and Maven get no
# timeout: they are defined here as None so callers do not invent their own values.
FETCH_TIMEOUT_SECONDS = 600.0
PUSH_TIMEOUT_SECONDS = 600.0
GH_TIMEOUT_SECONDS = 120.0
CLONE_TIMEOUT_SECONDS = None
MAVEN_TIMEOUT_SECONDS = None

# After SIGTERM, a timed-out command gets this long to exit before SIGKILL.
_TERMINATE_GRACE_SECONDS = 0.2

# The oldest git version draupnir supports, for --force-if-includes and
# rev-parse --path-format.
MIN_GIT_VERSION = (2, 31)

NETWORK_HINT = (
    "If git could not log in, load your SSH key into ssh-agent or set up a credential helper. "
    "draupnir cannot answer a password question."
)

_NETWORK_GIT_SUBCOMMANDS = {"clone", "fetch", "ls-remote", "push"}


class CommandError(OperationError):
    """A command failed. The message names the command and shows its last output lines."""

    def __init__(
        self,
        args: Sequence[str],
        code: int,
        output: str,
        *,
        reason: str | None = None,
        hint: str | None = None,
    ) -> None:
        self.command = list(args)
        self.code = code
        self.output = output
        tail = "\n".join(output.strip().splitlines()[-15:])
        headline = reason or f"{shlex.join(self.command)} failed with exit code {code}."
        parts = [headline]
        if tail:
            parts.append(tail)
        if hint:
            parts.append(hint)
        super().__init__("\n".join(parts))


@dataclass(frozen=True)
class Result:
    code: int
    stdout: str
    stderr: str


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(_EXTRA_ENV)
    return env


def _format_timeout(timeout: float) -> str:
    if timeout == int(timeout):
        return str(int(timeout))
    return f"{timeout:g}"


def _stop_process_group(proc: subprocess.Popen[str]) -> None:
    """Stops the command and every child process it started, such as ssh.

    Sends SIGTERM to the process group, waits a short grace period, and sends SIGKILL if the
    command is still running.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return

    try:
        proc.wait(timeout=_TERMINATE_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass

    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGKILL)


def _watch_for_timeout(
    proc: subprocess.Popen[str], timeout: float, timed_out: threading.Event
) -> None:
    """Waits up to `timeout` seconds, then stops the whole process group.

    Runs in its own thread, so the caller can keep streaming output while this thread
    waits.
    """
    try:
        proc.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        pass

    timed_out.set()
    _stop_process_group(proc)


def _collect(proc: subprocess.Popen[str], cmd: list[str], log: LogFn | None) -> Result:
    """Reads the output of proc until it exits. With log, it passes every line to log."""
    if log is None:
        stdout, stderr = proc.communicate()
        return Result(proc.wait(), stdout, stderr)
    log(f"$ {shlex.join(cmd)}")
    lines: list[str] = []
    assert proc.stdout is not None
    for raw_line in proc.stdout:
        line = raw_line.rstrip("\n")
        lines.append(line)
        log(line)
    return Result(proc.wait(), "\n".join(lines), "")


def run(
    args: Sequence[str | Path],
    cwd: Path,
    *,
    check: bool = True,
    log: LogFn | None = None,
    timeout: float | None = None,
) -> Result:
    """Runs a command. It never waits for input, and it can be stopped by a timeout.

    With log, the command line and every output line are passed to log, in order, as the
    command runs. stdout then holds both stdout and stderr, and stderr is empty. Without
    log, stdout and stderr are captured separately.

    Raises CommandError when the command cannot be started, when it runs longer than
    timeout, or when check is true and it exits with a non-zero code.
    """
    cmd = [str(a) for a in args]
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if log is not None else subprocess.PIPE,
            text=True,
            env=_env(),
            start_new_session=True,
        )
    except OSError as exc:
        raise CommandError(
            cmd,
            127,
            "",
            reason=f"{cmd[0]} was not found. Install it, or check that it is on PATH.",
        ) from exc

    timed_out = threading.Event()
    watchdog: threading.Thread | None = None
    if timeout is not None:
        watchdog = threading.Thread(
            target=_watch_for_timeout, args=(proc, timeout, timed_out), daemon=True
        )
        watchdog.start()

    try:
        result = _collect(proc, cmd, log)
    except BaseException:
        # The command runs in its own session, so Ctrl+C never reaches it. Stop it here, so it
        # does not keep running after draupnir has given up on it.
        _stop_process_group(proc)
        raise

    if watchdog is not None:
        watchdog.join()

    if timed_out.is_set():
        raise CommandError(
            cmd,
            result.code,
            result.stdout + result.stderr,
            reason=(
                f"The command took longer than {_format_timeout(timeout or 0)} seconds "
                "and was stopped."
            ),
        )

    if check and result.code != 0:
        raise CommandError(cmd, result.code, result.stdout + result.stderr)
    return result


def git(
    cwd: Path,
    *args: str,
    check: bool = True,
    log: LogFn | None = None,
    timeout: float | None = None,
) -> Result:
    return run(["git", *args], cwd, check=check, log=log, timeout=timeout)


def _is_push_refusal(output: str) -> bool:
    """True when a failed `git push --porcelain` failed only because refs were rejected.

    `git push --porcelain` prints one tab-separated line per ref, starting with `!` for a
    ref that was not updated. That flag is stable, machine-readable output (rule 3), not a
    message meant for people, so it is safe to read.
    """
    return any(line.startswith("!\t") for line in output.splitlines())


def git_network(
    cwd: Path,
    *args: str,
    check: bool = True,
    log: LogFn | None = None,
    timeout: float | None = None,
) -> Result:
    """Runs a git subcommand that reaches the network: clone, fetch, ls-remote or push.

    On failure, the error message ends with NETWORK_HINT, unless the command is a push
    that failed only because refs were rejected. That is a normal outcome the caller
    handles on its own, not a login or network problem.
    """
    try:
        return git(cwd, *args, check=check, log=log, timeout=timeout)
    except CommandError as exc:
        if args and args[0] == "push" and _is_push_refusal(exc.output):
            raise
        raise CommandError(exc.command, exc.code, exc.output, hint=NETWORK_HINT) from exc


_git_version_cache: tuple[int, int] | None = None


def _read_git_version() -> tuple[int, int]:
    result = run(["git", "--version"], Path.cwd())
    match = re.search(r"(\d+)\.(\d+)", result.stdout)
    if match is None:
        raise OperationError(
            f"draupnir could not read the git version from '{result.stdout.strip()}'. "
            "Check that git is installed correctly."
        )
    return (int(match.group(1)), int(match.group(2)))


def require_git() -> None:
    """Raises OperationError when the git on PATH is older than MIN_GIT_VERSION.

    Runs `git --version` once per process. Later calls reuse the cached result.
    """
    global _git_version_cache
    if _git_version_cache is None:
        _git_version_cache = _read_git_version()
    if _git_version_cache < MIN_GIT_VERSION:
        found = ".".join(str(part) for part in _git_version_cache)
        needed = ".".join(str(part) for part in MIN_GIT_VERSION)
        raise OperationError(
            f"draupnir found git {found}, but needs git {needed} or newer. "
            "Older git versions do not have --force-if-includes or "
            "rev-parse --path-format. Install a newer git and try again."
        )


def reset_git_version_cache() -> None:
    """Forgets the cached git version. Tests use this to check a different git binary."""
    global _git_version_cache
    _git_version_cache = None
