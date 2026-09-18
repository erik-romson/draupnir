"""A stand-in for the gh command line tool, for the tests. It never reaches a network.

conftest.py puts a small `gh` script on PATH that runs this file with the Python that runs the
tests. The fake keeps its state in the JSON file named by the FAKE_GH_STATE environment
variable. Tests change that state, and read the call log, through `FakeGh`.

The fake knows these calls:

- `gh --version`
- `gh auth status --hostname <host>`
- `gh api repos/{owner}/{repo}/commits/<sha>/check-runs?per_page=<n>&page=<n>`
- `gh api repos/{owner}/{repo}/commits/<sha>/status?per_page=<n>&page=<n>`
- `gh api repos/{owner}/{repo}/commits/<sha>/statuses?per_page=<n>`, a list of every status
- `gh api rate_limit`
- `gh api repos/{owner}/{repo}`, with `allow_auto_merge`
- `gh repo view --json rebaseMergeAllowed`
- `gh pr list --head <branch> --base <branch> --state open --json <fields>`
- `gh pr create --base <branch> --head <branch> --fill`
- `gh pr view <number> --json <fields>`
- `gh pr merge <number> --rebase [--auto] --match-head-commit <sha>`
- `gh event <name>`, which is not a gh command. A hook in the bare repository runs it, so a
  push shows up in the call log next to the gh calls, in the order they happened.

Like the real gh, the api, repo and pr calls must run inside a git clone. The fake finds the
bare repository that plays GitHub through the clone's origin. Anything else exits with code 1
and a message on standard error. Every call is recorded in the call log, with its arguments
and its working folder, before the fake answers it.

A build is a list of polls per commit. The n-th read of an endpoint for a commit answers with
the n-th poll, and the last poll answers every read after it. A poll can make one or both
endpoints fail, and `FakeGh.set_failing` makes an endpoint fail for every commit.

A pull request has a list of merge states. The n-th `pr view` answers with the n-th merge
state, and the last one answers every view after it. The head commit of an open pull request is
read from the bare repository, so a push to its branch changes it, as on GitHub. A merge does
what GitHub's "rebase and merge" does, with `rebase_merge`, and deletes the branch when the
repository is set to do so.

This file runs with `python -I -S`, so it only uses the standard library.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

STATE_VARIABLE = "FAKE_GH_STATE"
# Set for the git commands that the fake runs itself.
_INSIDE_VARIABLE = "FAKE_GH_INSIDE"

CHECK_RUNS = "check-runs"
STATUS = "status"
STATUSES = "statuses"
ENDPOINTS = (CHECK_RUNS, STATUS, STATUSES)

FAKE_VERSION = "gh version 2.92.0 (fake)"

OPEN = "OPEN"
MERGED = "MERGED"
CLOSED = "CLOSED"

# The merge states that GitHub, and gh, merge at once.
_MERGEABLE_NOW = ("CLEAN", "UNSTABLE", "HAS_HOOKS")

# The committer date of commits that a rebase merge writes. It differs from the date of every
# commit a test makes, so a rebase merge always gives new shas.
REBASE_MERGE_DATE = "2030-01-01T12:00:00+00:00"

_API_PATH = re.compile(
    r"^repos/\{owner\}/\{repo\}/commits/(?P<sha>[^/?]+)/(?P<endpoint>check-runs|statuses|status)"
    r"(?:\?(?P<query>.*))?$"
)
_REPO_API_PATH = "repos/{owner}/{repo}"


@dataclass(frozen=True)
class CheckRun:
    """One check run, as GitHub Actions or a GitHub App reports it.

    status is queued, in_progress or completed. conclusion only counts when the check run is
    completed, for example success, failure, neutral, skipped, cancelled or timed_out.
    """

    name: str
    conclusion: str | None = "success"
    status: str = "completed"
    url: str = ""
    started_at: str | None = None
    completed_at: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "conclusion": self.conclusion if self.status == "completed" else None,
            "html_url": self.url or f"https://ci.example.com/check-runs/{self.name}",
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


def running(name: str, url: str = "") -> CheckRun:
    """A check run that has started, but not finished."""
    return CheckRun(name, conclusion=None, status="in_progress", url=url)


@dataclass(frozen=True)
class CommitStatus:
    """One commit status, as a Jenkins-style build server reports it.

    state is pending, success, failure or error.
    """

    context: str
    state: str = "success"
    url: str = ""
    created_at: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "context": self.context,
            "state": self.state,
            "target_url": self.url or f"https://jenkins.example.com/job/{self.context}",
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class Poll:
    """What GitHub answers for one poll of a commit.

    fail names the endpoints that fail in this poll: CHECK_RUNS, STATUS, or both.
    """

    check_runs: Sequence[CheckRun] = ()
    statuses: Sequence[CommitStatus] = ()
    fail: Sequence[str] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "check_runs": [run.to_json() for run in self.check_runs],
            "statuses": [status.to_json() for status in self.statuses],
            "fail": list(self.fail),
        }


@dataclass(frozen=True)
class Call:
    """One call of the fake gh: its arguments and the folder it ran in."""

    args: list[str]
    cwd: Path


@dataclass(frozen=True)
class PullRequestInfo:
    """What the fake knows about one pull request, for the assertions of a test."""

    number: int
    head: str
    base: str
    state: str
    auto_merge: bool
    merged_head_sha: str


def _new_pr_settings(
    merge_states: Sequence[str], review_decision: str, draft: bool
) -> dict[str, Any]:
    assert merge_states, "A pull request needs at least one merge state."
    return {
        "merge_states": list(merge_states),
        "review_decision": review_decision,
        "draft": draft,
    }


def _default_state() -> dict[str, Any]:
    return {
        "logged_in": True,
        "failing": [],
        "builds": {},
        "reads": {},
        "calls": [],
        "repo": {
            "rebase_merge_allowed": True,
            # None leaves the field out, as GitHub does for users without admin rights.
            "allow_auto_merge": True,
            "delete_branch_on_merge": False,
        },
        "new_pr": _new_pr_settings(["CLEAN"], "", False),
        "rate_limit": {"remaining": 5000, "reset": 0},
        "prs": [],
    }


class FakeGh:
    """Reads and changes the state file of the fake gh.

    Every change holds a lock on the state file, so a test can change the state while the
    dashboard runs the fake in a worker thread, and no change gets lost.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def _locked(self) -> Generator[None]:
        lock_path = self.path.with_name(self.path.name + ".lock")
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return _default_state()
        state: dict[str, Any] = json.loads(self.path.read_text())
        return state

    def _save(self, state: dict[str, Any]) -> None:
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(state, indent=2))
        os.replace(temporary, self.path)

    @contextmanager
    def edit(self) -> Generator[dict[str, Any]]:
        """Yields the state, and saves it afterwards, while holding the lock."""
        with self._locked():
            state = self._load()
            yield state
            self._save(state)

    def read(self) -> dict[str, Any]:
        with self._locked():
            return self._load()

    def reset(self) -> None:
        """Starts from a clean state: logged in, no builds, nothing failing, no calls."""
        with self._locked():
            self._save(_default_state())

    def set_logged_in(self, logged_in: bool) -> None:
        with self.edit() as state:
            state["logged_in"] = logged_in

    def set_failing(self, endpoint: str, failing: bool = True) -> None:
        """Makes every read of endpoint fail, or work again."""
        assert endpoint in ENDPOINTS, endpoint
        with self.edit() as state:
            names = {name for name in state["failing"] if name != endpoint}
            if failing:
                names.add(endpoint)
            state["failing"] = sorted(names)

    def set_build(self, sha: str, polls: Sequence[Poll]) -> None:
        """Sets the answers for sha, one poll per read, and starts counting the reads again."""
        with self.edit() as state:
            state["builds"][sha] = [poll.to_json() for poll in polls]
            for endpoint in ENDPOINTS:
                state["reads"].pop(_read_key(endpoint, sha), None)

    def set_rate_limit(self, remaining: int, reset: int) -> None:
        """Sets what gh api rate_limit answers: requests left, and the reset as epoch seconds."""
        with self.edit() as state:
            state["rate_limit"] = {"remaining": remaining, "reset": reset}

    def reads(self, sha: str, endpoint: str = CHECK_RUNS) -> int:
        """How many times the fake answered a read of endpoint for sha."""
        count: int = self.read()["reads"].get(_read_key(endpoint, sha), 0)
        return count

    def calls(self) -> list[Call]:
        return [
            Call(args=[str(arg) for arg in call["args"]], cwd=Path(call["cwd"]))
            for call in self.read()["calls"]
        ]

    # ----- Repository settings ---------------------------------------------------------

    def set_rebase_merge_allowed(self, allowed: bool) -> None:
        with self.edit() as state:
            state["repo"]["rebase_merge_allowed"] = allowed

    def set_allow_auto_merge(self, allowed: bool | None) -> None:
        """Turns auto-merge on or off. None leaves allow_auto_merge out of the api answer."""
        with self.edit() as state:
            state["repo"]["allow_auto_merge"] = allowed

    def set_delete_branch_on_merge(self, delete: bool) -> None:
        with self.edit() as state:
            state["repo"]["delete_branch_on_merge"] = delete

    def record_pushes(self, remote: Path) -> None:
        """Adds a hook to the bare repository that puts every push into the call log."""
        hook = remote / "hooks" / "pre-receive"
        hook.parent.mkdir(parents=True, exist_ok=True)
        # A push by the fake itself, during a merge, is not recorded. The fake holds the lock on
        # the state file then, so recording it would wait forever.
        hook.write_text(
            "#!/bin/sh\n"
            "cat > /dev/null\n"
            f'if [ -n "${_INSIDE_VARIABLE}" ]; then exit 0; fi\n'
            f"{STATE_VARIABLE}={_shell_quote(str(self.path))} exec "
            f"{_shell_quote(sys.executable)} -I -S {_shell_quote(__file__)} event push\n"
        )
        hook.chmod(0o755)

    # ----- Pull requests ---------------------------------------------------------------

    def set_new_pr(
        self,
        merge_states: Sequence[str] = ("CLEAN",),
        review_decision: str = "",
        draft: bool = False,
    ) -> None:
        """Sets what a pull request that `gh pr create` makes looks like."""
        with self.edit() as state:
            state["new_pr"] = _new_pr_settings(merge_states, review_decision, draft)

    def add_pr(
        self,
        head: str,
        base: str,
        merge_states: Sequence[str] = ("CLEAN",),
        review_decision: str = "",
        draft: bool = False,
    ) -> int:
        """Adds an open pull request from head into base, and returns its number."""
        with self.edit() as state:
            pr = _new_pr(state, head, base, _new_pr_settings(merge_states, review_decision, draft))
            return int(pr["number"])

    def set_pr_base(self, number: int, base: str) -> None:
        """Changes the base branch of a pull request, as a user can on GitHub."""
        with self.edit() as state:
            pr = _find_pr(state, number)
            assert pr is not None, number
            pr["base"] = base

    def pull_requests(self) -> list[PullRequestInfo]:
        return [
            PullRequestInfo(
                number=int(pr["number"]),
                head=str(pr["head"]),
                base=str(pr["base"]),
                state=str(pr["state"]),
                auto_merge=bool(pr["auto_merge"]),
                merged_head_sha=str(pr["merged_head_sha"]),
            )
            for pr in self.read()["prs"]
        ]

    def pull_request(self, number: int) -> PullRequestInfo:
        for pr in self.pull_requests():
            if pr.number == number:
                return pr
        raise AssertionError(f"The fake gh has no pull request #{number}.")


def _shell_quote(text: str) -> str:
    return "'" + text.replace("'", "'\"'\"'") + "'"


def _read_key(endpoint: str, sha: str) -> str:
    return f"{endpoint}:{sha}"


# ----- Git on the bare repository ----------------------------------------------------------


def _git(cwd: Path, *args: str, extra_env: dict[str, str] | None = None) -> str:
    env = {**os.environ, _INSIDE_VARIABLE: "1", **(extra_env or {})}
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


def rebase_merge(
    remote: Path, work_parent: Path, branch: str, base: str, *, delete_branch: bool
) -> list[str]:
    """Does on the bare repository what GitHub's "rebase and merge" does.

    GitHub replays every commit of the branch onto base as a new commit with a new committer
    date, also when the branch is already on top of base. So base gets commits with the same
    changes as the branch, but with other shas. With delete_branch, GitHub then deletes the
    branch. Returns the shas of the new commits on base, oldest first.
    """
    helper = Path(tempfile.mkdtemp(prefix="github-rebase-merge-", dir=work_parent))
    try:
        _git(work_parent, "clone", "-q", str(remote), str(helper))
        _git(helper, "checkout", "-q", "-B", branch, f"origin/{branch}")
        _git(
            helper,
            "rebase",
            "-q",
            "--force-rebase",
            f"origin/{base}",
            extra_env={"GIT_COMMITTER_DATE": REBASE_MERGE_DATE},
        )
        new_commits = _git(helper, "rev-list", "--reverse", f"origin/{base}..HEAD").splitlines()
        _git(helper, "push", "-q", "origin", f"HEAD:refs/heads/{base}")
        if delete_branch:
            _git(helper, "push", "-q", "origin", "--delete", branch)
    finally:
        shutil.rmtree(helper, ignore_errors=True)
    return new_commits


def _inside_a_git_clone(folder: Path) -> bool:
    return any((candidate / ".git").exists() for candidate in (folder, *folder.parents))


def _remote_of_cwd() -> Path | None:
    """The bare repository that plays GitHub, found through the origin of the current clone."""
    if not _inside_a_git_clone(Path.cwd()):
        return None
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"], capture_output=True, text=True, check=False
    )
    url = result.stdout.strip()
    if result.returncode != 0 or not url:
        return None
    return Path(url)


def _branch_sha(remote: Path, branch: str) -> str | None:
    ref = f"refs/heads/{branch}"
    result = subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", "--verify", "--quiet", ref],
        capture_output=True,
        text=True,
        check=False,
    )
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and sha else None


# ----- Answers --------------------------------------------------------------------------


def _fail(message: str) -> int:
    print(f"fake gh: {message}", file=sys.stderr)
    return 1


def _options(
    args: Sequence[str], with_value: Sequence[str], flags: Sequence[str]
) -> tuple[list[str], dict[str, str], set[str]] | None:
    """Splits args into positional arguments, options with a value, and flags."""
    positional: list[str] = []
    values: dict[str, str] = {}
    seen: set[str] = set()
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in with_value and index + 1 < len(args):
            values[arg] = args[index + 1]
            index += 2
            continue
        if arg in flags:
            seen.add(arg)
        elif arg.startswith("-"):
            return None
        else:
            positional.append(arg)
        index += 1
    return positional, values, seen


def _api(state: dict[str, Any], path: str) -> int:
    if path == _REPO_API_PATH:
        return _repo_api(state)
    if path == "rate_limit":
        print(json.dumps({"resources": {"core": {"limit": 5000, **state["rate_limit"]}}}))
        return 0
    match = _API_PATH.match(path)
    if match is None:
        return _fail(f"the api path {path} is not supported.")
    if not _inside_a_git_clone(Path.cwd()):
        return _fail("{owner}/{repo} needs a git clone as the working folder.")

    sha = match.group("sha")
    endpoint = match.group("endpoint")
    query = parse_qs(match.group("query") or "")
    per_page = int(query.get("per_page", ["30"])[0])
    page = int(query.get("page", ["1"])[0])

    # Only the first page counts as a read. Later pages answer from the same poll.
    key = _read_key(endpoint, sha)
    count: int = state["reads"].get(key, 0)
    if page == 1:
        count += 1
        state["reads"][key] = count

    polls: list[dict[str, Any]] = state["builds"].get(sha, [])
    poll: dict[str, Any] = (
        polls[min(count - 1, len(polls) - 1)]
        if polls
        else {"check_runs": [], "statuses": [], "fail": []}
    )

    if endpoint in state["failing"] or endpoint in poll["fail"]:
        print(json.dumps({"message": "Server Error", "status": "500"}))
        print("gh: Server Error (HTTP 500)", file=sys.stderr)
        return 1

    start = (page - 1) * per_page
    if endpoint == STATUSES:
        # GitHub lists every status of a commit, newest first.
        print(json.dumps(list(reversed(poll["statuses"]))[start : start + per_page]))
        return 0
    if endpoint == CHECK_RUNS:
        runs: list[dict[str, Any]] = poll["check_runs"]
        page_runs = runs[start : start + per_page]
        print(json.dumps({"total_count": len(runs), "check_runs": page_runs}))
        return 0

    statuses: list[dict[str, Any]] = poll["statuses"]
    states = {status["state"] for status in statuses}
    if states & {"failure", "error"}:
        combined = "failure"
    elif not statuses or "pending" in states:
        combined = "pending"
    else:
        combined = "success"
    print(
        json.dumps(
            {
                "state": combined,
                "sha": sha,
                "total_count": len(statuses),
                "statuses": statuses[start : start + per_page],
            }
        )
    )
    return 0


def _repo_api(state: dict[str, Any]) -> int:
    if not _inside_a_git_clone(Path.cwd()):
        return _fail("{owner}/{repo} needs a git clone as the working folder.")
    repo: dict[str, Any] = state["repo"]
    answer: dict[str, Any] = {
        "full_name": "acme/shop",
        "allow_rebase_merge": repo["rebase_merge_allowed"],
        "delete_branch_on_merge": repo["delete_branch_on_merge"],
    }
    if repo["allow_auto_merge"] is not None:
        answer["allow_auto_merge"] = repo["allow_auto_merge"]
    print(json.dumps(answer))
    return 0


def _auth_status(state: dict[str, Any], args: list[str]) -> int:
    if len(args) != 2 or args[0] != "--hostname":
        return _fail("auth status needs exactly --hostname <host>.")
    host = args[1]
    if state["logged_in"]:
        print(f"{host}\n  Logged in to {host} (fake gh)")
        return 0
    print(f"You are not logged into {host}. Run gh auth login to authenticate.", file=sys.stderr)
    return 1


def _json_fields(values: dict[str, str], known: Sequence[str]) -> list[str] | None:
    fields = [field for field in values.get("--json", "").split(",") if field]
    if not fields or any(field not in known for field in fields):
        return None
    return fields


def _repo_view(state: dict[str, Any], args: list[str]) -> int:
    parsed = _options(args, ["--json"], [])
    if parsed is None or parsed[0]:
        return _fail(f"repo view {' '.join(args)} is not supported.")
    fields = _json_fields(parsed[1], ["rebaseMergeAllowed"])
    if fields is None:
        return _fail("repo view needs --json rebaseMergeAllowed.")
    if _remote_of_cwd() is None:
        return _fail("repo view needs a git clone with an origin as the working folder.")
    print(json.dumps({"rebaseMergeAllowed": state["repo"]["rebase_merge_allowed"]}))
    return 0


def _find_pr(state: dict[str, Any], number: int) -> dict[str, Any] | None:
    for pr in state["prs"]:
        if pr["number"] == number:
            found: dict[str, Any] = pr
            return found
    return None


def _new_pr(
    state: dict[str, Any], head: str, base: str, settings: dict[str, Any]
) -> dict[str, Any]:
    number = 1 + max((int(pr["number"]) for pr in state["prs"]), default=0)
    pr: dict[str, Any] = {
        "number": number,
        "url": f"https://github.example.com/acme/shop/pull/{number}",
        "head": head,
        "base": base,
        "state": OPEN,
        "draft": settings["draft"],
        "merge_states": list(settings["merge_states"]),
        "views": 0,
        "review_decision": settings["review_decision"],
        "auto_merge": False,
        "merged_head_sha": "",
    }
    state["prs"].append(pr)
    return pr


def _head_sha(pr: dict[str, Any], remote: Path) -> str:
    if pr["state"] != OPEN:
        return str(pr["merged_head_sha"])
    return _branch_sha(remote, str(pr["head"])) or ""


def _current_merge_state(pr: dict[str, Any]) -> str:
    """The merge state of the last `pr view`, or the first one before any view."""
    states: list[str] = pr["merge_states"]
    return states[min(max(int(pr["views"]) - 1, 0), len(states) - 1)]


def _pr_json(pr: dict[str, Any], remote: Path, fields: Sequence[str], view: bool) -> dict[str, Any]:
    states: list[str] = pr["merge_states"]
    merge_state = states[min(int(pr["views"]), len(states) - 1)] if view else ""
    values: dict[str, Any] = {
        "number": pr["number"],
        "url": pr["url"],
        "headRefName": pr["head"],
        "headRefOid": _head_sha(pr, remote),
        "baseRefName": pr["base"],
        "state": pr["state"],
        "isDraft": pr["draft"],
        "mergeStateStatus": merge_state,
        "reviewDecision": pr["review_decision"],
        "autoMergeRequest": {"mergeMethod": "REBASE"} if pr["auto_merge"] else None,
    }
    return {field: values[field] for field in fields}


_PR_FIELDS = (
    "number",
    "url",
    "headRefName",
    "headRefOid",
    "baseRefName",
    "state",
    "isDraft",
    "mergeStateStatus",
    "reviewDecision",
    "autoMergeRequest",
)


def _pr_list(state: dict[str, Any], args: list[str]) -> int:
    parsed = _options(args, ["--head", "--base", "--state", "--json", "--limit"], [])
    if parsed is None or parsed[0]:
        return _fail(f"pr list {' '.join(args)} is not supported.")
    _, values, _ = parsed
    fields = _json_fields(values, [field for field in _PR_FIELDS if field != "mergeStateStatus"])
    if fields is None:
        return _fail("pr list needs --json with known fields.")
    remote = _remote_of_cwd()
    if remote is None:
        return _fail("pr list needs a git clone with an origin as the working folder.")
    wanted_state = values.get("--state", "open").upper()
    found = [
        _pr_json(pr, remote, fields, view=False)
        for pr in state["prs"]
        if ("--head" not in values or pr["head"] == values["--head"])
        and ("--base" not in values or pr["base"] == values["--base"])
        and (wanted_state == "ALL" or pr["state"] == wanted_state)
    ]
    print(json.dumps(found))
    return 0


def _pr_create(state: dict[str, Any], args: list[str]) -> int:
    parsed = _options(args, ["--base", "--head"], ["--fill"])
    if parsed is None or parsed[0] or "--fill" not in parsed[2]:
        return _fail("pr create needs --base <branch> --head <branch> --fill.")
    _, values, _ = parsed
    if "--base" not in values or "--head" not in values:
        return _fail("pr create needs --base <branch> --head <branch> --fill.")
    remote = _remote_of_cwd()
    if remote is None:
        return _fail("pr create needs a git clone with an origin as the working folder.")
    head = values["--head"]
    base = values["--base"]
    if _branch_sha(remote, head) is None:
        return _fail(f"the branch {head} is not on GitHub.")
    for pr in state["prs"]:
        if pr["head"] == head and pr["base"] == base and pr["state"] == OPEN:
            return _fail(f"a pull request for {head} into {base} already exists.")
    pr = _new_pr(state, head, base, state["new_pr"])
    print(pr["url"])
    return 0


def _pr_number(positional: list[str], state: dict[str, Any]) -> dict[str, Any] | None:
    if len(positional) != 1 or not positional[0].isdigit():
        return None
    return _find_pr(state, int(positional[0]))


def _pr_view(state: dict[str, Any], args: list[str]) -> int:
    parsed = _options(args, ["--json"], [])
    if parsed is None:
        return _fail(f"pr view {' '.join(args)} is not supported.")
    positional, values, _ = parsed
    pr = _pr_number(positional, state)
    if pr is None:
        return _fail("pr view needs the number of a known pull request.")
    fields = _json_fields(values, _PR_FIELDS)
    if fields is None:
        return _fail("pr view needs --json with known fields.")
    remote = _remote_of_cwd()
    if remote is None:
        return _fail("pr view needs a git clone with an origin as the working folder.")
    print(json.dumps(_pr_json(pr, remote, fields, view=True)))
    pr["views"] = int(pr["views"]) + 1
    return 0


def _merge_now(state: dict[str, Any], pr: dict[str, Any], remote: Path) -> int:
    head_sha = _branch_sha(remote, str(pr["head"])) or ""
    try:
        rebase_merge(
            remote,
            Path(os.environ[STATE_VARIABLE]).parent,
            str(pr["head"]),
            str(pr["base"]),
            delete_branch=bool(state["repo"]["delete_branch_on_merge"]),
        )
    except subprocess.CalledProcessError as exc:
        return _fail(f"the rebase merge failed: {exc.stderr}")
    pr["state"] = MERGED
    pr["auto_merge"] = False
    pr["merged_head_sha"] = head_sha
    print(f"Rebased and merged pull request #{pr['number']} (fake gh)")
    return 0


def _pr_merge(state: dict[str, Any], args: list[str]) -> int:
    parsed = _options(args, ["--match-head-commit"], ["--rebase", "--auto"])
    if parsed is None:
        return _fail(f"pr merge {' '.join(args)} is not supported.")
    positional, values, flags = parsed
    pr = _pr_number(positional, state)
    if pr is None:
        return _fail("pr merge needs the number of a known pull request.")
    if "--rebase" not in flags:
        return _fail("the fake only merges with --rebase.")
    remote = _remote_of_cwd()
    if remote is None:
        return _fail("pr merge needs a git clone with an origin as the working folder.")
    if pr["state"] != OPEN:
        return _fail(f"pull request #{pr['number']} is not open.")
    if not state["repo"]["rebase_merge_allowed"]:
        return _fail("rebase merges are not allowed on this repository.")
    expected = values.get("--match-head-commit")
    if expected is not None and expected != _branch_sha(remote, str(pr["head"])):
        return _fail(f"the head of pull request #{pr['number']} is not {expected}.")

    merge_state = _current_merge_state(pr)
    if merge_state in _MERGEABLE_NOW and not pr["draft"]:
        return _merge_now(state, pr, remote)
    if "--auto" not in flags:
        return _fail(f"pull request #{pr['number']} is not mergeable: {merge_state}.")
    if state["repo"]["allow_auto_merge"] is False:
        return _fail("auto-merge is not allowed for this repository.")
    pr["auto_merge"] = True
    print(f"Pull request #{pr['number']} will be automatically merged via rebase (fake gh)")
    return 0


def main(argv: Sequence[str]) -> int:
    state_file = os.environ.get(STATE_VARIABLE)
    if not state_file:
        return _fail(f"{STATE_VARIABLE} is not set.")
    args = list(argv)
    with FakeGh(Path(state_file)).edit() as state:
        state["calls"].append({"args": args, "cwd": os.getcwd()})
        if args == ["--version"]:
            print(FAKE_VERSION)
            return 0
        if args[:1] == ["event"]:
            return 0
        if len(args) == 2 and args[0] == "api":
            return _api(state, args[1])
        if args[:2] == ["auth", "status"]:
            return _auth_status(state, args[2:])
        if args[:2] == ["repo", "view"]:
            return _repo_view(state, args[2:])
        pr_calls = {
            "list": _pr_list,
            "create": _pr_create,
            "view": _pr_view,
            "merge": _pr_merge,
        }
        if len(args) >= 2 and args[0] == "pr" and args[1] in pr_calls:
            return pr_calls[args[1]](state, args[2:])
        return _fail(f"the call gh {' '.join(args)} is not supported.")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
