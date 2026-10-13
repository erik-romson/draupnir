"""The New clone tab: branch and base inputs, and the Create button (R12, R14)."""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Button, Checkbox, Input, Label

from draupnir.setup import folder_for_branch
from draupnir.workspace import Workspace


class NewCloneView(Widget):
    """The form for creating a clone. Posts CreateRequested, and holds no other logic.

    The app decides whether the branch name is valid and runs the operation, so the widget
    itself never starts a worker.
    """

    DEFAULT_CSS = """
    NewCloneView { height: auto; padding: 1 2; }
    NewCloneView Input { margin-bottom: 1; width: 60; }
    NewCloneView Checkbox { margin-bottom: 1; }
    """

    class CreateRequested(Message):
        """The user asked to create a clone for branch, from base, in folder, with open_after.

        folder is None when the user left it empty, and the folder then gets the branch name.
        """

        def __init__(
            self, branch: str, base: str | None, folder: str | None, open_after: bool
        ) -> None:
            super().__init__()
            self.branch = branch
            self.base = base
            self.folder = folder
            self.open_after = open_after

    def __init__(
        self,
        *,
        ws: Workspace,
        main_branch: str,
        initial_branch: str | None,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.ws = ws
        # The Input starts empty. Its value is set to initial_branch once the widget is
        # mounted, because Input's reactive value watcher needs the active app, which does
        # not exist yet while the widget tree is still being built.
        self._initial_branch = initial_branch
        base_placeholder = ws.config.workspace.base_branch or main_branch
        self.branch_input = Input(placeholder="feature/vessel-owner", id="branch")
        self.base_input = Input(placeholder=base_placeholder, id="base")
        self.folder_input = Input(
            placeholder=folder_for_branch("feature/vessel-owner"), id="folder"
        )
        self.open_checkbox = Checkbox(
            "Open the IDE when the clone is ready", value=True, id="open-after"
        )

    def on_mount(self) -> None:
        if self._initial_branch:
            self.branch_input.value = self._initial_branch

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"Workspace: {self.ws.root}")
            yield Label(f"Repository: {self.ws.origin_url()}")
            yield Label("")
            yield Label("Branch name.")
            yield self.branch_input
            yield Label(f"Base branch on GitHub. Empty means {self.base_input.placeholder}.")
            yield self.base_input
            yield Label("Folder name. Empty means the branch name with / replaced by -.")
            yield self.folder_input
            yield self.open_checkbox
            yield Button(
                "Create clone",
                id="create",
                variant="primary",
                tooltip="Creates a clone of the branch next to main/, with its local files and "
                "Maven set up.",
            )

    def focus_branch_input(self) -> None:
        self.branch_input.focus()

    @on(Input.Changed, "#branch")
    def _branch_changed(self, event: Input.Changed) -> None:
        """Shows the folder the branch gets, as the placeholder of the folder input."""
        branch = event.value.strip() or "feature/vessel-owner"
        self.folder_input.placeholder = folder_for_branch(branch)

    @on(Button.Pressed, "#create")
    @on(Input.Submitted, "#branch, #base, #folder")
    def _create_pressed(self) -> None:
        branch = self.branch_input.value.strip()
        base = self.base_input.value.strip() or None
        folder = self.folder_input.value.strip() or None
        self.post_message(self.CreateRequested(branch, base, folder, self.open_checkbox.value))
