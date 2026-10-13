# User guide

The examples use a Java and Maven project called `shop` on `github.example.com`. Replace these
with your own project.

## Concepts

- **Workspace.** A folder for one project, for example `shop/`. It is a small git repository that
  holds only `.gitignore`, `draupnir.toml` and an optional `README.md`. Push it, so the config
  travels to other machines.
- **`main/`.** A clone of the project on the default branch. New clones copy their local files
  from it.
- **Feature clone.** An independent clone next to `main/`. Its folder is the branch name with `/`
  replaced by `-`, so `feature/vessel-owner` lives in `feature-vessel-owner/`. `--folder` gives
  the folder another name.
- **Local files.** Files that are not in git but that every clone needs, such as
  `.claude/settings.local.json`. `draupnir.toml` lists them.
- **Private Maven repository.** Each clone installs into its own `.m2repo`, and reads everything
  else from `~/.m2/repository` without changing it.

## Set up a workspace

**A new project.**

```
mkdir shop && cd shop
draupnir init git@github.example.com:acme/shop.git
```

This writes `.gitignore` and `draupnir.toml` and clones the project into `main/`. The main
branch is GitHub's default branch. When your team works against another branch, name it:

```
draupnir init git@github.example.com:acme/shop.git --main-branch develop
```

The main branch is what "Rebase on", "Fast-forward" and pull requests use, and what `main/` stays
on. `init` writes it as `workspace.main-branch` in `draupnir.toml`. To change it later, run
`draupnir init --main-branch <branch>` again in the workspace, or edit the file.

To share the
config, make the workspace a git repository and push it. Use a private repository when the project
is private, because the config holds its URL and file names.

```
git init -b main
git add .gitignore draupnir.toml
git commit -m "Set up the shop workspace"
git remote add origin git@github.example.com:acme/shop-workspace.git
git push -u origin main
```

**A new machine.** Clone the workspace and run `draupnir init` without a URL. It takes the URL
from `workspace.repository` in `draupnir.toml`.

```
git clone git@github.example.com:acme/shop-workspace.git shop
cd shop && draupnir init
```

**An existing `main/`.** Run `draupnir init` in the folder that holds `main/`, not inside `main/`.
The clone stays where it is, on the branch it has. `init` tells you when that is not the main
branch.

**Moving from worktrees.** For each worktree, push its branch, then replace the worktree with a
clone:

```
git -C main worktree remove ../old-worktree
draupnir new feature/vessel-owner
```

## Daily work

**Start a branch.** `draupnir new feature/vessel-owner` checks out the branch with tracking when
it exists on GitHub. Otherwise it creates the branch from the latest `origin/main`, or from
`--base <branch>`. `--folder <name>` puts the clone in a folder of that name instead of one named
after the branch, for example `draupnir new feature/vessel-owner --folder owner`. `--open` starts IntelliJ IDEA for a Java project, Android Studio for an Android
project and PyCharm for a Python project.

**Project names in the IDE.** Every clone of `shop` would otherwise show up in the IDE by its folder
name, and `main` says little. So when draupnir opens a clone that the IDE has opened before, it
names the project after the workspace and the folder, for example `shop main` or
`shop feature-vessel-owner`. It writes `.idea/.name` only when that file does not exist, so a
name you give the project in the IDE is kept.

**See every clone.** `draupnir status` prints one line per clone: branch, commits ahead and behind
the default branch and the upstream branch, and local changes. The numbers are only as new as the
last `draupnir fetch`.

**Rebase.** `draupnir rebase` fetches, then rebases on `origin/<default>`. It refuses when tracked
files are changed or another operation, such as a merge, is in progress. On a conflict, the rebase
stays in progress. Resolve the files in the IDE, then run `git rebase --continue` yourself.

**Push.** `draupnir push --follow` pushes and waits for the build. A feature branch is pushed with
`--force-with-lease --force-if-includes`, so commits on GitHub that you have not integrated are
never overwritten. The default branch is never force-pushed.

**Fast-forward the default branch.** `draupnir ff-main` pushes the clone's head to the default
branch without force. The branch must be on top of `origin/<default>`, and its build must be green.
So push the head first and wait for its build, for example with `draupnir push --follow`. Afterwards `main/` is updated. When the default branch requires a review, GitHub refuses this
push. Set `actions.fast-forward-main = false` and use `draupnir pr` instead.

**Merge through a pull request.** `draupnir pr` pushes, creates a pull request or reuses the open
one, waits for the build, and merges with rebase when the build is green. When a review is still
required, it turns on auto-merge instead. When the build fails or never reports, the pull request
stays open.

draupnir has no review check of its own. It relies on GitHub's branch protection. If reviews are
only a team rule, draupnir does not enforce them.

After the merge, `main/` is updated. `--remove` deletes the clone, and `--keep` keeps it. Without
either, draupnir asks on a terminal.

**Update other clones.** Clones share nothing, so each one fetches on its own. After a merge, run
`draupnir rebase` in the other feature clones.

**Remove a clone.** `draupnir remove` deletes the folder, with its `.m2repo` and local files. It
refuses when the clone has uncommitted changes, a stash, or commits that are not on GitHub.
Commits that GitHub rebase-merged count as saved. A squash merge made on GitHub does not, so use
`--force` after one.

**Claude Code.** Run `claude` inside a feature clone, so the session sees only that clone. draupnir
never asks a question without a terminal. Pass `--yes` to `remove`, `ff-main` and `pr`.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Done. For `build` and `push --follow`, the build is green. |
| 1 | Stopped with a message, for example outside a workspace. |
| 2 | Wrong arguments. |
| 3 | The build is not green: failed, pending, missing or timed out. |
| 130 | Stopped with Ctrl+C. A build on GitHub keeps running. |

## The dashboard

Run `draupnir` anywhere inside the workspace. `draupnir ui <branch>` opens the New clone tab with
the branch filled in.

**Projects tab.** One row per clone shows the branch, commits ahead and behind, local changes, an
operation in progress, and the last known build. The table is read again every
`ui.refresh-seconds`, without a fetch.

Select a row and press a button. Fast-forward main and Pull request and merge need a feature
branch, so they are disabled for `main/` and for a clone that sits on the default branch. Remove
clone is disabled for `main/` alone, and removes a clone on any branch. Actions that change the
default branch or delete a folder ask first. Only one action runs at a time.

The output area shows every command as it runs. "Stop waiting" ends the wait for a build, not the
build itself. "Build status" reads the result again later.

**Open a build.** The Build column looks like a button in the colour of the state. Click it, or
press `g` on the selected row, to open the build in the browser. draupnir opens the first failed
job, else the first job that still runs, else the first job. The details line under the table
lists the jobs of the selected clone's build, and a click on a job opens that job. Both work
while an action runs, because they only use the build that draupnir already read.

**Edit config.** The "Edit config" button opens `draupnir.toml` in the editor from
`DRAUPNIR_EDITOR`, `VISUAL` or `EDITOR`. The dashboard steps aside while the editor runs, and
comes back when it ends. When none of these variables is set, the button writes the file into the
output instead. draupnir reads the config again afterwards, and says so when the file no longer
parses. Which buttons are shown, the default branch and the refresh time are read when the
dashboard starts, so restart draupnir after you change them.

**New cmux workspace.** When draupnir runs in a cmux terminal, this button opens a new cmux
workspace with a shell in the selected clone and the cmux todo list below it, and switches to it.
The workspace is called `<workspace> <folder>`, like the IDE project. When the dashboard's cmux
workspace is in a sidebar group, the new workspace joins that group, right after the dashboard.
Outside cmux the button is hidden.

| Key | Action |
|---|---|
| `r` | Refresh, without a fetch |
| `f` | Fetch all |
| `b` | Open a branch from GitHub as a new clone |
| `c` | Edit `draupnir.toml` |
| `o` | Open the IDE |
| `g` | Open the build of the selected clone in the browser |
| `w` | Open a cmux workspace in the selected clone, when draupnir runs in cmux |
| `n` | New clone tab |
| `p` | Projects tab |
| `q` | Quit |

**Open branch.** Type part of a branch name, anywhere in the name. The list narrows as you type.
Use ↑ and ↓ to choose, and Enter to create the clone. The list comes from the last fetch, so run
Fetch all first to see new branches. Branches that already have a clone are left out. The folder
field is optional. Its placeholder shows the folder the highlighted branch gets when it stays
empty.

**New clone tab.** Enter a branch, an optional base branch and an optional folder name. This does
the same as `draupnir new`. A checkbox opens the IDE afterwards.
