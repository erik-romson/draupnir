"""Tests for src/draupnir/ide.py: finding and starting IntelliJ IDEA or PyCharm (R13)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from conftest import Env

from draupnir.cli import main
from draupnir.config import Config, IdeConfig
from draupnir.errors import OperationError
from draupnir.ide import ide_command, ide_kind, open_ide

# How long a test waits for the stub idea script to write its arguments.
_WAIT_SECONDS = 30.0


def _wait_for_content(path: Path) -> str:
    """Polls path until it has content, with a deadline, instead of a fixed sleep."""
    deadline = time.monotonic() + _WAIT_SECONDS
    while not path.is_file() or not path.read_text().strip():
        if time.monotonic() > deadline:
            raise AssertionError(f"{path} was not written to in time.")
        time.sleep(0.02)
    return path.read_text()


def test_ide_kind_picks_idea_for_pom_and_pycharm_for_pyproject(tmp_path: Path) -> None:
    java = tmp_path / "java"
    java.mkdir()
    (java / "pom.xml").write_text("<project/>\n")
    assert ide_kind(java) == "idea"

    gradle = tmp_path / "gradle"
    gradle.mkdir()
    (gradle / "build.gradle.kts").write_text("")
    assert ide_kind(gradle) == "idea"

    python_project = tmp_path / "python"
    python_project.mkdir()
    (python_project / "pyproject.toml").write_text("")
    assert ide_kind(python_project) == "pycharm"

    other_python = tmp_path / "other-python"
    other_python.mkdir()
    (other_python / "requirements.txt").write_text("")
    assert ide_kind(other_python) == "pycharm"

    empty = tmp_path / "empty"
    empty.mkdir()
    assert ide_kind(empty) == "idea"


def test_ide_command_prefers_environment_then_config_then_path(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Nothing configured: the stub idea from the fixture's PATH is found.
    assert ide_command(env.main, Config()) == [str(env.bin / "idea")]

    # The config wins over PATH.
    config = Config(ide=IdeConfig(java="config-idea"))
    assert ide_command(env.main, config) == ["config-idea"]

    # The environment variable wins over the config, and is split with shlex.
    monkeypatch.setenv("DRAUPNIR_IDEA", "env-idea --flag")
    assert ide_command(env.main, config) == ["env-idea", "--flag"]


def test_ide_command_finds_the_toolbox_script(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/does-not-exist")
    toolbox = Path.home() / ".local/share/JetBrains/Toolbox/scripts"
    toolbox.mkdir(parents=True)
    script = toolbox / "idea"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)

    assert ide_command(env.main, Config()) == [str(script)]


def test_ide_command_missing_gives_a_message_that_names_the_variable(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/does-not-exist")

    with pytest.raises(OperationError) as exc_info:
        ide_command(env.main, Config())

    assert "DRAUPNIR_IDEA" in str(exc_info.value)
    assert "ide.java" in str(exc_info.value)


def test_open_ide_explains_a_launcher_that_cannot_start(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DRAUPNIR_IDEA", str(env.tmp / "no-such-idea"))
    with pytest.raises(OperationError) as exc_info:
        open_ide(env.main, Config())
    assert "no-such-idea" in str(exc_info.value)

    monkeypatch.setenv("DRAUPNIR_IDEA", "'unbalanced")
    with pytest.raises(OperationError) as exc_info:
        open_ide(env.main, Config())
    assert "DRAUPNIR_IDEA" in str(exc_info.value)


def test_open_ide_starts_the_command_detached(env: Env) -> None:
    command = open_ide(env.main, env.ws().config)
    assert str(env.main) in command

    content = _wait_for_content(env.idea_log)
    assert content.strip() == str(env.main)


def test_new_command_with_open_starts_the_ide(env: Env) -> None:
    exit_code = main(["-C", str(env.root), "new", "feature/opens", "--open"])
    assert exit_code == 0

    content = _wait_for_content(env.idea_log)
    assert content.strip() == str(env.root / "feature-opens")
