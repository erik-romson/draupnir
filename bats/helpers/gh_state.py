"""Drives the fake gh from the BATS tests.

The pytest suite drives the same fake through `FakeGh` in `tests/fake_gh.py`. The BATS tests
need it from the shell, so this file wraps the calls they use. The helper `gh_state` in
`workspace.bash` runs it with `tests/` on PYTHONPATH.

Commands:

- `reset`: a clean state, logged in, with no builds and no pull requests.
- `build <sha> <result>`: what GitHub answers for that commit, where result is success,
  failure, pending or none.
- `pull-requests`: one line per pull request, with its number, head, base and state.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from fake_gh import CheckRun, FakeGh, Poll, running

# The answers of one poll, per build result a test asks for. The last poll answers every read
# after it, so one poll is enough for a result that does not change.
_POLLS = {
    "success": [Poll(check_runs=[CheckRun("build")])],
    "failure": [Poll(check_runs=[CheckRun("build", conclusion="failure")])],
    "pending": [Poll(check_runs=[running("build")])],
    "none": [Poll()],
}

_USAGE = "Give one of: reset, build <sha> <result>, pull-requests."


def _fake() -> FakeGh:
    path = os.environ.get("FAKE_GH_STATE")
    if not path:
        raise SystemExit("gh_state: FAKE_GH_STATE is not set.")
    return FakeGh(Path(path))


def main(argv: list[str]) -> int:
    if not argv:
        raise SystemExit(f"gh_state: {_USAGE}")
    command, arguments = argv[0], argv[1:]
    fake = _fake()

    if command == "reset" and not arguments:
        fake.reset()
        return 0
    if command == "build" and len(arguments) == 2:
        sha, result = arguments
        if result not in _POLLS:
            raise SystemExit(
                f"gh_state: '{result}' is not a build result. Give one of: {', '.join(_POLLS)}."
            )
        fake.set_build(sha, _POLLS[result])
        return 0
    if command == "pull-requests" and not arguments:
        for pull_request in fake.pull_requests():
            print(
                f"{pull_request.number} {pull_request.head} "
                f"{pull_request.base} {pull_request.state}"
            )
        return 0
    raise SystemExit(f"gh_state: '{' '.join(argv)}' is not a known call. {_USAGE}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
