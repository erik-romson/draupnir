#!/usr/bin/env bats
#
# Fast-forwarding the main branch from a clone (R17, R18).

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

@test "ff-main moves the main branch on GitHub and updates main/" {
    run draupnir push feature-a
    assert_success
    gh_state build "$SHA" success

    run draupnir ff-main feature-a --yes
    assert_success

    assert_equal "$(sha_on_github main)" "$SHA"
    assert_equal "$(git -C "$MAIN" rev-parse HEAD)" "$SHA"
    assert_file_exist "$MAIN/note.txt"
}

@test "ff-main refuses when the build of the commit failed" {
    local main_before
    main_before="$(sha_on_github main)"
    run draupnir push feature-a
    assert_success
    gh_state build "$SHA" failure

    run draupnir ff-main feature-a --yes
    assert_failure 1

    assert_equal "$(sha_on_github main)" "$main_before"
}

@test "ff-main refuses when the commit was never pushed" {
    run draupnir ff-main feature-a --yes
    assert_failure 1
    assert_output --partial "not pushed"
}

@test "ff-main refuses a clone that is not on top of the main branch" {
    set_config actions "fast-forward-needs-green-build = false"
    run draupnir push feature-a
    assert_success
    push_from_other_clone main edits/other-note > /dev/null

    run draupnir ff-main feature-a --yes
    assert_failure 1
    assert_output --partial "Rebase"
}

@test "ff-main refuses main/, because it needs a feature branch" {
    run draupnir ff-main main --yes
    assert_failure 1
    assert_output --partial "default branch"
}

@test "ff-main refuses a clone that has nothing to fast-forward" {
    set_config actions "fast-forward-needs-green-build = false"
    git -C "$CLONE" reset -q --hard origin/main

    run draupnir ff-main feature-a --yes
    assert_failure 1
    assert_output --partial "nothing to fast-forward"
}

@test "ff-main can be turned off in draupnir.toml" {
    set_config actions "fast-forward-main = false"

    run draupnir ff-main feature-a --yes
    assert_failure 1
    assert_output --partial "draupnir pr"
}

@test "ff-main asks first, and stops when it cannot ask" {
    set_config actions "fast-forward-needs-green-build = false"
    local main_before
    main_before="$(sha_on_github main)"
    run draupnir push feature-a
    assert_success

    # Without --yes and without a terminal to ask on, the action stops instead of guessing.
    run draupnir ff-main feature-a < /dev/null
    assert_failure 1
    assert_output --partial "--yes"

    assert_equal "$(sha_on_github main)" "$main_before"
}
