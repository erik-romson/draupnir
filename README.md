# draupnir

draupnir is a terminal dashboard and a command line tool for working on several branches of one
git repository at the same time. Each branch lives in its own full clone, next to a clone of the
default branch called `main/`. Every clone can run its own build, IDE and Claude Code session.

The dashboard shows the state of every clone. It runs the common steps: fetch, rebase, push,
follow the build, fast-forward the default branch, merge through a pull request, and remove a
clone. Every dashboard action is also a subcommand.

## Requirements

- git 2.31 or newer.
- [`gh`](https://cli.github.com), logged in to your GitHub host (`gh auth login`, or
  `gh auth login --hostname github.example.com` for GitHub Enterprise Server).
- [uv](https://docs.astral.sh/uv/). It installs Python and the dependencies.
- For Maven projects: Maven 3.9.0 or newer, or a `mvnw` wrapper of that version.
- For the cmux features, the `cmux` command line tool. A terminal that cmux started already has
  it on the `PATH`. In every other terminal, open cmux, press `⌘ ⇧ P` and run
  `Shell Command: Install 'cmux' in PATH`.
- macOS, Linux or WSL. Native Windows is not supported.

## Install

```
uv tool install git+https://github.com/erik-romson/draupnir
uv tool upgrade draupnir    # later, to update
```

## Quick start

```
mkdir shop && cd shop
draupnir init git@github.example.com:acme/shop.git   # writes draupnir.toml, clones main/
draupnir new feature/vessel-owner --open             # a clone for the branch, opened in the IDE
draupnir                                             # the dashboard
```

The result:

```
shop/
  .gitignore
  draupnir.toml
  main/                   clone on the default branch
  feature-vessel-owner/   clone of feature/vessel-owner
```

## Running locally

```
uv run --project [path to local checkout] draupnir init 
```


### A terminal for every clone

`draupnir new` gives each clone a terminal session of its own, through `cmux local-tmux`. The
session is named after the folders, such as `shop-feature-login`, and runs your login shell in the
clone. It survives a cmux restart. `draupnir remove` closes it together with the clone.

Inside cmux, `new` also opens a cmux workspace for the clone. It has the title of the IDE project,
it shows the session, and it joins the sidebar group of the workspace that you ran `new` in. `new`
does not switch to it. The `w` key of the dashboard goes to it. `--no-workspace` skips it for one
clone, and `[cmux]` in `draupnir.toml` turns the session or the workspace off for good.

To join the session from any other terminal, such as the console of an IDE, print the line that
attaches to it:

```
draupnir attach                  # prints: tmux -S <socket> attach-session -t '=shop-feature-login'
eval "$(draupnir attach)"        # joins the session of the clone you are in
```

You can also paste the printed line. `draupnir attach` starts the session when the clone has none,
for example after a reboot. The line runs tmux itself, so the session follows when you resize the
terminal. Two terminals in one session share one window size. By default, the terminal that you
typed in last decides it. See [Two terminals show one session](docs/troubleshooting.md). After cmux relaunches, `bin/cmux-resume` types the line into the clone's tab and
brings the tab back. See [Restart the dashboards after cmux relaunches](#restart-the-dashboards-after-cmux-relaunches).

### The status in the cmux sidebar

A dashboard that runs in cmux writes one line about its clones into the sidebar row of its own
workspace. You see which project needs you without opening its dashboard:

```
rebase in feature-login               red, something stops the work
2 clones are changed                  yellow, unfinished work
clean                                 green, every clone is clean and in sync
```

The line carries an icon of its own, so you can tell it from the lines cmux writes itself.

Red covers an operation in progress, a failed build of the current commit, and a clone draupnir
cannot read. Yellow covers local changes, commits that are not on GitHub, and a branch GitHub no
longer has. The line holds the three most important parts, worst first. draupnir writes it again
only when it changes, and takes it away when the dashboard quits.

Set `cmux-status = false` under `[ui]` in `draupnir.toml` to turn it off.

### Restart the dashboards after cmux relaunches

cmux restores its tabs after an update or a crash, but it does not start a dashboard again. A
command that was saved from the command line stays manual, because only a choice made in the cmux
window can let cmux run it on its own. So you save the command once for each dashboard, and you
run one script after cmux relaunches.

#### Save the command

Go to the tab where the dashboard runs. Quit the dashboard with `q`, so that you have a shell
prompt. Then change into the workspace the dashboard watches, and save the command there:

```
cd [path to workspace]
cmux surface resume set -- uv run --project [path to local checkout] draupnir
```

cmux writes the command, the tab and the current folder into its own config file. Start the
dashboard again afterwards. The folder has to be a draupnir workspace, so it needs `draupnir.toml`
and `main/` next to each other. Run `draupnir init` there first when it does not. A dashboard that
starts anywhere else prints that it found no workspace and quits at once.

Save the command in one tab for each workspace. Two tabs that carry the same command start two
dashboards on the same clones, which is confusing and doubles the git reads.

Three commands show and change what cmux saved for the tab you are in:

```
cmux surface resume show                 # the command, the tab and the folder
cmux surface resume set --cwd [path] -- uv run --project [checkout] draupnir
cmux surface resume clear                # the tab starts nothing again
```

#### Restart the dashboards

After cmux relaunches, run the script from the checkout:

```
bin/cmux-resume
```

It prints one line for each saved command. `start` means that it typed the command into the tab.
`busy` means that the tab already runs something, so the script left it alone. A tab that runs a
tmux client is always busy. `missing` means that the tab is gone. `gone` means that the folder of
the saved command no longer exists, so the script typed nothing. The script never types into a tab that runs a program, and it leaves the agent
sessions to cmux, which restarts those itself.

| Option | What it does |
|---|---|
| `--dry-run` | Prints the lines and types nothing |
| `--match <text>` | Only saved commands whose command line contains the text |
| `--open-missing` | Opens a new workspace for a saved command whose tab is gone |
| `--config <file>` | Reads another cmux config file |

## Commands

| Command | What it does |
|---|---|
| `draupnir`, `draupnir ui [branch]` | Start the dashboard |
| `init [url] [--main-branch b]` | Write the workspace files and clone the project into `main/` |
| `new <branch> [--base b] [--folder name] [--open] [--workspace\|--no-workspace]` | Create a clone next to `main/`, with its terminal session |
| `attach [folder]` | Print the line that joins the clone's terminal session, and start the session when it has none |
| `prepare [folder]` | Copy the local files and set up Maven again |
| `status` | Print the state of every clone |
| `fetch` | Fetch every clone |
| `rebase [folder]` | Fetch and rebase on the main branch |
| `push [folder] [--follow]` | Push, and optionally wait for the build |
| `build [folder]` | Read the build result once |
| `ff-main [folder]` | Push the clone's head to the main branch, after a green build |
| `pr [folder] [--remove\|--keep]` | Push, open a pull request, wait for the build, merge |
| `remove [folder] [--force]` | Delete a clone after checking for unsaved work |
| `open [folder]` | Start the IDE in a clone |

`draupnir --help` lists the commands, and `draupnir <command> --help` shows the options of one
command. A folder argument defaults to the clone you are in. `-C <folder>` runs a command as if it was
started in that folder. `--yes` skips confirmations, which scripts and Claude Code need.

## Documentation

- [User guide](docs/guide.md): workspaces, daily work, the dashboard, exit codes.
- [Configuration](docs/configuration.md): `draupnir.toml` and environment variables.
- [Troubleshooting](docs/troubleshooting.md): common problems, and what draupnir does not manage.
- For contributors: [AGENTS.md](AGENTS.md), [architecture](docs/architecture.md),
  [requirements](docs/requirements.md) and [decisions](docs/decisions.md).

## License

MIT. See [LICENSE](LICENSE).
