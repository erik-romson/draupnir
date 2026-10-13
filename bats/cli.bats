#!/usr/bin/env bats
#
# The surface of the command line: the version, the help, and what happens on a wrong call.

load 'helpers/workspace'

setup_file() {
    require_tools
}

setup() {
    # Not setup_workspace: these tests need no clone of the project.
    setup_sandbox
}

@test "draupnir --version prints the version" {
    run draupnir --version
    assert_success
    assert_output --regexp '^draupnir [0-9]+\.[0-9]+\.[0-9]+'
}

@test "draupnir --help lists the commands" {
    run draupnir --help
    assert_success
    assert_output --partial "init"
    assert_output --partial "new"
    assert_output --partial "status"
}

@test "draupnir rejects an unknown option" {
    run draupnir --nothing-like-this
    assert_failure 2
}

@test "draupnir rejects an unknown command" {
    run draupnir nothing-like-this
    assert_failure 2
}

@test "draupnir rejects a command with a missing argument" {
    run draupnir new
    assert_failure 2
}

@test "draupnir stops with a message when there is no workspace" {
    # The folders above the sandbox may well hold a workspace of their own, so this starts at
    # the root of the file system, which never has one.
    run draupnir_in / status
    assert_failure 1
    assert_output --partial "No workspace found"
}

@test "draupnir stops with a message when the workspace has no main/" {
    printf '[workspace]\n' > "$ROOT/draupnir.toml"

    run draupnir status
    assert_failure 1
    assert_output --partial "draupnir init"
}

@test "draupnir stops with a message when -C names a folder that is not there" {
    run draupnir_in "$TEST_DIR/nothing-like-this" status
    assert_failure 1
    assert_output --partial "-C"
}

@test "draupnir stops with a message on an unknown key in draupnir.toml" {
    setup_workspace
    set_config workspace 'nothing-like-this = "x"'

    run draupnir status
    assert_failure 1
    assert_output --partial "nothing-like-this"
}
