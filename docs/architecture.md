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
  ide.py          finds and starts IntelliJ IDEA or PyCharm
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
- Only `config.py` and `ide.py` read the environment.

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

`tests/conftest.py` builds a workspace in `tmp_path`:

- a bare repository, `project.git`, stands in for GitHub;
- `fake_gh.py` stands in for `gh`, driven by a JSON state file with builds, pull requests and a
  call log;
- stubs for `mvn` and `idea` sit on `PATH`;
- `HOME`, `GIT_CONFIG_GLOBAL` and `PATH` are replaced, and `DRAUPNIR_*` variables are removed.

`tests/helpers.py` holds typed helpers such as `commit`, `push_from_other_clone` and `logger`.

CLI tests call `cli.main(argv)` and read `capsys`. UI tests use Textual's pilot and wait on
`app.workers.wait_for_complete()` and `pilot.pause()`, never on fixed sleeps.

CI runs the quality gate on Ubuntu and macOS, then builds the wheel, installs it, and runs
`draupnir --version`.
