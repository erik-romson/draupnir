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

**The session belongs to another folder.** Another folder already has a session of the same name,
such as `shop/feature-login` and `shop-feature/login`. draupnir never uses or closes a session of
another folder. Close the old one with `cmux local-tmux close <name>` when you do not need it, or
give the clone another folder name with `--folder`.

**The tab came back as an empty shell.** cmux restored the tab, but nobody typed the attach line
into it. Run `bin/cmux-resume` from the draupnir checkout. It types the line into every tab that
holds a saved command and runs nothing.

**Every line wraps in the console of an IDE.** The terminal was attached with
`cmux local-tmux attach --headless`, which never tells tmux that the terminal changed size. Detach
with `Ctrl-b d` and attach again with the line that `draupnir attach` prints. That line runs tmux
itself, so tmux sees every resize.

**Two terminals show one session.** A tmux window has one size, so a terminal of another size sees
it cropped or with empty space. By default, the terminal that you typed in last decides the size.
To keep the size of one terminal until you move, add this key to `~/.tmux.conf`, and press
`Ctrl-b T` in the terminal that you work in:

```
bind T run-shell -C 'resize-window -x #{client_width} -y #{e|-:#{client_height},#{?status,1,0}}'
```

**The session is gone after a reboot.** A reboot stops tmux and every session with it. Run
`draupnir attach` in the clone. It starts the session again and prints the line that joins it.

**A session stays after a clone was deleted or moved by hand.** draupnir closes the session only
in `remove`. List the sessions with `cmux local-tmux list` and close the old one with
`cmux local-tmux close <name>`. A name with eight odd characters at the end comes from the clone's
path, so a clone that you move gets a new session under a new name, and the old session stays until
you close it.

**A workspace was left open because it has other tabs.** `remove` closes a clone's cmux workspace
only when it holds nothing but the terminal of the session and the todo pane. A second terminal, a
browser and a `markdown` pane each count as another tab. Close the workspace in cmux when you are
done with those tabs.

**The session name ends with eight odd characters.** The folder name has a character that cmux
does not accept in a session name, such as a `.`, a space or a letter with an accent, or the name
is too long. draupnir changes it and adds `-` and eight characters from the hash of the clone's
path, so two folders never share a name by accident. `draupnir attach` prints the exact name.

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
