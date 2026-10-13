# Decisions

Each decision says what was chosen and why. The code refers to these numbers, so a decision is
never renumbered. A replaced decision says what replaced it.

## Clones and setup

**D1. Separate clones, not worktrees.** Worktrees share branches and the stash, and git refuses the
same branch in two worktrees. Clones are fully independent. Cloning from `main/` on disk shares
object files through hard links, so the extra disk use is small.

**D2. Clone from `main/` on disk, then point at GitHub.** `git clone main/`, set `origin` to the
GitHub URL, and fetch with prune. This is faster than a network clone, and local branches of
`main/` do not leak into the new clone. An existing branch is checked out by its full
`origin/<branch>` name, so a file with the same name cannot make it ambiguous. When cloning,
fetching or checking out fails, the unfinished folder is removed.

**D3. Maven: a private repository per clone, with `~/.m2` as a read-only tail.**
`.mvn/maven.config` sets `maven.repo.local` to the clone's `.m2repo` and `maven.repo.local.tail`
to the shared repository. Installs stay private, and deleting a clone deletes its repository. Maven
reads this file from the terminal, `mvnw`, IntelliJ and Claude Code alike. Paths are absolute,
because Maven does not expand `~` there. Rejected: one shared repository with prefixes, which
leaves files behind, and copying resolved artifacts, which is complex.

**D4. Local files are listed, copied once, and excluded from git.** Copies, not symlinks, because
some files differ per branch. The list is committed, so it must never hold secrets.

**D5. Shell scripts for clone setup.** Replaced by D15.

**D14. One tool, installed once.** `uv tool install` puts draupnir on the machine. A workspace is
any folder with `draupnir.toml` and `main/`. One `uv tool upgrade` updates every workspace.

**D15. Clone setup in Python behind a CLI.** `init`, `new`, `prepare` and `remove` are Python
functions that the CLI and the dashboard share. One language gives one test suite and better
messages.

**D20. An optional repository URL in the config.** `init` writes `workspace.repository` and uses it
when no URL is given. At all other times the URL comes from `main/`.

**D21. What counts as a saved commit.** A commit on a remote branch is saved. A commit that is not
a merge is also saved when an equal change is on `origin/<default>`, which covers GitHub's rebase
merge. A local merge commit on no remote branch is never saved, because it can hold a conflict
resolution. Squash merges are not recognised.

**D38. A configurable main branch.** `workspace.main-branch` names the branch the workspace
works against, because a team can integrate into `develop` while GitHub's default is `main`.
Without it, draupnir uses GitHub's default. `init --main-branch` sets it, checks that GitHub has
it, and checks it out in a `main/` it just cloned. An adopted `main/` is never switched, because
it can hold work.

**D32. What `remove` checks.** An operation in progress and commits on a detached HEAD count as
unsaved work. `--force` skips the fetch and the unsaved-work checks, but never the checks on the
folder: `main/`, the root, and a folder that is not a clone of the project are always refused.
`remove` checks the folder, then asks, and only then goes to the network.

## Git and GitHub actions

**D6. GitHub through the `gh` CLI.** `gh` handles login, Enterprise hosts and `{owner}/{repo}`.
draupnir handles no tokens.

**D7. Build state from check runs and commit statuses.** Any failure means failure, and any pending
job means pending. No result after the grace period means "no build". This works for GitHub
Actions and for external CI, without knowing which one a project uses.

**D8. Rebase conflicts are resolved by hand.** On a conflict, the rebase stays in progress, and the
conflicting files are listed.

**D9. Force push only with lease, never on the default branch.** Feature branches use
`--force-with-lease --force-if-includes`.

**D10. Fast-forward main is a plain push.** `git push origin HEAD:refs/heads/<default>`, without
force, so GitHub accepts only a fast-forward. Afterwards `main/` is updated with
`git merge --ff-only`, but only when it is clean and on the default branch. A failed update is only
logged, because the push already succeeded.

**D11. Merge only after a green build.** Failure, no build, a timeout or a stopped wait leave the
pull request open.

**D16. Fast-forward main needs a green build.** It is on by default.
`actions.fast-forward-needs-green-build = false` turns it off. `actions.fast-forward-main = false`
hides the action for a protected default branch. A head that no branch on origin contains has no
build, so the check stops before it asks GitHub and says to push first.

**D17. Auto-merge when a review is missing.** When GitHub refuses the merge for a missing review,
the flow turns on auto-merge. After a finished merge, `main/` is updated and draupnir offers to
remove the clone. It never deletes the remote branch.

**D22. The pull request flow reads structured state.** It reads `gh pr view --json`, checks that
rebase merges are allowed before it pushes, only uses a pull request into the default branch, and
merges with `--match-head-commit`. It never reads `gh` error text.

**D27. Reviews come from branch protection.** draupnir has no approval check of its own.

**D31. A build state needs both reads.** When the check runs or the commit statuses cannot be read,
the state is unknown. A wait counts that as a failed poll, and fast-forward main refuses. Both
reads fetch every page, so a failure on a later page is never missed.

**D34. Following a build.** When the user stops waiting or the wait times out, the Build column
keeps the last state, because the build keeps running. The login check runs
`gh auth status --hostname <host>` and reads only the exit code. `build` and `push --follow` exit
with 3 when the build is not green.

**D39. Polling that respects the rate limit.** A fixed poll every 15 seconds costs two requests
each time, so one long build could use hundreds. The wait estimates the build time from an
earlier commit, sleeps until 80% of it, and then polls every twentieth of it, between
`poll-seconds` and 2 minutes. It reads `gh api rate_limit`, which is free, and pauses until the
reset when fewer than 50 requests are left. Until a build appears, it still polls every
`poll-seconds`, so the grace period keeps working.

**D40. What an earlier build is.** A commit on main often has jobs from later runs, such as a
scheduled CodeQL analysis or a second deploy hours after the push. Measuring from the first job to
the last one made a 20 minute build look like 11 hours, and the wait then slept for hours. So the
estimate counts only the first run of each job name or status context. It also stops at the first
gap of more than 15 minutes, because a job that waits for another one starts when that one ends.
The wait for the expected end is also capped at 15 minutes at a time, so a wrong estimate costs a
few extra requests instead of hours of silence.

**D35. Around the merge.** The flow reads the pull request again after the build. One that GitHub
already merged, with the pushed commit as head, counts as merged. A closed one stops the flow. After
a merge, a failed fetch or update is only logged. A merge state that stays `UNKNOWN` after five
reads stops the flow, and the pull request stays open.

## Commands and the dashboard

**D12. Textual for the dashboard, uv to run it.** Textual works over SSH and in tmux.

**D13. Plain language.** Messages and docs use short, complete sentences.

**D18. `draupnir.toml` plus `DRAUPNIR_*` variables.** The committed file holds shared settings.
Variables hold values that differ per machine.

**D19. Python 3.13, Textual 8, pytest-asyncio.** macOS, Linux and WSL are supported. Native
Windows is not.

**D23. No prompts, timeouts, and clean interrupts.** Every command runs in its own session with the
no-prompt variables. Network commands have a timeout. On a timeout or an interrupt, draupnir stops
the whole process group, so nothing keeps running after draupnir gives up.

**D24. Folder arguments, confirmations and exit codes.** A folder argument defaults to the current
clone. A command that asks refuses without a terminal unless `--yes` is given. Exit codes: 1 for a
message, 2 for wrong arguments, 3 for a build that is not green.

**D25. IDE variables win over the config.** `DRAUPNIR_IDEA`, `DRAUPNIR_STUDIO` and
`DRAUPNIR_PYCHARM` hold the machine's own value.

**D43. Editing the config from the dashboard.** The Edit config button starts the editor from
`DRAUPNIR_EDITOR`, `VISUAL` or `EDITOR` while the dashboard is suspended, and shows the file in
the output when no editor is set. It is the one place where draupnir starts a program that waits
for input, because the user asked for exactly that. Every command draupnir runs for its own work
still goes through `runner.run` and never waits (R22). Afterwards the config is read again, so a
file that no longer parses is reported at once, and the dashboard keeps using the config it had.
Buttons, the default branch and the refresh timer are read when the dashboard starts, so the
message says to restart draupnir after those change.

**D42. Local files are gitignore patterns.** `workspace.local-files` holds patterns, and
`git ls-files --others --ignored --exclude=<pattern>` in `main/` matches them. git does the
matching, so every rule of a `.gitignore` line works and draupnir parses no patterns of its own.
A tracked file never matches, because every clone already has it. Each matched file is copied and
written into the clone's `.git/info/exclude`, one line per file, so a later pattern change cannot
leave a wrong line behind. A pattern that starts with `.git/` stays a path, because git does not
list the files in the git folder.

**D48. The exclude file of a clone is merged, not copied.** `git clone` writes
`.git/info/exclude` in every new clone, and `prepare` writes the path of each local file into it.
So the file cannot be copied from `main/` like the other `.git/` paths, because the copy would
throw those lines away. When `workspace.local-files` lists `.git/info/exclude`, `prepare` adds the
lines of the file in `main/` to the file in the clone, and leaves a line that is already there
alone. Ignore rules written by hand in `main/` then reach every clone, and `prepare` stays safe to
run again. The lines are never removed, because draupnir cannot tell its own old line from a line
the user wrote.

**D41. IDE project names.** Opening a clone writes `.idea/.name` as `<workspace> <folder>`, so the
IDE windows of different clones can be told apart. It only writes when `.idea/` exists and the
file does not, so a name the user gave is kept, and a folder the IDE never opened is still imported
as a new project. The file is hidden in the clone's `.git/info/exclude`.

**D45. Android projects open in Android Studio.** A Gradle project is an Android project when a
Gradle file in its root names a plugin id that starts with `com.android`. Every id of the Android
Gradle plugin does, and a Kotlin Multiplatform project names it in its version catalog. The
command comes from `ide.android` or `DRAUPNIR_STUDIO`, so setting it to `idea` keeps IntelliJ
IDEA. Rejected: a key that names the IDE for the project, because the files already tell.

**D46. cmux workspaces from the dashboard.** The New cmux workspace button runs
`cmux workspace create --cwd <clone>`, so a shell or Claude Code session starts in the right clone
with one press. The button is shown only when `CMUX_WORKSPACE_ID` is set, which cmux sets in every
terminal it starts. Without `--group`, cmux puts the new workspace outside every sidebar group, far
from the dashboard. So draupnir reads the groups with `cmux workspace-group list --json`, and when
the dashboard's workspace is in one, it passes that group and places the new workspace right after
the dashboard. When the list fails, the workspace is still created, outside a group. A new
workspace has no todo list, so draupnir opens it with `cmux todo open`. cmux opens it as a tab next
to the terminal, so draupnir moves it into a pane below with `cmux split-off` and gives the terminal
the focus back. When that fails, the workspace stays open without the todo list. The commands
go through `runner.run` and ask nothing. draupnir reads only the JSON output.

**D26. The dashboard refreshes on a timer.** Every `ui.refresh-seconds`. The timer never fetches,
and skips a read while an action or another read runs.

**D33. One action at a time.** Every button is disabled while an action runs. Only one status read
runs at a time, and a queued read runs after it, so an older result never replaces a newer one.
The table updates cells in place, so the selection and scroll position stay.

**D47. Remove clone asks for a clone, not for a feature branch.** A clone that is on the default
branch holds no work of its own once it is merged, so the button is enabled for every clone but
`main/`. The branch says nothing about what would be lost. What decides is the check for unsaved
work from R7, which fetches and then lists uncommitted changes, stashes, an operation in progress
and commits that GitHub does not have. Fast-forward main and Pull request and merge still need a
feature branch, because both push a branch into the default branch.

**D37. Open branch reads local refs.** The branch list comes from `origin/*` in `main/`, so it
opens at once, needs no network and takes no locks. It is as new as the last fetch. A match is
text anywhere in the name, ignoring case.

**D49. A clone can have a folder name of its own.** The folder of a clone is the branch name with
`/` replaced by `-`, unless `new --folder`, or the folder field in the dashboard, names another
one. A long branch name then does not have to be a long folder name. draupnir already reads the
branch of every clone from git, so nothing else depends on the folder name. Open branch therefore
leaves out a branch that any clone is on, and not only a branch whose default folder exists. The
folder must be a plain name next to `main/`, so `/`, `.`, `..` and `main` are refused.

**D50. The Build cell is the button that opens the build.** A Textual table cannot hold a widget,
so the cell is text styled as a button, and a click on it opens the build. A separate button
would act on the selected row only, and the grid already has twelve buttons. The `g` key does the
same from the keyboard. The page is the first failed job, else the first job that still runs,
else the first job, because a failed job is the one to look at. The job URLs come from the
check runs and commit statuses that draupnir already reads, so no extra `gh` call is needed.
The browser starts through Textual's `open_url`, which does not wait for it.

## Project

**D28. A public repository on github.com.** Committed files use a neutral example and never name a
real project, company or internal host.

**D29. Where documentation lives.** `README.md` is the front page. `docs/` holds the user guide,
the configuration, troubleshooting, the architecture, the requirements and these decisions.
`AGENTS.md` is for coding agents. `docs/history/` keeps the finished implementation plan. `tmp/` is
never committed.

**D30. MIT license.**

**D36. CI builds and installs the package.** After the gate, CI builds the wheel, installs it, and
runs `draupnir --version`.

**D44. A script restarts the dashboards after cmux relaunches.** cmux restarts agent sessions
itself, but a command saved with `cmux surface resume set` stays manual. Only a choice in the cmux
window can make it restart on its own, and cmux signs that choice with a key in the Keychain. So
`bin/cmux-resume` reads the saved commands from the cmux config and types each one into the
terminals that cmux bound to it. It skips a terminal that runs something, and it matches terminals
by their binding rather than by folder, because many shells share a folder.
