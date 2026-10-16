"""The Textual app: the tabs, the key bindings, the state, and running actions."""

from __future__ import annotations

import contextlib
import subprocess
import threading
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rich.markup import escape
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header, TabbedContent, TabPane

from draupnir.cmux import (
    SidebarStatus,
    clear_sidebar_status,
    close_clone_terminal,
    in_cmux,
    open_clone_terminal,
    open_cmux_workspace,
    session_note,
    set_sidebar_status,
    sidebar_status,
)
from draupnir.config import CONFIG_FILE_NAME
from draupnir.errors import OperationError, RebaseConflict
from draupnir.github import CANCELLED, SUCCESS, BuildState, build_result_message
from draupnir.ide import editor_command, open_ide
from draupnir.operations import (
    MERGED,
    PrOutcome,
    check_build,
    fast_forward_main,
    fetch_all,
    pr_build_and_merge,
    push_and_follow,
    rebase_on_main,
)
from draupnir.runner import LogFn
from draupnir.setup import folder_for_branch, new_clone, remove_clone
from draupnir.status import ProjectStatus, read_status
from draupnir.ui.branchpicker import BranchChoice, BranchPickerScreen
from draupnir.ui.confirm import ConfirmScreen
from draupnir.ui.newclone import NewCloneView
from draupnir.ui.output import OutputArea
from draupnir.ui.projects import ProjectsView
from draupnir.workspace import Workspace

# An action. It returns None, or a BuildState when it waited for a build. A returned build
# state that is cancelled means the user pressed "Stop waiting".
Operation = Callable[[LogFn, threading.Event], object]
ConflictCallback = Callable[[list[str]], None]
AnswerCallback = Callable[[bool | None], None]
BuildCallback = Callable[[BuildState], None]
SuccessCallback = Callable[[object], None]

# The buttons of the Projects tab that are always shown. Fast-forward main depends on the config,
# and New cmux workspace on whether draupnir runs in cmux.
_BASE_SHOWN_ACTIONS = frozenset(
    {
        "fetch-all",
        "refresh",
        "config",
        "open-branch",
        "open-ide",
        "rebase",
        "push-follow",
        "pr-merge",
        "build-status",
        "remove",
    }
)

FETCH_ALL_TITLE = "Fetch all clones"


class DraupnirApp(App[None]):
    """The dashboard for one workspace.

    The app owns the state: the status of every clone, the known builds, whether an action runs,
    and the cancel event of that action. The widgets only show the state and post messages.
    """

    TITLE = "draupnir"
    CSS = """
    TabbedContent { height: 1fr; }
    """
    BINDINGS = [
        Binding("r", "refresh_status", "Refresh"),
        Binding("f", "fetch_all", "Fetch all"),
        Binding("o", "open_ide", "Open IDE"),
        Binding("w", "open_cmux_workspace", "cmux workspace"),
        Binding("b", "open_branch", "Open branch"),
        Binding("g", "open_build", "Open build"),
        Binding("c", "edit_config", "Edit config"),
        Binding("n", "show_new_clone", "New clone"),
        Binding("p", "show_projects", "Projects"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, ws: Workspace, initial_branch: str | None = None) -> None:
        super().__init__()
        self.ws = ws
        self.main_branch = ws.main_branch()
        # The branch from `draupnir ui <branch>`. The New clone tab uses it.
        self.initial_branch = initial_branch
        self.sub_title = str(ws.root)

        self.statuses: list[ProjectStatus] = []
        # The last build state read for each clone, by folder name. In memory only (Q12).
        self.builds: dict[str, BuildState] = {}
        self.busy = False
        self.running_title: str | None = None
        self.cancel_event = threading.Event()
        # True while a status read runs in a worker thread.
        self.reading = False
        # The last line written into the cmux sidebar, so the same line is not written twice.
        self.sidebar_shows: SidebarStatus | None = None
        # The sidebar status is turned off after one failed write, so a cmux that refuses it
        # is not called again every refresh.
        self.sidebar_enabled = in_cmux() and ws.config.ui.cmux_status
        self._read_again = False
        self._last_read_error: str | None = None

        shown_actions = _BASE_SHOWN_ACTIONS
        if ws.config.actions.fast_forward_main:
            shown_actions = shown_actions | {"ff-main"}
        if in_cmux():
            shown_actions = shown_actions | {"cmux-workspace"}
        self.projects = ProjectsView(
            main_folder=ws.main,
            main_branch=self.main_branch,
            shown_actions=shown_actions,
            id="projects-view",
        )
        self.newclone = NewCloneView(
            ws=ws,
            main_branch=self.main_branch,
            initial_branch=self.initial_branch,
            id="newclone-view",
        )
        self.output = OutputArea(id="output")

    # ----- Layout ---------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        initial = "new-clone" if self.initial_branch else "projects"
        with TabbedContent(id="tabs", initial=initial):
            with TabPane("New clone", id="new-clone"):
                yield self.newclone
            with TabPane("Projects", id="projects"):
                yield self.projects
        yield self.output
        yield Footer()

    def on_mount(self) -> None:
        if self.initial_branch:
            self.newclone.focus_branch_input()
        else:
            self.projects.table.focus()
        self.request_status_read()
        seconds = self.ws.config.ui.refresh_seconds
        if seconds > 0:
            self.set_interval(seconds, self.refresh_on_timer, name="refresh-status")

    def on_unmount(self) -> None:
        # A thread worker cannot be cancelled from outside. Ending a wait for a build here lets
        # the dashboard quit at once, and the build on GitHub keeps running.
        self.cancel_event.set()
        if self.sidebar_enabled and self.sidebar_shows is not None:
            # The dashboard is the only writer of its line, so it takes the line away when it
            # quits. This runs while the app stops, so a worker thread is no longer an option.
            with contextlib.suppress(OperationError):
                clear_sidebar_status(self.ws.root)

    # ----- Reading the status -----------------------------------------------------------

    def request_status_read(self) -> None:
        """Reads the status of every clone again, in a worker thread.

        When a read already runs, one more read starts after it ends, so the table always ends
        up showing the state after the last change.
        """
        if self.reading:
            self._read_again = True
            return
        self.reading = True
        self._read_statuses()

    def refresh_on_timer(self) -> None:
        """Called by the timer. It only reads, never fetches, and skips while anything runs."""
        if self.busy or self.reading:
            return
        self.request_status_read()

    @work(thread=True, group="status", exit_on_error=False)
    def _read_statuses(self) -> None:
        try:
            statuses = [read_status(self.ws, folder) for folder in self.ws.project_paths()]
        except OperationError as exc:
            self._call_ui(self._status_read_failed, str(exc))
        except Exception:
            self._call_ui(self._status_read_failed, traceback.format_exc())
        else:
            self._call_ui(self._show_statuses, statuses)
        finally:
            self._call_ui(self._status_read_done)

    def _show_statuses(self, statuses: list[ProjectStatus]) -> None:
        self._last_read_error = None
        self.statuses = statuses
        self.projects.show_statuses(statuses, self.builds, self.busy)
        self.publish_sidebar_status()

    def _status_read_failed(self, message: str) -> None:
        # The timer reads again and again, so the same failure is shown only once.
        if message == self._last_read_error:
            return
        self._last_read_error = message
        self.output.write_line(message, "red")
        self.notify(
            message.strip().splitlines()[0],
            title="Reading the status failed",
            severity="error",
            timeout=10,
        )

    def _status_read_done(self) -> None:
        self.reading = False
        if self._read_again:
            self._read_again = False
            self.request_status_read()

    # ----- The status in the cmux sidebar -----------------------------------------------

    def publish_sidebar_status(self) -> None:
        """Writes one line about the clones into the sidebar row of the cmux workspace (D52).

        It runs only in cmux, and only when ui.cmux-status is on. The same line is never
        written twice, so a dashboard that does not change calls cmux once.
        """
        if not self.sidebar_enabled:
            return
        status = sidebar_status(self.statuses, self.builds)
        if status is None or status == self.sidebar_shows:
            return
        self.sidebar_shows = status
        self._write_sidebar_status(status)

    @work(thread=True, group="sidebar-status", exit_on_error=False)
    def _write_sidebar_status(self, status: SidebarStatus) -> None:
        """Calls cmux in a worker thread, so a slow answer never holds up the dashboard."""
        try:
            result = set_sidebar_status(status, self.ws.root)
        except OperationError as exc:
            self._call_ui(self._sidebar_status_failed, str(exc))
            return
        if result.code != 0:
            lines = (result.stderr or result.stdout).strip().splitlines()
            reason = lines[0] if lines else "it gave no reason"
            self._call_ui(
                self._sidebar_status_failed,
                f"cmux refused the status of this workspace: {reason}.",
            )

    def _sidebar_status_failed(self, message: str) -> None:
        """Turns the sidebar status off after the first failure, and says so once."""
        self.sidebar_enabled = False
        self.sidebar_shows = None
        self.output.write_line(
            f"{message} draupnir does not write the status again. Set ui.cmux-status to false "
            "in draupnir.toml to turn it off for good.",
            "yellow",
        )

    # ----- Running actions ------------------------------------------------------------

    def start(
        self,
        title: str,
        operation: Operation,
        *,
        on_conflict: ConflictCallback | None = None,
        on_success: SuccessCallback | None = None,
    ) -> bool:
        """Runs operation in a worker thread, and shows its log lines in the output area.

        Only one action runs at a time (R19). When another action still runs, this refuses with
        a message in the output and a notification, and returns False.

        on_conflict, when given, is called on the UI thread with the conflicting files when
        operation raises RebaseConflict. The caller passes it a closure that already knows
        which clone was rebased, so the dialog offers the IDE for that clone even when the
        table's selection moves to another row while the rebase runs.

        on_success, when given, is called on the UI thread with the result of operation, when
        the action ended as done. It is called after the action has finished, so it can start
        a new action.
        """
        if self.busy:
            message = (
                f'"{title}" did not start, because "{self.running_title}" is still running. '
                "Wait until it ends, and try again."
            )
            self.output.write_line(message, "yellow")
            self.notify(message, severity="warning")
            return False
        self.busy = True
        self.running_title = title
        self.cancel_event = threading.Event()
        self.output.begin_action(title)
        self.projects.set_busy(True)
        self._run_operation(title, operation, self.cancel_event, on_conflict, on_success)
        return True

    @work(thread=True, group="action", exit_on_error=False)
    def _run_operation(
        self,
        title: str,
        operation: Operation,
        cancel: threading.Event,
        on_conflict: ConflictCallback | None,
        on_success: SuccessCallback | None,
    ) -> None:
        succeeded = False
        result: object = None
        try:
            result = operation(self._log_from_thread, cancel)
        except RebaseConflict as exc:
            self._call_ui(self._action_failed, title, str(exc))
            if on_conflict is not None:
                self._call_ui(on_conflict, exc.files)
        except OperationError as exc:
            self._call_ui(self._action_failed, title, str(exc))
        except Exception:
            self._call_ui(
                self._action_failed,
                title,
                "draupnir stopped because of an unexpected error. The output shows the details.\n"
                + traceback.format_exc(),
            )
        else:
            if isinstance(result, BuildState) and result.state == CANCELLED:
                self._call_ui(self._action_stopped_waiting, title)
            else:
                self._call_ui(self._action_succeeded, title)
                succeeded = True
        finally:
            self._call_ui(self._action_finished)
        if succeeded and on_success is not None:
            self._call_ui(on_success, result)

    def _log_from_thread(self, line: str) -> None:
        self._call_ui(self.output.write_line, line)

    def _call_ui(self, callback: Callable[..., Any], *args: Any) -> None:
        """Runs callback on the UI thread, from a worker thread.

        Does nothing when the app has already stopped, for example after the user quit while a
        fetch was still running.
        """
        if not self.is_running:
            return
        try:
            self.call_from_thread(callback, *args)
        except Exception:
            if self.is_running:
                raise

    def _action_succeeded(self, title: str) -> None:
        self.output.write_line(f"=== Done: {title}", "bold green")
        self.notify(f"Done: {title}")

    def _action_stopped_waiting(self, title: str) -> None:
        self.output.write_line(f"=== Stopped waiting: {title}", "bold yellow")
        self.notify(
            "The build keeps running on GitHub. Use Build status to read it again later.",
            title=f"Stopped waiting: {title}",
            severity="warning",
        )

    def _action_failed(self, title: str, message: str) -> None:
        self.output.write_line(message, "red")
        self.output.write_line(f"=== Stopped: {title}", "bold red")
        first_line = message.strip().splitlines()[0] if message.strip() else title
        self.notify(first_line, title=f"Stopped: {title}", severity="error", timeout=10)

    def _action_finished(self) -> None:
        self.busy = False
        self.running_title = None
        self.output.end_action()
        self.projects.set_busy(False)
        self.request_status_read()

    # ----- Messages from the widgets ----------------------------------------------------

    @on(ProjectsView.ActionRequested)
    def _action_requested(self, event: ProjectsView.ActionRequested) -> None:
        handlers: dict[str, Callable[[], None]] = {
            "refresh": self.action_refresh_status,
            "fetch-all": self.action_fetch_all,
            "config": self.action_edit_config,
            "open-branch": self.action_open_branch,
            "open-ide": self.action_open_ide,
            "cmux-workspace": self.action_open_cmux_workspace,
            "rebase": self.action_rebase,
            "push-follow": self.action_push_and_follow,
            "ff-main": self.action_ff_main,
            "pr-merge": self.action_pr_merge,
            "build-status": self.action_build_status,
            "remove": self.action_remove,
        }
        handler = handlers.get(event.action)
        if handler is not None:
            handler()

    @on(ProjectsView.BuildRequested)
    def _build_requested(self, event: ProjectsView.BuildRequested) -> None:
        self._open_build(event.name)

    @on(OutputArea.StopWaitingRequested)
    def _stop_waiting(self) -> None:
        if not self.busy or self.cancel_event.is_set():
            return
        self.cancel_event.set()
        self.output.write_line(
            "draupnir stops waiting. A command that runs now still runs to its end.", "yellow"
        )

    @on(NewCloneView.CreateRequested)
    def _create_requested(self, event: NewCloneView.CreateRequested) -> None:
        if not event.branch:
            self.notify("Enter a branch name.", severity="warning")
            return
        self._start_new_clone(event.branch, event.base, event.folder, event.open_after)

    def _start_new_clone(
        self, branch: str, base: str | None, folder_name: str | None, open_after: bool
    ) -> None:
        """Creates a clone for branch as an action, and starts the IDE in it when asked."""
        ws = self.ws

        def operation(log: LogFn, _cancel: threading.Event) -> None:
            folder = new_clone(ws, branch, base, log, folder_name)
            # It never raises, so a session that cannot start ends the action with Done.
            open_clone_terminal(folder, ws.config.cmux, None, log)
            if not open_after:
                return
            try:
                command = open_ide(folder, ws.config)
                log(f"Starting the IDE: {command}")
            except OperationError as exc:
                log(str(exc))

        self.start(f"Create a clone for {branch}", operation)

    # ----- Actions and key bindings -----------------------------------------------------

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Hides the key of New cmux workspace, and turns it off, when its button is hidden."""
        if action == "open_cmux_workspace":
            return "cmux-workspace" in self.projects.shown_actions
        return True

    def action_refresh_status(self) -> None:
        """Reads the status of every clone again, without a fetch."""
        if self.busy:
            self.notify(
                f'The status is read again when "{self.running_title}" ends.',
                severity="information",
            )
            return
        self.request_status_read()

    def action_fetch_all(self) -> None:
        ws = self.ws

        def operation(log: LogFn, _cancel: threading.Event) -> None:
            fetch_all(ws, log)

        self.start(FETCH_ALL_TITLE, operation)

    def action_open_branch(self) -> None:
        """Lets the user find a branch on GitHub by name, and opens it as a new clone."""
        try:
            branches = self.ws.remote_branches()
        except OperationError as exc:
            self.notify(str(exc).splitlines()[0], severity="error", timeout=10)
            return
        root = self.ws.root
        # A clone can have a folder name of its own, so the branches the clones are on count too.
        checked_out = {status.branch for status in self.statuses}
        without_clone = [
            b
            for b in branches
            if b not in checked_out and not (root / folder_for_branch(b)).exists()
        ]

        def chosen(choice: BranchChoice | None) -> None:
            if choice is not None:
                self._start_new_clone(choice.branch, None, choice.folder, choice.open_after)

        self.push_screen(BranchPickerScreen(without_clone), chosen)

    def action_open_build(self) -> None:
        """Opens the last known build of the selected clone in the browser."""
        selected = self.projects.selected_status()
        if selected is not None:
            self._open_build(selected.name)

    def action_open_url(self, url: str) -> None:
        """Opens url in the browser. The links in the details line use this action."""
        self.open_url(url)

    def _open_build(self, name: str) -> None:
        """Opens one job of the last known build of the clone called name in the browser.

        The job is the first failed job, else the first job that still runs, else the first job.
        This only reads what the dashboard already knows, so it works while an action runs.
        """
        build = self.builds.get(name)
        if build is None:
            self.notify(
                f"draupnir does not know a build of {name} yet. Press Build status to read it.",
                severity="warning",
            )
            return
        job = build.job_to_open()
        if job is None:
            self.notify(
                f"The build of {name} has no job with a web page to open.", severity="warning"
            )
            return
        self.open_url(job.url)
        self.notify(f"Opening {job.name} of {name} in the browser.")

    def action_show_projects(self) -> None:
        self.query_one("#tabs", TabbedContent).active = "projects"
        self.projects.table.focus()

    def action_show_new_clone(self) -> None:
        self.query_one("#tabs", TabbedContent).active = "new-clone"
        self.newclone.focus_branch_input()

    def action_open_ide(self) -> None:
        """Starts the IDE in the selected clone (R13)."""
        selected = self.projects.selected_status()
        if selected is None:
            return
        folder = selected.folder
        name = selected.name
        ws = self.ws

        def operation(log: LogFn, _cancel: threading.Event) -> None:
            command = open_ide(folder, ws.config)
            log(f"Starting the IDE: {command}")

        self.start(f"Open the IDE in {name}", operation)

    def action_open_cmux_workspace(self) -> None:
        """Opens a new cmux workspace with a shell in the selected clone."""
        selected = self.projects.selected_status()
        if selected is None:
            return
        folder = selected.folder

        def operation(log: LogFn, _cancel: threading.Event) -> None:
            open_cmux_workspace(folder, log)

        self.start(f"Open a cmux workspace in {selected.name}", operation)

    def action_edit_config(self) -> None:
        """Opens draupnir.toml in the editor, or shows it when no editor is set (R25).

        The editor takes over the terminal while it runs, so this refuses while an action
        runs. After the editor ends, the config is read again, and a config that cannot be
        read is reported without changing the one the app uses.
        """
        path = self.ws.root / CONFIG_FILE_NAME
        if self.busy:
            self.notify(
                f'The config can be edited when "{self.running_title}" ends.',
                severity="information",
            )
            return
        try:
            command = editor_command()
        except OperationError as exc:
            self.notify(str(exc), severity="error")
            return
        if command is None:
            self._show_config(path)
            return
        with self.suspend():
            subprocess.run([*command, str(path)], check=False)
        self._read_config_again()

    def _show_config(self, path: Path) -> None:
        """Writes draupnir.toml into the output, for a terminal without an editor."""
        self.output.write_line(f"{path}:")
        try:
            for line in path.read_text().splitlines():
                self.output.write_line(f"  {line}")
        except OSError as exc:
            self.output.write_line(f"draupnir could not read {path}: {exc.strerror}", "red")
            return
        self.output.write_line(
            "Set DRAUPNIR_EDITOR, VISUAL or EDITOR to edit this file from the dashboard.",
            "yellow",
        )

    def _read_config_again(self) -> None:
        """Reads draupnir.toml again after the editor ended, and says what a restart needs."""
        try:
            self.ws = Workspace.find(self.ws.root)
        except OperationError as exc:
            self.output.write_line(str(exc), "red")
            self.notify(str(exc).splitlines()[0], severity="error")
            return
        self.notify(
            f"{CONFIG_FILE_NAME} is read again. Restart draupnir when you changed which "
            "buttons are shown, the default branch or the refresh time."
        )

    def action_rebase(self) -> None:
        """Rebases the selected clone on the default branch (D8).

        On a conflict, the rebase stays in progress, and the app offers to open the IDE in
        the clone that was rebased. folder and name are captured here, when the action
        starts, so the dialog still names the right clone even when the user selects
        another row before the rebase ends.
        """
        selected = self.projects.selected_status()
        if selected is None:
            return
        folder = selected.folder
        name = selected.name
        ws = self.ws
        main_branch = self.main_branch

        def operation(log: LogFn, _cancel: threading.Event) -> None:
            rebase_on_main(ws, folder, log)

        def on_conflict(_files: list[str]) -> None:
            self._offer_ide_after_conflict(folder, name)

        self.start(f"Rebase {name} on {main_branch}", operation, on_conflict=on_conflict)

    def _offer_ide_after_conflict(self, folder: Path, name: str) -> None:
        def answered(yes: bool | None) -> None:
            if not yes:
                return
            ws = self.ws

            def operation(log: LogFn, _cancel: threading.Event) -> None:
                command = open_ide(folder, ws.config)
                log(f"Starting the IDE: {command}")

            self.start(f"Open the IDE in {name}", operation)

        self.push_screen(
            ConfirmScreen(
                f"The rebase of {name} stopped with conflicts.\n\nOpen the IDE to resolve them?"
            ),
            answered,
        )

    # ----- Builds -------------------------------------------------------------------------

    def _record_build(self, name: str, state: BuildState) -> None:
        """Remembers state as the last known build of the clone called name, and shows it."""
        self.builds[name] = state
        self.projects.show_statuses(self.statuses, self.builds, self.busy)
        self.publish_sidebar_status()

    def _build_recorder(self, name: str) -> BuildCallback:
        """A callback for a worker thread, that records every build state it gets for name."""

        def record(state: BuildState) -> None:
            self._call_ui(self._record_build, name, state)

        return record

    def action_push_and_follow(self) -> None:
        """Pushes the selected clone and waits for the build of the pushed commit (R17, R21).

        The push uses a safe force on a feature branch (D9). The Build column shows every new
        state while the wait runs. "Stop waiting" ends the wait, and the column keeps the last
        state it saw, because the build on GitHub keeps running (R19). A build that is not green
        ends the action with a message.
        """
        selected = self.projects.selected_status()
        if selected is None:
            return
        folder = selected.folder
        name = selected.name
        ws = self.ws
        on_update = self._build_recorder(name)

        def operation(log: LogFn, cancel: threading.Event) -> BuildState:
            state = push_and_follow(ws, folder, log, cancel, on_update)
            if state.state not in (SUCCESS, CANCELLED):
                raise OperationError(build_result_message(name, state))
            return state

        self.start(f"Push {name} and follow the build", operation)

    def action_build_status(self) -> None:
        """Asks GitHub once for the build state of the selected clone's HEAD (R17, R20)."""
        selected = self.projects.selected_status()
        if selected is None:
            return
        folder = selected.folder
        name = selected.name
        ws = self.ws
        on_update = self._build_recorder(name)

        def operation(log: LogFn, _cancel: threading.Event) -> None:
            state = check_build(ws, folder, log)
            on_update(state)
            log(build_result_message(name, state))

        self.start(f"Build status of {name}", operation)

    def action_ff_main(self) -> None:
        """Moves the default branch to the selected clone's head, after confirmation (R18).

        The dialog names the commit that goes to the default branch and, when the config
        needs one, says that its build must be green.
        """
        selected = self.projects.selected_status()
        if selected is None:
            return
        folder = selected.folder
        name = selected.name
        ws = self.ws
        main_branch = self.main_branch
        sha = selected.head[:10] if selected.head else "HEAD"

        def confirmed(yes: bool | None) -> None:
            if not yes:
                return

            def operation(log: LogFn, _cancel: threading.Event) -> None:
                fast_forward_main(ws, folder, log)

            self.start(f"Fast-forward {main_branch} to {name}", operation)

        build_note = (
            f" The build of {sha} must be green."
            if ws.config.actions.fast_forward_needs_green_build
            else ""
        )
        self.push_screen(
            ConfirmScreen(
                f"Move {main_branch} on GitHub to commit {sha}, the head of {name}?{build_note}"
                f"\n\n{ws.main.name}/ is updated afterwards."
            ),
            confirmed,
        )

    def action_pr_merge(self) -> None:
        """Runs the pull request flow for the selected clone, after confirmation (R17, R18).

        The dialog lists the steps of the flow. The Build column follows the build while the
        flow waits for it. When the pull request is merged, and offer-remove-after-merge is
        true, the app asks whether to remove the clone once the action has finished (D17).
        After auto-merge, the pull request is not merged yet, so no removal is offered.
        """
        selected = self.projects.selected_status()
        if selected is None:
            return
        folder = selected.folder
        name = selected.name
        branch = selected.branch or name
        ws = self.ws
        main_branch = self.main_branch
        on_update = self._build_recorder(name)

        def operation(log: LogFn, cancel: threading.Event) -> PrOutcome:
            return pr_build_and_merge(ws, folder, log, cancel, on_update)

        def on_success(result: object) -> None:
            if not isinstance(result, PrOutcome):
                return
            if result.result != MERGED:
                self.notify(result.message(), title=f"Pull request for {branch}", timeout=10)
                return
            if ws.config.actions.offer_remove_after_merge:
                self._offer_remove_after_merge(folder, name)

        def confirmed(yes: bool | None) -> None:
            if not yes:
                return
            self.start(f"Pull request for {branch}", operation, on_success=on_success)

        self.push_screen(
            ConfirmScreen(
                f"Pull request flow for {branch}:\n\n"
                "1. Push the branch.\n"
                f"2. Create a pull request into {main_branch}, or use the open one.\n"
                "3. Wait for the build.\n"
                "4. When the build is green, merge the pull request with rebase. When GitHub "
                "still waits for a review, turn on auto-merge instead.\n"
                f"5. Update {ws.main.name}/."
            ),
            confirmed,
        )

    def _offer_remove_after_merge(self, folder: Path, name: str) -> None:
        def answered(yes: bool | None) -> None:
            if yes:
                self._start_removal(folder, name)

        self._ask_about_removal(
            f"The branch is merged. Remove the clone {name}?\n\n"
            "draupnir still refuses when the clone has work that is not on GitHub.",
            folder,
            answered,
        )

    @work(thread=True, group="remove-question", exit_on_error=False)
    def _ask_about_removal(self, question: str, folder: Path, answered: AnswerCallback) -> None:
        """Asks question about the removal of folder, with the lines about its terminal session.

        The lines come from cmux, which can be slow, so they are read in this worker thread. The
        dialog opens when they are read. session_note never raises, so the dialog always opens.
        """
        lines = session_note(folder)
        self._call_ui(self._show_removal_question, question, lines, answered)

    def _show_removal_question(
        self, question: str, lines: list[str], answered: AnswerCallback
    ) -> None:
        if lines:
            # ConfirmScreen shows markup, and a program name such as [x] must show as it is.
            question += "\n\n" + "\n".join(escape(line) for line in lines)
        self.push_screen(ConfirmScreen(question), answered)

    def _start_removal(self, folder: Path, name: str) -> None:
        """Removes folder as a new action, with every safety check and without force (R7).

        The clone's terminal session is closed after the checks passed and before the delete.
        """
        ws = self.ws

        def operation(log: LogFn, _cancel: threading.Event) -> None:
            remove_clone(
                ws, folder, False, log, before_delete=lambda: close_clone_terminal(folder, log)
            )

        self.start(f"Remove {name}", operation)

    def action_remove(self) -> None:
        """Asks for confirmation, then removes the selected clone (R7, R18).

        The question also says what the clone's terminal session holds, and that the removal
        closes it. The dashboard never forces a removal. Use `draupnir remove --force` on the
        command line to delete a clone that has work that is not on GitHub.
        """
        selected = self.projects.selected_status()
        if selected is None:
            return
        folder = selected.folder
        name = selected.name

        def confirmed(yes: bool | None) -> None:
            if yes:
                self._start_removal(folder, name)

        self._ask_about_removal(
            f"Delete the folder {name}?\n\n"
            "This also deletes its .m2repo and the local files that were copied into "
            "it. draupnir refuses when the clone has work that is not on GitHub. Use "
            "draupnir remove --force on the command line to delete it anyway.",
            folder,
            confirmed,
        )
