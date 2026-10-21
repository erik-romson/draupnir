#!/usr/bin/env bats
#
# Removing a clone (R7): the folder goes, with .m2repo and the local files, but only when no
# work would be lost.

load 'helpers/workspace'

setup_file() {
    require_tools
}

setup() {
    setup_workspace
    set_local_files "local.properties"
    printf 'port=8080\n' > "$MAIN/local.properties"
    run draupnir new feature-a
    assert_success
    CLONE="$ROOT/feature-a"
    # new starts a session through the stub cmux. The tests here look at the calls of remove only.
    rm -f "$BATS_CMUX_LOG"
}

# How many times the stub cmux got a local-tmux close.
close_count() {
    if [ -f "$BATS_CMUX_LOG" ]; then
        grep -c "^local-tmux	close	" "$BATS_CMUX_LOG" || true
    else
        echo 0
    fi
}

@test "remove deletes a clone with its private Maven repository and its local files" {
    assert_dir_exist "$CLONE/.m2repo"
    assert_file_exist "$CLONE/local.properties"

    run draupnir remove feature-a --yes
    assert_success

    assert_dir_not_exist "$CLONE"
    # main/ is the source of the local files, so it keeps its own copy.
    assert_file_exist "$MAIN/local.properties"
}

@test "remove refuses a clone with changes that are not committed" {
    change_fixture "$CLONE" edits/name-from-branch

    run draupnir remove feature-a --yes
    assert_failure 1
    assert_output --partial "not on GitHub"

    assert_dir_exist "$CLONE"
}

@test "remove refuses a clone with a file that git does not track yet" {
    printf 'work in progress\n' > "$CLONE/scratch.txt"

    run draupnir remove feature-a --yes
    assert_failure 1
    assert_dir_exist "$CLONE"
}

@test "remove refuses a clone with a stash" {
    change_fixture "$CLONE" edits/name-from-branch
    git -C "$CLONE" stash -q

    run draupnir remove feature-a --yes
    assert_failure 1
    assert_output --partial "stash"

    assert_dir_exist "$CLONE"
}

@test "remove refuses a clone with commits that GitHub does not have" {
    commit_fixture "$CLONE" edits/note "Add a note" > /dev/null

    run draupnir remove feature-a --yes
    assert_failure 1
    assert_output --partial "not on GitHub"

    assert_dir_exist "$CLONE"
}

@test "remove deletes a clone whose only unpushed branch is a throwaway branch" {
    set_config workspace 'throwaway-branches = ["backup/*"]'
    git -C "$CLONE" checkout -q -b backup/feature-a-20260101
    commit_fixture "$CLONE" edits/note "Add a note" > /dev/null
    git -C "$CLONE" checkout -q feature-a

    run draupnir remove feature-a --yes
    assert_success

    assert_dir_not_exist "$CLONE"
}

@test "remove still refuses a branch that throwaway-branches does not name" {
    set_config workspace 'throwaway-branches = ["backup/*"]'
    git -C "$CLONE" checkout -q -b side-work
    commit_fixture "$CLONE" edits/note "Add a note" > /dev/null
    git -C "$CLONE" checkout -q feature-a

    run draupnir remove feature-a --yes
    assert_failure 1
    assert_output --partial "Branch side-work"

    assert_dir_exist "$CLONE"
}

@test "remove deletes a clone whose commits are all on GitHub" {
    commit_fixture "$CLONE" edits/note "Add a note" > /dev/null
    run draupnir push feature-a
    assert_success

    run draupnir remove feature-a --yes
    assert_success
    assert_dir_not_exist "$CLONE"
}

@test "remove --force deletes a clone that would lose work" {
    commit_fixture "$CLONE" edits/note "Add a note" > /dev/null

    run draupnir remove feature-a --yes --force
    assert_success
    assert_dir_not_exist "$CLONE"
}

@test "remove refuses main/, because every new clone is made from it" {
    run draupnir remove main --yes
    assert_failure 1

    assert_dir_exist "$MAIN/.git"
}

@test "remove refuses a folder that is not a clone of the project" {
    mkdir -p "$ROOT/notes"

    run draupnir remove notes --yes
    assert_failure 1
    assert_dir_exist "$ROOT/notes"
}

@test "remove asks first, and stops when it cannot ask" {
    run draupnir remove feature-a < /dev/null
    assert_failure 1
    assert_output --partial "--yes"

    assert_dir_exist "$CLONE"
}

@test "remove --yes closes the session of the clone" {
    cmux_session shop-feature-a "$(cd "$CLONE" && pwd -P)"

    run draupnir remove --yes feature-a
    assert_success

    assert_dir_not_exist "$CLONE"
    run grep -Fx "local-tmux	close	shop-feature-a	" "$BATS_CMUX_LOG"
    assert_success
}

@test "a refused removal keeps the session" {
    cmux_session shop-feature-a "$(cd "$CLONE" && pwd -P)"
    change_fixture "$CLONE" edits/name-from-branch

    run draupnir remove --yes feature-a
    assert_failure

    assert_dir_exist "$CLONE"
    assert_equal "$(close_count)" 0
}

@test "remove leaves another folder's session alone" {
    cmux_session shop-feature-a /elsewhere

    run draupnir remove --yes feature-a
    assert_success

    assert_dir_not_exist "$CLONE"
    assert_equal "$(close_count)" 0
}

@test "remove keeps what keep-on-remove matches, next to the clones" {
    set_config workspace 'keep-on-remove = ["notes/**"]'
    printf '/notes/\n' >> "$CLONE/.git/info/exclude"
    mkdir -p "$CLONE/notes"
    printf 'the plan\n' > "$CLONE/notes/plan.md"

    run draupnir remove feature-a --yes
    assert_success

    assert_dir_not_exist "$CLONE"
    local kept
    kept="$(find "$ROOT/removed" -name plan.md)"
    assert_equal "$(cat "$kept")" "the plan"
}
