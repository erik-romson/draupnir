#!/usr/bin/env bats
#
# Starting the IDE in a clone (R13). The stub idea on PATH writes the arguments it got into a
# log, because the real IDE is started without waiting for it.

load 'helpers/workspace'

setup_file() {
    require_tools
}

setup() {
    setup_workspace
}

@test "open starts the IDE in a clone" {
    run draupnir new feature-a
    assert_success

    run draupnir open feature-a
    assert_success

    wait_for_file "$BATS_IDEA_LOG"
    run cat "$BATS_IDEA_LOG"
    assert_output "$ROOT/feature-a"
}

@test "new --open starts the IDE in the new clone" {
    run draupnir new feature-a --open
    assert_success

    wait_for_file "$BATS_IDEA_LOG"
    run cat "$BATS_IDEA_LOG"
    assert_output "$ROOT/feature-a"
}

@test "the IDE command can be set in draupnir.toml" {
    cp "$FIXTURES/stubs/idea" "$STUB_BIN/other-ide"
    set_config ide "java = \"$STUB_BIN/other-ide\""

    run draupnir open main
    assert_success

    wait_for_file "$BATS_IDEA_LOG"
    run cat "$BATS_IDEA_LOG"
    assert_output "$MAIN"
}

@test "open stops with a message when the IDE launcher cannot be started" {
    set_config ide "java = \"$STUB_BIN/missing-ide\""

    run draupnir open main
    assert_failure 1
    assert_output --partial "IntelliJ IDEA"
    assert_output --partial "ide.java"
}
