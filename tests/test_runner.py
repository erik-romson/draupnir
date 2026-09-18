from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from draupnir.errors import OperationError
from draupnir.runner import (
    CommandError,
    require_git,
    reset_git_version_cache,
    run,
)


def _write_script(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\n{body}")
    path.chmod(0o755)


def test_run_captures_stdout_and_stderr_separately(tmp_path: Path) -> None:
    script = tmp_path / "both.sh"
    _write_script(script, "echo out-line\necho err-line 1>&2\n")

    result = run([str(script)], cwd=tmp_path)

    assert result.stdout == "out-line\n"
    assert result.stderr == "err-line\n"


def test_run_with_log_streams_every_line_in_order(tmp_path: Path) -> None:
    script = tmp_path / "ordered.sh"
    _write_script(script, "echo one\necho two\necho three\n")

    lines: list[str] = []
    run([str(script)], cwd=tmp_path, log=lines.append)

    assert lines[0].startswith("$ ")
    assert lines[1:] == ["one", "two", "three"]


def test_run_raises_command_error_with_the_last_lines(tmp_path: Path) -> None:
    script = tmp_path / "fail.sh"
    _write_script(
        script,
        'i=1\nwhile [ "$i" -le 20 ]; do echo "line-$i"; i=$((i + 1)); done\nexit 7\n',
    )

    with pytest.raises(CommandError) as exc_info:
        run([str(script)], cwd=tmp_path)

    message = str(exc_info.value)
    assert "failed with exit code 7" in message
    assert "line-20" in message
    assert "line-6" in message
    assert "line-5" not in message


def test_run_reports_a_missing_command(tmp_path: Path) -> None:
    with pytest.raises(CommandError) as exc_info:
        run(["a-command-that-does-not-exist-anywhere"], cwd=tmp_path)

    assert "a-command-that-does-not-exist-anywhere" in str(exc_info.value)
    assert "not found" in str(exc_info.value)


def test_run_sets_the_no_prompt_variables(tmp_path: Path) -> None:
    script = tmp_path / "env.sh"
    _write_script(
        script,
        "echo GIT_TERMINAL_PROMPT=$GIT_TERMINAL_PROMPT\n"
        "echo GIT_OPTIONAL_LOCKS=$GIT_OPTIONAL_LOCKS\n"
        "echo GIT_EDITOR=$GIT_EDITOR\n"
        "echo GIT_SEQUENCE_EDITOR=$GIT_SEQUENCE_EDITOR\n"
        "echo GCM_INTERACTIVE=$GCM_INTERACTIVE\n"
        "echo SSH_ASKPASS_REQUIRE=$SSH_ASKPASS_REQUIRE\n"
        "echo GH_PROMPT_DISABLED=$GH_PROMPT_DISABLED\n"
        "echo GH_NO_UPDATE_NOTIFIER=$GH_NO_UPDATE_NOTIFIER\n"
        "echo NO_COLOR=$NO_COLOR\n"
        "echo CLICOLOR=$CLICOLOR\n",
    )

    result = run([str(script)], cwd=tmp_path)

    assert result.stdout == (
        "GIT_TERMINAL_PROMPT=0\n"
        "GIT_OPTIONAL_LOCKS=0\n"
        "GIT_EDITOR=true\n"
        "GIT_SEQUENCE_EDITOR=true\n"
        "GCM_INTERACTIVE=never\n"
        "SSH_ASKPASS_REQUIRE=never\n"
        "GH_PROMPT_DISABLED=1\n"
        "GH_NO_UPDATE_NOTIFIER=1\n"
        "NO_COLOR=1\n"
        "CLICOLOR=0\n"
    )


def test_run_starts_the_command_in_its_own_session(tmp_path: Path) -> None:
    result = run(
        [sys.executable, "-c", "import os; print(os.getsid(0) == os.getpid())"],
        cwd=tmp_path,
    )

    assert result.stdout.strip() == "True"


def test_run_kills_a_command_after_the_timeout(tmp_path: Path) -> None:
    script = tmp_path / "slow.sh"
    _write_script(script, "sleep 30\n")

    start = time.monotonic()
    with pytest.raises(CommandError) as exc_info:
        run([str(script)], cwd=tmp_path, timeout=0.2)
    elapsed = time.monotonic() - start

    assert elapsed < 10
    assert "longer than" in str(exc_info.value)


def test_run_timeout_also_stops_child_processes(tmp_path: Path) -> None:
    script = tmp_path / "spawn_child.sh"
    _write_script(script, "sleep 30 &\necho $!\nwait\n")

    lines: list[str] = []
    with pytest.raises(CommandError):
        run([str(script)], cwd=tmp_path, log=lines.append, timeout=2)

    child_pid = int(lines[1])
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_run_stops_the_command_when_interrupted(tmp_path: Path) -> None:
    script = tmp_path / "spawn_child.sh"
    _write_script(script, "sleep 30 &\necho $!\nwait\n")

    child_pids: list[int] = []

    def interrupt_on_child_pid(line: str) -> None:
        if line.isdigit():
            child_pids.append(int(line))
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run([str(script)], cwd=tmp_path, log=interrupt_on_child_pid)

    assert not _is_running(child_pids[0])


def _is_running(pid: int) -> bool:
    """Whether pid still runs, allowing a few seconds for a stopped process to go away."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        time.sleep(0.05)
    return True


def test_require_git_refuses_an_old_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_git = tmp_path / "git"
    _write_script(fake_git, "echo 'git version 2.20.0'\n")
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    reset_git_version_cache()

    try:
        with pytest.raises(OperationError) as exc_info:
            require_git()
    finally:
        reset_git_version_cache()

    message = str(exc_info.value)
    assert "2.20" in message
    assert "2.31" in message
