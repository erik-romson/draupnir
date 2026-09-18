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
hides the action for a protected default branch.

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

**D25. IDE variables win over the config.** `DRAUPNIR_IDEA` and `DRAUPNIR_PYCHARM` hold the
machine's own value.

**D26. The dashboard refreshes on a timer.** Every `ui.refresh-seconds`. The timer never fetches,
and skips a read while an action or another read runs.

**D33. One action at a time.** Every button is disabled while an action runs. Only one status read
runs at a time, and a queued read runs after it, so an older result never replaces a newer one.
The table updates cells in place, so the selection and scroll position stay.

**D37. Open branch reads local refs.** The branch list comes from `origin/*` in `main/`, so it
opens at once, needs no network and takes no locks. It is as new as the last fetch. A match is
text anywhere in the name, ignoring case.

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
