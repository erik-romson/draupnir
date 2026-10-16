# Architecture

## Modules

```
src/draupnir/
  cli.py          argparse subcommands, confirmations, exit codes
  errors.py       OperationError, RebaseConflict
  runner.py       runs every external command: no prompts, own session, timeouts
  config.py       reads draupnir.toml and DRAUPNIR_* variables into a frozen Config
  workspace.py    finds the workspace, main/, the clones, and the clone for a folder
  setup.py        init, new clone, prepare, unsaved work, remove
  maven.py        Maven version check and .mvn/maven.config
  status.py       reads the git state of a clone without locks
  github.py       gh login check, build state, waiting for builds, pull requests
  operations.py   fetch, rebase, push, fast-forward main, the pull request flow
  ide.py          finds and starts IntelliJ IDEA, Android Studio or PyCharm
  cmux.py         finds the cmux tool, opens cmux workspaces, writes the sidebar status
  ui/
    app.py        the Textual app: tabs, key bindings, running one action at a time
    projects.py   the Projects tab
    newclone.py   the New clone tab
    confirm.py    the yes/no dialog
    output.py     the output area with "Stop waiting"
```

The CLI and the dashboard call the same functions. Only `ui/` imports Textual.

## Conventions

- A function that runs commands takes `log: LogFn`. It raises `OperationError` with a message
  for the user when it stops.
- A function that waits takes `cancel: threading.Event`.
- `setup.py` and `operations.py` take a `Workspace`, which holds the `Config`.
- Only `config.py`, `ide.py` and `cmux.py` read the environment.

## Running commands

All commands go through `runner.run`. It:

- closes stdin and starts the command in its own session, so nothing can ask a question;
- sets `GIT_TERMINAL_PROMPT=0`, `GIT_OPTIONAL_LOCKS=0`, `GIT_EDITOR=true`,
  `GIT_SEQUENCE_EDITOR=true`, `GCM_INTERACTIVE=never`, `SSH_ASKPASS_REQUIRE=never`,
  `GH_PROMPT_DISABLED=1`, `GH_NO_UPDATE_NOTIFIER=1`, `NO_COLOR=1` and `CLICOLOR=0`;
- stops the whole process group on a timeout or an interrupt.

Timeouts are constants in `runner.py`: 10 minutes for `git fetch` and `git push`, 2 minutes for
`gh`, none for `git clone` and Maven.

The tool parses only stable output: `git status --porcelain=v2`, `git push --porcelain`,
`rev-list --count` and JSON from `gh`. The one exception is the `Apache Maven x.y.z` line from
`mvn -v`.

## Tests

Two suites test draupnir. `AGENTS.md` says which suite a change needs.

### Unit tests: `tests/`

`tests/conftest.py` builds a workspace in `tmp_path`:

- a bare repository, `project.git`, stands in for GitHub;
- `fake_gh.py` stands in for `gh`, driven by a JSON state file with builds, pull requests and a
  call log;
- stubs for `mvn` and `idea` sit on `PATH`;
- `HOME`, `GIT_CONFIG_GLOBAL` and `PATH` are replaced, and `DRAUPNIR_*` variables are removed.
  The `PATH` is built from the stub folder and the system folders, and not from the `PATH` of the
  session that runs the tests. A cmux terminal has the real cmux on its `PATH`, and a test that
  found it would call the user's own desktop. A test that needs a cmux writes a stub with
  `write_stub_cmux`.

`tests/helpers.py` holds typed helpers such as `commit`, `push_from_other_clone` and `logger`.

CLI tests call `cli.main(argv)` and read `capsys`. UI tests use Textual's pilot and wait on
`app.workers.wait_for_complete()` and `pilot.pause()`, never on fixed sleeps.

### Integration tests: `bats/`

The integration tests run the installed `draupnir` command, with real git, and then read what is
on disk. They cover what a user can see: the files in a clone, the exclude file, the branches on
GitHub, and the exit code. They do not import draupnir, so they also catch what breaks between
the command line and the modules.

```
bats/
  helpers/workspace.bash   builds the sandbox, runs draupnir, and holds the assertions
  helpers/gh_state.py      drives tests/fake_gh.py from the shell
  testfiles/               the project, the stub commands, and the files a test needs
  cli.bats                 the version, the help, and a wrong call
  init.bats                draupnir init (R1, R2, R3)
  clone.bats               draupnir new (R4, R5)
  local-files.bats         the local files of a clone (R6)
  maven.bats               the private Maven repository (R8 to R11)
  status.bats              draupnir status, and reading a clone without locks (R15, R16, R23)
  git-ops.bats             fetch, rebase and push (R17)
  build.bats               following builds through gh (R20, R21)
  ff-main.bats             fast-forward main (R17, R18)
  pr.bats                  the pull request flow (R17)
  remove.bats              draupnir remove (R7)
  ide.bats                 starting the IDE (R13)
```

`setup_workspace` in the helper builds one sandbox per test under `tmp/batstest/`: the bare
repository `project.git`, stubs for `mvn`, `idea`, `gh` and `cmux` on `PATH`, its own `HOME` and git
config, and a workspace that `draupnir init` has set up. The sandbox stays behind after a
failure, so the workspace of a failed test can be looked at.

The `gh` stub runs `tests/fake_gh.py`, so both suites use the same stand-in for GitHub. Because
the sandbox lies inside the draupnir repository, the helper sets `GIT_CEILING_DIRECTORIES`, so
git does not find that repository when it searches upwards from a folder that is not a clone yet.

The suite needs `bats`, the `bats-support`, `bats-assert` and `bats-file` libraries, and
`uv sync`, because it runs `.venv/bin/draupnir`. The whole suite takes a few minutes, because
every test clones the project twice.

### CI

CI runs the quality gate on Ubuntu and macOS, then the integration tests, then builds the wheel,
installs it, and runs `draupnir --version`.
