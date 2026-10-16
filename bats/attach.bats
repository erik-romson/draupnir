#!/usr/bin/env bats
#
# draupnir attach: the line that joins the terminal session of a clone, and the session behind it.

bats_require_minimum_version 1.5.0

load 'helpers/workspace'

setup_file() {
    require_tools
}

setup() {
    setup_workspace
    run draupnir new feature-a
    assert_success
    CLONE="$(cd "$ROOT/feature-a" && pwd -P)"
    # Later steps let new start a session too. The tests here look at the calls of attach only.
    rm -f "$BATS_CMUX_LOG"
}

# How many times the stub cmux got a local-tmux start.
start_count() {
    if [ -f "$BATS_CMUX_LOG" ]; then
        grep -c "^local-tmux	start	" "$BATS_CMUX_LOG" || true
    else
        echo 0
    fi
}

@test "attach starts the session of the clone and prints its line" {
    # Standard error holds the log lines, so the output of the test is standard output only.
    run --separate-stderr draupnir_in "$ROOT/feature-a" attach
    assert_success
    # The line is the product of the command, so this test checks the output.
    assert_output "$STUB_BIN/cmux local-tmux attach shop-feature-a --headless"

    run grep -Fx "local-tmux	start	shop-feature-a	--cwd	$CLONE	--detached	" "$BATS_CMUX_LOG"
    assert_success
}

@test "attach does not start a session that runs" {
    cmux_session shop-feature-a "$CLONE"

    run draupnir_in "$ROOT/feature-a" attach
    assert_success

    assert_equal "$(start_count)" 0
}

@test "attach refuses a folder that is not a clone" {
    run draupnir attach typo
    assert_failure

    assert_equal "$(start_count)" 0
}
