#!/usr/bin/env bats
#
# Creating a clone next to main/ with draupnir new (R4, R5).

load 'helpers/workspace'

setup_file() {
    require_tools
}

setup() {
    setup_workspace
}

@test "new creates a branch from the main branch, and the branch has no upstream yet" {
    run draupnir new feature-a
    assert_success

    local clone="$ROOT/feature-a"
    assert_equal "$(git -C "$clone" symbolic-ref --short HEAD)" "feature-a"
    assert_equal "$(git -C "$clone" rev-parse HEAD)" "$(git -C "$clone" rev-parse origin/main)"

    # The branch is new, so nothing on GitHub tracks it yet.
    run git -C "$clone" rev-parse --abbrev-ref "feature-a@{upstream}"
    assert_failure
    refute_branch_on_github feature-a
}

@test "new checks out a branch that GitHub already has, and tracks it" {
    local sha
    sha="$(push_from_other_clone develop edits/other-note)"

    run draupnir new develop
    assert_success

    local clone="$ROOT/develop"
    assert_equal "$(git -C "$clone" rev-parse HEAD)" "$sha"
    assert_equal "$(git -C "$clone" rev-parse --abbrev-ref 'develop@{upstream}')" "origin/develop"
    assert_file_exist "$clone/note-from-github.txt"
}

@test "the new clone points at the project URL, not at main/" {
    run draupnir new feature-a
    assert_success

    # The clone is made from main/ on disk for speed, and then pointed at GitHub (R5).
    assert_equal "$(git -C "$ROOT/feature-a" remote get-url origin)" "$REMOTE"
}

@test "a branch name with a slash becomes a folder name with a dash" {
    run draupnir new feature/login
    assert_success

    local clone="$ROOT/feature-login"
    assert_dir_exist "$clone/.git"
    assert_equal "$(git -C "$clone" symbolic-ref --short HEAD)" "feature/login"
}

@test "new --folder puts the clone in the folder it names" {
    run draupnir new feature/login --folder login
    assert_success

    local clone="$ROOT/login"
    assert_equal "$(git -C "$clone" symbolic-ref --short HEAD)" "feature/login"
    assert_dir_not_exist "$ROOT/feature-login"
    assert_git_clean "$clone"
}

@test "new starts the branch from the base branch that --base names" {
    local sha
    sha="$(push_from_other_clone develop edits/other-note)"

    run draupnir new feature-a --base develop
    assert_success

    local clone="$ROOT/feature-a"
    assert_equal "$(git -C "$clone" rev-parse HEAD)" "$sha"
    assert_file_exist "$clone/note-from-github.txt"
}

@test "new starts the branch from the base branch in draupnir.toml" {
    local sha
    sha="$(push_from_other_clone develop edits/other-note)"
    set_config workspace 'base-branch = "develop"'

    run draupnir new feature-a
    assert_success
    assert_equal "$(git -C "$ROOT/feature-a" rev-parse HEAD)" "$sha"
}

@test "new only holds the branch it created" {
    run draupnir new feature-a
    assert_success

    # The clone starts on the main branch, and that branch is deleted again, so the clone has
    # one local branch only.
    run git -C "$ROOT/feature-a" for-each-ref --format='%(refname:short)' refs/heads
    assert_success
    assert_output "feature-a"
}

@test "new refuses a base branch that GitHub does not have" {
    run draupnir new feature-a --base nothing-like-this
    assert_failure 1
    assert_output --partial "nothing-like-this"

    assert_dir_not_exist "$ROOT/feature-a"
}

@test "new refuses a folder that already exists" {
    mkdir -p "$ROOT/feature-a"

    run draupnir new feature-a
    assert_failure 1
    assert_output --partial "already exists"
}

@test "new refuses a name that is not a valid git branch name" {
    run draupnir new "bad..name"
    assert_failure 1

    assert_dir_not_exist "$ROOT/bad..name"
}

@test "new starts a terminal session for the clone" {
    run draupnir new feature-a
    assert_success

    local clone
    clone="$(cd "$ROOT/feature-a" && pwd -P)"
    run grep -Fx "local-tmux	start	shop-feature-a	--cwd	$clone	--detached	" "$BATS_CMUX_LOG"
    assert_success
}

@test "new starts no session when session is false" {
    set_config cmux "session = false"

    run draupnir new feature-a
    assert_success

    run grep "^local-tmux	start	" "$BATS_CMUX_LOG"
    assert_failure
}

@test "a session that cannot start never stops new" {
    BATS_CMUX_FAIL="local-tmux start"

    run draupnir new feature-a
    assert_success

    assert_dir_exist "$ROOT/feature-a"
    assert_git_clean "$ROOT/feature-a"
}

@test "new inside cmux opens a workspace that shows the session" {
    export CMUX_WORKSPACE_ID=workspace-test
    cmux_server

    run draupnir new feature-a
    assert_success

    local socket="$TEST_DIR/tmux-socket"
    run grep -F "workspace	create	" "$BATS_CMUX_LOG"
    assert_success
    assert_output --partial "--command	$STUB_BIN/tmux -S $socket attach-session -t '=shop-feature-a'	"

    run grep -F "surface	resume	set	" "$BATS_CMUX_LOG"
    assert_success
    assert_output --partial "--cwd	$ROOT/feature-a	"
    assert_output --partial "	--	$STUB_BIN/tmux	-S	$socket	attach-session	-t	=shop-feature-a	"
}
