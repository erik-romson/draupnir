"""Tests for src/draupnir/ide.py: finding and starting IntelliJ IDEA, Android Studio or PyCharm
(R13)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from conftest import Env
from helpers import cmux_calls, logger, sh, write_stub_cmux

from draupnir.cli import main
from draupnir.cmux import in_cmux, open_cmux_workspace
from draupnir.config import Config, IdeConfig
from draupnir.errors import OperationError
from draupnir.ide import (
    editor_command,
    ide_command,
    ide_kind,
    name_project,
    open_ide,
)

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


def test_ide_kind_picks_android_studio_for_a_gradle_project_with_the_android_plugin(
    tmp_path: Path,
) -> None:
    in_build_file = tmp_path / "in-build-file"
    in_build_file.mkdir()
    (in_build_file / "build.gradle.kts").write_text('plugins { id("com.android.application") }\n')
    assert ide_kind(in_build_file) == "studio"

    in_catalog = tmp_path / "in-catalog"
    (in_catalog / "gradle").mkdir(parents=True)
    (in_catalog / "build.gradle.kts").write_text("plugins { alias(libs.plugins.app) }\n")
    (in_catalog / "gradle/libs.versions.toml").write_text(
        '[plugins]\napp = { id = "com.android.application", version = "8.0.0" }\n'
    )
    assert ide_kind(in_catalog) == "studio"

    # A Maven project stays with IntelliJ IDEA, even when a file names the plugin.
    maven = tmp_path / "maven"
    maven.mkdir()
    (maven / "pom.xml").write_text("<project><!-- com.android --></project>\n")
    assert ide_kind(maven) == "idea"


def test_ide_command_for_an_android_project_reads_its_own_variable_and_key(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    android = env.tmp / "android"
    android.mkdir()
    (android / "build.gradle.kts").write_text('plugins { id("com.android.application") }\n')

    config = Config(ide=IdeConfig(java="config-idea", android="config-studio"))
    assert ide_command(android, config) == ["config-studio"]

    monkeypatch.setenv("DRAUPNIR_STUDIO", "env-studio")
    assert ide_command(android, config) == ["env-studio"]

    monkeypatch.delenv("DRAUPNIR_STUDIO")
    monkeypatch.setenv("PATH", "/does-not-exist")
    with pytest.raises(OperationError) as exc_info:
        ide_command(android, Config())
    assert "Android Studio" in str(exc_info.value)
    assert "DRAUPNIR_STUDIO" in str(exc_info.value)
    assert "ide.android" in str(exc_info.value)


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


def test_editor_command_reads_the_variables_in_order(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert editor_command() is None

    monkeypatch.setenv("EDITOR", "vi")
    assert editor_command() == ["vi"]

    monkeypatch.setenv("VISUAL", "code --wait")
    assert editor_command() == ["code", "--wait"]

    monkeypatch.setenv("DRAUPNIR_EDITOR", '"/opt/my editor" -f')
    assert editor_command() == ["/opt/my editor", "-f"]


def test_editor_command_explains_an_unreadable_value(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DRAUPNIR_EDITOR", 'vi "unclosed')

    with pytest.raises(OperationError) as excinfo:
        editor_command()

    assert "DRAUPNIR_EDITOR" in str(excinfo.value)


def test_open_ide_names_the_project_after_the_workspace_and_the_folder(env: Env) -> None:
    (env.main / ".idea").mkdir()

    open_ide(env.main, env.ws().config)

    assert (env.main / ".idea" / ".name").read_text() == f"{env.root.name} main"
    assert sh(env.main, "git", "status", "--porcelain", "--untracked-files=all") == ""


def test_name_project_keeps_a_given_name_and_never_creates_idea(env: Env) -> None:
    name_project(env.main)
    assert not (env.main / ".idea").exists()

    (env.main / ".idea").mkdir()
    (env.main / ".idea" / ".name").write_text("my name")
    name_project(env.main)
    assert (env.main / ".idea" / ".name").read_text() == "my name"


def test_new_command_with_open_starts_the_ide(env: Env) -> None:
    exit_code = main(["-C", str(env.root), "new", "feature/opens", "--open"])
    assert exit_code == 0

    content = _wait_for_content(env.idea_log)
    assert content.strip() == str(env.root / "feature-opens")


def test_open_cmux_workspace_opens_the_clone_with_its_todo_list_below(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = write_stub_cmux(env.bin)
    assert not in_cmux()
    monkeypatch.setenv("CMUX_WORKSPACE_ID", "workspace-1")
    assert in_cmux()

    open_cmux_workspace(env.main, logger()[1])

    json_flags = ["--json", "--id-format", "both"]
    assert cmux_calls(calls) == [
        ["workspace-group", "list", *json_flags],
        [
            "workspace",
            "create",
            "--name",
            f"{env.root.name} main",
            "--cwd",
            str(env.main),
            "--focus",
            "true",
            *json_flags,
        ],
        ["todo", "open", "--workspace", "new-workspace", *json_flags],
        [
            "split-off",
            "--surface",
            "new-todo",
            "down",
            "--workspace",
            "new-workspace",
            "--focus",
            "false",
        ],
        ["focus-panel", "--panel", "new-terminal", "--workspace", "new-workspace"],
    ]


def test_open_cmux_workspace_joins_the_group_of_the_dashboard(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    groups = {
        "groups": [
            {"id": "group-other", "member_workspace_ids": ["workspace-2"]},
            {"id": "group-shop", "member_workspace_ids": ["workspace-0", "workspace-1"]},
        ]
    }
    calls = write_stub_cmux(env.bin, json.dumps(groups))
    monkeypatch.setenv("CMUX_WORKSPACE_ID", "workspace-1")

    open_cmux_workspace(env.main, logger()[1])

    create = cmux_calls(calls)[1]
    assert create[create.index("--group") :][:6] == [
        "--group",
        "group-shop",
        "--group-placement",
        "afterCurrent",
        "--group-reference",
        "workspace-1",
    ]


def test_open_cmux_workspace_keeps_the_workspace_when_the_todo_list_fails(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_stub_cmux(env.bin)
    stub = env.bin / "cmux"
    stub.write_text(stub.read_text().replace("  todo)", "  todo) exit 1 ;;\n  never)"))
    monkeypatch.setenv("CMUX_WORKSPACE_ID", "workspace-1")
    lines, log = logger()

    open_cmux_workspace(env.main, log)

    assert any("could not add the todo list" in line for line in lines)
