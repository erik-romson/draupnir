#!/usr/bin/env bats
#
# The private Maven repository of each clone (R8, R9, R10, R11).

load 'helpers/workspace'

setup_file() {
    require_tools
}

setup() {
    setup_workspace
}

@test "a new clone gets its own private Maven repository, written as absolute paths" {
    run draupnir new feature-a
    assert_success

    local clone="$ROOT/feature-a"
    assert_dir_exist "$clone/.m2repo"

    run cat "$clone/.mvn/maven.config"
    assert_success
    assert_line "-Dmaven.repo.local=$clone/.m2repo"
    assert_line "-Dmaven.repo.local.tail=$HOME/.m2/repository"

    assert_excluded "$clone" "/.mvn/maven.config"
    assert_excluded "$clone" "/.m2repo/"
    assert_git_clean "$clone"
}

@test "main/ gets its own private Maven repository too" {
    # Nothing from the project is installed into the shared repository (R11).
    assert_dir_exist "$MAIN/.m2repo"

    run cat "$MAIN/.mvn/maven.config"
    assert_success
    assert_line "-Dmaven.repo.local=$MAIN/.m2repo"

    assert_excluded "$MAIN" "/.mvn/maven.config"
    assert_git_clean "$MAIN"
}

@test "the shared repository can be set in draupnir.toml" {
    set_config maven "shared-repository = \"$TEST_DIR/shared-m2\""

    run draupnir new feature-a
    assert_success

    run cat "$ROOT/feature-a/.mvn/maven.config"
    assert_success
    assert_line "-Dmaven.repo.local.tail=$TEST_DIR/shared-m2"
}

@test "Maven can be turned off in draupnir.toml" {
    set_config maven "enabled = false"

    run draupnir new feature-a
    assert_success

    local clone="$ROOT/feature-a"
    assert_file_not_exist "$clone/.mvn/maven.config"
    assert_dir_not_exist "$clone/.m2repo"
    refute_excluded "$clone" "/.mvn/maven.config"
}

@test "prepare stops and changes nothing when git tracks .mvn/maven.config" {
    set_local_files "local.properties"
    printf 'port=8080\n' > "$MAIN/local.properties"

    run draupnir new feature-a
    assert_success

    # A project that keeps its Maven settings in git must not have them rewritten (R10).
    local clone="$ROOT/feature-a"
    mkdir -p "$clone/.mvn"
    cp "$FIXTURES/maven/maven.config" "$clone/.mvn/maven.config"
    git -C "$clone" add -f .mvn/maven.config
    git -C "$clone" commit -q -m "Keep the Maven settings in git"
    rm "$clone/local.properties"

    run draupnir prepare feature-a
    assert_failure 1
    assert_output --partial ".mvn/maven.config"

    # prepare checks everything before it changes anything, so the local file was not copied
    # and the tracked Maven settings are as they were.
    assert_file_not_exist "$clone/local.properties"
    assert_same_content "$clone/.mvn/maven.config" "$FIXTURES/maven/maven.config"
}

@test "prepare stops when Maven is too old to keep installs private" {
    cp "$FIXTURES/stubs/mvn-too-old" "$STUB_BIN/mvn"

    run draupnir prepare main
    assert_failure 1
    assert_output --partial "3.9.0"
}

@test "prepare stops when Maven is installed but cannot run" {
    cp "$FIXTURES/stubs/mvn-broken" "$STUB_BIN/mvn"

    run draupnir prepare main
    assert_failure 1
    assert_output --partial "Maven version"
}
