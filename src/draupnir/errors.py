from __future__ import annotations


class OperationError(Exception):
    """An operation stopped. The message is meant to be shown to the user."""


class RebaseConflict(OperationError):
    def __init__(self, files: list[str]) -> None:
        self.files = files
        super().__init__(
            f"The rebase stopped with conflicts in {len(files)} file(s). "
            "Resolve them in the IDE and continue the rebase there."
        )
