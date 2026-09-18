from __future__ import annotations

from pathlib import Path

import pytest

from draupnir.config import load_config
from draupnir.errors import OperationError


def _write(root: Path, text: str) -> None:
    (root / "draupnir.toml").write_text(text)


def test_empty_config_gives_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path)

    assert config.workspace.repository is None
    assert config.workspace.base_branch is None
    assert config.workspace.local_files == ()
    assert config.maven.enabled is None
    assert config.maven.shared_repository == Path("~/.m2/repository").expanduser()
    assert config.build.poll_seconds == 15
    assert config.build.grace_seconds == 180
    assert config.build.timeout_seconds == 3600
    assert config.actions.fast_forward_main is True
    assert config.actions.fast_forward_needs_green_build is True
    assert config.actions.offer_remove_after_merge is True
    assert config.ui.refresh_seconds == 10
    assert config.ide.java is None
    assert config.ide.python is None


def test_config_reads_every_section(tmp_path: Path) -> None:
    _write(
        tmp_path,
        """
[workspace]
repository = "git@github.example.com:acme/shop.git"
base-branch = "develop"
local-files = ["src/main/resources/local.properties"]

[maven]
enabled = false
shared-repository = "~/shared-repo"

[build]
poll-seconds = 5
grace-seconds = 30
timeout-seconds = 900

[actions]
fast-forward-main = false
fast-forward-needs-green-build = false
offer-remove-after-merge = false

[ui]
refresh-seconds = 2

[ide]
java = "idea-custom"
python = "pycharm-custom"
""",
    )

    config = load_config(tmp_path)

    assert config.workspace.repository == "git@github.example.com:acme/shop.git"
    assert config.workspace.base_branch == "develop"
    assert config.workspace.local_files == ("src/main/resources/local.properties",)
    assert config.maven.enabled is False
    assert config.maven.shared_repository == Path("~/shared-repo").expanduser()
    assert config.build.poll_seconds == 5
    assert config.build.grace_seconds == 30
    assert config.build.timeout_seconds == 900
    assert config.actions.fast_forward_main is False
    assert config.actions.fast_forward_needs_green_build is False
    assert config.actions.offer_remove_after_merge is False
    assert config.ui.refresh_seconds == 2
    assert config.ide.java == "idea-custom"
    assert config.ide.python == "pycharm-custom"


def test_config_rejects_an_unknown_key_with_its_name(tmp_path: Path) -> None:
    _write(tmp_path, "[build]\npoll-second = 1\n")

    with pytest.raises(OperationError) as exc_info:
        load_config(tmp_path)

    message = str(exc_info.value)
    assert "build.poll-second" in message
    assert "poll-seconds, grace-seconds and timeout-seconds" in message


def test_config_rejects_a_wrong_type_with_the_key(tmp_path: Path) -> None:
    _write(tmp_path, '[build]\npoll-seconds = "soon"\n')

    with pytest.raises(OperationError) as exc_info:
        load_config(tmp_path)

    assert "build.poll-seconds" in str(exc_info.value)


def test_config_rejects_a_local_file_outside_the_project(tmp_path: Path) -> None:
    _write(tmp_path, '[workspace]\nlocal-files = ["../secret.txt"]\n')

    with pytest.raises(OperationError) as exc_info:
        load_config(tmp_path)

    assert "../secret.txt" in str(exc_info.value)


def test_config_syntax_error_names_the_file_and_line(tmp_path: Path) -> None:
    _write(tmp_path, "[build\npoll-seconds = 1\n")

    with pytest.raises(OperationError) as exc_info:
        load_config(tmp_path)

    message = str(exc_info.value)
    assert "draupnir.toml" in message
    assert "line" in message


def test_config_accepts_zero_refresh_seconds(tmp_path: Path) -> None:
    _write(tmp_path, "[ui]\nrefresh-seconds = 0\n")

    config = load_config(tmp_path)

    assert config.ui.refresh_seconds == 0


def test_config_rejects_negative_refresh_seconds_and_non_positive_build_times(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "[ui]\nrefresh-seconds = -1\n")
    with pytest.raises(OperationError):
        load_config(tmp_path)

    _write(tmp_path, "[build]\npoll-seconds = 0\n")
    with pytest.raises(OperationError):
        load_config(tmp_path)

    _write(tmp_path, "[build]\ngrace-seconds = -5\n")
    with pytest.raises(OperationError):
        load_config(tmp_path)


def test_environment_overrides_ide_and_shared_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(
        tmp_path,
        """
[maven]
shared-repository = "~/from-file"

[ide]
java = "idea-from-file"
python = "pycharm-from-file"
""",
    )
    monkeypatch.setenv("DRAUPNIR_MAVEN_SHARED_REPO", "~/from-env")
    monkeypatch.setenv("DRAUPNIR_IDEA", "idea-from-env")
    monkeypatch.setenv("DRAUPNIR_PYCHARM", "pycharm-from-env")

    config = load_config(tmp_path)

    assert config.maven.shared_repository == Path("~/from-env").expanduser()
    assert config.ide.java == "idea-from-env"
    assert config.ide.python == "pycharm-from-env"
