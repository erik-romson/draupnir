# Configuration

## draupnir.toml

`draupnir.toml` lives in the workspace root and is committed. Every key has a default, so an
empty file is valid. `draupnir init` writes the file with `repository` and `main-branch` set, and
the other keys commented out. An unknown key or a wrong type stops draupnir with a message that names the key.

```toml
[workspace]
# The project's URL. Only init uses it, to clone main/ on a new machine.
repository = "git@github.example.com:acme/shop.git"
# The branch that clones rebase on, fast-forward main pushes to, pull requests go into, and main/
# stays on. init writes it. Default: the default branch on GitHub.
main-branch = "develop"
# The branch new clones start from. Default: main-branch.
base-branch = "develop"
# Files that are not in git but every clone needs. They are copied from main/ once, never
# overwritten, and hidden from git in each clone. Never list secrets, because this file is
# committed.
local-files = [
  "src/main/resources/local.properties",
  ".claude/settings.local.json",
  ".idea/runConfigurations",
  ".git/hooks/commit-msg",
]

[maven]
# Default: true when main/ has a pom.xml.
enabled = true
# The shared, read-only repository. Default: ~/.m2/repository.
shared-repository = "~/.m2/repository"

[build]
# The shortest time between two reads of the build. draupnir polls less often when it can: it
# waits for the expected end of the build, and backs off. See "Following a build" below.
# grace-seconds is how long to wait for a build to appear, timeout-seconds when to give up.
poll-seconds = 15
grace-seconds = 180
timeout-seconds = 3600

[actions]
# Set to false when the default branch requires a review. The button is then hidden.
fast-forward-main = true
# Fast-forward main refuses a commit whose build is not green.
fast-forward-needs-green-build = true
# After a merged pull request, ask whether to remove the clone.
offer-remove-after-merge = true

[ui]
# Read the status again after this many seconds. 0 turns it off. The timer never fetches.
refresh-seconds = 10

[ide]
# Commands that start the IDE. Default: idea and pycharm from PATH, JetBrains Toolbox or
# /Applications.
java = "idea"
python = "pycharm"
```

`local-files` can also list what `git clone` leaves out, such as hooks in `.git/hooks/`.

## Environment variables

These hold values that differ per machine, so they are never committed.

| Variable | Meaning |
|---|---|
| `DRAUPNIR_IDEA` | The command that starts IntelliJ IDEA. It wins over `ide.java`. |
| `DRAUPNIR_PYCHARM` | The command that starts PyCharm. It wins over `ide.python`. |
| `DRAUPNIR_MAVEN_SHARED_REPO` | The shared Maven repository, when it is not `~/.m2/repository`. |

## Following a build

A wait for a build asks GitHub as little as it can, so it stays far below the rate limit:

- Until a build appears, it asks every `poll-seconds`, so a missing build is noticed after
  `grace-seconds`.
- It estimates the build time from the finished build of one of the last three earlier commits.
  It then waits until 80% of that time has passed, and asks every twentieth of it after that.
- The estimate counts only the first run of each job. A job that starts more than 15 minutes
  after the rest of the build has ended belongs to a later run, such as a scheduled analysis,
  and does not count.
- Without an estimate, the pause grows with the time already waited.
- Between two reads, it waits at most 2 minutes. The exception is the wait for the expected end
  of the build, which lasts at most 15 minutes at a time.
- Before each read, it checks GitHub's rate limit, which costs nothing. When fewer than 50
  requests are left, it waits until the limit resets.

## Maven

`draupnir prepare`, which `init` and `new` run, writes `.mvn/maven.config` in each clone:

```
-Dmaven.repo.local=/abs/path/shop/feature-x/.m2repo
-Dmaven.repo.local.tail=/Users/you/.m2/repository
```

Maven reads this file from the terminal, `mvnw`, IntelliJ and Claude Code alike. The paths are
absolute, because Maven does not expand `~` there.
