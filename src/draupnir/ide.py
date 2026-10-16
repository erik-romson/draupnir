"""Finds and starts IntelliJ IDEA, Android Studio or PyCharm in a clone (R13), and the editor
for the config."""

from __future__ import annotations

import os
import platform
import shlex
import shutil
import subprocess
from pathlib import Path

from draupnir.config import Config
from draupnir.errors import OperationError
from draupnir.setup import exclude_from_git

_MAC_APPS = {
    "idea": ("IntelliJ IDEA.app", "IntelliJ IDEA Ultimate.app", "IntelliJ IDEA CE.app"),
    "studio": ("Android Studio.app", "Android Studio Preview.app"),
    "pycharm": ("PyCharm.app", "PyCharm Professional Edition.app", "PyCharm CE.app"),
}

# The variables that hold the editor of the config file, in the order they are read.
_EDITOR_VARIABLES = ("DRAUPNIR_EDITOR", "VISUAL", "EDITOR")

_NAMES = {"idea": "IntelliJ IDEA", "studio": "Android Studio", "pycharm": "PyCharm"}
_VARIABLES = {"idea": "DRAUPNIR_IDEA", "studio": "DRAUPNIR_STUDIO", "pycharm": "DRAUPNIR_PYCHARM"}
_CONFIG_KEYS = {"idea": "ide.java", "studio": "ide.android", "pycharm": "ide.python"}

_GRADLE_BUILD_FILES = ("build.gradle", "build.gradle.kts")

# The Gradle files that name the plugins of the whole build. The ids of the Android Gradle
# plugin all start with com.android, for example com.android.application.
_GRADLE_PLUGIN_FILES = (
    *_GRADLE_BUILD_FILES,
    "settings.gradle",
    "settings.gradle.kts",
    "gradle/libs.versions.toml",
)


def _uses_android_plugin(folder: Path) -> bool:
    """True when a Gradle file in the project root names the Android Gradle plugin."""
    for name in _GRADLE_PLUGIN_FILES:
        try:
            if "com.android" in (folder / name).read_text(errors="replace"):
                return True
        except OSError:
            continue
    return False


def ide_kind(folder: Path) -> str:
    """Returns "studio" for an Android project, "idea" for another Java or Gradle project, and
    "pycharm" for a Python project.

    An Android project is a Gradle project that uses the Android Gradle plugin. Returns "idea"
    for a project that matches none of them.
    """
    if any((folder / name).exists() for name in _GRADLE_BUILD_FILES):
        return "studio" if _uses_android_plugin(folder) else "idea"
    if (folder / "pom.xml").exists():
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
    """The command from DRAUPNIR_IDEA, DRAUPNIR_STUDIO or DRAUPNIR_PYCHARM, or from the config,
    in that order."""
    from_environment = os.environ.get(_VARIABLES[kind])
    if from_environment:
        return from_environment
    from_config = {
        "idea": config.ide.java,
        "studio": config.ide.android,
        "pycharm": config.ide.python,
    }
    return from_config[kind]


def ide_command(folder: Path, config: Config) -> list[str]:
    """The command that starts the IDE for folder, without the folder argument itself.

    The order is: the DRAUPNIR_IDEA, DRAUPNIR_STUDIO or DRAUPNIR_PYCHARM environment variable,
    then ide.java, ide.android or ide.python from the config, then idea, studio or pycharm on
    PATH or in the JetBrains Toolbox scripts folder, then the app bundles in /Applications and
    ~/Applications on macOS. The
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


def editor_command() -> list[str] | None:
    """The command that opens a file in an editor, or None when no editor is set.

    It reads DRAUPNIR_EDITOR, then VISUAL, then EDITOR, the variables every terminal program
    uses. The value is split with shlex.split, so a command with options works.

    Raises OperationError when the value cannot be split, for example on an open quote.
    """
    for variable in _EDITOR_VARIABLES:
        value = os.environ.get(variable)
        if not value:
            continue
        try:
            return shlex.split(value)
        except ValueError as exc:
            raise OperationError(
                f"draupnir could not read the editor command {value!r} from {variable}: {exc}. "
                "Fix the quotes in that variable."
            ) from exc
    return None


def project_name(folder: Path) -> str:
    """The name of a clone in other programs, like "shop feature-login".

    It is the workspace folder, then the clone folder, so clones of different workspaces can be
    told apart.
    """
    return f"{folder.parent.name} {folder.name}"


def name_project(folder: Path) -> None:
    """Gives the IDE project in folder a name like "shop feature-login", instead of the folder name.

    The name is the workspace folder, then the clone folder. The IDE reads it from .idea/.name.
    It is only written when .idea/ exists and has no name yet, so a name the user gave is kept,
    and a folder the IDE never opened is still imported as a new project. The file is hidden
    from git in the clone. A failed write is ignored.
    """
    idea = folder / ".idea"
    name_file = idea / ".name"
    if not idea.is_dir() or name_file.exists():
        return
    try:
        name_file.write_text(project_name(folder))
        exclude_from_git(folder, "/.idea/.name")
    except OSError:
        # The name is only a convenience, so the IDE still starts without it.
        return


def open_ide(folder: Path, config: Config) -> str:
    """Starts the IDE in folder without waiting for it, and returns the command as text.

    The IDE's own output goes nowhere, its input is closed, and it runs in its own session,
    so starting it never blocks draupnir and the IDE can outlive it. Before it starts, the
    project gets a name with name_project.

    Raises OperationError when no launcher is found or the launcher cannot be started.
    """
    command = [*ide_command(folder, config), str(folder)]
    name_project(folder)
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
