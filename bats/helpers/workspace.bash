#!/usr/bin/env bash
#
# Builds a throwaway draupnir workspace for one BATS test.
#
# Every test works on a bare repository on disk, project.git, that stands in for GitHub, and on
# stub versions of mvn, idea and gh. HOME, the global git config and PATH are replaced, and the
# DRAUPNIR_* variables are removed, so a test never reads or changes the real ones.

# see https://github.com/ztombol/bats-docs#installation
# expected folder structure
# /usr/local/lib/bats
# ├── bats-assert
# ├── bats-detik
# ├── bats-file
# └── bats-support
# otherwise set BATS_LIB_PATH
export BATS_LIB_PATH="/usr/local/lib/bats/:$BATS_LIB_PATH"

bats_load_library bats-support
bats_load_library bats-assert
bats_load_library bats-file

PROJECT_ROOT="$(cd "${BASH_SOURCE[0]%/*}/../.." && pwd)"
DRAUPNIR_COMMAND="$PROJECT_ROOT/.venv/bin/draupnir"
VENV_PYTHON="$PROJECT_ROOT/.venv/bin/python"
FIXTURES="$PROJECT_ROOT/bats/testfiles"
# The fake gh of the pytest suite. Both suites use the same stand-in for GitHub.
FAKE_GH="$PROJECT_ROOT/tests/fake_gh.py"
GH_STATE_COMMAND="$PROJECT_ROOT/bats/helpers/gh_state.py"

# How long a test waits for a program that draupnir started in the background, such as the IDE.
BACKGROUND_WAIT_SECONDS=5

# ----- setup_file ---------------------------------------------------------------------------

# Checks the tools every test needs. Call this from setup_file, so one missing tool fails the
# whole file once with a message that says how to fix it.
require_tools() {
    command -v git > /dev/null || fail "git is not installed or not on PATH."
    [ -x "$DRAUPNIR_COMMAND" ] \
        || fail "$DRAUPNIR_COMMAND is missing. Run 'uv sync' in $PROJECT_ROOT first."
    [ -f "$FAKE_GH" ] || fail "$FAKE_GH is missing."
}

# ----- Building the sandbox -----------------------------------------------------------------

# Builds the bare repository that stands in for GitHub, the stub commands, and an empty
# workspace folder. It does not run draupnir init, so a test can run init itself.
#
# Sets TEST_DIR, REMOTE, SOURCE, ROOT, MAIN, STUB_BIN, BATS_IDEA_LOG, BATS_CMUX_LOG,
# BATS_CMUX_SESSIONS, BATS_CMUX_FAIL and BATS_TMUX_LOG.
setup_sandbox() {
    local name
    name="$(basename "$BATS_TEST_FILENAME" .bats)"
    # A fixed folder per test, so a failed test leaves its workspace behind to look at.
    TEST_DIR="$PROJECT_ROOT/tmp/batstest/$name/$BATS_TEST_NUMBER"
    rm -rf "$TEST_DIR"
    mkdir -p "$TEST_DIR"

    _isolate_environment
    _write_stubs
    gh_state reset
    _build_remote

    ROOT="$TEST_DIR/shop"
    MAIN="$ROOT/main"
    mkdir -p "$ROOT"
}

# Builds the sandbox and runs draupnir init, so the test starts with a ready workspace that has
# main/ cloned. The build timings are short, so a test that follows a build does not wait.
setup_workspace() {
    setup_sandbox
    cat > "$ROOT/draupnir.toml" << 'EOF'
[build]
poll-seconds = 0.05
grace-seconds = 0.1
timeout-seconds = 30
EOF
    "$DRAUPNIR_COMMAND" -C "$ROOT" init "$REMOTE" > "$TEST_DIR/init.log" 2>&1 \
        || fail "draupnir init failed:
$(cat "$TEST_DIR/init.log")"
}

_isolate_environment() {
    # The test workspace lies inside the draupnir repository, so git would find that repository
    # when it searches upwards from a folder that is not a clone yet. The ceiling stops the
    # search at the sandbox, which is what a workspace in a folder of its own looks like.
    export GIT_CEILING_DIRECTORIES="$TEST_DIR"

    local variable
    for variable in $(env | sed -n 's/^\(DRAUPNIR_[A-Za-z0-9_]*\)=.*/\1/p'); do
        unset "$variable"
    done
    # draupnir reads the editor of the config from these, so no test may see the real one.
    unset EDITOR VISUAL
    # The tests may run in a cmux terminal, and must not open workspaces in it or write a
    # status into its sidebar. The stub cmux that _write_stubs puts first on the PATH is the
    # only one they can reach, because cmux puts the real one on the PATH as well.
    unset CMUX_WORKSPACE_ID CMUX_BUNDLED_CLI_PATH
    # The tests may also run inside a terminal session of cmux local-tmux, or in a virtual
    # environment, and must not see either. draupnir reads these to tell whether it runs inside
    # a session, and to build the environment of a new one.
    unset TMUX TMUX_PANE CMUX_SURFACE_ID CMUX_LOCAL_TMUX_BIN VIRTUAL_ENV

    export HOME="$TEST_DIR/home"
    mkdir -p "$HOME"
    export GIT_CONFIG_GLOBAL="$HOME/.gitconfig"
    cat > "$GIT_CONFIG_GLOBAL" << 'EOF'
[user]
	email = test@example.com
	name = Test
[init]
	defaultBranch = main
EOF

    export BATS_IDEA_LOG="$TEST_DIR/idea-calls.txt"
    export BATS_CMUX_LOG="$TEST_DIR/cmux-calls.txt"
    export BATS_CMUX_SESSIONS="$TEST_DIR/cmux-sessions.json"
    export BATS_TMUX_LOG="$TEST_DIR/tmux-calls.txt"
    export BATS_CMUX_FAIL=
    export BATS_VENV_PYTHON="$VENV_PYTHON"
    export BATS_FAKE_GH="$FAKE_GH"
    export FAKE_GH_STATE="$TEST_DIR/fake-gh-state.json"
}

_write_stubs() {
    STUB_BIN="$TEST_DIR/bin"
    mkdir -p "$STUB_BIN"
    cp "$FIXTURES/stubs/mvn" "$FIXTURES/stubs/idea" "$FIXTURES/stubs/gh" \
        "$FIXTURES/stubs/cmux" "$FIXTURES/stubs/tmux" "$STUB_BIN/"
    chmod +x "$STUB_BIN"/*
    export PATH="$STUB_BIN:$PATH"
}

_build_remote() {
    SOURCE="$TEST_DIR/source"
    mkdir -p "$SOURCE"
    cp -R "$FIXTURES/project/." "$SOURCE/"
    git -C "$SOURCE" init -q
    git -C "$SOURCE" add -A
    git -C "$SOURCE" commit -q -m "Add the shop project"
    # A second branch on GitHub, for the tests that give a base branch or another main branch.
    git -C "$SOURCE" branch develop

    # The bare clone's HEAD follows source's default branch, main, because init.defaultBranch
    # is set before source is created.
    REMOTE="$TEST_DIR/project.git"
    git -C "$TEST_DIR" clone -q --bare "$SOURCE" "$REMOTE"
}

# ----- Running draupnir ---------------------------------------------------------------------

# Runs the draupnir command line as if it was started in the workspace root.
draupnir() {
    "$DRAUPNIR_COMMAND" -C "$ROOT" "$@"
}

# Runs the draupnir command line as if it was started in the given folder.
draupnir_in() {
    local folder=$1
    shift
    "$DRAUPNIR_COMMAND" -C "$folder" "$@"
}

# ----- The fake gh --------------------------------------------------------------------------

# Drives the fake gh: 'gh_state reset', 'gh_state build <sha> <result>' with success, failure,
# pending or none, and 'gh_state pull-requests'.
gh_state() {
    PYTHONPATH="$PROJECT_ROOT/tests" "$VENV_PYTHON" "$GH_STATE_COMMAND" "$@"
}

# ----- The fake cmux sessions ----------------------------------------------------------------

# Makes the stub cmux list one live terminal session. The entry holds every field that draupnir
# needs of a running session, because it reads an entry with a missing field as unreadable.
# Usage: cmux_session <name> <cwd> [clients]
cmux_session() {
    local name=$1 cwd=$2 clients=${3:-0}
    printf '{"sessions": [{"session_name": "%s", "cwd": "%s", "live": true, ' "$name" "$cwd" \
        > "$BATS_CMUX_SESSIONS"
    printf '"socket_path": "%s/tmux-socket", "session_id": "$0", "clients": %s}]}\n' \
        "$TEST_DIR" "$clients" >> "$BATS_CMUX_SESSIONS"
}

# Makes the stub cmux list no session, and name the socket of its tmux server, as cmux does.
cmux_server() {
    printf '{"sessions": [], "socket_path": "%s/tmux-socket"}\n' "$TEST_DIR" > "$BATS_CMUX_SESSIONS"
}

# ----- Changing the config ------------------------------------------------------------------

# Adds one line to a section of draupnir.toml, and adds the section when it is not there yet.
set_config() {
    local section=$1 line=$2
    local toml="$ROOT/draupnir.toml"
    grep -Fxq "[$section]" "$toml" || printf '\n[%s]\n' "$section" >> "$toml"
    awk -v header="[$section]" -v line="$line" '
        { print }
        $0 == header && !inserted { print line; inserted = 1 }
        END { if (!inserted) { print "no " header " section" > "/dev/stderr"; exit 1 } }
    ' "$toml" > "$toml.new" || fail "set_config could not find [$section] in $toml."
    mv "$toml.new" "$toml"
}

# Sets workspace.local-files to the given patterns.
set_local_files() {
    local quoted="" pattern
    for pattern in "$@"; do
        quoted+="${quoted:+, }\"$pattern\""
    done
    set_config workspace "local-files = [$quoted]"
}

# ----- Building git history ------------------------------------------------------------------

# Copies a fixture folder over a clone, commits every change, and prints the new commit's sha.
commit_fixture() {
    local repo=$1 fixture=$2 message=$3
    cp -R "$FIXTURES/$fixture/." "$repo/"
    git -C "$repo" add -A
    git -C "$repo" commit -q -m "$message"
    git -C "$repo" rev-parse HEAD
}

# A throwaway clone of the bare repository pushes a commit to a branch, and the sha is printed.
#
# This stands in for a teammate who pushed to GitHub while the clone under test has not fetched
# yet, and so cannot see the new commit.
push_from_other_clone() {
    local branch=$1 fixture=$2
    local other="$TEST_DIR/other-${branch//\//-}"
    rm -rf "$other"
    git clone -q --branch "$branch" "$REMOTE" "$other"
    local sha
    sha="$(commit_fixture "$other" "$fixture" "Update $branch")"
    git -C "$other" push -q origin "$branch"
    printf '%s\n' "$sha"
}

# Copies a fixture folder over a clone without committing, to make a tracked file changed.
change_fixture() {
    local repo=$1 fixture=$2
    cp -R "$FIXTURES/$fixture/." "$repo/"
}

# ----- Assertions ----------------------------------------------------------------------------

# Fails unless the clone's own .git/info/exclude has exactly this line.
assert_excluded() {
    local clone=$1 line=$2
    local exclude="$clone/.git/info/exclude"
    grep -Fxq -- "$line" "$exclude" || fail "$exclude has no line '$line'. It has:
$(cat "$exclude")"
}

# Fails when the clone's own .git/info/exclude has this line.
refute_excluded() {
    local clone=$1 line=$2
    local exclude="$clone/.git/info/exclude"
    if grep -Fxq -- "$line" "$exclude"; then
        fail "$exclude has the line '$line', and it should not."
    fi
}

# How many times a line appears in the clone's own .git/info/exclude.
exclude_line_count() {
    local clone=$1 line=$2
    grep -Fxc -- "$line" "$clone/.git/info/exclude" || true
}

# Fails unless git in the clone sees no change at all, counting untracked files.
assert_git_clean() {
    local clone=$1 output
    output="$(git -C "$clone" status --porcelain --untracked-files=all)"
    [ -z "$output" ] || fail "git in $clone is not clean. It sees:
$output"
}

# Fails unless the two files have the same content.
assert_same_content() {
    local left=$1 right=$2
    cmp -s "$left" "$right" || fail "$left and $right have different content."
}

# The sha a branch points to in the bare repository that stands in for GitHub.
sha_on_github() {
    git --git-dir "$REMOTE" rev-parse "refs/heads/$1"
}

# Fails unless the bare repository has this branch.
assert_branch_on_github() {
    git --git-dir "$REMOTE" rev-parse --verify --quiet "refs/heads/$1" > /dev/null \
        || fail "GitHub has no branch called '$1'."
}

# Fails when the bare repository has this branch.
refute_branch_on_github() {
    if git --git-dir "$REMOTE" rev-parse --verify --quiet "refs/heads/$1" > /dev/null; then
        fail "GitHub has a branch called '$1', and it should not."
    fi
}

# Waits until a program draupnir started in the background has written a file.
wait_for_file() {
    local path=$1 waited=0
    while [ ! -s "$path" ]; do
        if [ "$waited" -ge $((BACKGROUND_WAIT_SECONDS * 10)) ]; then
            fail "$path was not written within $BACKGROUND_WAIT_SECONDS seconds."
        fi
        sleep 0.1
        waited=$((waited + 1))
    done
}
