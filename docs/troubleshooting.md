# Troubleshooting

## Common problems

**gh is not logged in.** The message names the host. Run
`gh auth login --hostname github.example.com`, then try again.

**git fails with a network error instead of asking for a password.** draupnir never lets a
command ask a question. Load your SSH key into `ssh-agent`, or set up a credential helper.

**Maven is too old.** Maven before 3.9.0 ignores `maven.repo.local.tail` without a warning, and
installs into `~/.m2`. Install a newer Maven or add a `mvnw` wrapper, then run `draupnir prepare`.
In IntelliJ, choose that Maven under Settings > Build, Execution, Deployment > Maven instead of the
bundled one.

**The project already tracks `.mvn/maven.config`.** `draupnir prepare` stops and changes nothing,
because a change would reach every clone through git. Set `maven.enabled = false` and manage the
file by hand.

**A rebase, merge or other operation is in progress.** Every action that changes a clone refuses,
and names the operation. Finish or abort it in the clone, for example with `git rebase --abort`.

**`main/` does not know the default branch.** Run `git -C main remote set-head origin --auto`.

**Two dashboards watch the same workspace after cmux relaunches.** Two cmux tabs carry the same
saved command. Run `cmux surface resume show` in each of them to see the folder, and
`cmux surface resume clear` in the one you do not want. See
[Restart the dashboards after cmux relaunches](../README.md#restart-the-dashboards-after-cmux-relaunches).

**A restarted dashboard quits at once.** The folder that cmux saved is not a draupnir workspace, so
the dashboard says that it found no workspace. Run `cmux surface resume show` in the tab to see the
folder. Run `draupnir init` in that folder, or save the command again with
`cmux surface resume set --cwd [path to workspace]`.

**A clone cannot be removed after a squash merge.** Squashed commits get new content, so draupnir
cannot see that they are merged. Use `draupnir remove --force`.

## What draupnir does not manage

- **Shared services.** Two clones that start the same app server compete for its ports. All clones
  share a local database schema. Give each clone its own port offset or schema by hand.
- **Old artifacts in `~/.m2`.** Every clone reads project artifacts installed there before
  draupnir. Delete them once, for example `rm -rf ~/.m2/repository/com/example/shop`.
- **Downloads.** A new dependency is downloaded into each clone's `.m2repo` separately.
- **Local git config and hooks.** `git clone` does not copy them. List hook files in
  `workspace.local-files`.
- **`git clean -fdx`.** It deletes `.m2repo`, `.mvn/maven.config` and the local files. Run
  `draupnir prepare` to restore them.
- **Moving the workspace.** The Maven paths are absolute. Run `draupnir prepare` in every clone
  after a move.
