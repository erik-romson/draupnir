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
# Files that are not in git but every clone needs. Each line is a pattern, written the way a
# line in .gitignore is written. The files that match in main/ are copied into a new clone
# once, never overwritten, and hidden from git there. Never list secrets, because this file
# is committed.
local-files = [
  "**/local.properties",
  ".claude/settings.local.json",
  ".idea/runConfigurations",
  ".git/hooks/commit-msg",
  ".git/info/exclude",
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
# Write one line about the clones into the sidebar of cmux. Only acts inside cmux.
cmux-status = true

[ide]
# Commands that start the IDE. Default: idea, studio and pycharm from PATH, JetBrains
# Toolbox or /Applications.
java = "idea"
android = "studio"
python = "pycharm"
```

draupnir picks the IDE from the files in the clone. A Gradle project whose `build.gradle.kts`,
`settings.gradle.kts` or `gradle/libs.versions.toml` names the Android Gradle plugin opens in
Android Studio. Any other Maven or Gradle project opens in IntelliJ IDEA, and a Python project
opens in PyCharm. To open an Android project in IntelliJ IDEA instead, set `android = "idea"`.

`local-files` patterns work like the lines of a `.gitignore` file, because git itself matches
them in `main/`. So `**/local.properties` matches the file in every folder, a folder name matches
the files in it, and a line that starts with `!` takes a file back that an earlier line matched. A
file that git tracks never matches, because every clone already has it. draupnir copies each
matched file into the new clone and writes its path into that clone's `.git/info/exclude`, so git
keeps ignoring it.

`local-files` can also list what `git clone` leaves out, such as hooks in `.git/hooks/`. A pattern
that starts with `.git/` is a path, not a pattern, because git does not list the files in the git
folder. Such a file is copied, but it needs no exclude line.

`.git/info/exclude` is the one path that is not copied. Every clone already has its own exclude
file, and draupnir writes the local files it copied into it, so a copy would throw those lines
away. When `local-files` lists it, draupnir adds the lines of `main/.git/info/exclude` to the
clone's own exclude file instead. A line that is already there stays as it is, so the ignore rules
written by hand in `main/` reach every clone, and running `prepare` again adds nothing twice.

## Environment variables

These hold values that differ per machine, so they are never committed.

| Variable | Meaning |
|---|---|
| `DRAUPNIR_IDEA` | The command that starts IntelliJ IDEA. It wins over `ide.java`. |
| `DRAUPNIR_STUDIO` | The command that starts Android Studio. It wins over `ide.android`. |
| `DRAUPNIR_PYCHARM` | The command that starts PyCharm. It wins over `ide.python`. |
| `DRAUPNIR_EDITOR` | The command that opens `draupnir.toml` from the dashboard. It wins over `VISUAL` and `EDITOR`. |
| `DRAUPNIR_MAVEN_SHARED_REPO` | The shared Maven repository, when it is not `~/.m2/repository`. |
| `DRAUPNIR_CMUX` | The command that runs the cmux command line tool. It wins over the tool on `PATH` and over `CMUX_BUNDLED_CLI_PATH`. |

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
