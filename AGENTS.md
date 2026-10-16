# Agent instructions

draupnir is a terminal dashboard and a command line tool for working on several clones of one git
repository. Read these first:

- `docs/architecture.md`: modules, conventions, how commands run, and the tests.
- `docs/requirements.md`: the requirements, R1 to R28.
- `docs/decisions.md`: why the design is the way it is. Do not reopen a decision without a reason.

## Where the session runs

Run the Claude Code session in a cmux terminal. draupnir talks to cmux, and the cmux command line
tool answers only a process that cmux started: every other process gets `Access denied - only
processes started inside cmux can connect`. A session outside cmux can read the code and run the
tests, but it cannot see what draupnir does to a workspace, a sidebar status or a terminal.

The `cmux` command line tool has to be on the `PATH`. A terminal that cmux started already has
it. In every other terminal, open cmux, press `⌘ ⇧ P` and run
`Shell Command: Install 'cmux' in PATH`.

Inside cmux, use only the commands that read, such as `cmux top`, `cmux tree`,
`cmux list-workspaces` and `cmux list-status`. Ask first before a command that changes the
desktop, because the desktop is the user's own. Never use `cmux send` or `cmux send-key`: they
type into a terminal that may be busy.

## Quality gate

Every change ends with this gate green:

```
uv run ruff check
uv run ruff format --check
uv run pyright
uv run pytest
```

A change to how draupnir treats a clone on disk, to git, or to `gh` also ends with the
integration tests green:

```
bats bats/
```

## Rules

1. **Commands never wait for input.** Run every command through `runner.run`.
2. **No locks when reading.** Status reads use only commands that respect
   `GIT_OPTIONAL_LOCKS=0`, and file checks in the git folder.
3. **Parse only stable output.** Never read git or gh messages meant for people. See
   `docs/architecture.md`.
4. **Messages.** An `OperationError` says what happened and what to do next, in full sentences.
   Its first line must make sense alone, because the dashboard shows it in a notification.
5. **Checks before actions.** An action that changes a clone refuses while a rebase, merge,
   cherry-pick, revert or `git am` is in progress, and names it.
6. **Module boundaries.** Only `src/draupnir/ui/` imports Textual. Only `config.py`, `ide.py`
   and `cmux.py` read the environment.
7. **No project or company names.** The repository is public. Code, tests and docs use a neutral
   example: a Java and Maven project called `shop` on `github.example.com`. The bare test
   repository is `project.git`.
8. **Docs stay current.** A new decision goes into `docs/decisions.md`. A change users can see
   goes into `README.md` or `docs/`.

## Writing style

Use short, complete sentences in messages, comments and docs. Avoid compact noun phrases.

## How to test functionality

Two suites test draupnir. A change needs the suite that can see it.

| What the change is about | Suite |
|---|---|
| One module's rules, a branch of a message, a parser | `tests/`, with pytest |
| The dashboard: tabs, keys, buttons, the output area | `tests/`, with Textual's pilot |
| What a command leaves on disk: files, the exclude file, branches | `bats/`, with bats |
| A whole flow from the command line, and the exit code it ends with | `bats/`, with bats |

A change that users can see usually needs both: a pytest test for each branch of the logic, and
one integration test that runs the real command and checks the result on disk.

### Unit tests: `tests/`, run with `uv run pytest`

These call the modules and `cli.main(argv)` in the same process. Put a new test next to the tests
of the same module. Tests never touch the real `HOME`, git config or GitHub. Wait on
`app.workers.wait_for_complete()` and `pilot.pause()`, never on fixed sleeps.

### Integration tests: `bats/`, run with `bats bats/`

These run the installed `draupnir` command on a workspace of their own, with real git, and then
read what is on disk. They answer "does the tool do this for a user", not "does this function
return that".

They need `bats`, and `bats-support`, `bats-assert` and `bats-file` in `/usr/local/lib/bats/`, or
`BATS_LIB_PATH` pointing at them. They also need `uv sync`, because they run
`.venv/bin/draupnir`.

A test file loads the helper, checks its tools once, and builds a workspace for each test:

```bash
load 'helpers/workspace'

setup_file() {
    require_tools
}

setup() {
    setup_workspace
}

@test "new copies the local files of main/ into the clone" {
    set_local_files "local.properties"
    printf 'port=8080\n' > "$MAIN/local.properties"

    run draupnir new feature-a
    assert_success

    assert_file_exist "$ROOT/feature-a/local.properties"
    assert_excluded "$ROOT/feature-a" "/local.properties"
    assert_git_clean "$ROOT/feature-a"
}
```

`setup_workspace` builds a sandbox in `tmp/batstest/<file>/<test number>/` and runs
`draupnir init` in it. It gives the test:

- `$REMOTE`, the bare repository `project.git` that stands in for GitHub;
- `$ROOT`, the workspace, and `$MAIN`, its `main/` clone;
- `$TEST_DIR`, the folder that holds all of them, which stays behind after a failure;
- stubs for `mvn`, `idea` and `gh` on `PATH`, and its own `HOME` and git config.

`setup_sandbox` does the same without running `init`, for a test about `init` itself.

`bats/helpers/workspace.bash` holds the helpers, and they are the vocabulary of these tests:

- `draupnir ...` and `draupnir_in <folder> ...` run the command line;
- `set_config <section> <line>` and `set_local_files <pattern>...` change `draupnir.toml`;
- `commit_fixture`, `change_fixture` and `push_from_other_clone` build git history from the
  fixtures in `bats/testfiles/`;
- `assert_excluded`, `refute_excluded`, `exclude_line_count`, `assert_git_clean`,
  `assert_same_content`, `sha_on_github`, `assert_branch_on_github` and
  `refute_branch_on_github` check the result;
- `gh_state build <sha> <success|failure|pending|none>` and `gh_state pull-requests` drive the
  fake GitHub, which is `tests/fake_gh.py`, the same fake the pytest suite uses;
- `wait_for_file` waits for a program draupnir started in the background, such as the IDE.

Rules for these tests:

- A file a test needs goes into `bats/testfiles/<what it is>/`, not into the test itself.
- Everything a test writes goes under `tmp/batstest/`, which git ignores.
- Check what is on disk, not what the command printed. Check output only for an exit code, a
  `--help` line, or an `OperationError` whose message is the point of the test.
- Use `assert_success`, `assert_failure` and `assert_equal`, so a failure says what it found.
- Never let a test reach the network, the real `HOME` or the real `gh`.
