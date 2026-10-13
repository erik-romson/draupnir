#!/usr/bin/env bats
#
# Setting up a workspace with draupnir init (R1, R2, R3).

load 'helpers/workspace'

setup_file() {
    require_tools
}

setup() {
    # Not setup_workspace: these tests run init themselves.
    setup_sandbox
}

@test "init writes the workspace files and clones the project into main/" {
    run draupnir init "$REMOTE"
    assert_success

    assert_file_exist "$ROOT/.gitignore"
    assert_file_exist "$ROOT/draupnir.toml"
    assert_dir_exist "$MAIN/.git"
    assert_file_exist "$MAIN/pom.xml"

    assert_equal "$(git -C "$MAIN" symbolic-ref --short HEAD)" "main"
    assert_equal "$(git -C "$MAIN" remote get-url origin)" "$REMOTE"
}

@test "init writes the repository URL and the main branch into draupnir.toml" {
    run draupnir init "$REMOTE"
    assert_success

    run cat "$ROOT/draupnir.toml"
    assert_output --partial "repository = \"$REMOTE\""
    assert_output --partial 'main-branch = "main"'
}

@test "init keeps main/ and the clones out of the workspace repository" {
    run draupnir init "$REMOTE"
    assert_success

    run draupnir new feature-a
    assert_success

    # The workspace repository only ever tracks its own files (R1), so git init plus add here
    # would stage nothing from main/ or the clone.
    git -C "$ROOT" init -q
    git -C "$ROOT" add -A
    run git -C "$ROOT" diff --cached --name-only
    assert_success
    assert_output --partial ".gitignore"
    assert_output --partial "draupnir.toml"
    refute_output --partial "main/"
    refute_output --partial "feature-a"
}

@test "a cloned workspace repository is set up by one init without a URL" {
    run draupnir init "$REMOTE"
    assert_success

    # The workspace repository is pushed to GitHub and cloned on another machine (R2). Only
    # .gitignore and draupnir.toml travel with it, because its .gitignore holds back the rest.
    git -C "$ROOT" init -q
    git -C "$ROOT" add -A
    git -C "$ROOT" commit -q -m "Add the workspace"
    local second="$TEST_DIR/shop-on-another-machine"
    git clone -q "$ROOT" "$second"
    assert_file_exist "$second/draupnir.toml"
    assert_dir_not_exist "$second/main"

    # The URL in draupnir.toml is all init needs to clone the project.
    run draupnir_in "$second" init
    assert_success

    assert_file_exist "$second/main/pom.xml"
    assert_equal "$(git -C "$second/main" symbolic-ref --short HEAD)" "main"
}

@test "init adopts an existing main/ without cloning it again" {
    git clone -q "$REMOTE" "$MAIN"
    printf 'work in progress\n' > "$MAIN/scratch.txt"

    run draupnir init
    assert_success

    # The clone that was already there is kept, with the file that is only in it.
    assert_file_exist "$MAIN/scratch.txt"
    run cat "$ROOT/draupnir.toml"
    assert_output --partial "repository = \"$REMOTE\""
}

@test "init takes the main branch from --main-branch" {
    run draupnir init "$REMOTE" --main-branch develop
    assert_success

    assert_equal "$(git -C "$MAIN" symbolic-ref --short HEAD)" "develop"
    run cat "$ROOT/draupnir.toml"
    assert_output --partial 'main-branch = "develop"'
}

@test "init can run again on a workspace it already set up" {
    run draupnir init "$REMOTE"
    assert_success

    run draupnir init
    assert_success
    assert_equal "$(git -C "$MAIN" symbolic-ref --short HEAD)" "main"
}

@test "init refuses a main branch that GitHub does not have" {
    run draupnir init "$REMOTE" --main-branch nothing-like-this
    assert_failure 1
    assert_output --partial "nothing-like-this"
}

@test "init refuses when main/ is missing and no URL is given" {
    run draupnir init
    assert_failure 1
    assert_output --partial "draupnir init"
}

@test "init refuses a URL that does not match an existing main/" {
    git clone -q "$REMOTE" "$MAIN"
    git -C "$TEST_DIR" clone -q --bare "$SOURCE" "$TEST_DIR/other.git"

    run draupnir init "$TEST_DIR/other.git"
    assert_failure 1
}

@test "init refuses to run inside a clone of the project" {
    run draupnir_in "$SOURCE" init "$REMOTE"
    assert_failure 1

    # Nothing was written, so the project clone is untouched.
    assert_file_not_exist "$SOURCE/draupnir.toml"
    assert_file_not_exist "$SOURCE/.gitignore"
}
