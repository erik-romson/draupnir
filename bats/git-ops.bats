#!/usr/bin/env bats
#
# Fetch, rebase on the main branch, and push (R17). The checks before an action that changes a
# clone are part of these tests too.

load 'helpers/workspace'

setup_file() {
    require_tools
}

setup() {
    setup_workspace
}

@test "fetch updates every clone" {
    run draupnir new feature-a
    assert_success
    local sha
    sha="$(push_from_other_clone main edits/other-note)"

    run draupnir fetch
    assert_success

    assert_equal "$(git -C "$MAIN" rev-parse origin/main)" "$sha"
    assert_equal "$(git -C "$ROOT/feature-a" rev-parse origin/main)" "$sha"
}

@test "rebase moves a branch onto the latest main branch" {
    run draupnir new feature-a
    assert_success
    local clone="$ROOT/feature-a"
    commit_fixture "$clone" edits/note "Add a note" > /dev/null
    local main_sha
    main_sha="$(push_from_other_clone main edits/other-note)"

    run draupnir rebase feature-a
    assert_success

    # The rebase fetches first, so it lands on the commit another clone just pushed.
    assert_equal "$(git -C "$clone" rev-parse "HEAD~1")" "$main_sha"
    assert_file_exist "$clone/note.txt"
    assert_file_exist "$clone/note-from-github.txt"
    assert_equal "$(git -C "$clone" log -1 --format=%s)" "Add a note"
}

@test "rebase stops on a conflict and leaves the rebase in progress" {
    run draupnir new feature-a
    assert_success
    local clone="$ROOT/feature-a"
    commit_fixture "$clone" edits/name-from-branch "Change the name" > /dev/null
    push_from_other_clone main edits/name-from-main > /dev/null

    run draupnir rebase feature-a
    assert_failure 1
    assert_output --partial "App.java"

    # draupnir never aborts the rebase for the user, so the conflict is there to resolve.
    assert_dir_exist "$clone/.git/rebase-merge"
    run git -C "$clone" status --porcelain
    assert_output --partial "UU src/main/java/example/App.java"
}

@test "an action refuses while a rebase is in progress, and names it" {
    run draupnir new feature-a
    assert_success
    commit_fixture "$ROOT/feature-a" edits/name-from-branch "Change the name" > /dev/null
    push_from_other_clone main edits/name-from-main > /dev/null
    run draupnir rebase feature-a
    assert_failure 1

    run draupnir push feature-a
    assert_failure 1
    assert_output --partial "rebase in progress"

    refute_branch_on_github feature-a
}

@test "rebase refuses when a tracked file is changed" {
    run draupnir new feature-a
    assert_success
    local clone="$ROOT/feature-a"
    change_fixture "$clone" edits/name-from-branch

    run draupnir rebase feature-a
    assert_failure 1

    # Nothing was rebased, and the change is still in the working tree.
    assert_dir_not_exist "$clone/.git/rebase-merge"
    run git -C "$clone" status --porcelain
    assert_output --partial "src/main/java/example/App.java"
}

@test "push sends the branch and its commits to GitHub" {
    run draupnir new feature-a
    assert_success
    local sha
    sha="$(commit_fixture "$ROOT/feature-a" edits/note "Add a note")"

    run draupnir push feature-a
    assert_success

    assert_equal "$(sha_on_github feature-a)" "$sha"
    assert_equal "$(git -C "$ROOT/feature-a" rev-parse --abbrev-ref 'feature-a@{upstream}')" \
        "origin/feature-a"
}

@test "push takes the clone from the folder it runs in" {
    run draupnir new feature-a
    assert_success
    local clone="$ROOT/feature-a"
    local sha
    sha="$(commit_fixture "$clone" edits/note "Add a note")"

    run draupnir_in "$clone" push
    assert_success
    assert_equal "$(sha_on_github feature-a)" "$sha"
}

@test "push sends a rebased branch again, because a feature branch may be forced" {
    run draupnir new feature-a
    assert_success
    local clone="$ROOT/feature-a"
    commit_fixture "$clone" edits/note "Add a note" > /dev/null
    run draupnir push feature-a
    assert_success

    push_from_other_clone main edits/other-note > /dev/null
    run draupnir rebase feature-a
    assert_success

    run draupnir push feature-a
    assert_success
    assert_equal "$(sha_on_github feature-a)" "$(git -C "$clone" rev-parse HEAD)"
}

@test "an action refuses a clone on a detached HEAD" {
    run draupnir new feature-a
    assert_success
    git -C "$ROOT/feature-a" checkout -q --detach HEAD

    run draupnir push feature-a
    assert_failure 1
    assert_output --partial "detached HEAD"
}
