"""Maven version check and .mvn/maven.config."""

from __future__ import annotations

import os
import re
from pathlib import Path

from draupnir.errors import OperationError
from draupnir.runner import LogFn, run

# The oldest Maven version that honours maven.repo.local.tail with a warning instead of
# silently ignoring it.
MIN_VERSION = (3, 9, 0)

_VERSION_LINE = re.compile(r"Apache Maven (\d+)\.(\d+)\.(\d+)")

_WHY_THE_VERSION_MATTERS = (
    "Maven 3.9.0 or newer is needed, because older versions ignore maven.repo.local.tail "
    "without a warning, so installs would go into the shared repository instead of "
    "staying private to this clone."
)


def _command(folder: Path) -> list[str]:
    if os.access(folder / "mvnw", os.X_OK):
        return ["./mvnw"]
    return ["mvn"]


def version(folder: Path) -> tuple[int, int, int]:
    """The Maven version in folder, read from `./mvnw -B -v` or `mvn -B -v`.

    mvnw is preferred over mvn when it is executable. The version comes from the
    "Apache Maven x.y.z" line, the one allowed exception to rule 3, because it is a stable
    version string and not a message meant for people.

    Raises OperationError when Maven cannot be run, and when the version is older than
    3.9.0.
    """
    command = _command(folder)
    result = None
    try:
        result = run([*command, "-B", "-v"], folder, check=False)
    except OperationError:
        result = None

    match = None
    if result is not None and result.code == 0:
        match = _VERSION_LINE.search(result.stdout + result.stderr)
    if match is None:
        raise OperationError(
            f"draupnir could not run '{' '.join(command)} -v' in {folder} and read its "
            f"Maven version. Install Maven, or check that mvnw is executable there. "
            f"{_WHY_THE_VERSION_MATTERS}"
        )

    found = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    if found < MIN_VERSION:
        found_str = ".".join(str(part) for part in found)
        needed_str = ".".join(str(part) for part in MIN_VERSION)
        raise OperationError(
            f"Maven {found_str} in {folder} is older than {needed_str}. {_WHY_THE_VERSION_MATTERS}"
        )
    return found


def write_config(folder: Path, shared_repository: Path, log: LogFn) -> None:
    """Writes .mvn/maven.config in folder, and creates its private repository .m2repo.

    The file is only written when its content would change, so running this again, for
    example on the next `prepare`, changes nothing on disk.
    """
    head = folder / ".m2repo"
    head.mkdir(parents=True, exist_ok=True)
    config_path = folder / ".mvn" / "maven.config"
    content = f"-Dmaven.repo.local={head}\n-Dmaven.repo.local.tail={shared_repository}\n"
    if not config_path.is_file() or config_path.read_text() != content:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(content)
    log(f"Maven in {folder} writes to {head} and reads from {shared_repository}.")
