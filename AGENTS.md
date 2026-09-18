# Agent instructions

draupnir is a terminal dashboard and a command line tool for working on several clones of one git
repository. Read these first:

- `docs/architecture.md`: modules, conventions, how commands run, and the tests.
- `docs/requirements.md`: the requirements, R1 to R24.
- `docs/decisions.md`: why the design is the way it is. Do not reopen a decision without a reason.

## Quality gate

Every change ends with this gate green:

```
uv run ruff check
uv run ruff format --check
uv run pyright
uv run pytest
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
6. **Module boundaries.** Only `src/draupnir/ui/` imports Textual. Only `config.py` and `ide.py`
   read the environment.
7. **No project or company names.** The repository is public. Code, tests and docs use a neutral
   example: a Java and Maven project called `shop` on `github.example.com`. The bare test
   repository is `project.git`.
8. **Docs stay current.** A new decision goes into `docs/decisions.md`. A change users can see
   goes into `README.md` or `docs/`.

## Writing style

Use short, complete sentences in messages, comments and docs. Avoid compact noun phrases.

## Tests

Tests never touch the real `HOME`, git config or GitHub. Put a new test next to the tests of the
same module. Wait on `app.workers.wait_for_complete()` and `pilot.pause()`, never on fixed sleeps.
