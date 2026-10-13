#!/usr/bin/env bats
#
# The local files of a clone (R6): the files that are not in git, but that every clone needs.
# draupnir copies them from main/ into a new clone, never overwrites one, and adds them to the
# clone's own .git/info/exclude, so git does not show them and they cannot be committed.

load 'helpers/workspace'

setup_file() {
    require_tools
}

setup() {
    setup_workspace
    seed_local_files
}

# Puts the files that no clone gets from git into main/, as a user would.
seed_local_files() {
    cp -R "$FIXTURES/local-files/." "$MAIN/"
    cp "$FIXTURES/git-hooks/commit-msg" "$MAIN/.git/hooks/commit-msg"
    chmod +x "$MAIN/.git/hooks/commit-msg"
}

@test "a new clone gets the local files of main/, and git ignores them" {
    set_local_files "**/local.properties" "runConfigurations"

    run draupnir new feature-a
    assert_success

    local clone="$ROOT/feature-a"
    assert_same_content "$clone/local.properties" "$MAIN/local.properties"
    assert_same_content "$clone/app/local.properties" "$MAIN/app/local.properties"
    assert_same_content "$clone/runConfigurations/Shop.xml" "$MAIN/runConfigurations/Shop.xml"

    assert_excluded "$clone" "/local.properties"
    assert_excluded "$clone" "/app/local.properties"
    # A pattern that names a folder copies and excludes each file in it, one line per file.
    assert_excluded "$clone" "/runConfigurations/Shop.xml"
    assert_excluded "$clone" "/runConfigurations/Tests.xml"
    assert_git_clean "$clone"
}

@test "a file that no pattern names stays in main/ and is not copied" {
    set_local_files "**/local.properties"

    run draupnir new feature-a
    assert_success

    assert_file_not_exist "$ROOT/feature-a/notes.txt"
    refute_excluded "$ROOT/feature-a" "/notes.txt"
}

@test "prepare keeps a local file that the clone already has" {
    set_local_files "local.properties"
    run draupnir new feature-a
    assert_success

    local clone="$ROOT/feature-a"
    cp "$FIXTURES/changed/local.properties" "$clone/local.properties"

    run draupnir prepare feature-a
    assert_success
    assert_same_content "$clone/local.properties" "$FIXTURES/changed/local.properties"
}

@test "prepare adds every exclude line only once" {
    set_local_files "local.properties"
    run draupnir new feature-a
    assert_success

    run draupnir prepare feature-a
    assert_success

    local clone="$ROOT/feature-a"
    assert_equal "$(exclude_line_count "$clone" "/local.properties")" 1
    assert_equal "$(exclude_line_count "$clone" "/.mvn/maven.config")" 1
}

@test "main/ only hides its own local files from git, and nothing is copied into it" {
    set_local_files "local.properties" "runConfigurations"

    run draupnir prepare main
    assert_success

    assert_excluded "$MAIN" "/local.properties"
    assert_excluded "$MAIN" "/runConfigurations/Shop.xml"
    # main/ is the folder every clone copies from, so prepare never copies into it.
    assert_file_not_exist "$MAIN/main"

    # The named files are hidden from git. A file that no pattern names is still untracked.
    run git -C "$MAIN" status --porcelain --untracked-files=all
    assert_success
    refute_output --partial "local.properties"
    refute_output --partial "runConfigurations"
    assert_output --partial "?? notes.txt"
}

@test "a pattern that git tracks in main/ is skipped" {
    set_local_files "pom.xml"

    run draupnir new feature-a
    assert_success

    refute_excluded "$ROOT/feature-a" "/pom.xml"
    assert_git_clean "$ROOT/feature-a"
}

@test "a pattern that starts with '!' takes a file back" {
    set_local_files "*.properties" "!public.properties"

    run draupnir new feature-a
    assert_success

    local clone="$ROOT/feature-a"
    assert_file_exist "$clone/local.properties"
    assert_file_not_exist "$clone/public.properties"
    refute_excluded "$clone" "/public.properties"
}

@test "a hook under .git/ is copied, and it is not added to the exclude file" {
    set_local_files ".git/hooks/commit-msg"

    run draupnir new feature-a
    assert_success

    local clone="$ROOT/feature-a"
    assert_file_exist "$clone/.git/hooks/commit-msg"
    assert_file_executable "$clone/.git/hooks/commit-msg"
    # A .gitignore-style file does not apply under .git/, so the path is never excluded.
    refute_excluded "$clone" "/.git/hooks/commit-msg"
}

@test "the ignore lines of main/ are added to a new clone, and the file is not copied" {
    set_local_files ".git/info/exclude"
    printf '/scratch.txt\n' >> "$MAIN/.git/info/exclude"

    run draupnir new feature-a
    assert_success

    local clone="$ROOT/feature-a"
    assert_excluded "$clone" "/scratch.txt"
    # The clone's own Maven lines are still there, so the file was added to, not overwritten.
    assert_excluded "$clone" "/.mvn/maven.config"
}
