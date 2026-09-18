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


## Commands

| Command | What it does |
|---|---|
| `draupnir`, `draupnir ui [branch]` | Start the dashboard |
| `init [url] [--main-branch b]` | Write the workspace files and clone the project into `main/` |
| `new <branch> [--base b] [--open]` | Create a clone next to `main/` |
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
