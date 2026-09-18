"""The draupnir command line: argparse subcommands and confirmations."""

from __future__ import annotations

import argparse
import os
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from draupnir import __version__
from draupnir.errors import OperationError, RebaseConflict
from draupnir.github import SUCCESS, build_result_message
from draupnir.ide import open_ide
from draupnir.operations import (
    MERGED,
    check_build,
    fast_forward_main,
    fetch_all,
    pr_build_and_merge,
    push,
    push_and_follow,
    rebase_on_main,
    require_feature_branch,
    require_ready,
)
from draupnir.runner import require_git
from draupnir.setup import check_removable, init, new_clone, prepare, remove_clone
from draupnir.status import ProjectStatus, read_status
from draupnir.workspace import Workspace

# The command ran, but the build is not green: failure, none, pending or timeout (D24).
EXIT_BUILD_NOT_GREEN = 3
# The command was stopped with Ctrl+C.
EXIT_INTERRUPTED = 130


def _log(line: str) -> None:
    print(line)


def confirm(question: str, yes: bool) -> bool:
    """Ask a yes/no question on the terminal, unless `yes` is already true.

    Returns true without asking when `yes` is true. Raises `OperationError`
    when standard input is not a terminal, because the question could not
    be answered.
    """
    if yes:
        return True
    if not sys.stdin.isatty():
        raise OperationError(
            f'draupnir cannot ask "{question}", because standard input is not a terminal. '
            "Run the command again with --yes to answer yes without the question."
        )
    answer = input(f"{question} [y/N] ")
    return answer.strip().lower() in ("y", "yes")


def run_ui(args: argparse.Namespace) -> int:
    """Starts the dashboard. Without a subcommand, `main` also calls this.

    The workspace and draupnir.toml are read before the app starts, so a missing workspace or a
    broken config stops with a message on standard error instead of an empty dashboard.
    """
    require_git()
    ws = Workspace.find(Path.cwd())
    # Textual is imported here, so only the dashboard pays for it, and so modules outside
    # draupnir.ui never import it.
    from draupnir.ui.app import DraupnirApp

    app = DraupnirApp(ws, initial_branch=getattr(args, "branch", None))
    app.run()
    return app.return_code or 0


def run_init(args: argparse.Namespace) -> int:
    require_git()
    ws = init(Path.cwd(), args.url, _log, main_branch=args.main_branch)
    _log(f"{ws.root} is ready. The main branch is {ws.main_branch()}.")
    return 0


def run_new(args: argparse.Namespace) -> int:
    require_git()
    ws = Workspace.find(Path.cwd())
    folder = new_clone(ws, args.branch, args.base, _log)
    _log(f"{folder} is ready.")
    if args.open:
        command = open_ide(folder, ws.config)
        _log(f"Starting the IDE: {command}")
    return 0


def run_prepare(args: argparse.Namespace) -> int:
    ws = Workspace.find(Path.cwd())
    folder = ws.resolve_folder(args.folder, Path.cwd())
    prepare(ws, folder, _log)
    return 0


def run_remove(args: argparse.Namespace) -> int:
    require_git()
    ws = Workspace.find(Path.cwd())
    folder = check_removable(ws, ws.resolve_folder(args.folder, Path.cwd()))
    if not confirm(f"Delete {folder} with its .m2repo and local files?", args.yes):
        _log(f"Kept the clone {folder}. Nothing was deleted.")
        return 0
    remove_clone(ws, folder, args.force, _log)
    return 0


def _format_status_line(status: ProjectStatus) -> str:
    """One line for `draupnir status`, with the table's columns except Build.

    The command line has no memory of a build result between runs, unlike the dashboard,
    so that column is left out here (Q12).
    """
    if status.error:
        return f"{status.name}: error: {status.error}"

    columns = [status.name, status.branch or "(detached)"]
    columns.append(f"main ↑{status.ahead_main} ↓{status.behind_main}")
    if status.upstream_gone:
        columns.append("deleted on GitHub")
    elif status.upstream is None:
        columns.append("not pushed")
    else:
        columns.append(f"upstream ↑{status.ahead_upstream} ↓{status.behind_upstream}")
    columns.append(f"{status.changed} changed, {status.untracked} untracked")
    if status.operation:
        columns.append(f"{status.operation} in progress")
    return "  ".join(columns)


def run_status(args: argparse.Namespace) -> int:
    ws = Workspace.find(Path.cwd())
    for folder in ws.project_paths():
        print(_format_status_line(read_status(ws, folder)))
    return 0


def run_fetch(args: argparse.Namespace) -> int:
    ws = Workspace.find(Path.cwd())
    fetch_all(ws, _log)
    return 0


def run_rebase(args: argparse.Namespace) -> int:
    ws = Workspace.find(Path.cwd())
    folder = ws.resolve_folder(args.folder, Path.cwd())
    try:
        rebase_on_main(ws, folder, _log)
    except RebaseConflict:
        _log(
            "Resolve them in the IDE (draupnir open), then run git rebase --continue in the clone."
        )
        return 1
    return 0


def run_push(args: argparse.Namespace) -> int:
    ws = Workspace.find(Path.cwd())
    folder = ws.resolve_folder(args.folder, Path.cwd())
    if not args.follow:
        sha = push(ws, folder, _log)
        _log(f"Pushed {sha[:10]}.")
        return 0

    cancel = threading.Event()
    try:
        state = push_and_follow(ws, folder, _log, cancel)
    except KeyboardInterrupt:
        # The same as "Stop waiting" in the dashboard: the wait ends, the build does not.
        cancel.set()
        print(
            "draupnir stopped, because of Ctrl+C. A build that already started keeps running "
            "on GitHub.",
            file=sys.stderr,
        )
        return EXIT_INTERRUPTED
    _log(build_result_message(folder.name, state))
    return 0 if state.state == SUCCESS else EXIT_BUILD_NOT_GREEN


def run_build(args: argparse.Namespace) -> int:
    ws = Workspace.find(Path.cwd())
    folder = ws.resolve_folder(args.folder, Path.cwd())
    state = check_build(ws, folder, _log)
    _log(build_result_message(folder.name, state))
    return 0 if state.state == SUCCESS else EXIT_BUILD_NOT_GREEN


def run_ff_main(args: argparse.Namespace) -> int:
    require_git()
    ws = Workspace.find(Path.cwd())
    if not ws.config.actions.fast_forward_main:
        raise OperationError(
            "Fast-forward main is turned off in draupnir.toml ([actions] fast-forward-main). "
            "Use draupnir pr instead."
        )
    folder = ws.resolve_folder(args.folder, Path.cwd())
    status = read_status(ws, folder)
    main_branch = ws.main_branch()
    sha = status.head[:10] if status.head else "HEAD"
    build_note = (
        f" The build of {sha} must be green."
        if ws.config.actions.fast_forward_needs_green_build
        else ""
    )
    question = f"Move {main_branch} on GitHub to commit {sha} from {folder.name}?{build_note}"
    if not confirm(question, args.yes):
        _log(f"Did not fast-forward {main_branch}.")
        return 0
    fast_forward_main(ws, folder, _log)
    return 0


def _remove_after_merge(ws: Workspace, folder: Path, args: argparse.Namespace) -> None:
    """Removes or keeps the clone after its pull request was merged.

    --remove removes it without a question, --keep keeps it. Without either, the command asks
    on a terminal. Without a terminal, or when offer-remove-after-merge is false, it keeps the
    clone and prints the command that removes it. A removal always runs the safety checks.
    """
    remove_command = f"draupnir remove {folder.name}"
    if args.keep:
        _log(f"Kept the clone {folder}.")
        return
    if not args.remove:
        if not ws.config.actions.offer_remove_after_merge or not sys.stdin.isatty():
            _log(f"Kept the clone {folder}. Remove it later with: {remove_command}")
            return
        if not confirm(f"The branch is merged. Remove the clone {folder.name}?", False):
            _log(f"Kept the clone {folder}. Remove it later with: {remove_command}")
            return
    remove_clone(ws, folder, False, _log)


def run_pr(args: argparse.Namespace) -> int:
    require_git()
    ws = Workspace.find(Path.cwd())
    folder = ws.resolve_folder(args.folder, Path.cwd())
    # The checks that need no network run before the question, so a clone that cannot run the
    # flow stops at once.
    branch = require_feature_branch(ws, require_ready(ws, folder))
    main_branch = ws.main_branch()
    question = (
        f"Run the pull request flow for {branch} in {folder.name}? draupnir pushes the branch, "
        f"creates or reuses a pull request into {main_branch}, waits for the build, and merges "
        "with rebase when the build is green."
    )
    if not confirm(question, args.yes):
        _log("Did not start the pull request flow.")
        return 0

    cancel = threading.Event()
    try:
        outcome = pr_build_and_merge(ws, folder, _log, cancel)
    except KeyboardInterrupt:
        # The same as "Stop waiting" in the dashboard: the wait ends, the build does not.
        cancel.set()
        print(
            "draupnir stopped, because of Ctrl+C. The pull request was not merged. A build that "
            "already started keeps running on GitHub.",
            file=sys.stderr,
        )
        return EXIT_INTERRUPTED
    if outcome.result == MERGED:
        _remove_after_merge(ws, folder, args)
    return 0


def run_open(args: argparse.Namespace) -> int:
    ws = Workspace.find(Path.cwd())
    folder = ws.resolve_folder(args.folder, Path.cwd())
    command = open_ide(folder, ws.config)
    _log(f"Starting the IDE: {command}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="draupnir",
        description="Work on several clones of one git repository at the same time.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "-C",
        dest="chdir",
        metavar="folder",
        help="run as if draupnir was started in this folder",
    )

    subparsers = parser.add_subparsers(dest="command")

    def add_command(name: str, help: str) -> argparse.ArgumentParser:
        # The same text is the line in `draupnir --help` and the description of the command's
        # own --help.
        return subparsers.add_parser(name, help=help, description=help)

    ui_parser = add_command(
        "ui", help="Start the dashboard. A branch name fills in the New clone tab."
    )
    ui_parser.add_argument("branch", nargs="?", help="branch name to fill in on the New clone tab")
    ui_parser.set_defaults(func=run_ui)

    init_parser = add_command(
        "init",
        help="Write the workspace files and clone the project into main/.",
    )
    init_parser.add_argument(
        "url",
        nargs="?",
        help="GitHub URL of the project, needed the first time main/ does not exist yet",
    )
    init_parser.add_argument(
        "--main-branch",
        metavar="branch",
        help="the branch to rebase on and merge into, for example develop. "
        "Default: the default branch on GitHub",
    )
    init_parser.set_defaults(func=run_init)

    new_parser = add_command("new", help="Create a clone next to main/.")
    new_parser.add_argument("branch", help="branch name for the new clone")
    new_parser.add_argument("--base", metavar="branch", help="branch the new branch starts from")
    new_parser.add_argument("--open", action="store_true", help="start the IDE in the new clone")
    new_parser.set_defaults(func=run_new)

    prepare_parser = add_command(
        "prepare", help="Copy the local files and set up Maven. Safe to run again."
    )
    prepare_parser.add_argument("folder", nargs="?", help="clone to prepare")
    prepare_parser.set_defaults(func=run_prepare)

    remove_parser = add_command("remove", help="Delete a clone after the safety checks.")
    remove_parser.add_argument("folder", nargs="?", help="clone to remove")
    remove_parser.add_argument(
        "--force", action="store_true", help="delete even when work would be lost"
    )
    remove_parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    remove_parser.set_defaults(func=run_remove)

    status_parser = add_command("status", help="Print the status table as text.")
    status_parser.set_defaults(func=run_status)

    fetch_parser = add_command("fetch", help="Fetch every clone.")
    fetch_parser.set_defaults(func=run_fetch)

    rebase_parser = add_command("rebase", help="Rebase a clone on the main branch.")
    rebase_parser.add_argument("folder", nargs="?", help="clone to rebase")
    rebase_parser.set_defaults(func=run_rebase)

    push_parser = add_command("push", help="Push a clone.")
    push_parser.add_argument("folder", nargs="?", help="clone to push")
    push_parser.add_argument(
        "--follow", action="store_true", help="wait for the build of the pushed commit"
    )
    push_parser.set_defaults(func=run_push)

    build_parser_ = add_command("build", help="Ask GitHub once for the build result of a clone.")
    build_parser_.add_argument("folder", nargs="?", help="clone to check")
    build_parser_.set_defaults(func=run_build)

    ff_main_parser = add_command("ff-main", help="Fast-forward the main branch to a clone's head.")
    ff_main_parser.add_argument("folder", nargs="?", help="clone to fast-forward from")
    ff_main_parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    ff_main_parser.set_defaults(func=run_ff_main)

    pr_parser = add_command(
        "pr",
        help="Push, create or reuse a pull request, follow the build, and merge.",
    )
    pr_parser.add_argument("folder", nargs="?", help="clone to push and merge")
    pr_parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    pr_remove_group = pr_parser.add_mutually_exclusive_group()
    pr_remove_group.add_argument(
        "--remove", action="store_true", help="remove the clone after a merge, without asking"
    )
    pr_remove_group.add_argument(
        "--keep", action="store_true", help="keep the clone after a merge, without asking"
    )
    pr_parser.set_defaults(func=run_pr)

    open_parser = add_command("open", help="Start the IDE in a clone.")
    open_parser.add_argument("folder", nargs="?", help="clone to open")
    open_parser.set_defaults(func=run_open)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.chdir:
        try:
            os.chdir(args.chdir)
        except OSError as exc:
            print(
                f"draupnir could not change to the folder {args.chdir}: {exc.strerror}. "
                "Check the path given with -C.",
                file=sys.stderr,
            )
            return 1

    func: Callable[[argparse.Namespace], int] = run_ui if args.command is None else args.func
    try:
        return func(args)
    except OperationError as exc:
        print(exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    raise SystemExit(main())
