#!/usr/bin/env bats
#
# The pull request flow (R17): push, create or reuse a pull request, follow the build, and merge
# with rebase when the build is green.

load 'helpers/workspace'

setup_file() {
    require_tools
}

setup() {
    setup_workspace
    run draupnir new feature-a
    assert_success
    CLONE="$ROOT/feature-a"
    SHA="$(commit_fixture "$CLONE" edits/note "Add a note")"
}

@test "pr pushes the branch, opens a pull request, and merges it on a green build" {
    gh_state build "$SHA" success

    run draupnir pr feature-a --yes --keep
    assert_success

    run gh_state pull-requests
    assert_success
    assert_output "1 feature-a main MERGED"

    # GitHub replayed the commit onto the main branch, and main/ was updated afterwards.
    assert_file_exist "$MAIN/note.txt"
    assert_equal "$(git -C "$MAIN" log -1 --format=%s)" "Add a note"
    assert_dir_exist "$CLONE"
}

@test "pr removes the clone after the merge when asked" {
    gh_state build "$SHA" success

    run draupnir pr feature-a --yes --remove
    assert_success

    assert_dir_not_exist "$CLONE"
    assert_file_exist "$MAIN/note.txt"
}

@test "pr stops before merging when the build fails" {
    gh_state build "$SHA" failure
    local main_before
    main_before="$(sha_on_github main)"

    run draupnir pr feature-a --yes --keep
    assert_failure 1

    # The pull request is open and the main branch did not move.
    run gh_state pull-requests
    assert_output "1 feature-a main OPEN"
    assert_equal "$(sha_on_github main)" "$main_before"
}

@test "pr reuses a pull request that is already open" {
    gh_state build "$SHA" failure
    run draupnir pr feature-a --yes --keep
    assert_failure 1

    gh_state build "$SHA" success
    run draupnir pr feature-a --yes --keep
    assert_success

    # The second run merged the pull request the first run created, instead of opening another.
    run gh_state pull-requests
    assert_output "1 feature-a main MERGED"
}

@test "pr refuses main/, because it needs a feature branch" {
    run draupnir pr main --yes
    assert_failure 1
    assert_output --partial "default branch"
}

@test "pr asks first, and stops when it cannot ask" {
    run draupnir pr feature-a < /dev/null
    assert_failure 1
    assert_output --partial "--yes"

    run gh_state pull-requests
    assert_output ""
    refute_branch_on_github feature-a
}
