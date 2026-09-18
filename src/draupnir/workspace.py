"""Finds the workspace, main/, the clones, and the clone for a folder."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from draupnir.config import CONFIG_FILE_NAME, Config, load_config
from draupnir.errors import OperationError
from draupnir.runner import git

MAIN_FOLDER_NAME = "main"


def normalize_url(url: str) -> str:
    """Makes different forms of the same git remote URL compare equal.

    `git@host:owner/repo.git`, `ssh://git@host/owner/repo` and `https://host/owner/repo.git`
    all normalize to `host/owner/repo`. A local path is resolved instead, so two clones of the
    same folder on disk also compare equal.
    """
    url = url.strip()
    if "://" not in url:
        host, sep, path = url.partition(":")
        if sep and "/" not in host:
            # scp-like syntax: [user@]host:path
            host = host.rpartition("@")[2] or host
            return f"{host}/{path.strip('/').removesuffix('.git')}"
        return str(Path(url).expanduser().resolve())

    parsed = urlsplit(url)
    host = parsed.hostname or ""
    path = parsed.path.strip("/").removesuffix(".git")
    return f"{host}/{path}"


@dataclass(frozen=True)
class Workspace:
    root: Path
    config: Config
    main: Path

    @classmethod
    def find(cls, start: Path) -> Workspace:
        """Searches start and the folders above it for a workspace.

        Raises OperationError when no draupnir.toml is found. When one is found but main/ is
        missing, the message says to run draupnir init instead of searching further up.
        """
        start = start.resolve()
        for folder in (start, *start.parents):
            if (folder / CONFIG_FILE_NAME).is_file():
                main = folder / MAIN_FOLDER_NAME
                if not (main / ".git").is_dir():
                    raise OperationError(
                        f"{folder} has {CONFIG_FILE_NAME}, but no {MAIN_FOLDER_NAME}/ clone. "
                        f"Run draupnir init in {folder} to clone the project."
                    )
                return cls(root=folder, config=load_config(folder), main=main)
        raise OperationError(
            f"No workspace found in {start} or in the folders above it. A workspace has "
            f"{CONFIG_FILE_NAME} and {MAIN_FOLDER_NAME}/."
        )

    def clone_for(self, path: Path) -> Path:
        """Returns the folder directly under the root that holds path.

        Raises OperationError when path is the root itself or outside the workspace.
        """
        path = path.resolve()
        try:
            relative = path.relative_to(self.root)
        except ValueError as exc:
            raise OperationError(f"{path} is not inside the workspace {self.root}.") from exc
        if not relative.parts:
            raise OperationError(f"{path} is the workspace root. Give the folder of a clone.")
        return self.root / relative.parts[0]

    def resolve_folder(self, arg: str | None, cwd: Path) -> Path:
        """Turns a folder argument from the command line into an absolute path.

        Without an argument, this is the clone that holds cwd. Otherwise, arg is either the
        name of a folder directly under the workspace root, or a path.
        """
        if arg is None:
            return self.clone_for(cwd)
        # Only a plain name, such as main or main/, is looked up under the root. "." and ".."
        # are paths from cwd.
        name = arg.rstrip("/")
        if "/" not in name and name not in ("", ".", ".."):
            candidate = self.root / name
            if candidate.is_dir():
                return candidate
        return (cwd / arg).resolve()

    def origin_url(self, repo: Path | None = None) -> str:
        return git(repo or self.main, "remote", "get-url", "origin").stdout.strip()

    def main_branch(self) -> str:
        """The branch this workspace works against: workspace.main-branch, or GitHub's default.

        Clones rebase on it, fast-forward main pushes to it, pull requests go into it, and main/
        stays on it.
        """
        if self.config.workspace.main_branch:
            return self.config.workspace.main_branch
        return self.github_default_branch()

    def github_default_branch(self) -> str:
        """The default branch on GitHub, as main/ knows it from origin/HEAD."""
        result = git(self.main, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False)
        name = result.stdout.strip()
        if result.code == 0 and name.startswith("origin/"):
            return name.removeprefix("origin/")
        raise OperationError(
            f"{MAIN_FOLDER_NAME}/ does not know the default branch of GitHub. Run "
            f"git -C {MAIN_FOLDER_NAME} remote set-head origin --auto."
        )

    def remote_branches(self) -> list[str]:
        """The branches on GitHub, as main/ knows them from its last fetch.

        Reads only local refs, so it needs no network and takes no locks.
        """
        result = git(
            self.main, "for-each-ref", "--format=%(refname:lstrip=3)", "refs/remotes/origin"
        )
        return sorted(name for name in result.stdout.splitlines() if name and name != "HEAD")

    def project_paths(self) -> list[Path]:
        """main/ first, then every folder next to it that is a clone of the same remote."""
        url = normalize_url(self.origin_url())
        found: list[Path] = []
        for folder in sorted(self.root.iterdir()):
            if folder == self.main or not folder.is_dir() or not (folder / ".git").is_dir():
                continue
            result = git(folder, "remote", "get-url", "origin", check=False)
            if result.code == 0 and normalize_url(result.stdout.strip()) == url:
                found.append(folder)
        return [self.main, *found]
