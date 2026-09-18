"""Actions that work on one clone or across every clone of a workspace."""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from draupnir.errors import OperationError, RebaseConflict
from draupnir.github import (
    CANCELLED,
    FAILURE,
    MERGEABLE_NOW,
    NONE,
    PENDING,
    PR_MERGED,
    PR_OPEN,
    SUCCESS,
    TIMEOUT,
    BuildState,
    BuildTiming,
    PrMergeState,
    PullRequest,
    auto_merge_allowed,
    build_state,
    create_pr,
    enable_auto_merge,
    find_open_pr,
    merge_pr,
    pr_merge_state,
    rebase_merge_allowed,
    require_gh,
    wait_for_build,
)
from draupnir.runner import (
    FETCH_TIMEOUT_SECONDS,
    PUSH_TIMEOUT_SECONDS,
    CommandError,
    LogFn,
    git,
    git_network,
)
from draupnir.status import ProjectStatus, operation_in_progress, read_status
from draupnir.workspace import Workspace


def fetch(ws: Workspace, folder: Path, log: LogFn) -> None:
    """Fetches folder from origin, with --prune, and the fetch timeout (rule 5).

    Raises OperationError, with NETWORK_HINT, when the fetch fails.
    """
    git_network(folder, "fetch", "--prune", "origin", timeout=FETCH_TIMEOUT_SECONDS, log=log)


def fetch_all(ws: Workspace, log: LogFn) -> None:
    """Fetches every clone of the workspace, and goes on when one of them fails.

    A clone that fails to fetch does not stop the others. At the end, this raises one
    OperationError that names every clone whose fetch failed, so the caller sees the whole
    picture instead of stopping at the first problem.
    """
    failed: list[str] = []
    for folder in ws.project_paths():
        try:
            fetch(ws, folder, log)
        except OperationError as exc:
            log(str(exc))
            failed.append(folder.name)
    if failed:
        names = ", ".join(failed)
        raise OperationError(
            f"draupnir could not fetch every clone. The fetch failed for: {names}. See the "
            "output above for the reason for each one."
        )


def require_ready(ws: Workspace, folder: Path) -> ProjectStatus:
    """Reads the status of folder, and refuses when no action can safely start there.

    Every action that changes a clone calls this first (rule 6). It refuses when the
    status read itself failed, when the clone is on a detached HEAD, and when a rebase,
    merge, cherry-pick, revert or git am is already in progress. The message names the
    operation, so the user knows what to finish or abort before trying again.
    """
    status = read_status(ws, folder)
    if status.error:
        raise OperationError(
            f"draupnir could not read the state of {status.name}. Run git status in the clone to "
            f"see the problem, fix it, and try again.\n{status.error}"
        )
    if status.operation:
        raise OperationError(
            f"{status.name} has a {status.operation} in progress. Finish it or abort it "
            "in the clone, then run this action again."
        )
    if status.branch is None:
        raise OperationError(
            f"{status.name} is on a detached HEAD, not on a branch. Check out a branch "
            "in the clone, then run this action again."
        )
    return status


def require_feature_branch(ws: Workspace, status: ProjectStatus) -> str:
    """Returns status's branch, and refuses when folder is main/ or on the default branch.

    Fast-forward main and the pull request flow only take commits from a feature branch
    into the default branch, so they refuse to run from main/ itself (R18).
    """
    branch = status.branch
    assert branch is not None  # require_ready raises OperationError otherwise.
    if status.folder == ws.main or branch == ws.main_branch():
        raise OperationError(
            f"{status.name} is on {branch}, the default branch. This action only takes "
            "commits from a feature branch. Select a feature branch clone and try again."
        )
    return branch


def rebase_on_main(ws: Workspace, folder: Path, log: LogFn) -> None:
    """Rebases folder's branch on origin/<default branch> (D8).

    Refuses when a tracked file is changed. An untracked file does not block the rebase,
    because git carries it through the rebase untouched. The rebase always fetches first,
    so it lands on GitHub's current default branch.

    On a conflict, the rebase stays in progress. This logs every conflicting file and
    raises RebaseConflict with the file list, so the caller can offer to resolve them in
    the IDE, without draupnir aborting the rebase for the user.
    """
    status = require_ready(ws, folder)
    if status.changed:
        raise OperationError(
            f"{status.name} has {status.changed} changed tracked file(s). Commit or stash "
            "them, then try again."
        )
    main_branch = ws.main_branch()
    fetch(ws, folder, log)
    result = git(folder, "rebase", f"origin/{main_branch}", check=False, log=log)
    if result.code == 0:
        log(f"{status.name} is now on top of origin/{main_branch}.")
        return
    if operation_in_progress(folder) == "rebase":
        files = [
            line
            for line in git(folder, "diff", "--name-only", "--diff-filter=U").stdout.splitlines()
            if line
        ]
        for name in files:
            log(f"Conflict: {name}")
        raise RebaseConflict(files)
    raise OperationError(
        f"git rebase stopped in {status.name} for a reason other than a conflict. See "
        "the output above, resolve the problem, and try again."
    )


# The reasons `git push --porcelain` gives, in parentheses after `[rejected]`, when the
# push was refused because this clone has not integrated commits GitHub already has.
_NOT_INTEGRATED_REASONS = (
    "remote ref updated since checkout",
    "stale info",
    "fetch first",
    "non-fast-forward",
)

# The summary field of a rejected ref line of `git push --porcelain`, for example
# "[rejected] (fetch first)" or "[remote rejected] (protected branch hook declined)".
_REJECTED_SUMMARY = re.compile(r"^\[(?P<flag>[^\]]*)\](?:\s*\((?P<reason>.*)\))?$")


def _rejected_ref_summary(output: str) -> tuple[str, str | None] | None:
    """Reads the flag and the reason of the first rejected ref of a push, or None.

    `git push --porcelain` prints one tab-separated line per ref, starting with `!` for a
    ref that was not updated. That flag, and the summary field after it, are stable,
    machine-readable output (rule 3), not a message meant for people.
    """
    for line in output.splitlines():
        if not line.startswith("!\t"):
            continue
        fields = line.split("\t")
        if len(fields) < 3:
            continue
        match = _REJECTED_SUMMARY.match(fields[2].strip())
        if match:
            return match.group("flag").strip(), match.group("reason")
    return None


def _push_error(name: str, branch: str, exc: CommandError) -> OperationError:
    """Turns a failed `git push --porcelain` into a message the user can act on."""
    summary = _rejected_ref_summary(exc.output)
    if summary is not None:
        flag, reason = summary
        if reason is not None and any(part in reason for part in _NOT_INTEGRATED_REASONS):
            return OperationError(
                f"GitHub has commits on {branch} that {name} has not integrated. Fetch, "
                "look at the commits, and rebase before you push again."
            )
        if flag == "remote rejected":
            detail = f" GitHub said: {reason}." if reason else ""
            return OperationError(
                f"GitHub rejected the push of {branch} from {name}.{detail} The branch "
                "may be protected. Check the rules for the branch on GitHub, and push again."
            )
    return OperationError(str(exc))


def push(ws: Workspace, folder: Path, log: LogFn) -> str:
    """Pushes folder's branch to origin, and returns the sha of the pushed commit.

    A feature branch is pushed with --force-with-lease --force-if-includes, because a
    rebase changes its history. That refuses to overwrite commits GitHub has that this
    clone has not fetched and integrated. The default branch is never force-pushed (D9).
    """
    status = require_ready(ws, folder)
    branch = status.branch
    assert branch is not None  # require_ready raises OperationError otherwise.
    args = ["push", "--porcelain"]
    if branch != ws.main_branch():
        args += ["--force-with-lease", "--force-if-includes"]
    args += ["-u", "origin", branch]
    try:
        git_network(folder, *args, timeout=PUSH_TIMEOUT_SECONDS, log=log)
    except CommandError as exc:
        raise _push_error(status.name, branch, exc) from exc
    return git(folder, "rev-parse", "HEAD").stdout.strip()


def _head_sha(folder: Path) -> str:
    result = git(folder, "rev-parse", "--verify", "--quiet", "HEAD^{commit}", check=False)
    sha = result.stdout.strip()
    if result.code != 0 or not sha:
        raise OperationError(
            f"{folder.name} has no commit yet, so it has no build. Make a commit, push it, and "
            "try again."
        )
    return sha


def check_build(ws: Workspace, folder: Path, log: LogFn) -> BuildState:
    """Asks GitHub once for the build state of folder's HEAD, and logs every job with its URL.

    This only reads, so it also works while an operation is in progress or on a detached HEAD.
    """
    require_gh(folder)
    sha = _head_sha(folder)
    state = build_state(folder, sha)
    log(f"Build of {folder.name} at {sha[:10]}: {state.summary()}")
    for item in state.items:
        log(f"  {item.name}: {item.state} {item.url}".rstrip())
    return state


def push_and_follow(
    ws: Workspace,
    folder: Path,
    log: LogFn,
    cancel: threading.Event,
    on_update: Callable[[BuildState], None] | None = None,
) -> BuildState:
    """Pushes folder's branch, then waits for the build of the pushed commit (R17, R21).

    The checks run before anything changes: the rule 6 checks first, then that gh is installed
    and logged in, so a missing gh stops before the push. The wait uses the times from
    [build] in draupnir.toml, and cancel ends it without stopping the build on GitHub.
    """
    require_ready(ws, folder)
    require_gh(folder)
    sha = push(ws, folder, log)
    timing = BuildTiming.from_config(ws.config.build)
    return wait_for_build(folder, sha, log, cancel, timing, on_update)


def _is_ancestor(folder: Path, ancestor: str, descendant: str) -> bool:
    """True when ancestor is an ancestor of descendant, or the same commit."""
    result = git(folder, "merge-base", "--is-ancestor", ancestor, descendant, check=False)
    return result.code == 0


def _require_green_build(folder: Path, sha: str, log: LogFn) -> None:
    """Stops fast-forward main unless the build of sha is green (D16).

    require_gh runs first, so a missing or logged-out gh stops before anything is read. A
    build state is only known when both the check runs and the commit statuses could be
    read (D31): a read that failed, even partly, stops here too, before anything is pushed.
    """
    require_gh(folder)
    short = sha[:10]
    try:
        state = build_state(folder, sha)
    except OperationError as exc:
        raise OperationError(
            f"draupnir could not read the build state of commit {short}, so fast-forward main "
            f"stopped before pushing anything. Check that gh works, and try again.\n{exc}"
        ) from exc
    log(f"Build of commit {short}: {state.summary()}")
    if state.state == SUCCESS:
        return
    if state.state == FAILURE:
        lines = [f"The build of commit {short} failed. Fix the problem and push again."]
        lines += [f"Failed: {item.name} {item.url}".rstrip() for item in state.failed_items()]
        raise OperationError("\n".join(lines))
    if state.state == PENDING:
        raise OperationError(
            f"The build of commit {short} is still running. Wait until it is green, for "
            "example with draupnir push --follow, and try again."
        )
    assert state.state == NONE
    raise OperationError(
        f"No build has reported for commit {short}. Push the branch, wait for a green "
        "build, and try again."
    )


def _ff_main_push_error(name: str, main_branch: str, exc: CommandError) -> OperationError:
    """Turns a failed push to the default branch into a message the user can act on."""
    summary = _rejected_ref_summary(exc.output)
    if summary is not None:
        flag, reason = summary
        if reason is not None and any(part in reason for part in _NOT_INTEGRATED_REASONS):
            return OperationError(
                f"GitHub got new commits on {main_branch} while draupnir was fast-forwarding "
                f"it from {name}. Rebase {name} on {main_branch} and try again."
            )
        if flag == "remote rejected":
            detail = f" GitHub said: {reason}." if reason else ""
            return OperationError(
                f"GitHub refused to move {main_branch} to the head of {name}.{detail} Use the "
                "pull request flow instead (draupnir pr), and consider setting "
                "fast-forward-main = false under [actions] in draupnir.toml."
            )
    return OperationError(str(exc))


def fast_forward_main(ws: Workspace, folder: Path, log: LogFn) -> None:
    """Moves the default branch on GitHub to folder's head, without force (R17, D16).

    The order of checks: rule 6 and that folder is on a feature branch, then a fetch, then
    that the branch is on top of origin/<default branch>, then that it has commits the
    default branch does not have yet, then, when actions.fast-forward-needs-green-build is
    true, that the build of the branch's head is green. Only then does this push. After a
    successful push, it updates main/ with update_main_clone, and only logs when that fails.
    """
    status = require_ready(ws, folder)
    branch = require_feature_branch(ws, status)
    main_branch = ws.main_branch()
    fetch(ws, folder, log)

    if not _is_ancestor(folder, f"origin/{main_branch}", "HEAD"):
        raise OperationError(
            f"{status.name} is not on top of origin/{main_branch}. Rebase {status.name} on "
            f"{main_branch}, then try again."
        )
    head_sha = git(folder, "rev-parse", "HEAD").stdout.strip()
    origin_main_sha = git(folder, "rev-parse", f"origin/{main_branch}").stdout.strip()
    if head_sha == origin_main_sha:
        raise OperationError(
            f"{branch} has no commits that are not already on {main_branch}. There is "
            "nothing to fast-forward. Commit your work on the branch first, then try again."
        )

    if ws.config.actions.fast_forward_needs_green_build:
        _require_green_build(folder, head_sha, log)

    try:
        git_network(
            folder,
            "push",
            "--porcelain",
            "origin",
            f"HEAD:refs/heads/{main_branch}",
            timeout=PUSH_TIMEOUT_SECONDS,
            log=log,
        )
    except CommandError as exc:
        raise _ff_main_push_error(status.name, main_branch, exc) from exc

    log(f"{main_branch} on GitHub now points to {head_sha[:10]}, the head of {branch}.")
    try:
        update_main_clone(ws, log)
    except OperationError as exc:
        # The push already moved the default branch, so this is not a failure of the action.
        log(str(exc))
        log(
            f"{main_branch} on GitHub is updated, but draupnir could not update "
            f"{ws.main.name}/. Run git pull there yourself."
        )


def update_main_clone(ws: Workspace, log: LogFn) -> None:
    """Fetches main/ and fast-forwards it to origin/<default branch>.

    Runs only when main/ is on the default branch, has no changed tracked files, and has no
    operation in progress. Otherwise this only logs that main/ was not updated, and to run
    git pull there. That is not an error: fast-forward main and the pull request flow (Step
    13) both already did the change that matters, on GitHub.
    """
    name = ws.main.name
    main_branch = ws.main_branch()
    fetch(ws, ws.main, log)
    status = read_status(ws, ws.main)
    if status.error or status.branch != main_branch or status.changed or status.operation:
        log(f"{name}/ was not updated. Run git pull there yourself.")
        return
    git(ws.main, "merge", "--ff-only", f"origin/{main_branch}", log=log)
    log(f"{name}/ is now on {main_branch} at the latest commit.")


# ----- The pull request flow ---------------------------------------------------------------

# The results of the pull request flow.
MERGED = "merged"
AUTO_MERGE = "auto-merge"

# How many times the flow reads a pull request again while GitHub still works out whether it
# can be merged.
UNKNOWN_MERGE_STATE_RETRIES = 5


@dataclass(frozen=True)
class PrOutcome:
    """How the pull request flow ended, when it did not stop with an error.

    result is MERGED or AUTO_MERGE. pr is the pull request, and build is the green build of the
    pushed commit.
    """

    result: str
    pr: PullRequest
    build: BuildState

    def message(self) -> str:
        if self.result == MERGED:
            return f"Pull request #{self.pr.number} is merged."
        return (
            f"Auto-merge is on. GitHub merges pull request #{self.pr.number} after the required "
            "review."
        )


def _build_not_green_message(pr: PullRequest, build: BuildState) -> str:
    """Why the flow did not merge pr after the wait for build, and what to do next."""
    short = build.sha[:10]
    stays_open = f"The pull request stays open at {pr.url}."
    if build.state == CANCELLED:
        return (
            f"draupnir stopped waiting for the build of commit {short}, so it did not merge pull "
            f"request #{pr.number}. {stays_open} The build keeps running on GitHub. Run the "
            "pull request flow again to merge after a green build."
        )
    first = (
        f"The build result of commit {short} is {build.state}, so draupnir did not merge pull "
        f"request #{pr.number}. {stays_open}"
    )
    if build.state == FAILURE:
        lines = [f"{first} Fix the problem, commit, and run the pull request flow again."]
        lines += [f"Failed: {item.name} {item.url}".rstrip() for item in build.failed_items()]
        return "\n".join(lines)
    if build.state == NONE:
        return (
            f"{first} Check that GitHub runs a build for this branch, and run the pull request "
            "flow again."
        )
    if build.state == TIMEOUT:
        return (
            f"{first} The build keeps running on GitHub. Run the pull request flow again to "
            "merge after a green build."
        )
    return f"{first} Run the pull request flow again when the build is green."


def _check_pr_unchanged(
    state: PrMergeState, pr: PullRequest, sha: str, branch: str, main_branch: str
) -> None:
    """Stops when the pull request changed on GitHub while draupnir waited for the build."""
    number = pr.number
    if state.state not in (PR_OPEN, PR_MERGED):
        raise OperationError(
            f"Pull request #{number} was closed on GitHub while draupnir waited for the build, "
            f"so draupnir did not merge it. Reopen it at {pr.url}, or run the pull request flow "
            "again to create a new one."
        )
    if state.head_sha != sha:
        raise OperationError(
            f"Someone pushed to {branch} while draupnir waited for the build, so draupnir did "
            f"not merge pull request #{number}. The pull request now ends at commit "
            f"{state.head_sha[:10]}, not at commit {sha[:10]} that was built. Fetch, look at the "
            "new commits, and run the pull request flow again."
        )
    if state.base != main_branch:
        raise OperationError(
            f"Someone changed the base of pull request #{number} to {state.base} while draupnir "
            f"waited for the build, so draupnir did not merge it. draupnir only merges pull "
            f"requests into {main_branch}. Check the pull request at {pr.url}, and run the pull "
            "request flow again."
        )


def _read_pr_for_merge(
    folder: Path,
    pr: PullRequest,
    sha: str,
    branch: str,
    main_branch: str,
    timing: BuildTiming,
    log: LogFn,
    cancel: threading.Event,
) -> PrMergeState:
    """Reads the pull request after the build, and reads it again while its state is UNKNOWN."""
    for attempt in range(UNKNOWN_MERGE_STATE_RETRIES + 1):
        state = pr_merge_state(folder, pr.number)
        _check_pr_unchanged(state, pr, sha, branch, main_branch)
        if state.state == PR_MERGED or state.merge_state_status != "UNKNOWN":
            return state
        if attempt == UNKNOWN_MERGE_STATE_RETRIES:
            break
        log(
            f"GitHub is still working out whether pull request #{pr.number} can be merged. "
            f"draupnir reads it again in {timing.poll_seconds:g} seconds."
        )
        if cancel.wait(timing.poll_seconds):
            raise OperationError(
                f"draupnir stopped before it merged pull request #{pr.number}. The pull request "
                "stays open. Run the pull request flow again to merge it."
            )
    raise OperationError(
        f"GitHub did not work out whether pull request #{pr.number} can be merged, so draupnir "
        f"did not merge it. The pull request stays open at {pr.url}. Run the pull request flow "
        "again in a minute, or merge the pull request on GitHub."
    )


def _merge_or_enable_auto_merge(
    folder: Path,
    pr: PullRequest,
    sha: str,
    state: PrMergeState,
    branch: str,
    main_branch: str,
    log: LogFn,
) -> str:
    """Merges the pull request, or turns on auto-merge, from GitHub's merge state (D17, D27).

    Returns MERGED or AUTO_MERGE. Every other merge state stops with a message.
    """
    number = pr.number
    status = state.merge_state_status
    if state.state == PR_MERGED:
        log(f"GitHub already merged pull request #{number}, with auto-merge.")
        return MERGED
    review = state.review_decision or "no review decision"
    log(f"Pull request #{number} has the merge state {status}, with {review}.")

    if state.is_draft or status == "DRAFT":
        raise OperationError(
            f"Pull request #{number} is a draft, so draupnir did not merge it. Mark the pull "
            f"request as ready for review at {pr.url}, then run the pull request flow again."
        )
    if status in MERGEABLE_NOW:
        try:
            merge_pr(folder, number, sha, log)
        except CommandError as exc:
            raise OperationError(
                f"GitHub did not merge pull request #{number}, so it stays open. See the gh "
                "output below, then merge the pull request on GitHub or run the pull request "
                f"flow again.\n{exc}"
            ) from exc
        return MERGED
    if status == "BLOCKED":
        return _enable_auto_merge(folder, pr, sha, log)
    if status == "BEHIND":
        raise OperationError(
            f"GitHub wants {branch} to be up to date with {main_branch} before it merges pull "
            f"request #{number}. Rebase on {main_branch}, and run the pull request flow again."
        )
    if status == "DIRTY":
        raise OperationError(
            f"{branch} has conflicts with {main_branch}, so GitHub cannot merge pull request "
            f"#{number}. Rebase on {main_branch}, resolve the conflicts, and run the pull "
            "request flow again."
        )
    raise OperationError(
        f"GitHub gives pull request #{number} the merge state {status}, which draupnir does not "
        f"know, so draupnir did not merge it. The pull request stays open. Merge it at {pr.url}."
    )


def _enable_auto_merge(folder: Path, pr: PullRequest, sha: str, log: LogFn) -> str:
    """Turns on auto-merge for a blocked pull request, and returns MERGED or AUTO_MERGE."""
    number = pr.number
    try:
        enable_auto_merge(folder, number, sha, log)
    except CommandError as exc:
        try:
            allowed = auto_merge_allowed(folder)
        except OperationError as read_error:
            log(str(read_error))
            allowed = None
        if allowed is False:
            raise OperationError(
                f"Auto-merge is turned off for this repository, so draupnir could not turn it on "
                f"for pull request #{number}. The pull request stays open. Merge it at {pr.url} "
                "after the required review."
            ) from exc
        raise OperationError(
            f"GitHub did not turn on auto-merge for pull request #{number}, so it stays open. "
            "See the gh output below, and merge the pull request on GitHub after the required "
            f"review.\n{exc}"
        ) from exc
    after = pr_merge_state(folder, number)
    return MERGED if after.state == PR_MERGED else AUTO_MERGE


def _update_after_merge(ws: Workspace, folder: Path, pr: PullRequest, log: LogFn) -> None:
    """Fetches the clone and updates main/. A failure here is logged, because the merge is done."""
    try:
        fetch(ws, folder, log)
        update_main_clone(ws, log)
    except OperationError as exc:
        log(str(exc))
        log(
            f"Pull request #{pr.number} is merged, but draupnir could not update the clones "
            f"afterwards. Run draupnir fetch, and git pull in {ws.main.name}/."
        )


def pr_build_and_merge(
    ws: Workspace,
    folder: Path,
    log: LogFn,
    cancel: threading.Event,
    on_update: Callable[[BuildState], None] | None = None,
) -> PrOutcome:
    """Pushes folder's branch, and merges it into the default branch through a pull request.

    The order: the rule 6 and feature branch checks, that gh works, and that the repository
    allows rebase merges, all before anything changes (D22). Then a fetch, the push, and the
    open pull request into the default branch, or a new one. Then the wait for the build of the
    pushed commit. Only a green build goes on (D11). Then the pull request is read again, and
    merged with rebase, or gets auto-merge when GitHub still waits for a review (D17, D27).
    After a merge, main/ is updated.

    Every stop raises OperationError, and the pull request stays open. The flow never deletes
    the branch on GitHub and never touches other clones.
    """
    status = require_ready(ws, folder)
    branch = require_feature_branch(ws, status)
    main_branch = ws.main_branch()
    require_gh(folder)
    if not rebase_merge_allowed(folder):
        raise OperationError(
            "The repository does not allow rebase merges, and draupnir merges only with rebase. "
            "Merge the pull request on GitHub. draupnir stopped before it pushed anything."
        )

    fetch(ws, folder, log)
    if not _is_ancestor(folder, f"origin/{main_branch}", "HEAD"):
        log(
            f"Note: {branch} is not on top of origin/{main_branch}. The build tests the branch "
            f"without the newest commits on {main_branch}."
        )

    sha = push(ws, folder, log)

    pr = find_open_pr(folder, branch, main_branch)
    if pr is None:
        pr = create_pr(folder, branch, main_branch, log)
        log(f"Created pull request #{pr.number} into {main_branch}: {pr.url}")
    else:
        log(f"Using the open pull request #{pr.number} into {main_branch}: {pr.url}")

    timing = BuildTiming.from_config(ws.config.build)
    try:
        build = wait_for_build(folder, sha, log, cancel, timing, on_update)
    except OperationError as exc:
        raise OperationError(
            f"draupnir could not follow the build of commit {sha[:10]}, so it did not merge pull "
            f"request #{pr.number}. The pull request stays open at {pr.url}. Check that gh "
            f"works, and run the pull request flow again.\n{exc}"
        ) from exc
    if build.state != SUCCESS:
        raise OperationError(_build_not_green_message(pr, build))

    state = _read_pr_for_merge(folder, pr, sha, branch, main_branch, timing, log, cancel)
    result = _merge_or_enable_auto_merge(folder, pr, sha, state, branch, main_branch, log)
    outcome = PrOutcome(result, pr, build)
    log(outcome.message())
    if result == MERGED:
        _update_after_merge(ws, folder, pr, log)
    return outcome
