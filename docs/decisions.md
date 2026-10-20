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
go through `runner.run` and ask nothing. draupnir reads only the JSON output. D55 changed what the
button does when the clone has a workspace.

**D51. One module for cmux, and a found command.** `cmux.py` holds everything that talks to
cmux, so the dashboard and later tools share it and `ide.py` is only about the IDE. The tool is
looked up as `DRAUPNIR_CMUX`, then `cmux` on `PATH`, then `CMUX_BUNDLED_CLI_PATH`, which cmux sets
in every terminal it starts, then the tool in `/Applications/cmux.app`. The `PATH` comes before the
variable from cmux, so a `cmux` that a test or a user put there still wins. Before this, draupnir
ran the bare name `cmux`, which only works inside a cmux terminal. Finding the tool does not make
it usable everywhere: its socket answers `Access denied - only processes started inside cmux can
connect` to a process that cmux did not start, unless a socket password is set. So every cmux
command that goes through the socket still needs a draupnir that runs inside cmux. That covers the
workspace, tree, surface, todo and status commands. The `local-tmux` commands `list`, `close`,
`start --detached` and `attach --headless` do not use the socket, so they also work in a terminal
that cmux did not start (D53). The found path only helps a terminal whose PATH does not hold the
tool.

**D52. The dashboard writes its state into the cmux sidebar.** After every status read, the
dashboard writes one line into the sidebar row of its own cmux workspace, with
`cmux set-status draupnir "<line>" --icon <symbol> --color <hex> --priority <n> --workspace <id>`.
A user who works on many projects then sees which of them needs attention in the sidebar they
already look at, without opening a dashboard. The line has three levels: red for what stops work,
which is an operation in progress, a failed build of the current commit and a clone that cannot
be read; yellow for unfinished work, which is local changes, commits that are not on GitHub and a
branch GitHub no longer has; green when every clone is clean. A merged branch is yellow and not
red, because it asks for a cleanup and does not block anything. The line holds at most three
parts, worst first, because a narrow sidebar cuts it off. The workspace is always named, so the
line stays on the dashboard's own row when the user has switched to another workspace. The same
line is never written twice, so a quiet dashboard calls cmux once. The call runs in a worker
thread and never raises: one failure turns the status off for the session and says so once in the
output, and `ui.cmux-status = false` turns it off for good. The line carries one icon,
`arrow.triangle.branch`, so a user tells draupnir's line from the ones cmux writes itself, which
cmux marks with icons of its own. The icon is the same at every level, because the colour already
carries the level. It names a symbol of the system, and cmux does not check the name, so a name
that does not exist simply draws nothing.

**D53. A terminal session per clone through `cmux local-tmux`.** Every clone can have a terminal
session that outlives a cmux restart, and `new` starts it. The session is named from the workspace
folder and the clone folder, such as `shop-feature-login`, because cmux accepts only letters,
digits, `_` and `-` in a session name. A letter, a digit, `_` and `-` stay, the one space between
the two folders becomes `-`, `.` and `:` become `_` as tmux does, and every other character
becomes `-`. A name that needed no change is used as it is. A name that had a character changed,
dropped or cut, or that is longer than 128 characters, gets `-` and the first 8 hex characters of
the SHA-256 of the clone's path, so `release-1.2.3` and `release-1_2_3` get different sessions.
The path is normalized first, so a folder that macOS stores in another unicode form gets the
same name. A hash on every name was rejected, because the common name stays readable without it.
Two folders can still map to the same exact name, so a session is the clone's only when its name
matches and its registered folder is the clone's folder. draupnir never starts, adopts or closes a
session of another folder, and says so. It reads the session list from `local-tmux list --json`
and matches the name exactly, never by prefix. Every field that decides what draupnir may do must
be there with the right type. A missing field, or a `live` that is not a JSON boolean, makes the
list unreadable, and an unreadable list never allows a close or a start. draupnir does not read
the message of `local-tmux status`, because it is meant for people. A session that is registered
but does not run is closed and started again. cmux 0.64.25 keeps such a record for good and
refuses to start the name (cmux issue #17269, fixed in 0.65.1), and `close` removes the record.
The session starts detached, with the user's login shell. It starts with draupnir's own
environment, without the no-prompt variables of `runner.run`, because the first start after a boot
starts the tmux server, which would hand `GIT_EDITOR=true` and `NO_COLOR=1` to every shell. It
also starts without `VIRTUAL_ENV` and without that folder on `PATH`, so `uv run draupnir` does not
leak its environment into the user's shell. The `local-tmux` commands that draupnir uses do not
need the cmux socket (D51), so `draupnir attach` works in the console of an IDE. `attach` starts a
session even with `session = false`, because the user asked for it. `new` and `remove` skip the
session without a word when no cmux tool is installed.

**D54. One attach line, with `--headless`.** The line is `cmux local-tmux attach <name>
--headless`, built from the same tool that draupnir found (D51). It is the first command of the
clone's cmux tab, the command saved for the tab with `cmux surface resume set`, and what
`draupnir attach` printed, until D58 replaced it with a line that runs tmux itself. Without
`--headless`, `attach` opens a tab through the cmux socket. A line that was typed into a restored
shell then opened a second tab and left the first shell idle. With `--headless`, the same line
works in any terminal. The saved command is stored as separate arguments, so no quoting can change
it.

**D55. A cmux workspace per clone.** Inside cmux, `new` opens a workspace for the clone. It has the
title of the IDE project (D41), it shows the clone's session, and it joins the sidebar group of the
caller's workspace (D46). Workspace titles are not unique, and an id that draupnir remembered
could be stale. So draupnir finds the clone's workspace by its title and by the attach line that
cmux saved for one of its terminals, with the clone's folder as the saved folder. It creates a
workspace only when every read succeeded and none matched. A failed read says so and creates
nothing, because the workspace may exist already, and a second press of `w` after a failed read
must not make a second one. `new` opens the workspace without switching to it, from the command
line and from the dashboard, because a clone that is made in the background must not take the
screen. `w` goes to the workspace, and opens it first when it does not exist. With
`session = false`, the workspace is the plain one that D46 describes, with no lookup and no saved
command. `--workspace` and `--no-workspace` on `new` override `cmux.clone-workspace`. The flag
outside cmux logs one line and `new` still exits 0. When `new` made a session but no workspace, it
prints the attach line instead.

**D56. What `remove` closes, and what it leaves open.** The question keeps its first line and comes
before the fetch, as D32 says, and gains lines about the session. They say whether the session is
open and how many clients it has, which programs still run in it, and that it is closed together
with the clone. A program is named as tmux names it, so Claude Code shows as its version. After
the checks pass, and before the folder is deleted, draupnir closes the clone's workspace and then
its session. So a refused removal closes nothing, and the session is gone before the folder.
A workspace stays open when draupnir runs in the clone's session, when draupnir runs in that very
workspace, and when it holds a tab that draupnir did not make. The terminal that holds the attach
line and the todo pane are draupnir's. Every other tab counts, whatever its type, so a notes pane
or a browser keeps the workspace open, and the user closes it. A session that draupnir runs in
stays open too, and the log says to type `exit` there. draupnir clears the saved resume command
before it closes a workspace, so `bin/cmux-resume` does not bring it back. Closing is best effort.
A failure, a missing tool or an unreadable list is logged and never stops the removal or changes
its exit code. When the folder cannot be deleted after the session was closed, the error of the
removal stands and the session stays closed. Reading the session and the processes in it has its
own short timeout, so a slow tmux cannot hold up the question.

**D57. Two guards in `bin/cmux-resume`.** A tab that runs a tmux client is never idle, whatever the
process tree looks like. A tmux client, or a command line that contains `local-tmux attach`, is
a live terminal, and typing a command into it would reach the user's shell inside the session. A
saved command whose folder no longer exists is reported as `gone`. Nothing is typed for it, and
`--open-missing` opens nothing for it, because the folder belongs to a clone that was removed.

**D58. Terminals attach with tmux itself.** `cmux local-tmux attach --headless` keeps the
terminal's foreground for itself and runs its tmux client in a process group of its own. A terminal
sends its size change only to the foreground, so that client never learns that the terminal was
resized. The console of an IDE then gets a window drawn for an old size, and every line wraps. So
`draupnir attach`, and the line that `new` prints, run `tmux -S <socket> attach-session -t
'=<name>'`. The socket is the `socket_path` of `local-tmux list --json`, and tmux is the one that
cmux uses (`CMUX_LOCAL_TMUX_BIN`, or the PATH). The quotes keep zsh from reading `=<name>` as a
command path. Without tmux or a socket, the line stays the cmux line of D54. The clone's cmux tab
runs the same command, and cmux saves it for the tab, so a tab that cmux restores follows resizes
too. A tab that was saved before keeps the cmux line, and the workspace lookup accepts both.

**D59. Finished is read from git, not set by hand.** A flag the user sets is wrong the moment
they commit one more line, so draupnir works the state out instead (R29). Three parts of the
answer were already there: the counts of `git status`, the operation markers in the git folder,
and the comparison D21 makes before a removal. Two choices matter. The first is that the branch
must have been pushed, or GitHub must have deleted it after a merge: a clone that was only just
made is ahead of the default branch by nothing either, so without this part every new clone
would read `done` on the day it was made. The second is that the commits are compared by their
change and not by their name, because `git rev-list --left-right --count` counts names and
GitHub's "rebase and merge" gives every commit a new one. `git rev-list --cherry-pick
--right-only --no-merges` answers the real question. A squash merge is still missed, because it
turns the whole branch into one commit that matches nothing; D21 lives with the same limit. The
two reads that cost something, the stash and the comparison of the changes, run only for a clone
that passed every cheap check, so a clone that is clearly busy costs nothing extra. Even so, the
comparison took about a second on a branch 47 commits from a main branch 166 commits ahead, and
the dashboard reads the status every few seconds. Its answer depends on nothing but the two
commits, so it is kept for that pair, in memory only, the way the build results are (Q12). The
word goes in the `In progress` column, which is empty for almost every clone and already answers
"what is this clone busy with".

**D60. The files git ignores are named, and kept only when asked.** Removing a clone ends in a
recursive delete, and the checks before it never see a file that `.gitignore` covers: such a file
is not untracked. A clone that reads clean in every column can therefore hold the notes and the
plan that were written for the change. So the question names them, with their size and the
folders they are in (R30), and the same lines come with the question after a merged pull request,
because that is when a clone is usually removed.

The removal is not refused over them. Ignored files are ignored on purpose, and a tool that stops
on build output nobody wants is a tool people pass `--force` to every time. draupnir also does
not know what a plan is: `keep-on-remove` takes patterns, the same way `local-files` does, and the
two are symmetric on purpose, one copying files into a new clone and the other copying them out
of one that is going away. The copies go to `removed/` in the workspace root, which is no git
clone, so nothing lands in a commit by accident, and draupnir never empties that folder itself.
What draupnir put in the clone is left out of the count, so a large `.m2repo` is never even
walked, and so are the files of `local-files`, because `main/` keeps its own copy.

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
by their binding rather than by folder, because many shells share a folder. D57 added two guards.
