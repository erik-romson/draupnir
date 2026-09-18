"""GitHub access through the gh command line tool, on github.com and GitHub Enterprise Server.

Every gh call runs inside a clone, so gh picks the host and the {owner}/{repo} placeholders from
the clone's remote (R20). The module reads only JSON from gh and exit codes, never messages
meant for people (rule 3).
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from draupnir.config import BuildConfig
from draupnir.errors import OperationError
from draupnir.runner import GH_TIMEOUT_SECONDS, CommandError, LogFn, git, run
from draupnir.workspace import normalize_url

PENDING = "pending"
SUCCESS = "success"
FAILURE = "failure"
NONE = "none"
CANCELLED = "cancelled"
TIMEOUT = "timeout"

# The conclusions of a finished check run that count as good. Every other conclusion of a
# finished check run counts as failure.
_GOOD_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})

# A wait for a build stops with an error after this many failed polls in a row.
MAX_FAILED_POLLS = 3

CHECK_RUNS_READ = "check runs"
COMMIT_STATUSES_READ = "commit statuses"

# The hosts that `gh auth status` accepted in this process.
_logged_in_hosts: set[str] = set()


@dataclass(frozen=True)
class BuildItem:
    """One job of a build: a check run or a commit status."""

    name: str
    state: str
    url: str


@dataclass(frozen=True)
class BuildState:
    """The build state of one commit, with its jobs.

    state is success, failure, pending or none when it was read from GitHub. A wait for a build
    can also end with cancelled or timeout. Those two carry the jobs of the last state it read.
    """

    sha: str
    state: str
    items: tuple[BuildItem, ...] = ()

    def summary(self) -> str:
        """The state and how many jobs are in each state, for example "pending (1 success)"."""
        counts: dict[str, int] = {}
        for item in self.items:
            counts[item.state] = counts.get(item.state, 0) + 1
        parts = ", ".join(f"{count} {state}" for state, count in sorted(counts.items()))
        return f"{self.state} ({parts})" if parts else self.state

    def failed_items(self) -> list[BuildItem]:
        return [item for item in self.items if item.state == FAILURE]


@dataclass(frozen=True)
class BuildTiming:
    """How often a wait asks GitHub, and how long it waits for a build to appear and to end."""

    poll_seconds: float
    grace_seconds: float
    timeout_seconds: float

    @classmethod
    def from_config(cls, config: BuildConfig) -> BuildTiming:
        return cls(
            poll_seconds=config.poll_seconds,
            grace_seconds=config.grace_seconds,
            timeout_seconds=config.timeout_seconds,
        )


def _seconds(value: float) -> str:
    return f"{value:g}"


# ----- gh and its login ------------------------------------------------------------------


def origin_host(repo: Path) -> str | None:
    """The host of repo's origin URL, or None when origin is a path on this machine."""
    result = git(repo, "remote", "get-url", "origin", check=False)
    url = result.stdout.strip()
    if result.code != 0 or not url:
        raise OperationError(
            f"{repo.name} has no remote called origin, so gh cannot tell which GitHub "
            "repository it belongs to. Add the origin remote in the clone and try again."
        )
    normalized = normalize_url(url)
    if normalized.startswith("/"):
        return None
    host = normalized.split("/", 1)[0]
    return host or None


def require_gh(repo: Path) -> None:
    """Checks that gh is installed, and that it is logged in to the host of repo's origin.

    The login check runs `gh auth status --hostname <host>` once per host per process, and
    reads only its exit code. An origin that is a path on this machine, as in the tests, has no
    host, so the login check is skipped.
    """
    if shutil.which("gh") is None:
        raise OperationError(
            "The GitHub command line tool gh was not found on PATH. Install it from "
            "https://cli.github.com, then log in with gh auth login. For GitHub Enterprise "
            "Server, log in with gh auth login --hostname <your host>."
        )
    host = origin_host(repo)
    if host is None or host in _logged_in_hosts:
        return
    try:
        result = run(
            ["gh", "auth", "status", "--hostname", host],
            repo,
            check=False,
            timeout=GH_TIMEOUT_SECONDS,
        )
    except CommandError as exc:
        raise OperationError(
            f"draupnir could not check whether gh is logged in to {host}. Check the network "
            f"connection and try again.\n{exc}"
        ) from exc
    if result.code != 0:
        raise OperationError(
            f"gh is not logged in to {host}. Run gh auth login --hostname {host} and try again."
        )
    _logged_in_hosts.add(host)


def reset_login_cache() -> None:
    """Forgets which hosts gh is logged in to. Tests use this, so each test checks again."""
    _logged_in_hosts.clear()


def _gh_json(repo: Path, *args: str) -> object:
    """Runs gh inside repo with the gh timeout, and returns its standard output as JSON."""
    result = run(["gh", *args], repo, timeout=GH_TIMEOUT_SECONDS)
    try:
        parsed: object = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OperationError(
            f"gh {' '.join(args)} did not answer with JSON. Check that gh works in {repo.name}, "
            "and try again."
        ) from exc
    return parsed


def _object_list(data: object, key: str, what: str) -> list[dict[str, object]]:
    """The JSON objects in data[key]. Raises OperationError when GitHub sent another shape."""
    value = cast(dict[str, object], data).get(key) if isinstance(data, dict) else None
    if not isinstance(value, list):
        raise OperationError(
            f"GitHub answered the read of the {what} without the list '{key}'. Check that gh "
            "talks to a GitHub server, and try again."
        )
    return [
        cast(dict[str, object], entry)
        for entry in cast(list[object], value)
        if isinstance(entry, dict)
    ]


def _text(obj: dict[str, object], key: str) -> str:
    value = obj.get(key)
    return "" if value is None else str(value)


# ----- Build state -----------------------------------------------------------------------


# GitHub answers the build reads with at most this many items per page.
_PAGE_SIZE = 100


def _read_all_pages(
    repo: Path, sha: str, endpoint: str, key: str, what: str
) -> list[dict[str, object]]:
    """Reads every page of a build endpoint, so a failed item on a later page is not missed."""
    entries: list[dict[str, object]] = []
    page = 1
    while True:
        data = _gh_json(
            repo,
            "api",
            f"repos/{{owner}}/{{repo}}/commits/{sha}/{endpoint}?per_page={_PAGE_SIZE}&page={page}",
        )
        page_entries = _object_list(data, key, what)
        entries.extend(page_entries)
        if len(page_entries) < _PAGE_SIZE:
            return entries
        page += 1


def _read_check_runs(repo: Path, sha: str) -> list[BuildItem]:
    items: list[BuildItem] = []
    for check in _read_all_pages(repo, sha, "check-runs", "check_runs", CHECK_RUNS_READ):
        if check.get("status") != "completed":
            state = PENDING
        elif check.get("conclusion") in _GOOD_CONCLUSIONS:
            state = SUCCESS
        else:
            state = FAILURE
        items.append(BuildItem(_text(check, "name"), state, _text(check, "html_url")))
    return items


def _read_commit_statuses(repo: Path, sha: str) -> list[BuildItem]:
    items: list[BuildItem] = []
    for status in _read_all_pages(repo, sha, "status", "statuses", COMMIT_STATUSES_READ):
        raw = status.get("state")
        if raw == "success":
            state = SUCCESS
        elif raw == "pending":
            state = PENDING
        else:
            state = FAILURE
        items.append(BuildItem(_text(status, "context"), state, _text(status, "target_url")))
    return items


def build_state(repo: Path, sha: str) -> BuildState:
    """Reads the build state of sha: check runs (GitHub Actions and apps) and commit statuses.

    A state is only returned when both reads succeeded (D31). When one of them fails, this
    raises OperationError and names the read that failed, because a green commit status next to
    unreadable check runs is not a green build.
    """
    items: list[BuildItem] = []
    failed: list[str] = []
    details: list[str] = []
    for what, read in (
        (CHECK_RUNS_READ, _read_check_runs),
        (COMMIT_STATUSES_READ, _read_commit_statuses),
    ):
        try:
            items.extend(read(repo, sha))
        except OperationError as exc:
            failed.append(what)
            details.append(str(exc))

    if failed:
        raise OperationError(
            f"draupnir could not read the {' and the '.join(failed)} of commit {sha[:10]} from "
            "GitHub, so its build state is unknown. See the details below, and try again.\n"
            + "\n".join(details)
        )

    if not items:
        overall = NONE
    elif any(item.state == FAILURE for item in items):
        overall = FAILURE
    elif any(item.state == PENDING for item in items):
        overall = PENDING
    else:
        overall = SUCCESS
    return BuildState(sha, overall, tuple(items))


# ----- Polling without hitting the rate limit ----------------------------------------------

# A wait never asks GitHub less often than this, so a long build still ends within 2 minutes.
MAX_POLL_SECONDS = 120.0

# With an estimate, the first poll after a build appears is at this share of the estimate.
EXPECTED_END_SHARE = 0.8

# How many earlier commits an estimate looks at. Each costs two or three GitHub requests.
ESTIMATE_COMMITS = 3

# An estimate is kept this long, per clone, for the life of the process.
ESTIMATE_CACHE_SECONDS = 3600.0

# A wait pauses until the rate limit resets when fewer requests than this are left.
RATE_LIMIT_RESERVE = 50

_estimates: dict[str, tuple[float, float | None]] = {}


def reset_build_estimate_cache() -> None:
    """Forgets the build estimates. Tests use this, so each test estimates again."""
    _estimates.clear()


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _finished_build_span(repo: Path, sha: str) -> float | None:
    """How long the finished build of sha took, in seconds, or None when it is not known.

    The span runs from the first check run start or commit status to the last check run end or
    commit status. It is None when sha has no build, or when any job has not finished.
    """
    starts: list[datetime] = []
    ends: list[datetime] = []
    for check in _read_all_pages(repo, sha, "check-runs", "check_runs", CHECK_RUNS_READ):
        started = _timestamp(check.get("started_at"))
        completed = _timestamp(check.get("completed_at"))
        if check.get("status") != "completed" or started is None or completed is None:
            return None
        starts.append(started)
        ends.append(completed)

    data = _gh_json(repo, "api", f"repos/{{owner}}/{{repo}}/commits/{sha}/statuses?per_page=100")
    entries = cast(list[object], data) if isinstance(data, list) else []
    latest_state: dict[str, object] = {}
    # GitHub lists the statuses of a commit newest first.
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        status = cast(dict[str, object], entry)
        created = _timestamp(status.get("created_at"))
        if created is None:
            continue
        latest_state.setdefault(_text(status, "context"), status.get("state"))
        starts.append(created)
        ends.append(created)
    if "pending" in latest_state.values() or not starts:
        return None
    return (max(ends) - min(starts)).total_seconds()


def estimate_build_seconds(repo: Path, sha: str) -> float | None:
    """How long a build usually takes, from the finished build of an earlier commit of sha.

    It looks at up to ESTIMATE_COMMITS first parents of sha and uses the first one with a
    finished build. A commit GitHub does not know is skipped. The result is cached per clone.
    """
    key = str(repo.resolve())
    cached = _estimates.get(key)
    if cached is not None and time.monotonic() - cached[0] < ESTIMATE_CACHE_SECONDS:
        return cached[1]

    parents = git(
        repo, "rev-list", "--first-parent", f"--max-count={ESTIMATE_COMMITS + 1}", sha, check=False
    ).stdout.split()[1:]
    estimate: float | None = None
    for parent in parents:
        try:
            estimate = _finished_build_span(repo, parent)
        except OperationError:
            continue
        if estimate is not None:
            break
    _estimates[key] = (time.monotonic(), estimate)
    return estimate


def poll_delay(
    elapsed: float, estimate: float | None, poll_seconds: float, build_seen: bool
) -> float:
    """How long to wait before the next poll, so a wait asks GitHub as little as it can.

    Until a build appears, it polls every poll_seconds, so the grace period works. With an
    estimate, it waits until EXPECTED_END_SHARE of it has passed, then polls every twentieth of
    it. Without one, the pause grows with the time already waited. Between polls, it waits at
    least poll_seconds and at most MAX_POLL_SECONDS, except for the one wait for the expected end.
    """
    if not build_seen:
        return poll_seconds
    if estimate is not None:
        expected_end = estimate * EXPECTED_END_SHARE
        if elapsed < expected_end:
            return max(poll_seconds, expected_end - elapsed)
        return min(MAX_POLL_SECONDS, max(poll_seconds, estimate / 20))
    return min(MAX_POLL_SECONDS, max(poll_seconds, elapsed / 10))


def rate_limit_wait(repo: Path) -> float | None:
    """Seconds until GitHub's rate limit resets, when few requests are left, else 0.

    Reading the rate limit does not count against it. Returns None when it cannot be read, for
    example on a GitHub Enterprise Server without a rate limit.
    """
    try:
        data = _gh_json(repo, "api", "rate_limit")
    except OperationError:
        return None
    resources = cast(dict[str, object], data).get("resources") if isinstance(data, dict) else None
    core = cast(dict[str, object], resources).get("core") if isinstance(resources, dict) else None
    if not isinstance(core, dict):
        return None
    core_limit = cast(dict[str, object], core)
    remaining = core_limit.get("remaining")
    reset = core_limit.get("reset")
    if not isinstance(remaining, int) or not isinstance(reset, int):
        return None
    if remaining >= RATE_LIMIT_RESERVE:
        return 0.0
    return max(0.0, reset - time.time()) + 1


def wait_for_build(
    repo: Path,
    sha: str,
    log: LogFn,
    cancel: threading.Event,
    timing: BuildTiming,
    on_update: Callable[[BuildState], None] | None = None,
) -> BuildState:
    """Waits until the build of sha has ended, and returns its last state (R21).

    The wait ends with success when every job succeeded, with failure at the first failed job,
    with none when no job has reported after the grace period, with timeout after the timeout,
    and with cancelled when cancel is set. It never stops the build on GitHub.

    on_update is called with every new state read from GitHub, so the dashboard can show it.
    It is not called with cancelled or timeout, so the dashboard keeps the last state it saw.

    A poll that fails is logged and tried again at the next poll. A poll where only one of the
    two reads failed is a failed poll too, and its partial result is not used (D31). After
    MAX_FAILED_POLLS failed polls in a row, this raises OperationError.

    To stay far below GitHub's rate limit, it polls as described in poll_delay, with an estimate
    from an earlier build, and it pauses when the rate limit is nearly used up.
    """
    started = time.monotonic()
    short = sha[:10]
    log(f"Waiting for the build of commit {short}.")
    last: BuildState | None = None
    failed_polls = 0
    estimate: float | None = None
    estimated = False
    check_rate_limit = True

    while True:
        if check_rate_limit:
            pause = rate_limit_wait(repo)
            if pause is None:
                check_rate_limit = False
            elif pause > 0:
                remaining_time = timing.timeout_seconds - (time.monotonic() - started)
                log(
                    f"GitHub's rate limit is nearly used up. draupnir waits {pause:.0f} seconds "
                    "for it to reset before it asks again."
                )
                if cancel.wait(max(0.0, min(pause, remaining_time))):
                    log(
                        f"Stopped waiting for the build of commit {short}. The build on GitHub "
                        "keeps running."
                    )
                    return BuildState(sha, CANCELLED, last.items if last is not None else ())

        state: BuildState | None
        try:
            state = build_state(repo, sha)
        except OperationError as exc:
            state = None
            failed_polls += 1
            if failed_polls >= MAX_FAILED_POLLS:
                raise OperationError(
                    f"draupnir stopped waiting for the build of commit {short}, because reading "
                    f"its state from GitHub failed {failed_polls} times in a row. The build on "
                    "GitHub keeps running. Check that gh works, then ask for the build status "
                    f"again.\n{exc}"
                ) from exc
            log(
                f"Reading the build state failed ({failed_polls} of {MAX_FAILED_POLLS} failed "
                "reads in a row). draupnir tries again at the next poll."
            )
            for line in str(exc).splitlines():
                log(line)
        else:
            failed_polls = 0
            _log_changes(log, last, state)
            if state != last and on_update is not None:
                on_update(state)
            last = state
            if state.state in (SUCCESS, FAILURE):
                log(f"The build of commit {short} ended: {state.summary()}.")
                return state

        build_seen = last is not None and last.state != NONE
        if build_seen and not estimated:
            estimated = True
            estimate = estimate_build_seconds(repo, sha)
            if estimate is not None:
                log(f"Earlier builds took about {estimate / 60:.0f} minutes.")

        items = last.items if last is not None else ()
        elapsed = time.monotonic() - started
        if state is not None and state.state == NONE and elapsed >= timing.grace_seconds:
            log(
                f"No build has reported for commit {short} within "
                f"{_seconds(timing.grace_seconds)} seconds."
            )
            return state
        if elapsed >= timing.timeout_seconds:
            log(
                f"draupnir stopped waiting for the build of commit {short}, because it took "
                f"longer than {_seconds(timing.timeout_seconds)} seconds. The build on GitHub "
                "keeps running."
            )
            return BuildState(sha, TIMEOUT, items)
        delay = poll_delay(elapsed, estimate, timing.poll_seconds, build_seen)
        delay = min(delay, timing.timeout_seconds - elapsed)
        if delay >= 60:
            log(f"draupnir asks GitHub again in {delay:.0f} seconds.")
        if cancel.wait(delay):
            log(
                f"Stopped waiting for the build of commit {short}. The build on GitHub keeps "
                "running."
            )
            return BuildState(sha, CANCELLED, items)


def _log_changes(log: LogFn, last: BuildState | None, state: BuildState) -> None:
    """Logs every job whose state is new or changed since the last poll."""
    before = {item.name: item.state for item in last.items} if last is not None else {}
    for item in state.items:
        if before.get(item.name) == item.state:
            continue
        line = f"  {item.name}: {item.state}"
        if item.state == FAILURE and item.url:
            line += f" {item.url}"
        log(line)


# ----- Pull requests ---------------------------------------------------------------------

# The states of a pull request, as gh pr view gives them.
PR_OPEN = "OPEN"
PR_MERGED = "MERGED"
PR_CLOSED = "CLOSED"

# The merge states in which GitHub merges a pull request at once. gh treats these three the
# same way when it decides between a merge and auto-merge.
MERGEABLE_NOW = frozenset({"CLEAN", "UNSTABLE", "HAS_HOOKS"})

_PR_LIST_FIELDS = ("number", "url", "headRefOid", "baseRefName")
_PR_VIEW_FIELDS = (
    "state",
    "isDraft",
    "mergeStateStatus",
    "reviewDecision",
    "headRefOid",
    "baseRefName",
    "autoMergeRequest",
)


@dataclass(frozen=True)
class PullRequest:
    """An open pull request: its number, its web address, its head commit and its base branch."""

    number: int
    url: str
    head_sha: str
    base: str


@dataclass(frozen=True)
class PrMergeState:
    """What gh pr view says about whether a pull request can be merged.

    state is OPEN, CLOSED or MERGED. merge_state_status is GitHub's mergeStateStatus, for
    example CLEAN, BLOCKED, BEHIND, DIRTY, DRAFT or UNKNOWN. review_decision is empty when the
    repository does not ask for a review.
    """

    state: str
    is_draft: bool
    merge_state_status: str
    review_decision: str
    head_sha: str
    base: str
    auto_merge: bool


def gh_version(repo: Path) -> str:
    """The first line of `gh --version`, to show in a message. It is never parsed."""
    try:
        result = run(["gh", "--version"], repo, check=False, timeout=GH_TIMEOUT_SECONDS)
    except CommandError:
        return "an unknown version"
    lines = result.stdout.strip().splitlines()
    return lines[0].strip() if result.code == 0 and lines else "an unknown version"


def _gh_read(repo: Path, what: str, *args: str) -> object:
    """Runs a gh call that reads JSON, and explains a failed call in a message."""
    try:
        return _gh_json(repo, *args)
    except CommandError as exc:
        raise OperationError(
            f"draupnir could not read {what} from GitHub. Check that gh works in {repo.name}, "
            f"and try again.\n{exc}"
        ) from exc


def _json_object(repo: Path, data: object, command: str) -> dict[str, object]:
    if not isinstance(data, dict):
        raise OperationError(
            f"gh {command} did not answer with a JSON object. Check that gh "
            f"({gh_version(repo)}) talks to a GitHub server, and try again."
        )
    return cast(dict[str, object], data)


def _field(repo: Path, obj: dict[str, object], key: str, command: str) -> object:
    """obj[key]. A missing field stops with a message that names the field and the gh version."""
    if key not in obj:
        raise OperationError(
            f"gh {command} answered without the field '{key}'. This gh ({gh_version(repo)}) "
            "may be too old for draupnir. Update gh and try again."
        )
    return obj[key]


def _wrong_field(repo: Path, key: str, command: str, expected: str) -> OperationError:
    return OperationError(
        f"gh {command} answered with a field '{key}' that is not {expected}. This gh "
        f"({gh_version(repo)}) may not fit draupnir. Update gh and try again."
    )


def _str_field(repo: Path, obj: dict[str, object], key: str, command: str) -> str:
    value = _field(repo, obj, key, command)
    if not isinstance(value, str):
        raise _wrong_field(repo, key, command, "a text")
    return value


def _bool_field(repo: Path, obj: dict[str, object], key: str, command: str) -> bool:
    value = _field(repo, obj, key, command)
    if not isinstance(value, bool):
        raise _wrong_field(repo, key, command, "true or false")
    return value


def _int_field(repo: Path, obj: dict[str, object], key: str, command: str) -> int:
    value = _field(repo, obj, key, command)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _wrong_field(repo, key, command, "a number")
    return value


def rebase_merge_allowed(repo: Path) -> bool:
    """True when the repository on GitHub allows "rebase and merge" for pull requests."""
    command = "repo view --json rebaseMergeAllowed"
    data = _gh_read(
        repo, "the merge settings of the repository", "repo", "view", "--json", "rebaseMergeAllowed"
    )
    return _bool_field(repo, _json_object(repo, data, command), "rebaseMergeAllowed", command)


def auto_merge_allowed(repo: Path) -> bool | None:
    """Reads allow_auto_merge of the repository. None when GitHub leaves the field out.

    GitHub only sends this field to users with admin rights on the repository.
    """
    command = "api repos/{owner}/{repo}"
    data = _gh_read(repo, "the settings of the repository", "api", "repos/{owner}/{repo}")
    value = _json_object(repo, data, command).get("allow_auto_merge")
    return value if isinstance(value, bool) else None


def find_open_pr(repo: Path, branch: str, base: str) -> PullRequest | None:
    """The open pull request from branch into base, or None when there is none.

    GitHub allows one open pull request per head and base branch. A pull request from branch
    into another base, for example a release branch, is never returned. When gh lists one
    anyway, this raises, so the flow never reuses or merges it.
    """
    command = "pr list"
    data = _gh_read(
        repo,
        f"the open pull requests of {branch}",
        "pr",
        "list",
        "--head",
        branch,
        "--base",
        base,
        "--state",
        "open",
        "--json",
        ",".join(_PR_LIST_FIELDS),
    )
    if not isinstance(data, list):
        raise OperationError(
            f"gh {command} did not answer with a JSON list. Check that gh ({gh_version(repo)}) "
            "talks to a GitHub server, and try again."
        )
    prs: list[PullRequest] = []
    for entry in cast(list[object], data):
        obj = _json_object(repo, entry, command)
        prs.append(
            PullRequest(
                number=_int_field(repo, obj, "number", command),
                url=_str_field(repo, obj, "url", command),
                head_sha=_str_field(repo, obj, "headRefOid", command),
                base=_str_field(repo, obj, "baseRefName", command),
            )
        )
    for pr in prs:
        if pr.base != base:
            raise OperationError(
                f"gh listed pull request #{pr.number} of {branch} into {pr.base}, but draupnir "
                f"asked only for pull requests into {base}. draupnir does not reuse or merge "
                f"it. Check the pull request at {pr.url}, update gh, and try again."
            )
    return prs[0] if prs else None


def create_pr(repo: Path, branch: str, base: str, log: LogFn) -> PullRequest:
    """Creates a pull request from branch into base, with the title and text of its commits."""
    try:
        run(
            ["gh", "pr", "create", "--base", base, "--head", branch, "--fill"],
            repo,
            log=log,
            timeout=GH_TIMEOUT_SECONDS,
        )
    except CommandError as exc:
        raise OperationError(
            f"gh could not create a pull request for {branch} into {base}. See the gh output "
            f"below, fix the problem, and run the pull request flow again.\n{exc}"
        ) from exc
    pr = find_open_pr(repo, branch, base)
    if pr is None:
        raise OperationError(
            f"gh created a pull request for {branch} into {base}, but draupnir cannot find it "
            "among the open pull requests. Look for it on GitHub, and run the pull request flow "
            "again."
        )
    return pr


def pr_merge_state(repo: Path, number: int) -> PrMergeState:
    """Reads the state of pull request number that decides whether and how it can be merged."""
    fields = ",".join(_PR_VIEW_FIELDS)
    command = f"pr view --json {fields}"
    data = _gh_read(repo, f"pull request #{number}", "pr", "view", str(number), "--json", fields)
    obj = _json_object(repo, data, command)
    review = _field(repo, obj, "reviewDecision", command)
    if review is not None and not isinstance(review, str):
        raise _wrong_field(repo, "reviewDecision", command, "a text")
    return PrMergeState(
        state=_str_field(repo, obj, "state", command),
        is_draft=_bool_field(repo, obj, "isDraft", command),
        merge_state_status=_str_field(repo, obj, "mergeStateStatus", command),
        review_decision=review or "",
        head_sha=_str_field(repo, obj, "headRefOid", command),
        base=_str_field(repo, obj, "baseRefName", command),
        auto_merge=_field(repo, obj, "autoMergeRequest", command) is not None,
    )


def merge_pr(repo: Path, number: int, sha: str, log: LogFn) -> None:
    """Merges pull request number with rebase, only when its head is still sha.

    Raises CommandError, with the gh output, when GitHub does not merge it.
    """
    run(
        ["gh", "pr", "merge", str(number), "--rebase", "--match-head-commit", sha],
        repo,
        log=log,
        timeout=GH_TIMEOUT_SECONDS,
    )


def enable_auto_merge(repo: Path, number: int, sha: str, log: LogFn) -> None:
    """Turns on auto-merge with rebase for pull request number, only when its head is still sha.

    GitHub then merges the pull request once its requirements, such as a review, are met. When
    they are met already, gh merges it at once. Raises CommandError, with the gh output, when
    gh fails.
    """
    run(
        ["gh", "pr", "merge", str(number), "--rebase", "--auto", "--match-head-commit", sha],
        repo,
        log=log,
        timeout=GH_TIMEOUT_SECONDS,
    )


def build_result_message(name: str, state: BuildState) -> str:
    """Sentences about a build result, for the end of a command or an action.

    name is the clone. For a failed build, every failed job follows on its own line.
    """
    short = state.sha[:10]
    if state.state == SUCCESS:
        return f"The build of {name} at {short} is green."
    if state.state == FAILURE:
        lines = [
            f"The build of {name} at {short} failed. Open the failed jobs, fix the problem, "
            "and push again."
        ]
        lines += [f"Failed: {item.name} {item.url}".rstrip() for item in state.failed_items()]
        return "\n".join(lines)
    if state.state == PENDING:
        return (
            f"The build of {name} at {short} is still running. Ask for the build status again "
            "later."
        )
    if state.state == NONE:
        return (
            f"No build has reported for {name} at {short}. Check that GitHub runs a build for "
            "this branch, and ask for the build status again later."
        )
    if state.state == TIMEOUT:
        return (
            f"The build of {name} at {short} did not end before draupnir stopped waiting. It "
            "keeps running on GitHub. Ask for the build status again later."
        )
    return (
        f"draupnir stopped waiting for the build of {name} at {short}. The build keeps running "
        "on GitHub."
    )
