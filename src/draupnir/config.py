"""Reads draupnir.toml and DRAUPNIR_* environment variables into a frozen Config."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, cast

from draupnir.errors import OperationError

CONFIG_FILE_NAME = "draupnir.toml"

_KNOWN_SECTIONS = ("workspace", "maven", "build", "actions", "ui", "ide")


@dataclass(frozen=True)
class WorkspaceConfig:
    repository: str | None = None
    main_branch: str | None = None
    base_branch: str | None = None
    local_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class MavenConfig:
    # None means "not set": maven.py then follows whether main/ has a pom.xml.
    enabled: bool | None = None
    shared_repository: Path = Path("~/.m2/repository").expanduser()


@dataclass(frozen=True)
class BuildConfig:
    poll_seconds: float = 15
    grace_seconds: float = 180
    timeout_seconds: float = 3600


@dataclass(frozen=True)
class ActionsConfig:
    fast_forward_main: bool = True
    fast_forward_needs_green_build: bool = True
    offer_remove_after_merge: bool = True


@dataclass(frozen=True)
class UiConfig:
    refresh_seconds: float = 10


@dataclass(frozen=True)
class IdeConfig:
    java: str | None = None
    python: str | None = None


@dataclass(frozen=True)
class Config:
    workspace: WorkspaceConfig = WorkspaceConfig()
    maven: MavenConfig = MavenConfig()
    build: BuildConfig = BuildConfig()
    actions: ActionsConfig = ActionsConfig()
    ui: UiConfig = UiConfig()
    ide: IdeConfig = IdeConfig()


def _join_and(items: Sequence[str]) -> str:
    """Joins items with commas and a final "and", the way a sentence would."""
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f" and {items[-1]}"


def _type_error(section: str, key: str, expected: str) -> OperationError:
    return OperationError(
        f"{CONFIG_FILE_NAME} has '{section}.{key}' set to a value that is not {expected}."
    )


def _check_keys(section: str, data: dict[str, Any], known: Sequence[str]) -> None:
    for key in data:
        if key not in known:
            raise OperationError(
                f"{CONFIG_FILE_NAME} has an unknown key '{section}.{key}'. Check the spelling. "
                f"The known keys in [{section}] are {_join_and(known)}."
            )


def _optional_str(section: str, key: str, data: dict[str, Any]) -> str | None:
    if key not in data:
        return None
    value = data[key]
    if not isinstance(value, str):
        raise _type_error(section, key, "a string")
    return value


def _optional_bool(section: str, key: str, data: dict[str, Any]) -> bool | None:
    if key not in data:
        return None
    value = data[key]
    if not isinstance(value, bool):
        raise _type_error(section, key, "a boolean")
    return value


def _bool(section: str, key: str, data: dict[str, Any], default: bool) -> bool:
    value = _optional_bool(section, key, data)
    return default if value is None else value


def _number(section: str, key: str, data: dict[str, Any], default: float) -> float:
    if key not in data:
        return default
    value = data[key]
    # A bool is an int in Python, but it is never a number here.
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _type_error(section, key, "a number")
    return float(value)


def _positive_number(section: str, key: str, data: dict[str, Any], default: float) -> float:
    value = _number(section, key, data, default)
    if value <= 0:
        raise OperationError(
            f"{CONFIG_FILE_NAME} has '{section}.{key}' set to {value}, but it must be a "
            "positive number."
        )
    return value


def _string_list(section: str, key: str, data: dict[str, Any]) -> list[str]:
    if key not in data:
        return []
    value = data[key]
    if not isinstance(value, list):
        raise _type_error(section, key, "a list of strings")
    result: list[str] = []
    for item in cast("list[object]", value):
        if not isinstance(item, str):
            raise _type_error(section, key, "a list of strings")
        result.append(item)
    return result


def _check_local_file(entry: str) -> None:
    path = PurePosixPath(entry)
    if path.is_absolute() or ".." in path.parts:
        raise OperationError(
            f"{CONFIG_FILE_NAME} lists '{entry}' in workspace.local-files, but the path must "
            "be relative to the project root and must not contain '..'."
        )


def _load_workspace(data: dict[str, Any]) -> WorkspaceConfig:
    _check_keys("workspace", data, ["repository", "main-branch", "base-branch", "local-files"])
    local_files = _string_list("workspace", "local-files", data)
    for entry in local_files:
        _check_local_file(entry)
    return WorkspaceConfig(
        repository=_optional_str("workspace", "repository", data),
        main_branch=_optional_str("workspace", "main-branch", data),
        base_branch=_optional_str("workspace", "base-branch", data),
        local_files=tuple(local_files),
    )


def _load_maven(data: dict[str, Any]) -> MavenConfig:
    _check_keys("maven", data, ["enabled", "shared-repository"])
    raw_shared_repository = _optional_str("maven", "shared-repository", data)
    shared_repository = Path(raw_shared_repository or "~/.m2/repository").expanduser()
    return MavenConfig(
        enabled=_optional_bool("maven", "enabled", data),
        shared_repository=shared_repository,
    )


def _load_build(data: dict[str, Any]) -> BuildConfig:
    _check_keys("build", data, ["poll-seconds", "grace-seconds", "timeout-seconds"])
    return BuildConfig(
        poll_seconds=_positive_number("build", "poll-seconds", data, 15),
        grace_seconds=_positive_number("build", "grace-seconds", data, 180),
        timeout_seconds=_positive_number("build", "timeout-seconds", data, 3600),
    )


def _load_actions(data: dict[str, Any]) -> ActionsConfig:
    _check_keys(
        "actions",
        data,
        ["fast-forward-main", "fast-forward-needs-green-build", "offer-remove-after-merge"],
    )
    return ActionsConfig(
        fast_forward_main=_bool("actions", "fast-forward-main", data, True),
        fast_forward_needs_green_build=_bool(
            "actions", "fast-forward-needs-green-build", data, True
        ),
        offer_remove_after_merge=_bool("actions", "offer-remove-after-merge", data, True),
    )


def _load_ui(data: dict[str, Any]) -> UiConfig:
    _check_keys("ui", data, ["refresh-seconds"])
    value = _number("ui", "refresh-seconds", data, 10)
    if value < 0:
        raise OperationError(
            f"{CONFIG_FILE_NAME} has 'ui.refresh-seconds' set to {value}, but it must be zero "
            "or a positive number. Zero turns the timer off."
        )
    return UiConfig(refresh_seconds=value)


def _load_ide(data: dict[str, Any]) -> IdeConfig:
    _check_keys("ide", data, ["java", "python"])
    return IdeConfig(
        java=_optional_str("ide", "java", data),
        python=_optional_str("ide", "python", data),
    )


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise OperationError(f"{path} has a syntax error: {exc}") from exc


def _check_sections(data: dict[str, Any]) -> None:
    for section, value in data.items():
        if section not in _KNOWN_SECTIONS:
            raise OperationError(
                f"{CONFIG_FILE_NAME} has an unknown section '[{section}]'. Check the spelling. "
                f"The known sections are {_join_and(_KNOWN_SECTIONS)}."
            )
        if not isinstance(value, dict):
            raise OperationError(
                f"{CONFIG_FILE_NAME} has '{section}' set to a value that is not a table. Use "
                f"[{section}] with keys underneath it."
            )


def _apply_environment(config: Config) -> Config:
    """Environment variables win over the file, because they are this machine's own value."""
    maven = config.maven
    shared_repo = os.environ.get("DRAUPNIR_MAVEN_SHARED_REPO")
    if shared_repo:
        maven = replace(maven, shared_repository=Path(shared_repo).expanduser())

    ide = config.ide
    idea = os.environ.get("DRAUPNIR_IDEA")
    pycharm = os.environ.get("DRAUPNIR_PYCHARM")
    if idea:
        ide = replace(ide, java=idea)
    if pycharm:
        ide = replace(ide, python=pycharm)

    return replace(config, maven=maven, ide=ide)


def load_config(root: Path) -> Config:
    """Reads root / draupnir.toml once. An empty or missing file is valid.

    The order is: defaults, then the file, then the environment variables.
    """
    data = _read_toml(root / CONFIG_FILE_NAME)
    _check_sections(data)
    config = Config(
        workspace=_load_workspace(data.get("workspace", {})),
        maven=_load_maven(data.get("maven", {})),
        build=_load_build(data.get("build", {})),
        actions=_load_actions(data.get("actions", {})),
        ui=_load_ui(data.get("ui", {})),
        ide=_load_ide(data.get("ide", {})),
    )
    return _apply_environment(config)
