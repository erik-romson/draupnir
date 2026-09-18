"""Setting up clones: init, clone main, new clone, prepare, unsaved work and remove."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
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
    tracked: bool
    exists_in_folder: bool
    exists_in_main: bool


def _plan_local_files(ws: Workspace, folder: Path) -> list[_LocalFile]:
    """Reads, for every configured local file, whether it is tracked, and where it exists.

    This only reads. Nothing is copied here.
    """
    plans: list[_LocalFile] = []
    for path in ws.config.workspace.local_files:
        plans.append(
            _LocalFile(
                path=path,
                tracked=_is_tracked(ws.main, path),
                exists_in_folder=(folder / path).exists(),
                exists_in_main=(ws.main / path).exists(),
            )
        )
    return plans


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


def _apply_local_file(
    main: Path, folder: Path, is_main: bool, plan: _LocalFile, log: LogFn
) -> list[str]:
    """Copies plan into folder when needed, and returns the exclude lines it needs.

    A file that git tracks needs no exclude line, because every clone already has it. A
    path under .git/ is never excluded either, because .gitignore-style files do not apply
    there. main/ itself is only ever excluded, never copied into, because it is the
    source the other clones copy from.
    """
    if plan.tracked:
        log(f"Skipped {plan.path}: git tracks it in main/, so every clone already has it.")
        return []

    lines = [] if plan.path.startswith(".git/") else [f"/{plan.path}"]
    if is_main:
        return lines

    target = folder / plan.path
    if plan.exists_in_folder:
        log(f"Kept {plan.path}: it already exists in this folder.")
    elif plan.exists_in_main:
        source = main / plan.path
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
        log(f"Copied {plan.path} from main/.")
    else:
        log(f"Skipped {plan.path}: it does not exist in main/.")
    return lines


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
    plans = _plan_local_files(ws, folder)
    maven_on = _maven_enabled(ws)
    if maven_on:
        maven.version(folder)
        _check_maven_config_not_tracked(folder)

    is_main = folder == ws.main
    exclude_lines: list[str] = []
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

# Files that are not in git, but that every clone needs. Copied from main/ once,
# never overwritten, and hidden from git in each clone. Never list secrets here.
# local-files = [
#   "src/main/resources/local.properties",
#   ".claude/settings.local.json",
#   ".idea/runConfigurations",
#   ".git/hooks/commit-msg",
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
# Commands that start the IDE. Default: idea and pycharm from PATH, JetBrains Toolbox
# or /Applications. Machine override: DRAUPNIR_IDEA and DRAUPNIR_PYCHARM.
# java = "idea"
# python = "pycharm"
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


def new_clone(ws: Workspace, branch: str, base: str | None, log: LogFn) -> Path:
    """Creates a clone of branch next to main/, made from main/ on disk.

    The clone is then pointed at the GitHub URL of main/ and fetched, so it does not
    depend on main/ afterwards (R5). branch is checked out with tracking when it already
    exists on GitHub. Otherwise it is created from origin/<base>, without tracking.
    """
    _validate_branch_name(ws.root, branch)
    folder = ws.root / folder_for_branch(branch)
    if folder.exists():
        raise OperationError(
            f"{folder} already exists. Remove it first, or choose a different branch name."
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


def remove_clone(ws: Workspace, folder: Path, force: bool, log: LogFn) -> None:
    """Deletes a clone, with its .m2repo and local files, when no work would be lost (R7).

    Without force, it fetches first and refuses when unsaved_work finds anything. The message
    lists every reason. With force, it skips the fetch and the checks, but it still refuses
    main/, the workspace root, and folders that are not clones of the project.
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
