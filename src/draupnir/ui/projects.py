"""The Projects tab: the action buttons, the status table and the details line."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Grid
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Button, DataTable, Static
from textual.widgets.button import ButtonVariant

from draupnir.github import BuildState
from draupnir.status import ProjectStatus


class Needs(Enum):
    """What an action needs before its button is enabled."""

    NOTHING = "nothing"
    PROJECT = "a selected clone"
    FEATURE_BRANCH = "a selected clone on a feature branch"


@dataclass(frozen=True)
class ActionButton:
    """One button in the grid. name is also the id of the button.

    `{main}` in label and tooltip is replaced with the name of the default branch. The tooltip
    shows when the mouse is over the button.
    """

    name: str
    label: str
    needs: Needs
    tooltip: str
    variant: ButtonVariant = "default"


# Every action from R17, in the order of the grid. The app decides which ones are shown.
ACTION_BUTTONS: tuple[ActionButton, ...] = (
    ActionButton(
        "fetch-all",
        "Fetch all",
        Needs.NOTHING,
        "Fetches every clone from GitHub, then reads the status again. Key: f.",
    ),
    ActionButton(
        "refresh",
        "Refresh",
        Needs.NOTHING,
        "Reads the status of every clone again, without a fetch. Key: r.",
    ),
    ActionButton(
        "open-branch",
        "Open branch",
        Needs.NOTHING,
        "Finds a branch on GitHub by typing part of its name, and opens it as a new clone. Key: b.",
    ),
    ActionButton(
        "open-ide",
        "Open IDE",
        Needs.PROJECT,
        "Starts IntelliJ IDEA or PyCharm in the selected clone. Key: o.",
    ),
    ActionButton(
        "rebase",
        "Rebase on {main}",
        Needs.PROJECT,
        "Fetches, then rebases the selected clone on origin/{main}. Refuses when tracked files "
        "are changed. On a conflict, the rebase stays in progress for you to resolve.",
    ),
    ActionButton(
        "push-follow",
        "Push and follow build",
        Needs.PROJECT,
        "Pushes the branch, with a safe force after a rebase, and waits for its build on GitHub.",
    ),
    ActionButton(
        "ff-main",
        "Fast-forward {main}",
        Needs.FEATURE_BRANCH,
        "Pushes this branch's head to {main} on GitHub without force, after a green build. The "
        "branch must be on top of origin/{main}. Needs a clone on a feature branch.",
        "warning",
    ),
    ActionButton(
        "pr-merge",
        "Pull request and merge",
        Needs.FEATURE_BRANCH,
        "Pushes, opens or reuses a pull request, waits for the build, and merges with rebase "
        "when it is green. Needs a clone on a feature branch.",
        "success",
    ),
    ActionButton(
        "build-status",
        "Build status",
        Needs.PROJECT,
        "Asks GitHub once for the build result of the selected clone's commit.",
    ),
    ActionButton(
        "remove",
        "Remove clone",
        Needs.FEATURE_BRANCH,
        "Deletes the selected clone's folder, after checking for unsaved work. Needs a clone on "
        "a feature branch.",
        "error",
    ),
)


# How many jobs of a build the details line shows.
DETAILS_JOBS = 4


# The keys of the table columns, in order, and their labels (R16).
COLUMNS: tuple[tuple[str, str], ...] = (
    ("folder", "Folder"),
    ("branch", "Branch"),
    ("main", "vs origin/{main}"),
    ("upstream", "vs upstream"),
    ("changes", "Local changes"),
    ("operation", "In progress"),
    ("build", "Build"),
)

_BUILD_STYLES = {"success": "green", "failure": "red", "pending": "yellow"}


def _is_older_commit(status: ProjectStatus, build: BuildState) -> bool:
    """True when the clone's HEAD has moved since build was read."""
    return status.head is not None and build.sha != status.head


def status_row(status: ProjectStatus, main_branch: str, build: BuildState | None) -> list[Text]:
    """The cells of one table row, in the order of COLUMNS.

    build is the last build state the dashboard read for this clone, or None. It is kept in
    memory only (Q12).
    """
    if status.error:
        first_line = status.error.strip().splitlines()[0] if status.error.strip() else ""
        return [
            Text(status.name),
            Text("error", style="bold red"),
            Text(""),
            Text(""),
            Text(first_line, style="red"),
            Text(""),
            Text(""),
        ]

    branch_style = "bold" if status.branch == main_branch else ""
    branch = Text(status.branch or "(detached)", style=branch_style)

    main_style = "yellow" if status.behind_main else "green"
    vs_main = Text(f"↑{status.ahead_main} ↓{status.behind_main}", style=main_style)

    if status.upstream_gone:
        vs_upstream = Text("deleted on GitHub", style="red")
    elif status.upstream is None:
        vs_upstream = Text("not pushed", style="yellow")
    else:
        in_sync = not status.ahead_upstream and not status.behind_upstream
        vs_upstream = Text(
            f"↑{status.ahead_upstream} ↓{status.behind_upstream}",
            style="green" if in_sync else "yellow",
        )

    parts: list[str] = []
    if status.changed:
        parts.append(f"{status.changed} changed")
    if status.untracked:
        parts.append(f"{status.untracked} untracked")
    changes = Text(", ".join(parts) or "clean", style="yellow" if parts else "green")

    operation = Text(status.operation or "", style="bold red")

    build_cell = Text("")
    if build is not None:
        label = build.state
        if _is_older_commit(status, build):
            label += " (older commit)"
        build_cell = Text(label, style=_BUILD_STYLES.get(build.state, ""))

    return [Text(status.name), branch, vs_main, vs_upstream, changes, operation, build_cell]


def details_text(status: ProjectStatus | None, build: BuildState | None) -> Text:
    """The details line under the table, for the selected clone."""
    if status is None:
        return Text("")
    lines = [str(status.folder)]
    if status.error:
        lines.append(f"draupnir could not read this clone: {status.error.strip()}")
        return Text("\n".join(lines))
    commit = status.head[:10] if status.head else "none"
    lines.append(f"Commit {commit}   Upstream {status.upstream or 'none'}")
    if status.operation:
        lines.append(
            f"A {status.operation} is in progress. Finish it or abort it before you start an "
            "action on this clone."
        )
    if build is not None:
        older = " (older commit)" if _is_older_commit(status, build) else ""
        lines.append(f"Build of {build.sha[:10]}{older}: {build.summary()}")
        for item in build.items[:DETAILS_JOBS]:
            lines.append(f"  {item.name}: {item.state}")
        hidden = len(build.items) - DETAILS_JOBS
        if hidden > 0:
            lines.append(f"  and {hidden} more. Build status shows every job in the output.")
    return Text("\n".join(lines))


class ProjectsView(Widget):
    """The buttons, the table and the details line of the Projects tab.

    The widget holds no logic. The app gives it the statuses, the known builds and whether an
    action runs, and the widget shows them. Button presses and a new selection are posted to the
    app as `ActionRequested` and `ProjectSelected`.
    """

    DEFAULT_CSS = """
    ProjectsView { height: 1fr; }
    ProjectsView #project-buttons { grid-size: 5; grid-gutter: 0 1; height: auto; padding: 0 1; }
    ProjectsView #project-buttons Button { width: 100%; }
    ProjectsView #table { height: 1fr; }
    ProjectsView #details { height: auto; max-height: 10; padding: 0 1; color: $text-muted; }
    """

    class ActionRequested(Message):
        """The user pressed the button of the action called `action`."""

        def __init__(self, action: str) -> None:
            super().__init__()
            self.action = action

    class ProjectSelected(Message):
        """The user selected another row. status is None when the table is empty."""

        def __init__(self, status: ProjectStatus | None) -> None:
            super().__init__()
            self.status = status

    def __init__(
        self,
        *,
        main_folder: Path,
        main_branch: str,
        shown_actions: Collection[str],
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.main_folder = main_folder
        self.main_branch = main_branch
        self.shown_actions = frozenset(shown_actions)
        self.table: DataTable[Text] = DataTable(id="table", cursor_type="row", zebra_stripes=True)
        self.details = Static(id="details")
        self.buttons = {
            spec.name: Button(
                spec.label.format(main=main_branch),
                id=spec.name,
                variant=spec.variant,
                tooltip=spec.tooltip.format(main=main_branch),
            )
            for spec in ACTION_BUTTONS
        }
        self._statuses: list[ProjectStatus] = []
        self._builds: dict[str, BuildState] = {}
        self._busy = False

    def compose(self) -> ComposeResult:
        with Grid(id="project-buttons"):
            for spec in ACTION_BUTTONS:
                button = self.buttons[spec.name]
                button.display = spec.name in self.shown_actions
                yield button
        yield self.table
        yield self.details

    def on_mount(self) -> None:
        for key, label in COLUMNS:
            self.table.add_column(label.format(main=self.main_branch), key=key)
        self._update_buttons()

    def show_statuses(
        self,
        statuses: Sequence[ProjectStatus],
        builds: Mapping[str, BuildState],
        busy: bool,
    ) -> None:
        """Shows statuses in the table, and keeps the selected clone selected."""
        selected = self.selected_status()
        selected_name = selected.name if selected else None
        old_names = [status.name for status in self._statuses]
        self._statuses = list(statuses)
        self._builds = dict(builds)
        self._busy = busy

        new_names = [status.name for status in self._statuses]
        rows = [
            status_row(status, self.main_branch, self._builds.get(status.name))
            for status in self._statuses
        ]
        if new_names == old_names and self.table.row_count == len(new_names):
            # The same clones as before: change the cells, so the cursor and the scroll stay.
            for name, row in zip(new_names, rows, strict=True):
                for (column_key, _), cell in zip(COLUMNS, row, strict=True):
                    self.table.update_cell(name, column_key, cell, update_width=True)
        else:
            self.table.clear()
            for name, row in zip(new_names, rows, strict=True):
                self.table.add_row(*row, key=name)
            if selected_name in new_names:
                self.table.move_cursor(row=new_names.index(selected_name))
        self._update_details()
        self._update_buttons()

    def set_busy(self, busy: bool) -> None:
        """Enables or disables the buttons for an action that starts or ends."""
        self._busy = busy
        self._update_buttons()

    def selected_status(self) -> ProjectStatus | None:
        """The status of the selected row, or None when the table is empty."""
        row = self.table.cursor_row
        if 0 <= row < len(self._statuses) and row < self.table.row_count:
            return self._statuses[row]
        return None

    def _is_feature_branch(self, status: ProjectStatus) -> bool:
        return status.folder != self.main_folder and status.branch != self.main_branch

    def _update_buttons(self) -> None:
        selected = self.selected_status()
        for spec in ACTION_BUTTONS:
            if spec.needs is Needs.NOTHING:
                disabled = self._busy
            elif spec.needs is Needs.PROJECT:
                disabled = self._busy or selected is None
            else:
                disabled = self._busy or selected is None or not self._is_feature_branch(selected)
            self.buttons[spec.name].disabled = disabled

    def _update_details(self) -> None:
        selected = self.selected_status()
        build = self._builds.get(selected.name) if selected else None
        self.details.update(details_text(selected, build))

    @on(DataTable.RowHighlighted, "#table")
    def _row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        event.stop()
        self._update_details()
        self._update_buttons()
        self.post_message(self.ProjectSelected(self.selected_status()))

    @on(Button.Pressed)
    def _button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id in self.buttons:
            event.stop()
            self.post_message(self.ActionRequested(event.button.id))
