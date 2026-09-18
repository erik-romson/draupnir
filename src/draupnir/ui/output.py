"""The output area: every command and its output, and the "Stop waiting" button."""

from __future__ import annotations

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Button, Label, RichLog

OUTPUT_TITLE = "Output"


class OutputArea(Widget):
    """Shows the log of the running action, and lets the user stop waiting for a build.

    The widget holds no logic. The "Stop waiting" button posts `StopWaitingRequested`, and the
    app sets the cancel event of the running action.
    """

    DEFAULT_CSS = """
    OutputArea { height: auto; }
    OutputArea #output-bar { height: 3; padding: 0 1; }
    OutputArea #output-title { width: 1fr; height: 3; content-align: left middle; }
    OutputArea #output-log { height: 14; border: round $primary-darken-2; }
    """

    class StopWaitingRequested(Message):
        """The user pressed "Stop waiting"."""

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.title_label = Label(OUTPUT_TITLE, id="output-title")
        self.stop_button = Button(
            "Stop waiting",
            id="stop-waiting",
            disabled=True,
            tooltip="Stops waiting for the build. The build keeps running on GitHub.",
        )
        self.log_view = RichLog(
            id="output-log", wrap=True, markup=False, highlight=False, max_lines=5000
        )

    def compose(self) -> ComposeResult:
        with Horizontal(id="output-bar"):
            yield self.title_label
            yield self.stop_button
        yield self.log_view

    def write_line(self, line: str, style: str = "") -> None:
        """Adds one line to the bottom of the output."""
        self.log_view.write(Text(line, style=style))

    def begin_action(self, title: str) -> None:
        """Shows that the action called title has started, and enables "Stop waiting"."""
        self.title_label.update(f"{OUTPUT_TITLE}: {title}")
        self.write_line(f"=== {title}", "bold")
        self.stop_button.disabled = False

    def end_action(self) -> None:
        """Shows that no action runs any more, and disables "Stop waiting"."""
        self.title_label.update(OUTPUT_TITLE)
        self.stop_button.disabled = True

    @on(Button.Pressed, "#stop-waiting")
    def _stop_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.post_message(self.StopWaitingRequested())
