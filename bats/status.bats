#!/usr/bin/env bats
#
# Reading the state of every clone (R15, R16, R23). draupnir status prints the columns of the
# dashboard's table as text, so these tests read its output.

load 'helpers/workspace'

setup_file() {
    require_tools
}

setup() {
    setup_workspace
}

@test "status lists main/ and every clone of the same project next to it" {
    run draupnir new feature-a
    assert_success

    # A folder that is not a clone of the project is not a clone of this workspace (R15).
    mkdir -p "$ROOT/notes"
    git clone -q --bare "$SOURCE" "$TEST_DIR/other.git"
    git clone -q "$TEST_DIR/other.git" "$ROOT/other-project"

    run draupnir status
    assert_success
    assert_line --index 0 --partial "main"
    assert_line --index 1 --partial "feature-a"
    assert_equal "${#lines[@]}" 2
}

@test "status shows a branch that was never pushed as not pushed" {
    run draupnir new feature-a
    assert_success

    run draupnir status
    assert_success
    assert_line --partial "feature-a"
    assert_line --regexp "^feature-a .*not pushed"
}

@test "status counts the commits a clone is ahead of the main branch" {
    run draupnir new feature-a
    assert_success
    commit_fixture "$ROOT/feature-a" edits/note "Add a note" > /dev/null

    run draupnir status
    assert_success
    assert_line --regexp "^feature-a .*main ↑1 ↓0"
}

@test "status counts the commits a clone is behind the main branch" {
    run draupnir new feature-a
    assert_success
    push_from_other_clone main edits/other-note > /dev/null

    run draupnir fetch
    assert_success

    run draupnir status
    assert_success
    assert_line --regexp "^feature-a .*main ↑0 ↓1"
}

@test "status counts changed and untracked files" {
    run draupnir new feature-a
    assert_success
    change_fixture "$ROOT/feature-a" edits/name-from-branch
    printf 'scratch\n' > "$ROOT/feature-a/scratch.txt"

    run draupnir status
    assert_success
    assert_line --regexp "^feature-a .*1 changed, 1 untracked"
}

@test "status reads a clone while git holds the index lock" {
    run draupnir new feature-a
    assert_success

    # An IDE or another tool in the clone holds this lock. draupnir must still read the state,
    # and it must not take the lock itself (R23).
    touch "$ROOT/feature-a/.git/index.lock"

    run draupnir status
    assert_success
    assert_line --partial "feature-a"
    refute_output --partial "error"

    assert_file_exist "$ROOT/feature-a/.git/index.lock"
}

@test "status names an operation that is in progress" {
    run draupnir new feature-a
    assert_success
    commit_fixture "$ROOT/feature-a" edits/name-from-branch "Change the name" > /dev/null
    push_from_other_clone main edits/name-from-main > /dev/null

    run draupnir rebase feature-a
    assert_failure 1

    run draupnir status
    assert_success
    assert_line --regexp "^feature-a .*rebase in progress"
}
