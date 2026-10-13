#!/usr/bin/env bats
#
# Following builds through gh (R20, R21). GitHub is the bare repository project.git, and gh is
# the fake gh that the pytest suite uses as well.

load 'helpers/workspace'

# The command ran, but the build is not green.
EXIT_BUILD_NOT_GREEN=3

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

@test "build reports a green build" {
    run draupnir push feature-a
    assert_success
    gh_state build "$SHA" success

    run draupnir build feature-a
    assert_success
    assert_output --partial "success"
}

@test "build reports a failed build" {
    run draupnir push feature-a
    assert_success
    gh_state build "$SHA" failure

    run draupnir build feature-a
    assert_failure "$EXIT_BUILD_NOT_GREEN"
    assert_output --partial "failed"
}

@test "build reports that nothing has reported for the commit" {
    run draupnir push feature-a
    assert_success

    run draupnir build feature-a
    assert_failure "$EXIT_BUILD_NOT_GREEN"
    assert_output --partial "No build has reported"
}

@test "push --follow pushes the branch and waits for a green build" {
    gh_state build "$SHA" success

    run draupnir push feature-a --follow
    assert_success

    assert_equal "$(sha_on_github feature-a)" "$SHA"
    assert_output --partial "success"
}

@test "push --follow ends with the exit code for a build that is not green" {
    gh_state build "$SHA" failure

    run draupnir push feature-a --follow
    assert_failure "$EXIT_BUILD_NOT_GREEN"

    # The push itself worked, so the branch is on GitHub even though the build failed.
    assert_equal "$(sha_on_github feature-a)" "$SHA"
}

@test "push --follow stops waiting after the grace period when nothing reports" {
    # No build is set for the commit, so the wait ends with "no build" (R21).
    run draupnir push feature-a --follow
    assert_failure "$EXIT_BUILD_NOT_GREEN"
    assert_output --partial "No build has reported"
}
