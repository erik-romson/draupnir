"""Reads the git state of a clone, without taking a lock or changing anything (R23)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from draupnir.runner import git
from draupnir.workspace import Workspace


@dataclass(frozen=True)
class ProjectStatus:
    """The state of one clone, read without a fetch and without taking a lock.

    branch is None when the clone is on a detached HEAD. upstream is None when the branch
    has no upstream at all, which the table shows as "not pushed". upstream_gone is true
    when the branch has an upstream, but GitHub deleted it, so ahead_upstream and
    behind_upstream are also None. error is set instead of every other field when the read
    itself failed, for example because the folder is not a git clone. head is the sha of the
    commit that is checked out, or None when the branch has no commits yet. finished is true
    when the clone has nothing left to do, which _finished explains.
    """

    folder: Path
    branch: str | None
    ahead_main: int
    behind_main: int
    upstream: str | None
    upstream_gone: bool
    ahead_upstream: int | None
    behind_upstream: int | None
    changed: int
    untracked: int
    operation: str | None
    error: str | None = None
    head: str | None = None
    finished: bool = False

    @property
    def name(self) -> str:
        return self.folder.name


# Files and folders in the git folder that show an operation in progress, and the name of
# the operation. rebase-apply/applying comes before rebase-apply, because git am also uses
# rebase-apply.
_OPERATION_MARKERS: tuple[tuple[str, str], ...] = (
    ("rebase-merge", "rebase"),
    ("rebase-apply/applying", "git am"),
    ("rebase-apply", "rebase"),
    ("MERGE_HEAD", "merge"),
    ("CHERRY_PICK_HEAD", "cherry-pick"),
    ("REVERT_HEAD", "revert"),
    ("BISECT_LOG", "bisect"),
)


def git_dir(folder: Path) -> Path:
    """The absolute path of folder's git folder, usually folder/.git."""
    return Path(git(folder, "rev-parse", "--path-format=absolute", "--git-dir").stdout.strip())


def operation_in_progress(folder: Path) -> str | None:
    """The name of the operation in progress in folder, such as "rebase", or None."""
    folder_git_dir = git_dir(folder)
    for marker, name in _OPERATION_MARKERS:
        if (folder_git_dir / marker).exists():
            return name
    return None


def _error_status(folder: Path, error: str) -> ProjectStatus:
    return ProjectStatus(
        folder=folder,
        branch=None,
        ahead_main=0,
        behind_main=0,
        upstream=None,
        upstream_gone=False,
        ahead_upstream=None,
        behind_upstream=None,
        changed=0,
        untracked=0,
        operation=None,
        error=error,
    )


def _has_stash(folder: Path) -> bool:
    """True when the clone has at least one stash.

    git never pushes a stash, so a clone that has one still holds work of its own. Reading the
    list takes no lock and does not write the index (rule 2).
    """
    result = git(folder, "stash", "list", "--format=%gd", check=False)
    return result.code == 0 and bool(result.stdout.strip())


# What the comparison of the changes answered, for each clone and the two commits it compared.
# The dashboard reads the status every few seconds, and this is the one read that costs real
# time: on a branch that is 47 commits from a main branch 166 commits ahead it takes about a
# second. Neither side moves between most reads, so the answer is kept. It is kept in memory
# only, like the build results (Q12).
_comparison_cache: dict[tuple[str, str, str], bool] = {}


def reset_comparison_cache() -> None:
    """Forgets every comparison of the changes, so one test cannot answer another."""
    _comparison_cache.clear()


def _main_has_every_change(folder: Path, main_ref: str, ahead_main: int, head: str | None) -> bool:
    """True when main_ref already has every change the checked out branch has (D59).

    A branch that is ahead of main by no commit at all has nothing main could be missing, and
    then no git runs. Otherwise the commits are compared by their change and not by their name,
    the way D21 compares them before a removal, because a merge on GitHub gives the same change
    a new commit. Merge commits are left out: a merge carries no change of its own.

    The answer depends on nothing but the two commits, so it is read once for each pair.
    """
    if ahead_main == 0:
        return True
    main_sha = git(folder, "rev-parse", "--verify", "--quiet", main_ref, check=False).stdout.strip()
    if head is None or not main_sha:
        return False

    key = (str(folder), head, main_sha)
    answered = _comparison_cache.get(key)
    if answered is not None:
        return answered

    result = git(
        folder,
        "rev-list",
        "--count",
        "--cherry-pick",
        "--right-only",
        "--no-merges",
        f"{main_sha}...HEAD",
        check=False,
    )
    answered = result.code == 0 and result.stdout.strip() == "0"
    _comparison_cache[key] = answered
    return answered


def _finished(ws: Workspace, status: ProjectStatus) -> bool:
    """True when the clone has nothing left to do and nothing that would be lost (R29).

    The clone is finished when the work left the machine and the main branch on GitHub has all
    of it: the branch has an upstream, or had one that GitHub deleted; the branch and its
    upstream hold the same commits; the working tree is clean; there is no stash; no operation
    is in progress; and the main branch has every change of the branch.

    main/ is never finished, because it is not work. A clone that was never pushed is not
    finished either, and that is what tells a clone that is done from one that was just made:
    neither has a commit the main branch is missing.

    The two reads that cost something, the stash and the comparison of the changes, run only
    for a clone that passed every cheap check.
    """
    if status.error is not None or status.branch is None:
        return False
    if status.folder.resolve() == ws.main.resolve():
        return False
    if status.operation or status.changed or status.untracked:
        return False
    if status.upstream is None:
        return False
    if not status.upstream_gone and (status.ahead_upstream or status.behind_upstream):
        return False
    if _has_stash(status.folder):
        return False
    return _main_has_every_change(
        status.folder, f"origin/{ws.main_branch()}", status.ahead_main, status.head
    )


def read_status(ws: Workspace, path: Path) -> ProjectStatus:
    """Reads the state of the clone at path, without a fetch and without taking a lock.

    Uses only commands that read: `git status --porcelain=v2 --branch`, `git rev-list` with
    `--count`, `git rev-parse`, `git stash list`, and file checks in the git folder (rule 2,
    R23). Never runs `git fetch`, and never writes the index. The last two commands run only
    for a clone that could still be finished, which _finished explains. When the read itself
    fails, for example because path is not a git clone, the result carries that failure in
    `error` instead of raising, so one broken clone does not stop a table of many.
    """
    result = git(path, "status", "--porcelain=v2", "--branch", "--untracked-files=all", check=False)
    if result.code != 0:
        reason = (result.stderr or result.stdout).strip() or f"git status failed in {path}."
        return _error_status(path, reason)

    branch: str | None = None
    head: str | None = None
    upstream: str | None = None
    has_ab = False
    ahead_upstream = behind_upstream = 0
    changed = untracked = 0
    for line in result.stdout.splitlines():
        if line.startswith("# branch.oid "):
            oid = line.removeprefix("# branch.oid ")
            head = None if oid == "(initial)" else oid
        elif line.startswith("# branch.head "):
            name = line.removeprefix("# branch.head ")
            branch = None if name == "(detached)" else name
        elif line.startswith("# branch.upstream "):
            upstream = line.removeprefix("# branch.upstream ")
        elif line.startswith("# branch.ab "):
            has_ab = True
            ahead, behind = line.removeprefix("# branch.ab ").split()
            ahead_upstream, behind_upstream = int(ahead), -int(behind)
        elif line.startswith("? "):
            untracked += 1
        elif line[:2] in ("1 ", "2 ", "u "):
            changed += 1

    upstream_gone = upstream is not None and not has_ab
    has_upstream_counts = upstream is not None and not upstream_gone
    resolved_ahead_upstream: int | None = ahead_upstream if has_upstream_counts else None
    resolved_behind_upstream: int | None = behind_upstream if has_upstream_counts else None

    ahead_main = behind_main = 0
    counts = git(
        path,
        "rev-list",
        "--left-right",
        "--count",
        f"HEAD...origin/{ws.main_branch()}",
        check=False,
    )
    if counts.code == 0 and counts.stdout.strip():
        left, right = counts.stdout.split()
        ahead_main, behind_main = int(left), int(right)

    status = ProjectStatus(
        folder=path,
        branch=branch,
        ahead_main=ahead_main,
        behind_main=behind_main,
        upstream=upstream,
        upstream_gone=upstream_gone,
        ahead_upstream=resolved_ahead_upstream,
        behind_upstream=resolved_behind_upstream,
        changed=changed,
        untracked=untracked,
        operation=operation_in_progress(path),
        head=head,
    )
    return replace(status, finished=_finished(ws, status))
