# Requirements

## Goal

draupnir works on several branches of the same git repository at the same time. Each branch
lives in its own clone, next to a clone of the project's default branch, called `main/`. Every
clone can run its own build, its own IDE and its own Claude Code session, without the clones
affecting each other.

A terminal dashboard shows the state of all clones on one screen. From the dashboard, a user
creates new clones and runs the common git and GitHub steps: fetch, rebase, push, follow the
build, fast-forward the default branch, open a pull request and merge it, and remove a clone.

draupnir works for any git project on github.com or GitHub Enterprise Server.

## Concepts

The workspace, `main/`, feature clones, local files and the private Maven repository are described
in the [user guide](guide.md#concepts).

## Requirements

### Workspace repository

- **R1.** The workspace is a git repository that can be pushed to GitHub. Its `.gitignore`
  ignores everything in the top folder except its own files, so `main/` and the feature clones
  are never committed to it.
- **R2.** The workspace can be set up on a new machine by cloning it and running one command that
  clones the project into `main/`.
- **R3.** An existing folder that already has `main/` inside can be turned into the workspace
  without moving `main/`.

### Clones

- **R4.** Creating a clone takes a branch name, an optional base branch and an optional folder
  name. If the branch exists on GitHub, the clone checks it out and tracks it. Otherwise a new
  branch is created from the latest `origin/<base>`. Without a folder name, the folder is the
  branch name with `/` replaced by `-`.
- **R5.** The clone is made from `main/` on disk, for speed, and then pointed at the GitHub URL
  of `main/` and fetched. After that it does not depend on `main/`.
- **R6.** The files named in `draupnir.toml` are copied from `main/` into the clone. Existing
  files are never overwritten. The copied files are added to `.git/info/exclude` of the clone, so
  git does not show them and they cannot be committed by accident.
- **R7.** Removing a clone deletes the folder, including `.m2repo` and the local files. It
  refuses when the clone has uncommitted changes, stashed changes, or commits on any local branch
  that are not on GitHub, unless forced.

### Maven

- **R8.** Each clone gets `.mvn/maven.config` with `-Dmaven.repo.local=<clone>/.m2repo` and
  `-Dmaven.repo.local.tail=<shared repository>`, written as full, absolute paths.
- **R9.** Setup checks that Maven, or the project's `mvnw`, is version 3.9.0 or newer, because
  older versions ignore the tail setting without a warning.
- **R10.** Setup stops and does not change anything when the project tracks
  `.mvn/maven.config` in git.
- **R11.** `main/` gets its own `.m2repo` too, so nothing from the project is installed into the
  shared `~/.m2/repository`.

### Dashboard: new clone

- **R12.** A tab with inputs for the branch name, the base branch and the folder name, and a
  button that creates the clone as in R4 to R6 and R8.
- **R13.** After creating the clone, the dashboard can start the IDE in the new folder: IntelliJ
  IDEA for Java projects, Android Studio for Android projects, PyCharm for Python projects.
- **R14.** The branch name can also be passed on the command line, to fill in the input.

### Dashboard: projects

- **R15.** A tab lists `main/` and every folder next to it that is a clone of the same remote.
- **R16.** For each clone the list shows: folder, branch, commits ahead and behind
  `origin/<default branch>`, commits ahead and behind the branch on GitHub (or "not pushed"),
  local changes, an operation in progress such as a rebase, and the last known build result.
  A click on the build result, or a key, opens the build in the browser.
- **R17.** Actions on the selected clone:

  | Action | Behaviour |
  |---|---|
  | Fetch all / Refresh | Fetch every clone, or only read the state again |
  | Open IDE | Start the IDE in the clone |
  | cmux workspace | Go to the clone's own cmux workspace, or open it with the clone's terminal session as R27 says. Only shown in cmux |
  | Rebase on main | Refuse when tracked files are changed. Fetch, then rebase on `origin/<default>`. On a conflict, stop, leave the rebase in progress, list the files, and offer to open the IDE |
  | Push and follow build | Push the branch. Feature branches use `--force-with-lease --force-if-includes`. The default branch is never force-pushed. Then follow the build of the pushed commit in the background |
  | Fast-forward main | Only when the branch is on top of `origin/<default>`. Push `HEAD` to the default branch without force. Then update `main/` |
  | Pull request and merge | Push, create a pull request or reuse the open one, follow the build, merge with rebase only when the build succeeds, then update `main/` |
  | Build status | Ask GitHub once for the build result of the current commit |
  | Edit config | Open `draupnir.toml` in the editor, as in R25 |
  | Remove clone | As R7 |

- **R18.** Actions that change the default branch or delete a folder ask for confirmation first.
  Fast-forward main and Pull request and merge need a feature branch, so they are disabled for
  `main/` and for every clone that is on the default branch. Remove clone is disabled for `main/`
  only, because a clone on the default branch can be removed as well.
- **R19.** An output area shows every command and its output while it runs. Only one action runs
  at a time. Following the build of a pushed commit is not an action: the dashboard keeps working
  while it waits, and it can follow the builds of several clones at once. Waiting for a build can
  be stopped without stopping the build itself.

### Following builds

- **R20.** Build results come from GitHub, through the `gh` CLI, so github.com and GitHub
  Enterprise Server both work. The dashboard reads check runs (GitHub Actions, apps) and commit
  statuses (for example an external CI server that reports back to GitHub).
- **R21.** Waiting ends when any job fails or all jobs succeed. When nothing has reported after a
  grace period, the result is "no build". There is an overall timeout. The build column updates
  while waiting.

### Safety and robustness

- **R22.** External commands never wait for input: no credential prompts, no editors, no `gh`
  prompts.
- **R23.** Reading the state of a clone does not take git locks, so the dashboard does not
  disturb an IDE or Claude Code working in the same clone.
- **R24.** Every message to the user says what happened and what to do next, in plain sentences.
- **R25.** The dashboard can open `draupnir.toml` in the user's own editor, and shows the file in
  the output when no editor is set. This is the one command that takes over the terminal, and it
  only runs when the user asks for it. R22 still holds for every command draupnir runs itself.

### Terminals

- **R26.** Each clone can have a terminal session, named from its folder. `new` starts it.
  `attach` prints the line that joins it from any terminal, and starts the session when it is
  missing.
- **R27.** Inside cmux, `new` opens a workspace for the clone that shows the session, in the
  caller's sidebar group, and `w` goes to it. The tab comes back after a cmux restart through
  `bin/cmux-resume`.
- **R28.** `remove` names the session in its question, and closes the session and the clone's
  workspace with the clone. A failure, or a session or workspace that draupnir runs in, never
  stops the removal.

### When a clone is finished

- **R29.** The list says `done` for a clone that has nothing left to do: the branch was pushed,
  the default branch on GitHub has every change the branch has, the working tree is clean, there
  is no stash, and no operation is in progress. The changes are compared by what they do and not
  by the name of the commit, so a branch that GitHub merged with rebase still counts. A clone
  that was never pushed is not finished, which is what tells a clone that is done from one that
  was only just made. A finished clone is not unfinished work in the sidebar line of cmux.
- **R30.** A clone can read clean and still hold files that git ignores, such as the notes and
  the plan written for the change. The question before a removal names them, with their size and
  where they are, so nothing is deleted unseen. `keep-on-remove` lists patterns whose files are
  copied into `removed/<clone>-<date>/` in the workspace root before the clone is deleted. A copy
  that fails stops the removal.

## Things outside this tool

See [what draupnir does not manage](troubleshooting.md#what-draupnir-does-not-manage).

## Not now

These are left out on purpose, and can be revisited later.

- **Parallel actions.** Only one action runs at a time, and only inside one dashboard. Following
  a build is the one thing that runs beside an action.
- **Desktop notifications.** The dashboard only shows results inside its own output area and
  notifications. It does not send a notification to the desktop.
- **Remembered build results.** Build results are kept in memory only, for the life of the
  dashboard process. "Build status" fetches the result for one clone again on request.
- **Claude Code integration.** draupnir does not start Claude Code in a clone, and it does not
  show which clones have a running Claude Code session.
- **Shared services.** draupnir does not assign port offsets or database schemas per clone.
