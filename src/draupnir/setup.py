"""Setting up clones: init, clone main, new clone, prepare, unsaved work and remove."""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable, Collection, Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from draupnir import maven
from draupnir.config import CONFIG_FILE_NAME, load_config
from draupnir.errors import OperationError
from draupnir.runner import FETCH_TIMEOUT_SECONDS, CommandError, LogFn, git, git_network
from draupnir.status import git_dir, operation_in_progress
from draupnir.workspace import MAIN_FOLDER_NAME, Workspace, normalize_url


def _is_tracked(repo: Path, path: str) -> bool:
    """True when git in repo tracks path, a file or a folder, without taking a lock."""
    result = git(repo, "ls-files", "--", path, check=False)
    return bool(result.stdout.strip())


def _require_clone_top(ws: Workspace, folder: Path) -> Path:
    """Checks that folder is the top of a clone, and not the workspace root itself."""
    folder = folder.resolve()
    if folder == ws.root:
        raise OperationError(
            f"{folder} is the workspace root, not a clone. Give main/ or the folder of a "
            "clone next to it."
        )
    if not folder.is_dir():
        raise OperationError(
            f"{folder} is not a folder. Give main/ or the folder of a clone next to it."
        )
    result = git(folder, "rev-parse", "--show-toplevel", check=False)
    top = None
    if result.code == 0 and result.stdout.strip():
        top = Path(result.stdout.strip()).resolve()
    if top != folder:
        raise OperationError(
            f"{folder} is not the top of a git clone. Give main/ or the folder of a clone "
            "next to it."
        )
    return folder


def _exclude_path(folder: Path) -> Path:
    """The path of folder's own .git/info/exclude file, which works like .gitignore."""
    return git_dir(folder) / "info" / "exclude"


@dataclass(frozen=True)
class _LocalFile:
    path: str
    in_git_folder: bool
    exists_in_folder: bool


def _matching_files(folder: Path, patterns: list[str]) -> list[str]:
    """The files in folder that patterns match, the way git reads the lines of a .gitignore.

    git itself does the matching, so every rule works, including '**' and a '!' line that
    takes a file back. The patterns are read in the order the config lists them. A file that
    git tracks never matches, because every clone already has it.
    """
    if not patterns:
        return []
    options = [f"--exclude={pattern}" for pattern in patterns]
    found = git(folder, "ls-files", "-z", "--others", "--ignored", *options)
    return [path for path in found.stdout.split("\0") if path]


def _plan_local_files(ws: Workspace, folder: Path) -> tuple[list[_LocalFile], list[str]]:
    """Finds the files that the configured patterns match in main/, and where they exist.

    Returns the files to copy, and the patterns that no file in main/ matches. A pattern that
    starts with .git/ is a path, because git does not list the files in the git folder. This
    only reads. Nothing is copied here.
    """
    git_folder_paths: list[str] = []
    patterns: list[str] = []
    unmatched: list[str] = []
    for entry in ws.config.workspace.local_files:
        if not entry.startswith(".git/"):
            patterns.append(entry)
        elif (ws.main / entry).exists():
            git_folder_paths.append(entry)
        else:
            unmatched.append(entry)

    paths = git_folder_paths + _matching_files(ws.main, patterns)
    unmatched += [
        pattern
        for pattern in patterns
        if not pattern.startswith("!") and not _matching_files(ws.main, [pattern])
    ]
    plans = [
        _LocalFile(
            path=path,
            in_git_folder=path.startswith(".git/"),
            exists_in_folder=(folder / path).exists(),
        )
        for path in dict.fromkeys(paths)
    ]
    return plans, unmatched


def _maven_enabled(ws: Workspace) -> bool:
    """Maven is on when the config says so, or, when it does not say, when main/ has a pom.xml.

    Every clone follows main/, so a branch that adds or removes pom.xml does not change the
    Maven setup.
    """
    if ws.config.maven.enabled is not None:
        return ws.config.maven.enabled
    return (ws.main / "pom.xml").is_file()


def _check_maven_config_not_tracked(folder: Path) -> None:
    if _is_tracked(folder, ".mvn/maven.config"):
        raise OperationError(
            f"{folder} tracks .mvn/maven.config in git, so draupnir will not change it. "
            "Move these Maven repository settings to a file git does not track, for "
            "example by keeping maven.repo.local and maven.repo.local.tail out of "
            ".mvn/maven.config, or stop tracking the file."
        )


_EXCLUDE_FILE = ".git/info/exclude"


def _lines_of_exclude_in_main(main: Path, is_main: bool, log: LogFn) -> list[str]:
    """The ignore lines of main/.git/info/exclude, to add to the exclude file of the clone.

    This file is never copied, because git clone writes one in every new clone, and a copy
    would throw away the lines that draupnir and the user put there. Its lines are added
    instead, and a line that is already there is left alone.
    """
    if is_main:
        return []
    text = (main / _EXCLUDE_FILE).read_text()
    log(f"Added the ignore lines of main/{_EXCLUDE_FILE}.")
    return [line for line in text.splitlines() if line.strip()]


def _apply_local_file(
    main: Path, folder: Path, is_main: bool, plan: _LocalFile, log: LogFn
) -> list[str]:
    """Copies plan into folder when needed, and returns the exclude lines it needs.

    A path under .git/ is never excluded, because .gitignore-style files do not apply there.
    The exclude file itself is the one path whose lines are added instead of being copied.
    main/ itself is only ever excluded, never copied into, because it is the source the other
    clones copy from.
    """
    if plan.path == _EXCLUDE_FILE:
        return _lines_of_exclude_in_main(main, is_main, log)

    lines = [] if plan.in_git_folder else [f"/{plan.path}"]
    if is_main:
        return lines

    target = folder / plan.path
    if plan.exists_in_folder:
        log(f"Kept {plan.path}: it already exists in this folder.")
        return lines
    source = main / plan.path
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, target)
    else:
        shutil.copy2(source, target)
    log(f"Copied {plan.path} from main/.")
    return lines


def _log_unmatched(main: Path, patterns: list[str], log: LogFn) -> None:
    """Says, for every pattern that matched no file in main/, why it matched nothing."""
    for pattern in patterns:
        if _is_tracked(main, pattern):
            log(f"Skipped {pattern}: git tracks it in main/, so every clone already has it.")
        else:
            log(f"Skipped {pattern}: no file in main/ matches it.")


def exclude_from_git(folder: Path, *lines: str) -> None:
    """Hides the paths in lines from git in folder, through its own .git/info/exclude."""
    _add_exclude_lines(_exclude_path(folder), list(lines))


def _add_exclude_lines(exclude_path: Path, lines: list[str]) -> None:
    """Adds each of lines to exclude_path, unless it is already there."""
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    existing: set[str] = (
        set(exclude_path.read_text().splitlines()) if exclude_path.is_file() else set()
    )
    to_add = [line for line in dict.fromkeys(lines) if line not in existing]
    if not to_add:
        return
    with exclude_path.open("a") as handle:
        for line in to_add:
            handle.write(f"{line}\n")


def prepare(ws: Workspace, folder: Path, log: LogFn) -> None:
    """Copies the local files from main/, hides them from git, and sets up Maven.

    This works in two phases. Phase 1 only reads: it plans the local files and checks
    Maven. Phase 2 makes the changes, and it only starts when every check in phase 1
    passed. So when prepare stops in phase 1, folder, its exclude file and main/ are
    exactly as they were before (R10). Running prepare again is always safe, because every
    change it makes is safe to repeat.
    """
    folder = _require_clone_top(ws, folder)
    exclude_path = _exclude_path(folder)
    plans, unmatched = _plan_local_files(ws, folder)
    maven_on = _maven_enabled(ws)
    if maven_on:
        maven.version(folder)
        _check_maven_config_not_tracked(folder)

    is_main = folder == ws.main
    exclude_lines: list[str] = []
    _log_unmatched(ws.main, unmatched, log)
    for plan in plans:
        exclude_lines.extend(_apply_local_file(ws.main, folder, is_main, plan, log))

    if maven_on:
        maven.write_config(folder, ws.config.maven.shared_repository, log)
        exclude_lines.extend(["/.mvn/maven.config", "/.m2repo/"])

    _add_exclude_lines(exclude_path, exclude_lines)


_GITIGNORE = (
    "# This folder is a draupnir workspace. Git keeps only the files below.\n"
    "# main/ and the clones next to it are ignored.\n"
    "/*\n"
    "!/.gitignore\n"
    "!/README.md\n"
    "!/draupnir.toml\n"
)


def _write_gitignore_if_missing(root: Path) -> None:
    path = root / ".gitignore"
    if not path.is_file():
        path.write_text(_GITIGNORE)


def _toml_template(url: str | None) -> str:
    """The draupnir.toml written by init: every key present, all but repository commented out."""
    if url:
        repository_line = f'repository = "{url}"'
    else:
        repository_line = '# repository = "git@github.example.com:acme/shop.git"'
    return f"""[workspace]
# GitHub URL of the project. draupnir init writes it. It is only used by init, to clone
# main/ on a new machine. At all other times the URL comes from main/.
{repository_line}

# The branch that clones rebase on, pull requests go into, and main/ stays on. draupnir init
# writes it. Default: the default branch on GitHub.
# main-branch = "main"

# Branch that new clones start from. Default: main-branch.
# base-branch = "main"

# Files that are not in git, but that every clone needs. Each line is a pattern, written
# the way a line in .gitignore is written. The files that match in main/ are copied once,
# never overwritten, and hidden from git in each clone. Never list secrets here.
# local-files = [
#   "**/local.properties",
#   ".claude/settings.local.json",
#   ".idea/runConfigurations",
#   ".git/hooks/commit-msg",
# ]

# Files that git ignores and that remove copies out of the clone before it deletes it, into
# removed/ next to main/. Patterns are written the way a line in .gitignore is written. Use
# it for notes and plans that live in the clone and nowhere else.
# keep-on-remove = [
#   "notes/**",
# ]

[maven]
# Default: true when main/ has a pom.xml. Every clone follows main/, also a clone
# whose branch adds or removes pom.xml.
# enabled = true
# The read-only tail. Default: ~/.m2/repository. Machine override: DRAUPNIR_MAVEN_SHARED_REPO.
# shared-repository = "~/.m2/repository"

[build]
# poll-seconds = 15
# grace-seconds = 180
# timeout-seconds = 3600

[actions]
# Set to false when the default branch is protected on GitHub. The button is then hidden.
# fast-forward-main = true
# Fast-forward main refuses when the build of the pushed commit is not green.
# fast-forward-needs-green-build = true
# After a merged pull request, ask whether to remove the clone.
# offer-remove-after-merge = true

[ui]
# Read the status of every clone again after this many seconds. 0 turns it off.
# The timer never fetches, and it skips a read while an action or another read runs.
# refresh-seconds = 10

[ide]
# Commands that start the IDE. A Gradle project that uses the Android Gradle plugin opens
# in Android Studio. Default: idea, studio and pycharm from PATH, JetBrains Toolbox or
# /Applications. Machine override: DRAUPNIR_IDEA, DRAUPNIR_STUDIO and DRAUPNIR_PYCHARM.
# java = "idea"
# android = "studio"
# python = "pycharm"

# The header below stays commented out on purpose. A draupnir version without this section
# refuses a draupnir.toml that has a real [cmux] header. Remove the "# " of the header only
# when everyone who shares this file has a draupnir version that knows the section.
# [cmux]
# Give every new clone a terminal session through cmux local-tmux.
# session = true
# Inside cmux, open a cmux workspace for every new clone, with its terminal session.
# clone-workspace = true
"""


def _write_toml_if_missing(root: Path, url: str | None) -> None:
    path = root / CONFIG_FILE_NAME
    if not path.is_file():
        path.write_text(_toml_template(url))


def _set_workspace_key(root: Path, key: str, value: str) -> None:
    """Writes key = value into the [workspace] section of draupnir.toml, keeping the rest.

    It replaces the line that sets key, or the commented placeholder for it that init wrote.
    Otherwise it adds the key under the [workspace] header, and adds that header when there is
    none yet. Every other line stays as it is.
    """
    path = root / CONFIG_FILE_NAME
    lines = path.read_text().splitlines() if path.is_file() else []
    new_line = f'{key} = "{value}"'

    workspace_at: int | None = None
    for i, line in enumerate(lines):
        if line.strip() == "[workspace]":
            workspace_at = i
            break

    if workspace_at is None:
        lines = ["[workspace]", new_line, "", *lines]
    else:
        key_at: int | None = None
        for i in range(workspace_at + 1, len(lines)):
            stripped = lines[i].strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                break
            setting = stripped.lstrip("#").strip()
            if re.match(rf"{re.escape(key)}\s*=", setting):
                key_at = i
                # An active line wins over a commented placeholder above it.
                if not stripped.startswith("#"):
                    break
        if key_at is None:
            lines.insert(workspace_at + 1, new_line)
        else:
            lines[key_at] = new_line

    path.write_text("\n".join(lines) + "\n")


def _refuse_inside_a_project_clone(root: Path) -> None:
    """Raises OperationError when root is inside, or itself, a clone of the project.

    A project clone tracks real project files. A workspace repository, freshly cloned for
    R2, only ever tracks .gitignore, README.md and draupnir.toml.
    """
    result = git(root, "rev-parse", "--show-toplevel", check=False)
    if result.code != 0 or not result.stdout.strip():
        return
    top = Path(result.stdout.strip()).resolve()
    if top != root:
        raise OperationError(
            f"{root} is inside the git clone at {top}. Run draupnir init in a folder of "
            "its own, not inside a clone of a project."
        )
    tracked = [line for line in git(root, "ls-files").stdout.splitlines() if line]
    allowed = {".gitignore", "README.md", CONFIG_FILE_NAME}
    extra = [path for path in tracked if path not in allowed]
    if extra:
        raise OperationError(
            f"{root} already tracks '{extra[0]}' in git. It looks like a clone of a "
            "project, not a draupnir workspace. Run draupnir init in a different folder."
        )


def clone_main(url: str, main: Path, log: LogFn) -> None:
    """Clones url into main. No timeout: the first clone of a project can take a while."""
    git_network(main.parent, "clone", url, str(main), log=log)


def init(root: Path, url: str | None, log: LogFn, main_branch: str | None = None) -> Workspace:
    """Sets up root as a workspace: the two files, and main/ cloned or adopted.

    Refuses to run inside or on top of a clone of the project itself. Writes .gitignore
    and draupnir.toml when they are missing. Clones main/ when it does not exist yet, or
    checks that an existing main/ matches the given URL. Never runs git init: the
    workspace repository itself is set up, and pushed, by hand.

    The main branch is main_branch, then workspace.main-branch, then GitHub's default branch.
    init checks that it exists on GitHub, writes it into draupnir.toml, and checks it out in a
    main/ that it cloned just now.
    """
    root = root.resolve()
    _refuse_inside_a_project_clone(root)

    _write_gitignore_if_missing(root)
    _write_toml_if_missing(root, url)

    main = root / MAIN_FOLDER_NAME
    config = load_config(root)
    effective_url = url or config.workspace.repository

    adopted = (main / ".git").is_dir()
    if adopted:
        if effective_url:
            main_url = git(main, "remote", "get-url", "origin").stdout.strip()
            if normalize_url(effective_url) != normalize_url(main_url):
                raise OperationError(
                    f"{root} already has {MAIN_FOLDER_NAME}/ cloned from {main_url}, but "
                    f"draupnir init was given {effective_url}. Give the URL that matches "
                    f"{MAIN_FOLDER_NAME}/, or leave the URL out to keep the one it has."
                )
    else:
        if not effective_url:
            raise OperationError(
                f"{root} has no {MAIN_FOLDER_NAME}/ yet, and no repository URL was given. "
                f"Run draupnir init <url> once, to clone the project into {MAIN_FOLDER_NAME}/."
            )
        clone_main(effective_url, main, log)

    if config.workspace.repository is None:
        main_url = git(main, "remote", "get-url", "origin").stdout.strip()
        _set_workspace_key(root, "repository", main_url)
        config = load_config(root)

    branch = _choose_main_branch(main, main_branch or config.workspace.main_branch, adopted, log)
    if config.workspace.main_branch != branch:
        _set_workspace_key(root, "main-branch", branch)
        config = load_config(root)
    _put_main_on_branch(main, branch, adopted, log)

    ws = Workspace(root=root, config=config, main=main)
    prepare(ws, main, log)
    return ws


def _choose_main_branch(main: Path, requested: str | None, adopted: bool, log: LogFn) -> str:
    """requested, or GitHub's default branch. Raises OperationError when GitHub lacks it.

    A fresh clone already knows GitHub's default branch. An adopted main/ asks GitHub again,
    because its origin/HEAD can be missing or old.
    """
    if adopted:
        git_network(
            main, "remote", "set-head", "origin", "--auto", timeout=FETCH_TIMEOUT_SECONDS, log=log
        )
    head = git(main, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").stdout.strip()
    github_default = head.removeprefix("origin/")
    branch = requested or github_default
    if branch != github_default and branch not in _remote_branch_names(main, log):
        raise OperationError(
            f"GitHub has no branch called '{branch}', so it cannot be the main branch. Give an "
            f"existing branch with --main-branch, or fix main-branch in {CONFIG_FILE_NAME}. "
            f"GitHub's default branch is {github_default}."
        )
    return branch


def _put_main_on_branch(main: Path, branch: str, adopted: bool, log: LogFn) -> None:
    """Checks out branch in a main/ cloned just now. An adopted main/ is never switched."""
    current = git(main, "symbolic-ref", "--short", "HEAD", check=False).stdout.strip()
    if current == branch:
        return
    if adopted:
        log(
            f"{MAIN_FOLDER_NAME}/ is on {current or 'a detached HEAD'}, but the main branch is "
            f"{branch}. Switch it with git -C {MAIN_FOLDER_NAME} switch {branch} when it has no "
            "work in progress."
        )
        return
    git(main, "checkout", "-q", "-B", branch, "--track", f"origin/{branch}", log=log)


def folder_for_branch(branch: str) -> str:
    """The clone folder name for branch: every '/' becomes '-'."""
    return branch.replace("/", "-")


def _validate_branch_name(root: Path, branch: str) -> None:
    result = git(root, "check-ref-format", "--branch", branch, check=False)
    if result.code != 0:
        raise OperationError(
            f"'{branch}' is not a valid git branch name. Use letters, digits, '-', '_' "
            "and '/', and avoid spaces, '..' and a leading or trailing '/'."
        )


def _remote_branch_names(main: Path, log: LogFn) -> set[str]:
    """The names of every branch origin has, read with git ls-remote, without a local clone."""
    result = git_network(
        main, "ls-remote", "--heads", "origin", timeout=FETCH_TIMEOUT_SECONDS, log=log
    )
    names: set[str] = set()
    for line in result.stdout.splitlines():
        _, _, ref = line.partition("\t")
        if ref.startswith("refs/heads/"):
            names.add(ref.removeprefix("refs/heads/"))
    return names


def _clone_and_check_out(
    ws: Workspace,
    folder: Path,
    branch: str,
    base_branch: str,
    branch_exists_on_origin: bool,
    log: LogFn,
) -> None:
    """Clones main/ into folder, points it at GitHub, fetches, and checks out branch."""
    git(ws.root, "clone", "-q", str(ws.main), str(folder), log=log)
    original_branch = git(folder, "symbolic-ref", "--short", "HEAD", check=False).stdout.strip()
    git(folder, "remote", "set-url", "origin", ws.origin_url(), log=log)
    git_network(folder, "fetch", "--prune", "origin", timeout=FETCH_TIMEOUT_SECONDS, log=log)

    # The start point is always named, so a file or folder with the branch's name cannot make
    # the checkout ambiguous. -B also works when branch is the one the clone started on.
    if branch_exists_on_origin:
        git(folder, "checkout", "-q", "-B", branch, "--track", f"origin/{branch}", log=log)
    else:
        git(
            folder,
            "checkout",
            "-q",
            "-b",
            branch,
            "--no-track",
            f"origin/{base_branch}",
            log=log,
        )

    if original_branch and original_branch != branch:
        git(folder, "branch", "-D", original_branch, check=False)


def _validate_folder_name(name: str) -> None:
    if "/" in name or "\\" in name or name in ("", ".", "..", MAIN_FOLDER_NAME):
        raise OperationError(
            f"'{name}' cannot be the folder of a clone. Give a plain folder name without '/', "
            f"other than {MAIN_FOLDER_NAME}, '.' and '..'."
        )


def new_clone(
    ws: Workspace, branch: str, base: str | None, log: LogFn, folder_name: str | None = None
) -> Path:
    """Creates a clone of branch next to main/, made from main/ on disk.

    The clone is then pointed at the GitHub URL of main/ and fetched, so it does not
    depend on main/ afterwards (R5). branch is checked out with tracking when it already
    exists on GitHub. Otherwise it is created from origin/<base>, without tracking.
    The folder is called folder_name. Without it, the folder gets the name of the branch.
    """
    _validate_branch_name(ws.root, branch)
    if folder_name is None:
        folder_name = folder_for_branch(branch)
    else:
        _validate_folder_name(folder_name)
    folder = ws.root / folder_name
    if folder.exists():
        raise OperationError(
            f"{folder} already exists. Remove it first, or choose a different folder name."
        )

    base_branch = base or ws.config.workspace.base_branch or ws.main_branch()
    remote_branches = _remote_branch_names(ws.main, log)
    if base_branch not in remote_branches:
        raise OperationError(
            f"origin does not have a branch called '{base_branch}' yet. Push {base_branch} "
            "to GitHub first, or give a different base branch."
        )
    branch_exists_on_origin = branch in remote_branches

    try:
        _clone_and_check_out(ws, folder, branch, base_branch, branch_exists_on_origin, log)
    except BaseException:
        # Without this, the half-made folder blocks the next try with "already exists".
        shutil.rmtree(folder, ignore_errors=True)
        log(f"draupnir removed the unfinished clone {folder}.")
        raise

    try:
        prepare(ws, folder, log)
    except OperationError as exc:
        raise OperationError(
            f"The clone {folder} was created, but the setup stopped: {exc} Fix the problem "
            f"and run draupnir prepare {folder}."
        ) from exc

    return folder


# How many commit subjects are logged for each branch with commits that are not on GitHub.
_LOGGED_COMMITS = 5


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _rev_list(folder: Path, *args: str) -> list[str]:
    return [line for line in git(folder, "rev-list", *args).stdout.splitlines() if line]


@dataclass(frozen=True)
class _UnsavedCommits:
    commits: list[str]
    merge_commits: int


def _unsaved_commits(
    folder: Path, rev: str, saved_refs: list[str], default_ref: str
) -> _UnsavedCommits:
    """Finds the commits of rev that would be lost when the clone is deleted (D21).

    A commit is saved when it is on one of saved_refs, which always includes every remote
    branch. A commit that is not a merge is also saved when an equal change is already on
    default_ref. That is the case after GitHub's "rebase and merge", also when GitHub deleted
    the branch. A merge commit is never saved that way, because git cannot compare its change,
    and it can hold a conflict resolution that exists nowhere else.
    """
    # A line of rev-list --parents is the commit followed by its parents. A commit with more
    # than one parent is a merge commit, which is the same set rev-list --merges gives.
    not_saved = [line.split() for line in _rev_list(folder, "--parents", rev, "--not", *saved_refs)]
    merges = {fields[0] for fields in not_saved if len(fields) > 2}
    same_change_missing: set[str] = set()
    if len(merges) < len(not_saved):
        same_change_missing = set(
            _rev_list(
                folder, "--cherry-pick", "--right-only", "--no-merges", f"{default_ref}...{rev}"
            )
        )
    commits = [
        fields[0] for fields in not_saved if fields[0] in merges or fields[0] in same_change_missing
    ]
    return _UnsavedCommits(commits=commits, merge_commits=len(merges))


def _commits_sentence(what: str, unsaved: _UnsavedCommits) -> str:
    total = len(unsaved.commits)
    merges = unsaved.merge_commits
    if total == 1:
        kind = "a merge commit" if merges else "not a merge commit"
        return f"{what} has 1 commit that is not on GitHub, and it is {kind}."
    if merges == 0:
        of_them = "none of them is a merge commit"
    elif merges == 1:
        of_them = "1 of them is a merge commit"
    else:
        of_them = f"{merges} of them are merge commits"
    return f"{what} has {total} commits that are not on GitHub, and {of_them}."


def _log_commit_subjects(folder: Path, what: str, commits: list[str], log: LogFn) -> None:
    shown = commits[:_LOGGED_COMMITS]
    result = git(folder, "log", "--no-walk=unsorted", "--format=%h %s", *shown)
    log(f"These commits of {what} are not on GitHub:")
    for line in result.stdout.splitlines():
        if line:
            log(f"  {line}")
    more = len(commits) - len(shown)
    if more == 1:
        log("  There is 1 more.")
    elif more > 1:
        log(f"  There are {more} more.")


@dataclass(frozen=True)
class _UnsavedWork:
    reasons: list[str]
    merge_commits: int


def _find_unsaved_work(ws: Workspace, folder: Path, log: LogFn) -> _UnsavedWork:
    try:
        git_network(folder, "fetch", "--prune", "origin", timeout=FETCH_TIMEOUT_SECONDS, log=log)
    except CommandError as exc:
        raise OperationError(
            f"draupnir could not fetch {folder} from GitHub, so it cannot tell whether the "
            "clone has work that GitHub does not have. Nothing was deleted. Check the "
            "connection and try again, or use --force to delete the clone without this check.\n"
            f"{exc}"
        ) from exc

    default_branch = ws.main_branch()
    default_ref = f"refs/remotes/origin/{default_branch}"
    if git(folder, "rev-parse", "--verify", "--quiet", default_ref, check=False).code != 0:
        raise OperationError(
            f"{folder} has no origin/{default_branch} after the fetch, so draupnir cannot tell "
            f"which commits are already on {default_branch}. Check that origin in the clone is "
            "the project on GitHub, or use --force to delete the clone without this check."
        )

    reasons: list[str] = []
    merge_commits = 0

    status = git(folder, "status", "--porcelain=v2", "--untracked-files=all").stdout
    changed = sum(1 for line in status.splitlines() if line and not line.startswith("#"))
    if changed:
        files = _plural(changed, "file has", "files have")
        reasons.append(
            f"{files} changes that are not committed, counting files that git does not track yet."
        )

    stashes = git(folder, "stash", "list", "--format=%gd").stdout
    stash_count = sum(1 for line in stashes.splitlines() if line)
    if stash_count:
        reasons.append(
            f"The clone has {_plural(stash_count, 'stash', 'stashes')}, and git never pushes "
            "stashes to GitHub."
        )

    operation = operation_in_progress(folder)
    if operation:
        reasons.append(f"A {operation} is in progress, and its work would be lost.")

    branches = git(folder, "for-each-ref", "--format=%(refname)", "refs/heads/").stdout
    checks: list[tuple[str, str, list[str]]] = [
        (ref, f"Branch {ref.removeprefix('refs/heads/')}", ["--remotes"])
        for ref in branches.splitlines()
        if ref
    ]
    if git(folder, "symbolic-ref", "--quiet", "HEAD", check=False).code != 0:
        # A detached HEAD can have commits that are on no branch at all, for example while a
        # rebase runs. Commits that are on a local branch are counted with that branch.
        checks.append(("HEAD", "The detached HEAD", ["--remotes", "--branches"]))

    for rev, what, saved_refs in checks:
        unsaved = _unsaved_commits(folder, rev, saved_refs, default_ref)
        if unsaved.commits:
            reasons.append(_commits_sentence(what, unsaved))
            merge_commits += unsaved.merge_commits
            _log_commit_subjects(folder, what.lower(), unsaved.commits, log)

    return _UnsavedWork(reasons=reasons, merge_commits=merge_commits)


def unsaved_work(ws: Workspace, folder: Path, log: LogFn) -> list[str]:
    """Fetches folder, then returns one sentence for each kind of work that would be lost.

    The kinds are uncommitted changes, counting untracked files, stashes, an operation in
    progress, and commits that are not saved on GitHub (D21). An empty list means that
    deleting the clone loses nothing. Raises OperationError when the fetch fails, because
    without a fetch draupnir cannot know what GitHub has.
    """
    return _find_unsaved_work(ws, folder.resolve(), log).reasons


def check_removable(ws: Workspace, folder: Path) -> Path:
    """Checks that folder is a clone that draupnir may delete, and returns its full path.

    This only reads, and it does not fetch. It refuses main/, the workspace root, a folder
    that is not directly under the root, and a folder that is not a clone of the project.
    """
    folder = folder.resolve()
    give_a_clone = f"Give the folder of a clone next to {MAIN_FOLDER_NAME}/."
    if folder == ws.root:
        raise OperationError(f"{folder} is the workspace root, not a clone. {give_a_clone}")
    if folder == ws.main:
        raise OperationError(
            f"draupnir does not remove {folder}, because every new clone is made from "
            f"{MAIN_FOLDER_NAME}/. {give_a_clone}"
        )
    if folder.parent != ws.root:
        raise OperationError(
            f"{folder} is not a folder directly under the workspace {ws.root}, so draupnir "
            f"does not remove it. {give_a_clone}"
        )
    if not folder.is_dir():
        problem = "is not a folder" if folder.exists() else "does not exist"
        raise OperationError(f"{folder} {problem}. {give_a_clone}")

    result = git(folder, "rev-parse", "--show-toplevel", check=False)
    top = (
        Path(result.stdout.strip()).resolve()
        if result.code == 0 and result.stdout.strip()
        else None
    )
    if top != folder:
        raise OperationError(
            f"{folder} is not a git clone, so draupnir does not remove it. {give_a_clone} "
            "Delete other folders by hand."
        )

    main_url = ws.origin_url()
    origin = git(folder, "remote", "get-url", "origin", check=False)
    if origin.code != 0 or normalize_url(origin.stdout.strip()) != normalize_url(main_url):
        raise OperationError(
            f"{folder} is not a clone of {main_url}, so draupnir does not remove it. "
            f"{give_a_clone} Delete other folders by hand."
        )
    return folder


# The folder where remove keeps what workspace.keep-on-remove matches, next to main/ and the
# clones. The workspace root is no git clone, so nothing in it is committed by accident.
REMOVED_FOLDER_NAME = "removed"

# What draupnir itself puts into a clone, and git ignores. It is not the user's work, so the
# question about the files git ignores leaves it out.
_DRAUPNIR_OWN = (".m2repo", ".mvn/maven.config")

_SIZE_UNITS: tuple[tuple[str, int], ...] = (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024))

# How many folders the question names before it counts the rest.
_TOPS_NAMED = 3


@dataclass(frozen=True)
class _IgnoredFiles:
    """What a clone holds that git ignores, for the question before a removal (R30)."""

    count: int
    size: int
    # The top of each path, a folder with its slash or a file, the largest first.
    tops: tuple[str, ...]


def _size_text(size: int) -> str:
    """A size a person can read, such as "408 KB" or "1.4 MB"."""
    for name, step in _SIZE_UNITS:
        if size >= step:
            scaled = size / step
            return f"{scaled:.0f} {name}" if scaled >= 10 else f"{scaled:.1f} {name}"
    return _plural(size, "byte", "bytes")


def _tops_text(tops: Sequence[str]) -> str:
    """The folders the files are in, largest first, as a sentence part.

    A clone with a build tree in it has more folders than fit in a question, so a long list
    ends in "and 6 more". The names alone cannot say which of them hold work, which is why the
    line that follows names keep-on-remove.
    """
    if len(tops) == 1:
        return tops[0]
    if len(tops) <= _TOPS_NAMED + 1:
        return ", ".join(tops[:-1]) + f" and {tops[-1]}"
    return ", ".join(tops[:_TOPS_NAMED]) + f" and {len(tops) - _TOPS_NAMED} more"


def _ignored_entries(folder: Path) -> list[str]:
    """The paths in folder that git ignores. A folder that is ignored whole is one entry."""
    found = git(
        folder,
        "ls-files",
        "-z",
        "--others",
        "--ignored",
        "--exclude-standard",
        "--directory",
        check=False,
    )
    if found.code != 0:
        return []
    return [path for path in found.stdout.split("\0") if path]


def _files_under(folder: Path, entry: str) -> Iterator[tuple[str, int]]:
    """Every file of one ignored entry, as its path inside folder and its size in bytes.

    A symbolic link is left out, because its target is counted where the target lives, and
    following one could leave the clone.
    """
    path = folder / entry
    if path.is_symlink():
        return
    if path.is_dir():
        children = (child for child in path.rglob("*") if not child.is_symlink())
        for child in children:
            if child.is_file():
                yield child.relative_to(folder).as_posix(), child.stat().st_size
    elif path.is_file():
        yield entry.rstrip("/"), path.stat().st_size


def _belongs_to_the_user(path: str, local_files: Collection[str]) -> bool:
    """False for a file draupnir put there itself, and for a file of workspace.local-files."""
    if path in local_files:
        return False
    return not any(path == own or path.startswith(f"{own}/") for own in _DRAUPNIR_OWN)


def _local_file_patterns(ws: Workspace) -> list[str]:
    """The workspace.local-files patterns that can match a file this count would include.

    A pattern inside the git folder is left out, because git never lists those files. So is a
    pattern inside what draupnir puts in the clone, which is skipped as a whole entry already:
    one of them is the clone's Maven repository, and matching it means listing a hundred
    thousand files to throw them all away again.
    """
    patterns: list[str] = []
    for pattern in ws.config.workspace.local_files:
        bare = pattern.removeprefix("!").removeprefix("/").rstrip("/")
        if bare.startswith(".git/"):
            continue
        if any(bare == own or bare.startswith(f"{own}/") for own in _DRAUPNIR_OWN):
            continue
        patterns.append(pattern)
    return patterns


def _ignored_files(ws: Workspace, folder: Path) -> _IgnoredFiles:
    """Counts and measures what folder holds that git ignores, and where it is.

    The files draupnir put there, and the files of workspace.local-files, are left out: the
    first are draupnir's own, and main/ keeps its copy of the second.
    """
    local_files = set(_matching_files(folder, _local_file_patterns(ws)))
    sizes: dict[str, int] = {}
    count = 0
    total = 0
    for entry in _ignored_entries(folder):
        # A folder of draupnir's own is skipped whole, so a large .m2repo is never walked.
        if not _belongs_to_the_user(entry.rstrip("/"), local_files):
            continue
        for path, size in _files_under(folder, entry):
            if not _belongs_to_the_user(path, local_files):
                continue
            head, slash, _ = path.partition("/")
            top = head + slash
            sizes[top] = sizes.get(top, 0) + size
            count += 1
            total += size
    tops = sorted(sizes, key=lambda top: sizes[top], reverse=True)
    return _IgnoredFiles(count=count, size=total, tops=tuple(tops))


def ignored_files_note(ws: Workspace, folder: Path) -> list[str]:
    """The lines about the files git ignores that go under the remove question (R30).

    A clone can read clean in every column and still hold the notes and the plan that were
    written for the change, because git ignores them and the checks before a removal never see
    them. The question names them, so the user decides what happens to them before the clone
    goes.

    It returns no lines when there is nothing to say. It never raises: the question has to be
    asked even when the files cannot be read.
    """
    try:
        found = _ignored_files(ws, folder)
        if not found.count:
            return []
        kept = len(_matching_files(folder, list(ws.config.workspace.keep_on_remove)))
    except (CommandError, OperationError, OSError):
        return []

    lines = [
        f"It holds {_size_text(found.size)} in {_plural(found.count, 'file', 'files')} that "
        f"git ignores: {_tops_text(found.tops)}."
    ]
    if kept:
        lines.append(
            f"draupnir copies the {_plural(kept, 'file', 'files')} that keep-on-remove matches "
            f"into {REMOVED_FOLDER_NAME}/ first, and deletes the rest."
        )
    else:
        lines.append(
            "They are deleted with the clone. Set keep-on-remove in "
            f"{CONFIG_FILE_NAME} to copy the ones you want to keep out of it first."
        )
    return lines


def _keep_destination(ws: Workspace, folder: Path) -> Path:
    """A folder of its own under <workspace root>/removed/ for the files kept from folder."""
    base = ws.root / REMOVED_FOLDER_NAME / f"{folder.name}-{date.today().isoformat()}"
    candidate = base
    number = 2
    while candidate.exists():
        candidate = base.with_name(f"{base.name}-{number}")
        number += 1
    return candidate


def _keep_files_of(ws: Workspace, folder: Path, log: LogFn) -> None:
    """Copies what workspace.keep-on-remove matches out of folder, before it is deleted (R30).

    The copy keeps the paths the files have in the clone. A copy that fails raises, and the
    clone is then not deleted, because the point of the key is that these files survive.
    """
    patterns = list(ws.config.workspace.keep_on_remove)
    if not patterns:
        return
    paths = _matching_files(folder, patterns)
    if not paths:
        log(f"Nothing in {folder} matches workspace.keep-on-remove.")
        return

    destination = _keep_destination(ws, folder)
    try:
        for path in paths:
            target = destination / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(folder / path, target)
    except OSError as exc:
        raise OperationError(
            f"draupnir could not copy the files that workspace.keep-on-remove matches out of "
            f"{folder}, so it did not delete the clone. Copy them yourself, or take the "
            f"pattern out of {CONFIG_FILE_NAME}, and remove the clone again. The error was: "
            f"{exc}"
        ) from exc
    log(f"Copied {_plural(len(paths), 'file', 'files')} into {destination}.")


def remove_clone(
    ws: Workspace,
    folder: Path,
    force: bool,
    log: LogFn,
    before_delete: Callable[[], None] | None = None,
) -> None:
    """Deletes a clone, with its .m2repo and local files, when no work would be lost (R7).

    Without force, it fetches first and refuses when unsaved_work finds anything. The message
    lists every reason. With force, it skips the fetch and the checks, but it still refuses
    main/, the workspace root, and folders that are not clones of the project.

    What workspace.keep-on-remove matches is copied out of the clone first (R30), with force
    as well: force skips the check for work that GitHub does not have, not the files the user
    asked to keep.

    before_delete, when given, is called after the checks passed and before the delete. The
    callers close the clone's terminal there, so a program in it cannot write into the folder
    while it is deleted. A removal that is refused never calls it. This module does not close
    the terminal itself, because the cmux module imports this one.
    """
    folder = check_removable(ws, folder)

    if force:
        log(
            f"Skipped the check for work that is not on GitHub, because the removal of "
            f"{folder} is forced."
        )
    else:
        work = _find_unsaved_work(ws, folder, log)
        if work.reasons:
            lines = [
                f"draupnir did not delete {folder}, because it has work that is not on GitHub."
            ]
            lines.extend(f"- {reason}" for reason in work.reasons)
            lines.append(
                "Commit and push what you want to keep, then remove the clone again. Use "
                "--force to delete it anyway."
            )
            if work.merge_commits:
                lines.append(
                    "A merge commit can hold a conflict resolution that exists nowhere else. "
                    "Push the branch first, or use --force when the merge is no longer needed."
                )
            raise OperationError("\n".join(lines))
        log(f"Nothing in {folder} is missing on GitHub.")

    _keep_files_of(ws, folder, log)
    if before_delete is not None:
        before_delete()
    log(f"Deleting {folder}. A large .m2repo can take a while.")
    try:
        shutil.rmtree(folder)
    except OSError as exc:
        raise OperationError(
            f"draupnir could not delete every file in {folder}, so part of the clone may be "
            "left. Close programs that use files in the folder, such as the IDE, and delete "
            f"what is left by hand. The error was: {exc}"
        ) from exc
    log(f"Deleted {folder}.")
