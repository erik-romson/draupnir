# Implementation plan

> **Finished.** All 14 steps are done. This plan is kept as a record of how draupnir was built.
> For the current design, see `docs/architecture.md` and `docs/decisions.md`.

draupnir is a terminal dashboard and a command line tool for working on several clones of one
git repository at the same time. This plan splits the work into 14 small steps. Each step can be
built and tested in one session, and each step ends with the quality gate green.

The steps run in order, one per session. A step records any new decision in `docs/decisions.md`
and marks itself done by appending "(done)" to its heading in this file.

## 2. Target structure

### 2.1 The tool repository

This repository is the tool. It is installed once per machine.

```
draupnir/
  pyproject.toml              uv project, hatchling, ruff, pyright strict, pytest
  uv.lock                     committed
  README.md                   for users: install, set up a workspace, daily work
  AGENTS.md                   for coding agents: gate, module rules, message style, where docs are
  CLAUDE.md                   one line, @AGENTS.md, so Claude Code reads the same instructions
  LICENSE                     MIT, copyright Erik Romson
  .gitignore                  also ignores tmp/
  .github/workflows/ci.yml    runs the quality gate on push, on ubuntu and macos
  docs/
    requirements.md           goal, concepts, R1 to R24, things outside the tool, later ideas
    decisions.md              D1 onwards, with the reasons
    implementation-plan.md    this plan, the steps and their state
  src/draupnir/
    __init__.py               version string, the single source of the version
    __main__.py               python -m draupnir
    cli.py                    argparse: subcommands, the default "ui" command, confirmations
    errors.py                 OperationError, RebaseConflict
    runner.py                 runs git, gh and other commands without prompts, git version check
    config.py                 reads draupnir.toml and DRAUPNIR_* variables into a frozen Config
    workspace.py              finds the workspace, main/, the clones, and the clone for a folder
    setup.py                  init, clone main, new clone, prepare, unsaved work, remove
    maven.py                  Maven version check and .mvn/maven.config
    status.py                 reads the git state of a clone without locks
    github.py                 login check, build state, waiting for builds, pull requests
    operations.py             fetch, update main/, rebase, push, fast-forward, pull request flow
    ide.py                    finds and starts IntelliJ IDEA or PyCharm
    ui/
      __init__.py
      app.py                  the Textual app: tabs, key bindings, state, running actions
      projects.py             the Projects tab: buttons, table, details line
      newclone.py             the New clone tab
      confirm.py              the yes/no dialog
      output.py                the output area with the "Stop waiting" button
  tests/
    conftest.py               builds a test workspace with a bare "GitHub" repository
    fake_gh.py                stand-in for gh, driven by a JSON state file
    helpers.py                typed helpers: commit, push from a second clone, logger
    test_cli.py
    test_runner.py
    test_config.py
    test_workspace.py
    test_setup.py
    test_maven.py
    test_status.py
    test_operations.py
    test_github.py
    test_ide.py
    test_ui_projects.py
    test_ui_newclone.py
    test_ui_actions.py
```

Rules for the modules:

- Modules outside `ui/` never import Textual. The UI calls them, and so does `cli.py`.
- Every function that runs commands takes a `log: LogFn` argument and raises `OperationError`
  with a message for the user when it stops.
- Functions that wait take a `cancel: threading.Event`.
- `setup.py` and `operations.py` take a `Workspace`, which holds the `Config`. No module reads
  the environment except `config.py` and `ide.py`.

### 2.2 A workspace

A workspace is a folder for one project. It is a small git repository that can be pushed to
GitHub, so the config travels between machines.

```
shop/                         the workspace, a git repository with up to three files
  .gitignore                  ignores everything except the files below
  README.md                   optional notes for this project
  draupnir.toml               config, see section 3
  main/                       clone on the default branch, ignored by git
    .m2repo/
    .mvn/maven.config
  feature-vessel-owner/       clone of branch feature/vessel-owner, ignored by git
    .m2repo/
    .mvn/maven.config
```

`draupnir init` writes `.gitignore` and `draupnir.toml`. It does not run `git init`. The README
tells the user how to push the workspace.

The `.gitignore` written by `init`:

```
# This folder is a draupnir workspace. Git keeps only the files below.
# main/ and the clones next to it are ignored.
/*
!/.gitignore
!/README.md
!/draupnir.toml
```

### 2.3 The command line

Every action in the dashboard is also a subcommand. This makes the tool usable from a shell and
from Claude Code, and it makes the actions testable without the UI.

| Command | What it does | Requirements |
|---|---|---|
| `draupnir` or `draupnir ui [branch]` | Starts the dashboard. A branch name fills in the New clone tab | R12, R14 |
| `draupnir init [url]` | Writes the workspace files and clones the project into `main/`. The url can be left out when `main/` exists or when `draupnir.toml` has `repository` | R1, R2, R3 |
| `draupnir new <branch> [--base <branch>] [--open]` | Creates a clone next to `main/` | R4, R5, R6, R8 |
| `draupnir prepare [folder]` | Copies the local files and sets up Maven. Safe to run again | R6, R8 to R11 |
| `draupnir remove [folder] [--force] [--yes]` | Deletes a clone after the safety checks | R7, R18 |
| `draupnir status` | Prints the status table as text | R15, R16 |
| `draupnir fetch` | Fetches every clone | R17 |
| `draupnir rebase [folder]` | Rebases on the default branch | R17 |
| `draupnir push [folder] [--follow]` | Pushes, and with `--follow` waits for the build | R17, R20, R21 |
| `draupnir build [folder]` | Asks GitHub once for the build result | R17 |
| `draupnir ff-main [folder] [--yes]` | Fast-forwards the default branch | R17, R18 |
| `draupnir pr [folder] [--yes] [--remove \| --keep]` | Pushes, creates or reuses a pull request, follows the build, merges | R17, R18 |
| `draupnir open [folder]` | Starts the IDE | R13, R17 |

Rules that hold for every subcommand:

- **Global option.** `-C <folder>` runs the command as if it was started in that folder. Tests and
  scripts use it.
- **Workspace search.** Every command except `init` searches upwards from the current folder for
  `draupnir.toml`. `init` works on the current folder.
- **Folder argument.** `[folder]` is a path, or the name of a folder directly under the workspace
  root. Without it, the command uses the clone that holds the current folder. When the current
  folder is not inside a clone, the message says to give a folder.
- **Confirmations.** `remove`, `ff-main` and `pr` ask "... [y/N]" on the terminal unless `--yes`
  is given. When standard input is not a terminal and `--yes` is missing, the command stops and
  says to add `--yes`. In the dashboard the confirmation is a dialog (R18).
- **After a merge in `pr`.** With `--remove`, the clone is removed without a question. With
  `--keep`, it stays. Without either, the command asks on a terminal, and without a terminal it
  keeps the clone and prints the `draupnir remove` command.
- **Ctrl+C** while waiting for a build stops the wait, the same as "Stop waiting", and the command
  exits with 130.
- **Output.** Every log line goes to standard output as it happens. Error messages go to standard
  error.
- **`draupnir <branch>`** is not a shortcut for `ui`. The branch must follow `ui`, so a typing
  mistake in a subcommand name gives an error and does not start the dashboard.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | The command did what it should. For `build` and `push --follow`: the build is green |
| 1 | The command stopped with a message (`OperationError`). This includes "no workspace found" and a broken `draupnir.toml` |
| 2 | Wrong arguments, reported by argparse |
| 3 | The command ran, but the build is not green: failure, no build, pending, timeout |
| 130 | Stopped with Ctrl+C |

## 3. Configuration

### 3.1 draupnir.toml

The file lives in the workspace root and is committed. Every key has a default, so an empty file
is valid. Paths in `local-files` are relative to the root of the project repository.

```toml
[workspace]
# GitHub URL of the project. draupnir init writes it. It is only used by init, to clone
# main/ on a new machine. At all other times the URL comes from main/.
repository = "git@github.example.com:acme/shop.git"

# Branch that new clones start from. Default: the default branch of origin, as main/ knows it.
base-branch = "main"

# Files that are not in git, but that every clone needs. Copied from main/ once,
# never overwritten, and hidden from git in each clone. Never list secrets here.
local-files = [
  "src/main/resources/local.properties",
  ".claude/settings.local.json",
  ".idea/runConfigurations",
  ".git/hooks/commit-msg",
]

[maven]
# Default: true when main/ has a pom.xml. Every clone follows main/, also a clone
# whose branch adds or removes pom.xml.
enabled = true
# The read-only tail. Default: ~/.m2/repository. Machine override: DRAUPNIR_MAVEN_SHARED_REPO.
shared-repository = "~/.m2/repository"

[build]
poll-seconds = 15
grace-seconds = 180
timeout-seconds = 3600

[actions]
# Set to false when the default branch is protected on GitHub. The button is then hidden.
fast-forward-main = true
# Fast-forward main refuses when the build of the pushed commit is not green.
fast-forward-needs-green-build = true
# After a merged pull request, ask whether to remove the clone.
offer-remove-after-merge = true

[ui]
# Read the status of every clone again after this many seconds. 0 turns it off.
# The timer never fetches, and it skips a read while an action or another read runs.
refresh-seconds = 10

[ide]
# Commands that start the IDE. Default: idea and pycharm from PATH, JetBrains Toolbox
# or /Applications. Machine override: DRAUPNIR_IDEA and DRAUPNIR_PYCHARM.
java = "idea"
python = "pycharm"
```

`draupnir init` writes a file with `repository` filled in and every other key present but
commented out, with the comments above. The user can then see what can be set.

### 3.2 Environment variables

These hold values that differ per machine and must not be committed.

| Variable | Meaning |
|---|---|
| `DRAUPNIR_IDEA` | Command that starts IntelliJ IDEA, for example `idea` or `/opt/idea/bin/idea.sh` |
| `DRAUPNIR_PYCHARM` | Command that starts PyCharm |
| `DRAUPNIR_MAVEN_SHARED_REPO` | The read-only Maven tail, when it is not `~/.m2/repository` |

### 3.3 How config is read

- `load_config(root)` reads the file once with `tomllib` into a frozen `Config` dataclass with
  one frozen dataclass per section.
- The order is: defaults, then the file, then the environment variables. `~` is expanded in
  paths.
- For the IDE, an environment variable wins over the file, because the variable is the machine's
  own value. The order is set up in Step 9.
- Unknown sections, unknown keys and wrong types stop the tool. The message names the file and
  the key, for example: "draupnir.toml has an unknown key 'build.poll-second'. Check the
  spelling. The known keys in [build] are poll-seconds, grace-seconds and timeout-seconds."
- A path in `local-files` must be relative and must not contain `..`. Otherwise the tool stops
  and names the entry.
- The times in `[build]` must be positive numbers. `ui.refresh-seconds` must be zero or positive,
  because 0 turns the timer off. Floats are allowed, so tests can use milliseconds.
- A syntax error in the file gives the line number from `tomllib` and the file name.

## 4. Tooling and quality gate

- **uv** manages Python, the virtual environment and the lock file. Python 3.13 or newer.
  `uv.lock` is committed.
- **hatchling** builds the wheel from `src/draupnir`. The version is read from
  `src/draupnir/__init__.py`. The script entry point is `draupnir = "draupnir.cli:main"`.
- **ruff** with `select = ["E", "F", "I", "B", "UP", "SIM"]`, line length 100, target `py313`, and
  `extend-exclude = ["tmp"]`. `ruff format` is used for formatting.
- **pyright** in strict mode over `src` and `tests`, Python 3.13.
- **pytest** with **pytest-asyncio** in auto mode, so UI tests are plain `async def` functions.
- **textual** pinned to `>=8.2,<9`, because the widget and test pilot APIs change between major
  versions.
- The gate is `uv run ruff check`, `uv run ruff format --check`, `uv run pyright` and
  `uv run pytest`. Every step ends with the gate green.
- Install on a machine with `uv tool install git+<repository url>` or, from a checkout,
  `uv tool install --editable .`. During development, `uv run draupnir`. The repository is
  public, so `uv tool install git+https://github.com/erik-romson/draupnir` works without
  credentials.
- CI runs on GitHub Actions in `github.com/erik-romson/draupnir`.

Checked lower bounds on 2026-09-16: textual 8.2, ruff 0.16, pyright 1.1.414, pytest 9,
pytest-asyncio 1.4, hatchling 1.32.

Runtime needs outside Python: git 2.31 or newer (for `--force-if-includes` and
`rev-parse --path-format`), `gh` for the GitHub actions, Maven 3.9.0 or newer for Maven projects.

## 5. Testing approach

`tests/conftest.py` builds a small workspace in `tmp_path`:

- A bare repository plays GitHub. Tests push to it and read from it with plain git.
- A stub `mvn` on `PATH` prints a version line. Tests can choose the version. A stub `mvnw` can be
  written into a clone.
- `fake_gh.py` on `PATH` plays `gh`. A JSON file holds the build results per poll, the pull
  requests and the repository settings. The fake grows with the steps (see below).
- A stub `idea` on `PATH` writes its arguments to a file.
- `HOME`, `GIT_CONFIG_GLOBAL` and `PATH` are replaced for the test, so nothing touches the real
  machine. `DRAUPNIR_*` variables are removed.
- The fixture writes `draupnir.toml` with millisecond build times and runs `setup.init` in
  Python, not a script.
- `tests/helpers.py` has typed helpers, so pyright strict passes for the tests too:
  `commit(repo, name, text, message) -> str`, `push_from_other_clone(env, branch)`,
  `logger() -> tuple[list[str], LogFn]`.

How `fake_gh.py` grows:

| Step | What the fake learns |
|---|---|
| 11 | `api .../check-runs` and `api .../status` (commit statuses, for a Jenkins-style result). A switch per endpoint makes that call fail, so a test can break one of the two reads. `auth status --hostname <host>` with a "logged in" switch. It records every call with its folder in the state file |
| 13 | `repo view --json rebaseMergeAllowed`, with a setting that is true by default. `pr list --head <branch> --base <branch> --json number,url,headRefOid,baseRefName`, which filters on both. `pr create`, `pr view --json state,isDraft,mergeStateStatus,reviewDecision,headRefOid,baseRefName,autoMergeRequest`, `pr merge --rebase --match-head-commit`, `pr merge --rebase --auto`, `api repos/{owner}/{repo}` for `allow_auto_merge`. The state can hold several open pull requests for one head branch with different bases. A merge rewrites the commits with a new committer date, the way GitHub's rebase merge does, and can delete the head branch |

Tests call the Python functions directly. CLI tests call `cli.main(argv)` and read `capsys`. UI
tests drive the app with Textual's test pilot, including the confirmation dialog. They wait on
`app.workers.wait_for_complete()` and `pilot.pause()`, never on fixed sleeps.

Every step lists the tests that prove it. A test name says what the test checks, for example
`test_remove_refuses_when_a_stash_exists`. Section 8 maps every requirement to its tests.

## 6. Rules that every step follows

These rules come from the requirements and the decisions. They are written once here, so the
steps can be short.

1. **Commands never wait for input (R22).** All commands go through `runner.run`. It sets
   `stdin` to nothing, starts the command in its own session, and sets `GIT_TERMINAL_PROMPT=0`,
   `GIT_OPTIONAL_LOCKS=0`, `GIT_EDITOR=true`, `GIT_SEQUENCE_EDITOR=true`, `GCM_INTERACTIVE=never`,
   `SSH_ASKPASS_REQUIRE=never`, `GH_PROMPT_DISABLED=1`, `GH_NO_UPDATE_NOTIFIER=1`, `NO_COLOR=1`
   and `CLICOLOR=0`.
2. **No locks when reading (R23).** Status reads use only commands that respect
   `GIT_OPTIONAL_LOCKS=0`, and file checks in the git folder.
3. **Parse only stable output.** The tool reads `git status --porcelain=v2`,
   `git push --porcelain`, `rev-list --count` and JSON from `gh`. It never reads messages meant
   for people, because git and gh translate and reword them. The one exception is the
   `Apache Maven x.y.z` line from `mvn -v`.
4. **Messages (R24, D13).** Every `OperationError` says what happened and what to do next, in
   full sentences. The first line must make sense alone, because the dashboard shows it in a
   notification.
5. **Timeouts.** Network commands get a timeout, so a dead VPN cannot block the one action slot
   forever: `git fetch` and `git push` 10 minutes, `gh` calls 2 minutes. `git clone` and Maven get
   none. The values are constants in `runner.py`, not config.
6. **Checks before actions.** Every action that changes a clone refuses while a rebase, merge,
   cherry-pick, revert or `git am` is in progress, and names the operation.
7. **No project or company names in committed files.** The repository is public. Code, messages,
   defaults, tests, `README.md`, `AGENTS.md` and `docs/` use a neutral example: a Java and Maven
   project called `shop`, on `github.example.com`. Test fixtures call the bare repository
   `project.git`.
8. **Docs stay current.** A step that makes a decision adds it to `docs/decisions.md`. A finished
   step is marked "(done)" in its heading in `docs/implementation-plan.md`.

## 7. Steps

Each step says what it builds, which files it touches, which requirements it covers, whether it
reuses, changes or rewrites earlier code, and which tests prove it. "Done when" is the same for
every step: the listed tests exist and pass, and the gate is green. A step also follows rule 8 in
section 6: it records new decisions in `docs/decisions.md` and marks itself done in this file.

### Step 1: Project skeleton, docs and quality gate (done)

**Goal.** A `draupnir` command exists, prints its version and help, and the quality gate runs
green. The repository has the docs that later sessions need, and it is pushed to
`github.com/erik-romson/draupnir` with CI running.

**Files.** `pyproject.toml`, `uv.lock`, `.gitignore`, `LICENSE`, `README.md` (short first
version), `AGENTS.md`, `CLAUDE.md`, `docs/requirements.md`, `docs/decisions.md`,
`docs/implementation-plan.md`, `src/draupnir/__init__.py`, `__main__.py`, `cli.py`, `errors.py`,
`tests/test_cli.py`, `.github/workflows/ci.yml`.

**Covers.** Groundwork for everything. No R-number.

**Details.**
- `.gitignore` ignores `tmp/`, `.venv/`, `__pycache__/`, `.pytest_cache/`, `.ruff_cache/`,
  `dist/`, `.idea/` and `plan_with_review.*`.
- `pyproject.toml` gets `license = "MIT"` and `license-files = ["LICENSE"]` in the PEP 639 form
  that hatchling supports, `readme = "README.md"`, and `[project.urls]` for Homepage, Repository
  and Issues on `github.com/erik-romson/draupnir`. It gets no license classifier, because PEP 639
  replaces license classifiers with the `license` field.
- `LICENSE` holds the standard MIT license text with the line "Copyright (c) 2026 Erik Romson".
- `cli.py` has an argparse parser with subparsers and the global `-C` option. Every subcommand
  from section 2.3 is registered with its arguments. In this step each one prints "This command
  is not built yet." and returns 1. Each subcommand later gets a small `run_<name>(args) -> int`
  function in `cli.py` that calls the module that does the work.
- With no subcommand, `main` will start the dashboard. Until Step 8 it prints the help.
- `cli.py` has `confirm(question, yes: bool) -> bool` for the terminal. It returns true for
  `--yes`, asks on a terminal, and raises `OperationError` when standard input is not a terminal.
- `main` catches `OperationError`, prints it to standard error and returns 1. It catches
  `KeyboardInterrupt` and returns 130. There is no other exception type for exit codes. Only
  argparse returns 2.
- `ci.yml` installs uv with `astral-sh/setup-uv`, runs `uv sync --locked` and the gate on
  `ubuntu-latest` and `macos-latest`. It also sets the git user name and email, which the tests
  need.

**Tests.**
- `test_help_lists_every_subcommand`
- `test_unknown_subcommand_exits_with_two`
- `test_version_prints_the_package_version`
- `test_confirm_without_a_terminal_asks_for_yes`
- `test_confirm_with_yes_does_not_ask`

### Step 2: Command runner (done)

**Goal.** One place that runs external commands, never waits for input, streams output to a log
function, and raises a clear error on failure.

**Files.** `src/draupnir/runner.py`, `tests/test_runner.py`.

**Covers.** R22, R23 (through `GIT_OPTIONAL_LOCKS=0`), R24 (error message shape).

**Details.**
- `run` gets a `timeout` argument. The runner then starts a timer. When it fires, the timer sends
  SIGTERM and then SIGKILL to the whole process group, and `run` raises `CommandError` with the
  sentence "The command took longer than N seconds and was stopped."
- Commands start with `start_new_session=True`. This takes the terminal away from them, so ssh
  cannot ask for a passphrase, and it lets the timeout stop child processes such as `ssh`.
- The environment gets the extra variables from rule 1 in section 6.
- `git(cwd, *args)` keeps `cwd` first, as before.
- New `require_git()` runs `git --version` once, and raises when git is older than 2.31. The
  message names the version found and the version needed.
- New `NETWORK_HINT` sentence. When `git clone`, `fetch`, `ls-remote` or `push` fails with an
  error that is not a push refusal, the message ends with: "If git could not log in, load your
  SSH key into ssh-agent or set up a credential helper. draupnir cannot answer a password
  question." The hint does not depend on the text of the git error.

**Tests.**
- `test_run_captures_stdout_and_stderr_separately`
- `test_run_with_log_streams_every_line_in_order`
- `test_run_raises_command_error_with_the_last_lines`
- `test_run_reports_a_missing_command`
- `test_run_sets_the_no_prompt_variables` (a shell script echoes the variables)
- `test_run_starts_the_command_in_its_own_session` (a script prints whether it leads its session)
- `test_run_kills_a_command_after_the_timeout`
- `test_run_timeout_also_stops_child_processes` (a script starts `sleep` in the background and
  waits)
- `test_require_git_refuses_an_old_version` (a stub `git` prints `git version 2.20.0`)

### Step 3: Config and workspace discovery (done)

**Goal.** Read `draupnir.toml`, find the workspace root from any folder inside it, find the clone
for a folder, and list `main/` and the clones next to it.

**Files.** `src/draupnir/config.py`, `workspace.py`, `tests/test_config.py`,
`tests/test_workspace.py`, `tests/conftest.py`, `tests/helpers.py`.

**Covers.** R15, the config half of R6, R8, R13 and R21.

**Details.**
- `Config` and `load_config(root)` as in section 3.3.
- `Workspace` holds `root`, `config` and `main`. `Workspace.find(start)` raises `OperationError`
  that names the folder searched and says that a workspace has `draupnir.toml` and `main/`. When
  `draupnir.toml` is found but `main/` is missing, the message says to run `draupnir init`.
- `Workspace.clone_for(path)` returns the folder directly under the root that holds `path`. It
  raises when `path` is the root or outside it. The CLI uses it for the optional folder argument.
- `main_branch()` reads `refs/remotes/origin/HEAD` in `main/`. When that ref is missing, it
  raises: "main/ does not know the default branch of GitHub. Run
  git -C main remote set-head origin --auto."
- `normalize_url(url)` makes `git@host:owner/repo.git`, `ssh://git@host/owner/repo` and
  `https://host/owner/repo.git` equal, so a clone made by hand with the other URL form is still
  listed. Local paths are compared as resolved paths.

**Tests.**
- `test_empty_config_gives_defaults`
- `test_config_reads_every_section`
- `test_config_rejects_an_unknown_key_with_its_name`
- `test_config_rejects_a_wrong_type_with_the_key`
- `test_config_rejects_a_local_file_outside_the_project`
- `test_config_syntax_error_names_the_file_and_line`
- `test_config_accepts_zero_refresh_seconds`
- `test_config_rejects_negative_refresh_seconds_and_non_positive_build_times`
- `test_environment_overrides_ide_and_shared_repository`
- `test_find_workspace_from_inside_a_clone`
- `test_find_workspace_fails_with_a_helpful_message`
- `test_find_workspace_without_main_says_to_run_init`
- `test_clone_for_a_subfolder_gives_the_clone`
- `test_project_paths_lists_main_first_then_clones_of_the_same_remote`
- `test_project_paths_ignores_a_clone_of_another_repository`
- `test_project_paths_matches_ssh_and_https_urls`
- `test_main_branch_comes_from_origin_head`
- `test_main_branch_without_origin_head_explains_the_fix`

### Step 4: Prepare a folder: local files and Maven (done)

**Goal.** `draupnir prepare [folder]` copies the local files from `main/`, hides them from git,
and writes the Maven config. Running it twice changes nothing.

**Files.** `src/draupnir/setup.py` (the `prepare` function), `maven.py`, `cli.py`,
`tests/test_setup.py`, `tests/test_maven.py`, `tests/test_cli.py`.

**Covers.** R6, R8, R9, R10, R11.

**Details.**
- `prepare(ws, folder, log)` works in two phases. The first phase only reads. The second phase
  changes files, and it starts only when every check of the first phase passed. So when `prepare`
  stops in the first phase, the clone, the exclude file and `main/` are exactly as before (R10).
- Phase 1, the checks:
  - `folder` is the top of a clone (`git rev-parse --show-toplevel`) and is not the workspace
    root.
  - The exclude file path is `$(git rev-parse --path-format=absolute --git-dir)/info/exclude`.
    It is only computed here.
  - Local files: for each path in `config.workspace.local_files`, find out whether git tracks it
    in `main/`, whether it exists in the folder, and whether it exists in `main/`. The result is
    a list of planned outcomes. Nothing is copied yet.
  - Maven is on when `config.maven.enabled` is true, or, when the key is not set, when `main/`
    has a `pom.xml`. Every clone follows `main/`, so a branch that adds or removes `pom.xml` does
    not change the Maven setup. A project that needs another choice sets the key.
  - When Maven is on: `maven.version(folder)` runs `./mvnw -B -v` when `mvnw` is executable,
    otherwise `mvn -B -v`, and reads the `Apache Maven x.y.z` line. It raises when Maven cannot be
    run, and when the version is below 3.9.0. Both messages say why the version matters.
  - When Maven is on and `.mvn/maven.config` is tracked, `prepare` raises. The message says the
    project needs another place for these settings.
- Phase 2, the changes:
  - Create the folder of the exclude file when it is missing. A line is added to the exclude file
    only when it is not already there.
  - Local files: add `/<path>` to the exclude file unless the path is tracked or starts with
    `.git/`, then copy it from `main/` when it is missing. Folders are copied with their files,
    and file modes are kept, so a copied hook stays executable. `main/` itself only gets the
    exclude lines. Each outcome is logged in one sentence: copied, kept, skipped because tracked,
    skipped because missing in `main/`.
  - Maven, when on: write the two `-D` lines with absolute paths into `.mvn/maven.config`, create
    `.m2repo`, and add `/.mvn/maven.config` and `/.m2repo/` to the exclude file. When the file
    already has the same content, it is not written again, so running twice changes nothing.
- A failure in phase 2, for example a full disk, can leave a partial result. Running `prepare`
  again finishes it, because every change is safe to repeat.
- The CLI finds the workspace with `Workspace.find`. Outside a workspace the command prints the
  message on standard error and exits with 1 (section 2.3).

**Tests.**
- `test_prepare_copies_missing_local_files_and_keeps_existing_ones`
- `test_prepare_copies_a_folder_with_its_files`
- `test_prepare_skips_files_that_git_tracks`
- `test_prepare_adds_local_files_and_maven_files_to_exclude`
- `test_prepare_hides_but_does_not_copy_into_main`
- `test_prepare_copies_a_git_hook_without_excluding_it`
- `test_prepare_can_run_twice_without_changes`
- `test_prepare_refuses_the_workspace_root`
- `test_prepare_gives_main_its_own_m2repo` (R11)
- `test_maven_config_has_absolute_paths_for_head_and_tail`
- `test_maven_shared_repository_comes_from_config_and_environment`
- `test_maven_prefers_mvnw_over_mvn`
- `test_maven_older_than_3_9_0_is_refused`
- `test_maven_that_cannot_run_gives_a_message`
- `test_prepare_refuses_when_maven_config_is_tracked_and_changes_nothing` (with local files
  configured and missing in the clone: no file is copied, the exclude file is unchanged, and no
  `.mvn` or `.m2repo` folder is created)
- `test_prepare_refuses_an_old_maven_and_changes_nothing`
- `test_prepare_skips_maven_when_main_has_no_pom`
- `test_prepare_sets_up_maven_in_a_clone_whose_branch_removed_the_pom`
- `test_prepare_skips_maven_in_a_clone_whose_branch_added_a_pom`
- `test_prepare_command_works_on_the_current_clone`
- `test_command_outside_a_workspace_exits_with_one`

### Step 5: Init, clone main, new clone (done)

**Goal.** A new machine gets a working workspace with `draupnir init <url>`, or with
`draupnir init` in a cloned workspace. An existing folder with `main/` becomes a workspace with
`draupnir init`. `draupnir new <branch>` creates a clone.

**Files.** `src/draupnir/setup.py` (`init`, `clone_main`, `new_clone`, `folder_for_branch`),
`cli.py`, `tests/test_setup.py`, `tests/conftest.py`.

**Covers.** R1, R2, R3, R4, R5.

**Details.**
- `init(root, url, log)`:
  1. Refuses when `root` is inside a git work tree whose top is not `root`, for example
     `main/src`.
  2. Refuses when `root` is itself the top of a git work tree that tracks any file other than
     `.gitignore`, `README.md` and `draupnir.toml`. That catches `main/` and other project
     clones. A cloned workspace repository passes, which is the R2 case.
  3. Writes `.gitignore` when it is missing. Writes `draupnir.toml` when it is missing.
  4. Finds the URL: the argument, or `workspace.repository` from the config.
  5. When `main/` is missing, clones the URL into `main/`. Without a URL it stops and says to
     give one.
  6. When `main/` exists, the URL must be empty or match the origin of `main/` after
     `normalize_url`.
  7. Writes `repository` into `draupnir.toml` when the key is not set.
  8. Runs `git remote set-head origin --auto` in `main/` when `origin/HEAD` is missing.
  9. Prepares `main/`.
- `new_clone(ws, branch, base, log)`:
  1. Validates the name with `git check-ref-format --branch`.
  2. Refuses an existing folder.
  3. Checks with `git ls-remote --heads origin` in `main/` whether the branch and the base exist
     on GitHub. The base must exist, otherwise it stops and says to push the base first.
  4. Clones from `main/` on disk, sets `origin` to the GitHub URL of `main/`, and fetches with
     prune.
  5. Checks out the branch with tracking when it exists on origin. Otherwise creates the branch
     from `origin/<base>` without tracking. `base` defaults to `workspace.base-branch`, then to
     the default branch.
  6. Deletes the local branch that `git clone` created from `main/`, when its name differs from
     the new branch. The clone then has one local branch.
  7. Calls `prepare`. When `prepare` fails, the folder stays, and the message says: "The clone
     <folder> was created, but the setup stopped: <reason>. Fix the problem and run
     draupnir prepare <folder>."
  8. Returns the folder.
- `folder_for_branch` replaces `/` with `-`.
- The conftest fixture now calls `setup.init`.

**Tests.**
- `test_init_clones_main_and_writes_the_workspace_files`
- `test_init_writes_the_repository_url_into_the_config`
- `test_init_in_a_cloned_workspace_uses_the_url_from_the_config` (R2)
- `test_init_adopts_an_existing_main_folder` (R3)
- `test_init_refuses_a_url_that_differs_from_main`
- `test_init_refuses_to_run_inside_a_project_clone`
- `test_init_sets_origin_head_when_it_is_missing`
- `test_init_can_run_twice`
- `test_workspace_gitignore_hides_main_and_clones` (R1: `git status --porcelain` in the workspace
  shows only the three files after `git init`)
- `test_new_clone_tracks_a_branch_that_exists_on_github`
- `test_new_clone_creates_a_branch_from_the_base`
- `test_new_clone_refuses_a_base_that_is_not_on_github`
- `test_new_clone_points_origin_at_github_not_at_main`
- `test_new_clone_does_not_get_local_only_branches_from_main`
- `test_new_clone_has_only_its_own_local_branch`
- `test_new_clone_works_after_main_is_moved_away` (R5: `git log` and `git fsck` succeed)
- `test_new_clone_refuses_an_existing_folder`
- `test_new_clone_refuses_a_bad_branch_name`
- `test_new_clone_folder_name_replaces_slashes`
- `test_new_clone_keeps_the_folder_and_explains_when_prepare_fails`

### Step 6: Remove a clone (done)

**Goal.** `draupnir remove [folder]` deletes a clone, but only when nothing would be lost.

**Files.** `src/draupnir/setup.py` (`unsaved_work`, `remove_clone`), `cli.py`,
`tests/test_setup.py`.

**Covers.** R7, R18 (the terminal confirmation).

**Details.**
- `unsaved_work(ws, folder, log) -> list[str]` fetches with prune first. When the fetch fails, it
  raises, because without a fetch it cannot know what GitHub has. Then it returns one sentence
  per finding:
  - Uncommitted changes, from `git status --porcelain`. Untracked files count too, except the
    local files and Maven files, which the exclude file hides anyway.
  - Stashes, from `git stash list`.
  - Commits on local branches that are not saved. A commit on local branch `b` counts as saved
    when it is on any remote branch. A commit that is not a merge also counts as saved when an
    equal change is already on `origin/<default>`. A merge commit never counts as saved that way,
    because git cannot compare its change, and it can hold a conflict resolution that exists
    nowhere else. The tool finds the unsaved commits in two parts:
    - Every merge commit in `git rev-list --merges b --not --remotes`.
    - Every commit that is in both `git rev-list --no-merges b --not --remotes` and
      `git rev-list --cherry-pick --right-only --no-merges origin/<default>...b`. The second list
      drops commits whose change is already on the default branch, which is the case after
      GitHub's "rebase and merge", even when GitHub deleted the branch.
  - The sentence names the branch and the number of commits, and says how many of them are merge
    commits. The first five commit subjects are logged.
- An empty list means removal is safe.
- `remove_clone(ws, folder, force, log)` refuses `main/`, the root, and any folder that is not
  directly under the root or is not a clone. Without `force`, it raises when `unsaved_work` is
  not empty. The message lists every reason and says to push, or to use `--force` to delete
  anyway.
- Deleting logs "Deleting <folder>. A large .m2repo can take a while." and uses `shutil.rmtree`.
- The CLI asks "Delete <folder> with its .m2repo and local files? [y/N]" unless `--yes`.
- A squash merge is not recognised. That is fine, because the pull request flow always merges
  with rebase (D11). The README says what to do after a squash merge made by hand.
- A clone with a local merge commit that was never pushed is refused, also after its pull request
  was merged. The message says to push the branch first, or to use `--force` when the merge is no
  longer needed.

**Tests.**
- `test_remove_deletes_a_clean_clone_with_its_m2repo`
- `test_remove_refuses_uncommitted_changes`
- `test_remove_refuses_when_a_stash_exists`
- `test_remove_refuses_unpushed_commits_on_any_branch`
- `test_remove_accepts_commits_that_were_rebase_merged_and_whose_branch_was_deleted`
- `test_remove_refuses_an_unpushed_merge_commit_whose_parents_are_on_github` (the feature branch
  is pushed, `origin/main` gets a new commit, the clone merges `origin/main` with a conflict
  resolution and does not push)
- `test_remove_refuses_when_the_fetch_fails`
- `test_remove_message_lists_every_reason_and_the_next_step`
- `test_remove_with_force_deletes_anyway`
- `test_remove_refuses_main_and_folders_outside_the_workspace`
- `test_remove_command_asks_before_deleting`

### Step 7: Read the status of a clone, and fetch (done)

**Goal.** A pure read of the state a clone is in, for the table and for the checks before
actions. A fetch for every clone.

**Files.** `src/draupnir/status.py`, `operations.py` (`fetch`, `fetch_all`), `cli.py` (`status`,
`fetch`), `tests/test_status.py`, `tests/test_operations.py`.

**Covers.** R15, R16, R17 (Fetch all, as a command), R23.

**Details.**
- `read_status(ws, path)` takes the `Workspace`, so it knows the default branch.
- It uses `git status --porcelain=v2 --branch`, `git rev-list --left-right --count
  HEAD...origin/<default>`, and file checks in the git folder. No command takes a lock, because
  the runner sets `GIT_OPTIONAL_LOCKS=0`.
- New field `upstream_gone`. When `# branch.upstream` is present but `# branch.ab` is missing,
  the branch on GitHub was deleted. The table shows "deleted on GitHub".
- The operation in progress detects: rebase (`rebase-merge`, or `rebase-apply` without
  `applying`), `git am` (`rebase-apply/applying`), merge, cherry-pick, revert (`REVERT_HEAD`) and
  bisect (`BISECT_LOG`).
- `ProjectStatus.error` keeps one broken clone showing an error in its row, without stopping the
  table.
- `fetch_all(ws, log)` fetches every clone with prune. When one fetch fails, it logs the reason
  and goes on with the next clone, and at the end it raises one error that names the clones that
  failed.
- `draupnir status` prints one line per clone with the columns of the table except Build, because
  the command line has no build memory.

**Tests.**
- `test_status_counts_ahead_and_behind_main`
- `test_status_counts_ahead_and_behind_remote`
- `test_status_reports_not_pushed_when_there_is_no_upstream`
- `test_status_reports_an_upstream_that_was_deleted_on_github`
- `test_status_counts_changed_and_untracked_files`
- `test_status_detects_a_rebase_in_progress`
- `test_status_detects_a_merge_cherry_pick_revert_and_bisect`
- `test_status_reports_detached_head`
- `test_status_of_a_broken_clone_is_an_error_row`
- `test_status_does_not_touch_the_index` (the index file's mtime is unchanged after a read)
- `test_status_command_prints_one_line_per_clone`
- `test_fetch_all_updates_every_clone_and_names_the_ones_that_failed`

### Step 8: Read-only dashboard (done)

**Goal.** `draupnir` opens the Projects tab with the table, the details line, Refresh, Fetch all,
and the output area. Only one action runs at a time.

**Files.** `src/draupnir/ui/__init__.py`, `ui/app.py`, `ui/projects.py`, `ui/output.py`, `cli.py`
(`ui` as the default subcommand), `tests/test_ui_projects.py`.

**Covers.** R15, R16, R17 (Fetch all, Refresh), R19, R23 (the timer only reads).

**Details.**
- `app.py` owns the state: statuses, known builds, `busy`, the cancel event, and
  `start(title, operation)`. Operations run in a thread worker. Every log line goes through
  `call_from_thread`. Success and failure are shown in the output with a colour, and in a
  notification.
- `projects.py` is a `Widget` with the button grid, the `DataTable` and the details `Static`. It
  posts messages such as `ProjectSelected` and `ActionRequested(name)` to the app, so it holds no
  logic. All buttons from R17 are in the grid from this step on, but the ones not built yet are
  hidden.
- `output.py` holds the `RichLog` and the "Stop waiting" button.
- Buttons are disabled while an action runs. Actions that need a feature branch are disabled when
  `main/` or a clone on the default branch is selected.
- Key bindings: `r` Refresh, `f` Fetch all, `p` Projects, `q` Quit. `o` and `n` come in Step 9.
- Timer: every `ui.refresh-seconds` the app reads the status again, but only when no action runs
  and no status read is running. It never fetches. The default is 10 seconds, and 0 turns the
  timer off.
- A broken `draupnir.toml` or a missing workspace stops the command before the app starts, with
  the message on standard error and exit code 1.

**Tests.**
- `test_projects_tab_lists_main_and_every_clone`
- `test_table_shows_every_column_from_r16`
- `test_refresh_shows_new_commits`
- `test_fetch_all_updates_behind_counts`
- `test_output_area_shows_the_command_and_its_output`
- `test_only_one_action_runs_at_a_time` (a second start while busy is refused with a message)
- `test_feature_actions_are_disabled_for_main`
- `test_timer_reads_the_status_again_without_fetching`
- `test_timer_does_not_read_while_an_action_runs`

### Step 9: New clone tab, IDE and Remove clone (done)

**Goal.** Create a clone from the dashboard, start the IDE in it, and remove a clone after a
confirmation. A branch name on the command line fills in the form.

**Files.** `src/draupnir/ui/newclone.py`, `ui/confirm.py`, `ui/app.py`, `ui/projects.py`,
`ide.py`, `cli.py` (`open`, `new --open`, `ui <branch>`), `tests/test_ui_newclone.py`,
`tests/test_ui_actions.py`, `tests/test_ide.py`.

**Covers.** R7 and R18 in the dashboard, R12, R13, R14, R17 (Open IDE, Remove clone).

**Details.**
- `ide_kind(folder)`: `idea` for `pom.xml` and Gradle files, `pycharm` for `pyproject.toml`,
  `setup.py` and `requirements.txt`, `idea` otherwise.
- `ide_command(folder, config)` order: `DRAUPNIR_IDEA` or `DRAUPNIR_PYCHARM`, then `ide.java` or
  `ide.python` from the config, then `idea` or `pycharm` on `PATH` or in the Toolbox scripts
  folder, then the app bundles in `/Applications` and `~/Applications` on macOS. The variable
  comes before the config, because the variable is this machine's own value.
- A configured command is split with `shlex.split`, so paths with spaces work when they are
  quoted.
- Starting the IDE never blocks. Its output goes nowhere, and it runs in its own session.
- The message when nothing is found names the variable and the config key.
- The form validates the branch name before it starts the operation, so an empty name gives a
  message without a worker.
- The form's base input shows the default base branch as a placeholder.
- Key bindings `o` Open IDE and `n` New clone.
- "Remove clone" asks "Delete the folder <name>? ..." After a refusal, the output shows every
  reason from `unsaved_work`. The dashboard never forces a removal. The command line with
  `--force` is the way to do that, and the message says so.

**Tests.**
- `test_ide_kind_picks_idea_for_pom_and_pycharm_for_pyproject`
- `test_ide_command_prefers_environment_then_config_then_path`
- `test_ide_command_finds_the_toolbox_script`
- `test_ide_command_missing_gives_a_message_that_names_the_variable`
- `test_open_ide_starts_the_command_detached` (a stub `idea` script writes its arguments to a
  file)
- `test_new_command_with_open_starts_the_ide`
- `test_new_clone_tab_creates_a_clone_and_the_table_shows_it`
- `test_branch_argument_fills_in_the_form_and_opens_the_tab`
- `test_new_clone_tab_shows_a_message_for_an_empty_name`
- `test_ui_remove_asks_for_confirmation_and_deletes_the_clone`
- `test_ui_remove_answered_no_keeps_the_clone`
- `test_ui_remove_refusal_shows_every_reason`

### Step 10: Rebase and push (done)

**Goal.** Rebase the selected clone on the default branch, handle conflicts as decided, and push
with a safe force.

**Files.** `src/draupnir/operations.py`, `ui/app.py`, `ui/projects.py`, `cli.py` (`rebase`,
`push`), `tests/test_operations.py`, `tests/test_ui_actions.py`.

**Covers.** R17 (Rebase on main, Push), D8, D9.

**Details.**
- Every action first reads the status and refuses a detached HEAD and any operation in progress
  (rule 6).
- Rebase refuses when tracked files are changed. Untracked files are fine. Then it fetches and
  runs `git rebase origin/<default>`.
- On a conflict the rebase stays in progress. The operation logs each file and raises
  `RebaseConflict` with the file list. The app shows the files and asks "Open the IDE to resolve
  them?". The IDE opens in the clone that was rebased.
- On the command line, a conflict prints the files and says: "Resolve them in the IDE
  (draupnir open), then run git rebase --continue in the clone." The exit code is 1.
- Push runs `git push --porcelain -u origin <branch>`, and adds
  `--force-with-lease --force-if-includes` for feature branches only. The default branch is never
  force-pushed.
- A refused push is read from the porcelain line of the ref, for example
  `!	refs/heads/feat:refs/heads/feat	[rejected] (remote ref updated since checkout)`. The
  reasons `remote ref updated since checkout`, `stale info`, `fetch first` and
  `non-fast-forward` give: "GitHub has commits on <branch> that this clone has not integrated.
  Fetch, look at the commits, and rebase before you push again." `remote rejected` gives GitHub's
  reason and says the branch may be protected.
- Push returns the pushed commit, which Step 11 follows.

**Tests.**
- `test_rebase_puts_the_branch_on_top_of_main`
- `test_rebase_refuses_changed_tracked_files`
- `test_rebase_conflict_stays_in_progress_and_lists_the_files`
- `test_actions_refuse_while_a_rebase_is_in_progress`
- `test_push_sets_the_upstream_of_a_new_branch`
- `test_push_after_rebase_uses_force_with_lease`
- `test_push_of_the_default_branch_never_uses_force`
- `test_push_refuses_when_github_has_new_commits` (a second clone pushes to the same branch, the
  first clone rebases, which fetches, and then pushes. `--force-if-includes` refuses)
- `test_rebase_command_prints_the_conflicting_files_and_exits_with_one`
- `test_ui_rebase_conflict_offers_the_ide_for_the_rebased_clone`

### Step 11: Build status and following a build (done)

**Goal.** Ask GitHub for the build state of a commit, follow it after a push, show it in the
Build column, and stop waiting on request.

**Files.** `src/draupnir/github.py`, `operations.py` (`check_build`, `push_and_follow`),
`ui/app.py`, `ui/projects.py`, `cli.py` (`build`, `push --follow`), `tests/fake_gh.py`,
`tests/test_github.py`, `tests/test_operations.py`, `tests/test_ui_actions.py`.

**Covers.** R17 (Push and follow build, Build status), R19 (stop waiting), R20, R21.

**Details.**
- `require_gh(repo)` checks that `gh` is on `PATH`. It reads the host from the origin URL of the
  clone. When the URL has a host, it runs `gh auth status --hostname <host>` once per host per
  process. A failure gives: "gh is not logged in to <host>. Run gh auth login --hostname <host>
  and try again." Local paths, as in the tests, skip the login check.
- Every `gh` call runs inside the clone, so `gh` picks the host and the `{owner}/{repo}`
  placeholders from the clone's remote. This is how github.com and GitHub Enterprise Server both
  work (R20).
- `build_state(repo, sha)` combines check runs (`commits/<sha>/check-runs?per_page=100`) and the
  combined commit status (`commits/<sha>/status`). `success`, `neutral` and `skipped` count as
  good. Any other finished check run counts as failure. A commit status `error` or `failure`
  counts as failure.
- `build_state` returns a state only when both reads succeeded. When either read fails, it raises
  `OperationError`, and the message names the read that failed. There is no exception for an
  endpoint that the server does not support: both endpoints exist on github.com and on GitHub
  Enterprise Server, and telling "not supported" apart from other errors would mean reading `gh`
  error text (rule 3).
- `BuildTiming` holds `poll_seconds`, `grace_seconds` and `timeout_seconds`. It is built from
  `Config.build`.
- `wait_for_build(repo, sha, log, cancel, timing, on_update)` ends on the first failure, on all
  success, with "none" after the grace period, with "timeout" after the timeout, and with
  "cancelled" when the cancel event is set. A poll that fails is logged and tried again at the
  next poll. A poll where only one of the two reads failed is a failed poll too, and its partial
  result is not used. After three failed polls in a row, the wait stops with the error. A wait
  that stops this way never ends as success.
- `operations.check_build` and `operations.push_and_follow`, with `require_gh` first.
- The Build column shows the last known state per clone, with "(older commit)" when the clone's
  HEAD has moved. The details line shows the first four jobs.
- `draupnir build` prints the state and every job with its URL. `draupnir push --follow` prints
  each change of a job. Both exit with 0 on success and 3 otherwise.

**Tests.**
- `test_build_state_combines_check_runs_and_commit_statuses`
- `test_build_state_is_failure_when_any_item_fails`
- `test_build_state_counts_neutral_and_skipped_as_success`
- `test_build_state_is_none_without_items`
- `test_build_state_raises_when_check_runs_fail_and_statuses_are_green`
- `test_build_state_raises_when_statuses_fail_and_check_runs_are_green`
- `test_wait_treats_a_partial_read_as_a_failed_poll`
- `test_wait_ends_on_success_and_on_first_failure`
- `test_wait_gives_none_after_the_grace_period`
- `test_wait_gives_timeout_after_the_timeout`
- `test_wait_can_be_stopped`
- `test_wait_survives_a_failed_poll`
- `test_missing_gh_gives_install_and_login_steps`
- `test_missing_gh_login_names_the_host` (a clone with the origin `https://ghe.example.com/org/repo.git` and the fake set to "not logged in")
- `test_gh_runs_inside_the_clone` (the fake's call log)
- `test_build_command_exit_code_follows_the_build_state`
- `test_ui_push_and_follow_updates_the_build_column_while_waiting`
- `test_ui_stop_waiting_ends_the_action_and_keeps_the_last_state`

### Step 12: Fast-forward main (done)

**Goal.** Move the default branch on GitHub to the head of a feature branch, without force, and
only after a green build.

**Files.** `src/draupnir/operations.py` (`fast_forward_main`), `ui/app.py`, `ui/projects.py`,
`cli.py` (`ff-main`), `tests/test_operations.py`, `tests/test_ui_actions.py`.

**Covers.** R17 (Fast-forward main), R18, D10, D16.

**Details.**
- When `actions.fast-forward-main` is false, the button is hidden, and `draupnir ff-main` stops
  with: "Fast-forward main is turned off in draupnir.toml ([actions] fast-forward-main). Use
  draupnir pr instead."
- The order of checks: status and feature branch, fetch, the branch is on top of
  `origin/<default>`, the branch has new commits, then the build.
- Build check, when `actions.fast-forward-needs-green-build` is true: `require_gh`, then
  `build_state` of HEAD once.
  - Success: go on.
  - Pending: "The build of <sha> is still running. Wait until it is green, for example with
    draupnir push --follow, and try again."
  - Failure: names every failed job with its URL.
  - None: "No build has reported for <sha>. Push the branch, wait for a green build, and try
    again."
  - A read that failed, also when only one of the two reads failed: stop before the push. The
    message says that the build state of <sha> could not be read, and to try again.
- The push is `git push --porcelain origin HEAD:refs/heads/<default>`, without force.
  `fetch first` or `non-fast-forward` means GitHub got new commits in the meantime: rebase and
  try again. `remote rejected` means GitHub refused, for example because the branch is protected:
  use the pull request flow, and consider setting `fast-forward-main = false`.
- Branch protection that requires a review also refuses direct pushes, unless the user may bypass
  the protection. The action stays in the tool, because other projects allow direct pushes. A
  workspace where the push is always refused sets `fast-forward-main = false`, and the README
  says so.
- Afterwards it fetches and updates `main/` with `git merge --ff-only`, but only when `main/` is
  on the default branch, has no changed tracked files, and has no operation in progress.
  Otherwise it logs that `main/` was not updated and says to run `git pull` there.
- The dialog in the dashboard, and the terminal question, say which commit goes to the default
  branch and that the build of it must be green.

**Tests.**
- `test_ff_main_pushes_head_to_the_default_branch_and_updates_main`
- `test_ff_main_refuses_when_not_on_top_of_main`
- `test_ff_main_refuses_without_new_commits`
- `test_ff_main_refuses_a_failed_build_and_names_the_job`
- `test_ff_main_refuses_a_pending_build`
- `test_ff_main_refuses_when_no_build_exists`
- `test_ff_main_refuses_when_the_build_state_is_only_partly_readable`
- `test_ff_main_without_the_build_check_pushes_anyway`
- `test_ff_main_explains_a_push_that_github_refuses` (a `pre-receive` hook in the bare repository
  refuses the push)
- `test_update_main_skips_a_main_folder_with_changes`
- `test_ff_main_is_hidden_when_disabled_in_config`
- `test_ff_main_command_refuses_when_disabled_in_config`
- `test_ui_ff_main_asks_for_confirmation`

### Step 13: The pull request flow (done)

**Goal.** Run the whole pull request flow: push, pull request, build, merge or auto-merge, update
`main/`, and offer to remove the clone.

**Files.** `src/draupnir/operations.py` (`pr_build_and_merge`, `PrOutcome`), `github.py`
(`find_open_pr`, `create_pr`, `pr_merge_state`, `merge_pr`, `enable_auto_merge`,
`rebase_merge_allowed`), `ui/app.py`, `cli.py` (`pr`), `tests/fake_gh.py`, `tests/test_github.py`,
`tests/test_operations.py`, `tests/test_ui_actions.py`.

**Covers.** R17 (Pull request and merge), R18, D11, D17.

**Details.**
1. Checks: status, feature branch, `require_gh`. Then `gh repo view --json rebaseMergeAllowed`.
   When rebase merges are not allowed, the flow stops before it pushes: "The repository does not
   allow rebase merges, and draupnir merges only with rebase. Merge the pull request on GitHub."
2. Fetch. When the branch is not on top of `origin/<default>`, log a note.
3. Push, as in Step 10. Remember the pushed commit.
4. Find the open pull request for the branch into the default branch, or create one with
   `gh pr create --base <default> --head <branch> --fill`. `find_open_pr(repo, branch, base)`
   runs `gh pr list --head <branch> --base <default> --state open --json
   number,url,headRefOid,baseRefName`. It checks that `baseRefName` of the result is the default
   branch before it reuses the pull request, and raises otherwise. An open pull request from the
   same branch into another base, for example a release branch, is never reused or merged.
   GitHub allows one open pull request per head and base, so the flow creates its own pull
   request into the default branch next to it.
5. Wait for the build of the pushed commit. Anything but success stops the flow, and the pull
   request stays open. The message names the build state and the pull request.
6. Read `gh pr view <n> --json state,isDraft,mergeStateStatus,reviewDecision,headRefOid,
   baseRefName`. When `headRefOid` differs from the pushed commit, stop: someone pushed during
   the build. When `baseRefName` is no longer the default branch, stop: someone changed the base
   of the pull request during the build. When `mergeStateStatus` is `UNKNOWN`, read again after
   `poll-seconds`, up to five times.
7. Decide from `mergeStateStatus`:
   - `CLEAN`, `UNSTABLE` or `HAS_HOOKS`: run `gh pr merge <n> --rebase --match-head-commit <sha>`.
     The outcome is "merged".
   - `BLOCKED`: run `gh pr merge <n> --rebase --auto --match-head-commit <sha>`. Then read the
     pull request again. When it is merged, the outcome is "merged". Otherwise the outcome is
     "auto-merge", and the message says: "Auto-merge is on. GitHub merges pull request #<n> after
     the required review." When the command fails, read `allow_auto_merge` with
     `gh api repos/{owner}/{repo}`. GitHub may leave this field out for users without admin
     rights. When it is present and false, the message says that auto-merge is turned off for the
     repository, that the pull request stays open, and that it can be merged on GitHub after the
     review. Otherwise the message shows the `gh` output.
   - `BEHIND`: stop. GitHub wants the branch up to date: rebase on main and run the flow again.
   - `DIRTY`: stop. The branch has conflicts with the default branch: rebase on main, resolve
     them, and run the flow again.
   - `DRAFT`, or `isDraft` true: stop. Mark the pull request as ready on GitHub first.
8. When the outcome is "merged": fetch, update `main/` as in Step 12, and return.
9. The flow returns `PrOutcome(result, pr, build)`, where `result` is "merged" or "auto-merge".
   Every stop is an `OperationError`.
10. The dashboard asks first, with the steps of the flow listed. When the outcome is "merged" and
    `actions.offer-remove-after-merge` is true, the app waits until the action has finished and
    then asks: "The branch is merged. Remove the clone <folder>?". A yes starts the removal as a
    new action, with the safety checks from Step 6.
11. The command line asks first unless `--yes`. After a merge it removes with `--remove`, keeps
    with `--keep`, and otherwise asks as described in section 2.3.
12. The tool never deletes the remote branch and never touches other clones (D17).

Reviews: a project can enforce the review through branch protection. The flow trusts GitHub's
`mergeStateStatus` and has no approval check of its own. A pull request without the approval is
`BLOCKED` and gets auto-merge. A pull request that is already approved is `CLEAN` and is merged
at once. A project where reviews are only a team rule is not protected by draupnir. The README
says so, and says to turn on branch protection.

**Tests.**
- `test_pr_flow_merges_after_a_green_build_and_updates_main`
- `test_pr_flow_keeps_the_pr_open_after_a_red_build`
- `test_pr_flow_keeps_the_pr_open_when_the_wait_is_stopped`
- `test_pr_flow_reuses_an_open_pr`
- `test_pr_flow_ignores_an_open_pr_for_the_same_branch_into_another_base` (the fake holds an open
  pull request into `release`. The flow creates and merges a new one into the default branch, and
  the `release` pull request stays open and unmerged)
- `test_pr_flow_stops_when_the_base_changed_during_the_build`
- `test_pr_flow_enables_auto_merge_when_a_review_is_required`
- `test_pr_flow_merges_at_once_when_the_review_is_already_given`
- `test_pr_flow_explains_when_auto_merge_is_turned_off`
- `test_pr_flow_stops_when_the_pr_head_changed_during_the_build`
- `test_pr_flow_stops_when_github_wants_the_branch_up_to_date`
- `test_pr_flow_checks_that_rebase_merge_is_allowed_before_pushing` (the fake's default: allowed.
  The call log shows `repo view` before the push)
- `test_pr_flow_stops_before_pushing_when_rebase_merge_is_not_allowed`
- `test_merge_after_rebase_merge_and_branch_deletion_allows_removal` (the fake rewrites the
  commits and deletes the branch. `unsaved_work` is empty afterwards)
- `test_pr_command_with_remove_deletes_the_clone_after_the_merge`
- `test_pr_command_without_a_terminal_keeps_the_clone_and_prints_the_command`
- `test_ui_pr_flow_asks_to_remove_the_clone_after_the_merge`
- `test_ui_pr_flow_does_not_offer_removal_after_auto_merge`

### Step 14: README and packaging (done)

**Goal.** A user can install draupnir on a new machine, set up a workspace for a project, and
find every rule from "Things outside this tool" in the README.

**Files.** `README.md`, `AGENTS.md` (final check), `docs/requirements.md`, `docs/decisions.md`
and `docs/implementation-plan.md` (final check), `pyproject.toml` (description, classifiers),
`.github/workflows/ci.yml` (adds `uv build`).

**Covers.** R2, R3 (the documented steps), R24.

**Details.**
- Requirements: git 2.31 or newer, `gh` logged in to the project's host, uv, and Maven 3.9.0 or
  newer for Maven projects. macOS, Linux and WSL.
- Install: `uv tool install git+https://github.com/erik-romson/draupnir`, and
  `uv tool upgrade draupnir`. From a checkout: `uv tool install --editable .`.
- New project: `mkdir shop && cd shop && draupnir init <url>`, then `git init`, commit and push
  the workspace.
- A workspace can hold internal URLs and file names. The README says to push it to a private
  repository when the project is private.
- New machine: `git clone <workspace url> shop && cd shop && draupnir init`.
- Existing `main/`: `cd shop && draupnir init`.
- A "Configuration" section with every key of `draupnir.toml` and the variables.
- A "Daily work" section with the CLI commands and the plain git commands.
- The dashboard: tabs, columns, buttons, keys, what "Stop waiting" does and does not do.
- The pull request flow: what happens with a required review, that draupnir relies on GitHub's
  branch protection to require the review, and that a clone whose pull request GitHub merged
  later can be removed with `draupnir remove`, because the check sees the rebased commits on the
  default branch. After a squash merge made on GitHub by hand, `draupnir remove --force` is
  needed.
- Troubleshooting: `gh` not logged in on GitHub Enterprise Server, git asking for a password,
  Maven too old or IntelliJ's bundled Maven too old, tracked `maven.config`, a rebase that is
  still in progress, `origin/HEAD` missing in `main/`, unknown keys in `draupnir.toml`.
- Using draupnir from Claude Code: run commands inside the clone, add `--yes` for `remove`,
  `ff-main` and `pr`, and read the exit codes.
- Fast-forward main: a default branch whose protection requires a review refuses the push. Set
  `fast-forward-main = false` in such a workspace.
- Contributing: a short pointer to `AGENTS.md` and `docs/`.
- Final check of `AGENTS.md` and `docs/`: every step is marked done, the decisions made during
  the steps are in `docs/decisions.md`, and `git grep -i` for the first project's name and the
  company name finds nothing.

**Tests.** None. The CI workflow runs `uv build`, installs the wheel into a fresh environment
with `uv tool install dist/*.whl`, and runs `draupnir --version`.

## 8. Requirements and the tests that prove them

Every requirement has at least one test. Test names are from the steps above.

| Req | Tests | Step |
|---|---|---|
| R1 | `test_workspace_gitignore_hides_main_and_clones`, `test_init_clones_main_and_writes_the_workspace_files` | 5 |
| R2 | `test_init_clones_main_and_writes_the_workspace_files`, `test_init_in_a_cloned_workspace_uses_the_url_from_the_config`, `test_init_can_run_twice` | 5 |
| R3 | `test_init_adopts_an_existing_main_folder`, `test_init_refuses_a_url_that_differs_from_main`, `test_init_sets_origin_head_when_it_is_missing` | 5 |
| R4 | `test_new_clone_tracks_a_branch_that_exists_on_github`, `test_new_clone_creates_a_branch_from_the_base`, `test_new_clone_refuses_a_base_that_is_not_on_github`, `test_new_clone_refuses_a_bad_branch_name` | 5 |
| R5 | `test_new_clone_points_origin_at_github_not_at_main`, `test_new_clone_does_not_get_local_only_branches_from_main`, `test_new_clone_has_only_its_own_local_branch`, `test_new_clone_works_after_main_is_moved_away` | 5 |
| R6 | `test_prepare_copies_missing_local_files_and_keeps_existing_ones`, `test_prepare_copies_a_folder_with_its_files`, `test_prepare_skips_files_that_git_tracks`, `test_prepare_adds_local_files_and_maven_files_to_exclude`, `test_prepare_copies_a_git_hook_without_excluding_it` | 4 |
| R7 | `test_remove_deletes_a_clean_clone_with_its_m2repo`, `test_remove_refuses_uncommitted_changes`, `test_remove_refuses_when_a_stash_exists`, `test_remove_refuses_unpushed_commits_on_any_branch`, `test_remove_refuses_an_unpushed_merge_commit_whose_parents_are_on_github`, `test_remove_with_force_deletes_anyway`, `test_remove_accepts_commits_that_were_rebase_merged_and_whose_branch_was_deleted` | 6 |
| R8 | `test_maven_config_has_absolute_paths_for_head_and_tail`, `test_maven_shared_repository_comes_from_config_and_environment` | 4 |
| R9 | `test_maven_older_than_3_9_0_is_refused`, `test_maven_prefers_mvnw_over_mvn`, `test_maven_that_cannot_run_gives_a_message` | 4 |
| R10 | `test_prepare_refuses_when_maven_config_is_tracked_and_changes_nothing`, `test_prepare_refuses_an_old_maven_and_changes_nothing` | 4 |
| R11 | `test_prepare_gives_main_its_own_m2repo` | 4 |
| R12 | `test_new_clone_tab_creates_a_clone_and_the_table_shows_it`, `test_new_clone_tab_shows_a_message_for_an_empty_name` | 9 |
| R13 | `test_ide_kind_picks_idea_for_pom_and_pycharm_for_pyproject`, `test_open_ide_starts_the_command_detached`, `test_new_command_with_open_starts_the_ide` | 9 |
| R14 | `test_branch_argument_fills_in_the_form_and_opens_the_tab` | 9 |
| R15 | `test_project_paths_lists_main_first_then_clones_of_the_same_remote`, `test_project_paths_ignores_a_clone_of_another_repository`, `test_projects_tab_lists_main_and_every_clone` | 3, 8 |
| R16 | `test_status_counts_ahead_and_behind_main`, `test_status_counts_ahead_and_behind_remote`, `test_status_reports_not_pushed_when_there_is_no_upstream`, `test_status_counts_changed_and_untracked_files`, `test_status_detects_a_rebase_in_progress`, `test_table_shows_every_column_from_r16` | 7, 8 |
| R17 | Refresh and Fetch all: `test_refresh_shows_new_commits`, `test_fetch_all_updates_behind_counts`. Open IDE: `test_open_ide_starts_the_command_detached`. Rebase: `test_rebase_puts_the_branch_on_top_of_main`, `test_rebase_conflict_stays_in_progress_and_lists_the_files`. Push and follow: `test_push_after_rebase_uses_force_with_lease`, `test_ui_push_and_follow_updates_the_build_column_while_waiting`. Fast-forward main: `test_ff_main_pushes_head_to_the_default_branch_and_updates_main`. Pull request: `test_pr_flow_merges_after_a_green_build_and_updates_main`, `test_pr_flow_ignores_an_open_pr_for_the_same_branch_into_another_base`. Build status: `test_build_command_exit_code_follows_the_build_state`. Remove: `test_ui_remove_asks_for_confirmation_and_deletes_the_clone` | 7 to 13 |
| R18 | `test_feature_actions_are_disabled_for_main`, `test_ui_remove_asks_for_confirmation_and_deletes_the_clone`, `test_ui_ff_main_asks_for_confirmation`, `test_ui_pr_flow_asks_to_remove_the_clone_after_the_merge`, `test_remove_command_asks_before_deleting`, `test_confirm_without_a_terminal_asks_for_yes` | 1, 6, 8, 9, 12, 13 |
| R19 | `test_output_area_shows_the_command_and_its_output`, `test_only_one_action_runs_at_a_time`, `test_wait_can_be_stopped`, `test_ui_stop_waiting_ends_the_action_and_keeps_the_last_state` | 8, 11 |
| R20 | `test_build_state_combines_check_runs_and_commit_statuses`, `test_build_state_raises_when_check_runs_fail_and_statuses_are_green`, `test_build_state_raises_when_statuses_fail_and_check_runs_are_green`, `test_gh_runs_inside_the_clone`, `test_missing_gh_login_names_the_host` | 11 |
| R21 | `test_wait_ends_on_success_and_on_first_failure`, `test_wait_gives_none_after_the_grace_period`, `test_wait_gives_timeout_after_the_timeout`, `test_ui_push_and_follow_updates_the_build_column_while_waiting` | 11 |
| R22 | `test_run_sets_the_no_prompt_variables`, `test_run_starts_the_command_in_its_own_session`, `test_run_kills_a_command_after_the_timeout`, `test_confirm_without_a_terminal_asks_for_yes` | 1, 2 |
| R23 | `test_status_does_not_touch_the_index`, `test_timer_reads_the_status_again_without_fetching` | 7, 8 |
| R24 | `test_run_raises_command_error_with_the_last_lines`, `test_find_workspace_fails_with_a_helpful_message`, `test_command_outside_a_workspace_exits_with_one`, `test_config_rejects_an_unknown_key_with_its_name`, `test_remove_message_lists_every_reason_and_the_next_step`, `test_missing_gh_login_names_the_host`, `test_ff_main_refuses_a_failed_build_and_names_the_job`, `test_pr_flow_explains_when_auto_merge_is_turned_off` | 2 to 13 |

R24 is also a review rule. At the end of Steps 6, 10 and 13, read every `OperationError` message
in the changed modules and check it against rule 4 in section 6.

## 9. Risks and how the steps reduce them

| Risk | Where it bites | What reduces it |
|---|---|---|
| The dashboard disturbs an IDE or Claude Code in the same clone by taking git locks | Status reads on every refresh and on the timer | Step 2 sets `GIT_OPTIONAL_LOCKS=0` for every command. Step 7 tests that the index is untouched. Step 8's timer only reads |
| A command asks for a password or a passphrase and hangs, or draws over the dashboard | Fetch, push, clone | Step 2 starts every command in its own session and sets the variables for git, ssh and Git Credential Manager |
| A dead network blocks the one action slot | Fetch and push over VPN | Step 2 adds timeouts that also stop child processes |
| An old Maven silently ignores the tail, so installs go into `~/.m2` | Any clone | Step 4 refuses Maven below 3.9.0 with a message. IntelliJ's bundled Maven is covered in the README |
| A force push overwrites a colleague's commits | Push after rebase | Step 10 uses `--force-with-lease --force-if-includes` and tests the refusal after a fetch |
| Fast-forward main pushes an unbuilt commit | Fast-forward main | Step 12 checks the build first, on by default |
| The pull request flow merges a commit that was not built | Someone pushes during the wait | Step 13 compares the head and merges with `--match-head-commit` |
| The pull request flow cannot finish because reviews are required | A project with required reviews | Step 13 enables auto-merge and says so. The user is never left guessing |
| The pull request flow merges without a review | A project where the review is only a team rule | A project can enforce the review with branch protection, so GitHub reports `BLOCKED` until the review is given. The README says that draupnir relies on branch protection for reviews |
| Fast-forward main is always refused | A default branch whose protection requires a review | Step 12 explains the refusal and points to `fast-forward-main = false`. The README says so |
| After a merge, the clone cannot be removed, because GitHub's rebase merge gave the commits new ids and deleted the branch | The removal offer after a merge | Step 6 counts commits whose change is on the default branch as saved. Step 13 tests it with a fake merge that rewrites the commits |
| Removal deletes a local merge commit and its conflict resolution, because the parents of the merge are already on GitHub | `draupnir remove` after merging `origin/main` into a branch without pushing | Step 6 counts every local merge commit that is on no remote branch as unsaved, and tests this case |
| A build looks green because one of the two GitHub reads failed | Steps 11 to 13, when GitHub or the network has a short failure | Step 11 returns a state only when both reads succeeded. A partial read is a failed poll. Step 12 refuses the push, and Step 13 retries under the three-poll rule |
| The pull request flow merges a pull request into the wrong base branch | A branch with an open pull request into a release branch | Step 13 lists only pull requests into the default branch, checks `baseRefName` before reuse and again before the merge |
| `prepare` changes a clone and then stops | A tracked `.mvn/maven.config`, or Maven missing or too old | Step 4 runs every check before the first change (R10) |
| `gh` changes its messages, or GitHub Enterprise Server lacks a JSON field | Steps 11 and 13 | The tool reads only JSON fields and exit codes. A missing field gives a message that names the field and the `gh` version |
| The fake `gh` behaves differently from the real one | Steps 11 and 13 | Manual check at the end of Step 13: run `draupnir pr` once against a scratch repository on the real GitHub host, with and without a required review |
| `gh` is not logged in on the Enterprise host | First GitHub call | Step 11 runs `gh auth status --hostname` and gives the exact login command |
| A rebase left in progress confuses later actions | After a conflict | Step 7 shows it in the table. Every action in Steps 10 to 13 refuses while an operation is in progress |
| A failed setup leaves a half-made clone | `draupnir new` when Maven is missing | Step 5 keeps the folder and says to run `draupnir prepare` after the fix |
| Claude Code runs a command that asks a question | `remove`, `ff-main`, `pr` | Step 1 refuses without a terminal and asks for `--yes` |
| Textual changes its API | UI code and tests | Textual is pinned to major version 8. The UI is split so logic lives outside the widgets |
| UI tests are slow or flaky | Steps 8 to 13 | Polling times come from config and are milliseconds in tests. Tests wait on `workers.wait_for_complete()`, not on sleeps |
| A user runs `draupnir` in the wrong folder | Any command | Step 3 searches upwards for `draupnir.toml` and explains what a workspace is when it finds none |
| `init` turns a project clone into a workspace by mistake | `draupnir init` in `main/` | Step 5 refuses a folder that tracks project files |
| Moving the workspace breaks the absolute Maven paths | After `mv` | `draupnir prepare` rewrites them. The README says so |
| Names of a user's project or company end up in the public repository | Docs, README, test fixtures | Rule 7 in section 6 uses a neutral example. Steps 1 and 14 check by hand with `git grep` before pushing |
| A later session misses the plan or a decision | Steps 2 to 14 | Step 1 writes `AGENTS.md` and `docs/`. Rule 8 keeps the decisions and the step state current |

## 10. Order and dependencies

```
1 skeleton
└─ 2 runner
   └─ 3 config + workspace
      ├─ 4 prepare ─ 5 init + new ─ 6 remove ─────────────────────┐
      │                                                            │
      └─ 7 status + fetch ─ 8 dashboard ─ 9 new clone tab, IDE, remove (needs 5 and 6)
                                │
                               10 rebase + push ─ 11 build ─ 12 ff-main ─ 13 pull request flow (needs 6) ─ 14 README
```

Steps 1 to 7 give the clone setup on the command line: `init`, `new`, `prepare`, `remove`,
`status` and `fetch`. The git and GitHub actions get their commands in Steps 10 to 13, together
with their buttons. Configuration is not a separate step, because every step reads what it needs
from `Config` as soon as Step 3 exists.

Steps 4 to 6 and Step 7 do not depend on each other and can be done in either order after Step 3.
