"""Tests of bin/cmux-resume, which runs as a subprocess against a stub cmux and a stub ps."""

import json
import subprocess
import sys
from pathlib import Path

from helpers import write_executable

SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "cmux-resume"
ATTACH = ["cmux", "local-tmux", "attach", "shop-feature-a", "--headless"]

_STUB_CMUX = """#!/bin/sh
echo "$*" >> "$STUB_DIR/cmux.log"
case "$1 $2" in
  "tree --all") cat "$STUB_DIR/tree.json" ;;
  "surface resume") cat "$STUB_DIR/binding.json" ;;
esac
"""

_STUB_PS = """#!/bin/sh
cat "$STUB_DIR/ps.txt"
"""


def run_resume(
    tmp_path: Path, ps_rows: str, saved_cwd: Path, bound_cwd: Path, *flags: str
) -> tuple[str, list[str]]:
    """Runs the script, and returns its output and the lines the stub cmux logged."""
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    write_executable(stubs / "cmux", _STUB_CMUX)
    write_executable(stubs / "ps", _STUB_PS)
    tree = {
        "windows": [
            {
                "workspaces": [
                    {
                        "id": "workspace-id",
                        "title": "shop feature-a",
                        "panes": [
                            {
                                "surfaces": [
                                    {
                                        "id": "surface-id",
                                        "ref": "surface:1",
                                        "type": "terminal",
                                        # /dev/null exists, so the script reads the process list.
                                        "tty": "null",
                                    }
                                ]
                            }
                        ],
                    }
                ]
            }
        ]
    }
    binding = {
        "resume_binding": {
            "launch_command": {"arguments": ATTACH},
            "cwd": str(bound_cwd),
        }
    }
    (stubs / "tree.json").write_text(json.dumps(tree))
    (stubs / "binding.json").write_text(json.dumps(binding))
    (stubs / "ps.txt").write_text(ps_rows)
    config = tmp_path / "cmux.json"
    config.write_text(
        json.dumps(
            {"terminal": {"resumeCommands": [{"cwd": str(saved_cwd), "commandPrefix": ATTACH}]}}
        )
    )
    result = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--config", str(config), *flags],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={"PATH": f"{stubs}:/usr/bin:/bin", "STUB_DIR": str(stubs)},
    )
    assert result.returncode == 0, result.stderr
    log = stubs / "cmux.log"
    return result.stdout, log.read_text().splitlines() if log.exists() else []


def test_a_live_attach_client_is_busy(tmp_path: Path) -> None:
    folder = tmp_path / "feature-a"
    folder.mkdir()
    rows = "100 1 S+ cmux local-tmux attach shop-feature-a --headless\n"

    output, log = run_resume(tmp_path, rows, folder, folder)

    assert output.startswith("busy     ")
    assert not [line for line in log if line.startswith("send")]


def test_a_tmux_client_is_busy(tmp_path: Path) -> None:
    folder = tmp_path / "feature-a"
    folder.mkdir()
    rows = "100 1 S+ tmux -S /x attach\n"

    output, log = run_resume(tmp_path, rows, folder, folder)

    assert output.startswith("busy     ")
    assert not [line for line in log if line.startswith("send")]


def test_a_tmux_client_with_a_path_is_busy(tmp_path: Path) -> None:
    folder = tmp_path / "feature-a"
    folder.mkdir()
    rows = "100 1 Ss+ -zsh\n101 100 S+ /opt/homebrew/bin/tmux attach\n"

    output, log = run_resume(tmp_path, rows, folder, folder)

    assert output.startswith("busy     ")
    assert not [line for line in log if line.startswith("send")]


def test_an_idle_shell_gets_the_resume_line(tmp_path: Path) -> None:
    folder = tmp_path / "feature-a"
    folder.mkdir()

    output, log = run_resume(tmp_path, "100 1 Ss+ -zsh\n", folder, folder)

    assert output.startswith("start    ")
    assert [line for line in log if line.startswith("send")]


def test_a_saved_command_whose_folder_is_gone_gets_nothing(tmp_path: Path) -> None:
    gone = tmp_path / "removed"
    bound = tmp_path / "feature-a"
    bound.mkdir()

    output, log = run_resume(tmp_path, "100 1 Ss+ -zsh\n", gone, bound, "--open-missing")

    assert output == f"gone     {gone} (the folder does not exist)\n"
    assert not [line for line in log if line.startswith(("send", "new-workspace"))]
