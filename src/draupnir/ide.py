"""Finds and starts IntelliJ IDEA or PyCharm in a clone (R13)."""

from __future__ import annotations

import os
import platform
import shlex
import shutil
import subprocess
from pathlib import Path

from draupnir.config import Config
from draupnir.errors import OperationError

_MAC_APPS = {
    "idea": ("IntelliJ IDEA.app", "IntelliJ IDEA Ultimate.app", "IntelliJ IDEA CE.app"),
    "pycharm": ("PyCharm.app", "PyCharm Professional Edition.app", "PyCharm CE.app"),
}

_NAMES = {"idea": "IntelliJ IDEA", "pycharm": "PyCharm"}
_VARIABLES = {"idea": "DRAUPNIR_IDEA", "pycharm": "DRAUPNIR_PYCHARM"}
_CONFIG_KEYS = {"idea": "ide.java", "pycharm": "ide.python"}


def ide_kind(folder: Path) -> str:
    """Returns "idea" for a Java or Gradle project, "pycharm" for a Python project.

    Returns "idea" for a project that matches neither.
    """
    if any((folder / name).exists() for name in ("pom.xml", "build.gradle", "build.gradle.kts")):
        return "idea"
    if any((folder / name).exists() for name in ("pyproject.toml", "setup.py", "requirements.txt")):
        return "pycharm"
    return "idea"


def _toolbox_scripts() -> list[Path]:
    """The folders JetBrains Toolbox installs its launcher scripts into."""
    home = Path.home()
    return [
        home / "Library/Application Support/JetBrains/Toolbox/scripts",
        home / ".local/share/JetBrains/Toolbox/scripts",
    ]


def _from_path_or_toolbox(kind: str) -> list[str] | None:
    search = os.pathsep.join(
        [os.environ.get("PATH", ""), *(str(folder) for folder in _toolbox_scripts())]
    )
    for name in (kind, f"{kind}.sh"):
        found = shutil.which(name, path=search)
        if found:
            return [found]
    return None


def _from_mac_apps(kind: str) -> list[str] | None:
    if platform.system() != "Darwin":
        return None
    for base in (Path("/Applications"), Path.home() / "Applications"):
        for app in _MAC_APPS[kind]:
            if (base / app).exists():
                return ["open", "-na", str(base / app), "--args"]
    return None


def _configured_command(kind: str, config: Config) -> str | None:
    """The command from DRAUPNIR_IDEA/DRAUPNIR_PYCHARM, or from the config, in that order."""
    from_environment = os.environ.get(_VARIABLES[kind])
    if from_environment:
        return from_environment
    return config.ide.java if kind == "idea" else config.ide.python


def ide_command(folder: Path, config: Config) -> list[str]:
    """The command that starts the IDE for folder, without the folder argument itself.

    The order is: the DRAUPNIR_IDEA or DRAUPNIR_PYCHARM environment variable, then ide.java or
    ide.python from the config, then idea or pycharm on PATH or in the JetBrains Toolbox
    scripts folder, then the app bundles in /Applications and ~/Applications on macOS. The
    environment variable comes before the config, because it is this machine's own value
    (D25). A configured command is split with shlex.split, so a path with spaces works when
    it is quoted.

    Raises OperationError when no launcher is found. The message names the environment
    variable and the config key to set.
    """
    kind = ide_kind(folder)
    configured = _configured_command(kind, config)
    if configured:
        try:
            return shlex.split(configured)
        except ValueError as exc:
            raise OperationError(
                f"draupnir could not read the {_NAMES[kind]} command {configured!r}: {exc}. "
                f"Fix the quotes in {_VARIABLES[kind]} or {_CONFIG_KEYS[kind]}."
            ) from exc

    found = _from_path_or_toolbox(kind) or _from_mac_apps(kind)
    if found is not None:
        return found

    raise OperationError(
        f"draupnir found no launcher for {_NAMES[kind]}. Set {_VARIABLES[kind]} to the "
        f"command that starts it, for example {kind}, or set {_CONFIG_KEYS[kind]} in "
        "draupnir.toml."
    )


def open_ide(folder: Path, config: Config) -> str:
    """Starts the IDE in folder without waiting for it, and returns the command as text.

    The IDE's own output goes nowhere, its input is closed, and it runs in its own session,
    so starting it never blocks draupnir and the IDE can outlive it.

    Raises OperationError when no launcher is found or the launcher cannot be started.
    """
    command = [*ide_command(folder, config), str(folder)]
    try:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        kind = ide_kind(folder)
        raise OperationError(
            f"draupnir could not start {_NAMES[kind]} with {command[0]}: {exc.strerror}. "
            f"Check {_VARIABLES[kind]} or {_CONFIG_KEYS[kind]} in draupnir.toml."
        ) from exc
    return shlex.join(command)
