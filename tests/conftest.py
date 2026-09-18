"""Builds a small workspace in tmp_path, with a bare repository standing in for GitHub."""

from __future__ import annotations

import os
import shlex
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from fake_gh import STATE_VARIABLE, FakeGh

from draupnir.github import reset_build_estimate_cache, reset_login_cache
from draupnir.setup import init
from draupnir.workspace import Workspace


def _sh(cwd: Path, *args: str) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@dataclass
class Env:
    tmp: Path
    remote: Path
    root: Path
    main: Path
    idea_log: Path
    bin: Path
    gh: FakeGh

    def ws(self) -> Workspace:
        """Builds a fresh Workspace, so a test can change draupnir.toml first."""
        return Workspace.find(self.root)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    home.mkdir()
    bin_dir.mkdir()

    idea_log = tmp_path / "idea-calls.txt"
    mvn = bin_dir / "mvn"
    mvn.write_text('#!/bin/sh\necho "Apache Maven 3.9.9 (test fixture)"\n')
    idea = bin_dir / "idea"
    idea.write_text(f'#!/bin/sh\nprintf \'%s\\n\' "$@" >> "{idea_log}"\n')
    # The fake gh runs with the Python that runs the tests. -I and -S keep its start fast and
    # independent of the environment, because it only needs the standard library.
    gh = bin_dir / "gh"
    fake_gh = Path(__file__).with_name("fake_gh.py")
    gh.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -I -S {shlex.quote(str(fake_gh))} "$@"\n'
    )
    for script in (mvn, idea, gh):
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
    gh_state = tmp_path / "fake-gh-state.json"
    fake = FakeGh(gh_state)
    fake.reset()
    reset_login_cache()
    reset_build_estimate_cache()

    for name in list(os.environ):
        if name.startswith("DRAUPNIR_"):
            monkeypatch.delenv(name, raising=False)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    monkeypatch.setenv(STATE_VARIABLE, str(gh_state))

    # Written directly instead of three "git config --global" calls, which would each start
    # their own git process. Every test pays for this setup, so the fixture avoids process
    # starts it does not need.
    (home / ".gitconfig").write_text(
        "[user]\n\temail = test@example.com\n\tname = Test\n[init]\n\tdefaultBranch = main\n"
    )

    source = tmp_path / "source"
    source.mkdir()
    _sh(source, "git", "init", "-q")
    (source / "pom.xml").write_text("<project/>\n")
    (source / "App.java").write_text("line 1\nline 2\nline 3\n")
    _sh(source, "git", "add", ".")
    _sh(source, "git", "commit", "-q", "-m", "initial")

    # The bare clone's HEAD already follows source's default branch, main, because
    # init.defaultBranch above is set before source is initialized. No extra
    # "git symbolic-ref" call is needed to point it there.
    remote = tmp_path / "project.git"
    _sh(tmp_path, "git", "clone", "-q", "--bare", str(source), str(remote))

    root = tmp_path / "shop"
    root.mkdir()
    (root / "draupnir.toml").write_text(
        "[build]\npoll-seconds = 0.01\ngrace-seconds = 0.05\ntimeout-seconds = 2\n"
        # The dashboard timer is off, so it does not disturb tests. A test that needs the timer
        # sets its own value.
        "\n[ui]\nrefresh-seconds = 0\n"
    )

    ws = init(root, str(remote), lambda _line: None)

    return Env(
        tmp=tmp_path,
        remote=remote,
        root=root,
        main=ws.main,
        idea_log=idea_log,
        bin=bin_dir,
        gh=fake,
    )
