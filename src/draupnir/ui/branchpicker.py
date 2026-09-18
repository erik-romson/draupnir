"""The dialog that finds a branch on GitHub and opens it as a new clone."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Checkbox, Input, Label, OptionList
from textual.widgets.option_list import Option


def matching_branches(branches: Sequence[str], text: str) -> list[str]:
    """The branches that contain text anywhere in their name, ignoring case."""
    needle = text.strip().lower()
    return [branch for branch in branches if needle in branch.lower()]


@dataclass(frozen=True)
class BranchChoice:
    """The branch the user picked, and whether to open the IDE in the new clone."""

    branch: str
    open_after: bool


class BranchPickerScreen(ModalScreen[BranchChoice | None]):
    """Filters branches while the user types, and dismisses with the chosen branch.

    Up and down move through the list, enter or a click picks a branch, and escape cancels.
    """

    DEFAULT_CSS = """
    BranchPickerScreen { align: center middle; }
    BranchPickerScreen > Vertical {
        width: 90; height: 80%; padding: 1 2;
        border: thick $primary; background: $surface;
    }
    BranchPickerScreen OptionList { height: 1fr; margin: 1 0; }
    """
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("down", "move(1)", "Next", show=False),
        Binding("up", "move(-1)", "Previous", show=False),
    ]

    def __init__(self, branches: Sequence[str]) -> None:
        super().__init__()
        self.branches = list(branches)
        self.search = Input(placeholder="Type part of the branch name", id="branch-search")
        self.options = OptionList(id="branch-options")
        self.count = Label(id="branch-count")
        self.open_checkbox = Checkbox(
            "Open the IDE when the clone is ready", value=True, id="branch-open-after"
        )

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(
                "Open a branch from GitHub as a new clone. The list is as new as the last fetch, "
                "and leaves out branches that already have a clone."
            )
            yield self.search
            yield self.count
            yield self.options
            yield self.open_checkbox

    def on_mount(self) -> None:
        self._show_matches("")
        self.search.focus()

    @on(Input.Changed, "#branch-search")
    def _search_changed(self, event: Input.Changed) -> None:
        self._show_matches(event.value)

    def _show_matches(self, text: str) -> None:
        matches = matching_branches(self.branches, text)
        self.options.set_options(Option(branch, id=branch) for branch in matches)
        if matches:
            self.options.highlighted = 0
        self.count.update(f"{len(matches)} of {len(self.branches)} branches")

    def action_move(self, step: int) -> None:
        if self.options.option_count == 0:
            return
        current = self.options.highlighted or 0
        self.options.highlighted = max(0, min(self.options.option_count - 1, current + step))

    @on(Input.Submitted, "#branch-search")
    def _search_submitted(self) -> None:
        option = self.options.highlighted_option
        if option is not None and option.id is not None:
            self._choose(option.id)

    @on(OptionList.OptionSelected, "#branch-options")
    def _option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id is not None:
            self._choose(event.option.id)

    def _choose(self, branch: str) -> None:
        self.dismiss(BranchChoice(branch, self.open_checkbox.value))

    def action_cancel(self) -> None:
        self.dismiss(None)
