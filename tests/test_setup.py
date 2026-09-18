from __future__ import annotations

import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import Env
from helpers import (
    FakeStdin,
    commit,
    logger,
    push_from_other_clone,
    rebase_merge_on_github,
    sh,
    write_executable,
)

from draupnir.cli import main
from draupnir.errors import OperationError
from draupnir.runner import NETWORK_HINT
from draupnir.setup import (
    folder_for_branch,
    init,
    new_clone,
    prepare,
    remove_clone,
    unsaved_work,
)


def _configure_local_files(root: Path, paths: list[str]) -> None:
    quoted = ", ".join(f'"{path}"' for path in paths)
    line = f"local-files = [{quoted}]"
    path = root / "draupnir.toml"
    lines = path.read_text().splitlines()
    for i, existing_line in enumerate(lines):
        if existing_line.strip() == "[workspace]":
            lines.insert(i + 1, line)
            path.write_text("\n".join(lines) + "\n")
            return
    lines.extend(["", "[workspace]", line])
    path.write_text("\n".join(lines) + "\n")


def _clone(env: Env, name: str) -> Path:
    folder = env.root / name
    sh(env.root, "git", "clone", "-q", str(env.remote), str(folder))
    return folder


def _exclude_lines(folder: Path) -> list[str]:
    exclude_path = folder / ".git" / "info" / "exclude"
    if not exclude_path.is_file():
        return []
    return exclude_path.read_text().splitlines()


def test_prepare_copies_missing_local_files_and_keeps_existing_ones(env: Env) -> None:
    (env.main / "missing.properties").write_text("from main\n")
    (env.main / "present.properties").write_text("from main too\n")
    _configure_local_files(env.root, ["missing.properties", "present.properties"])
    feature = _clone(env, "feature-a")
    (feature / "present.properties").write_text("already here\n")

    _, log = logger()
    prepare(env.ws(), feature, log)

    assert (feature / "missing.properties").read_text() == "from main\n"
    assert (feature / "present.properties").read_text() == "already here\n"


def test_prepare_copies_a_folder_with_its_files(env: Env) -> None:
    run_configs = env.main / "runConfigurations"
    run_configs.mkdir()
    (run_configs / "app.xml").write_text("<configuration/>\n")
    (run_configs / "sub").mkdir()
    (run_configs / "sub" / "nested.xml").write_text("<nested/>\n")
    _configure_local_files(env.root, ["runConfigurations"])
    feature = _clone(env, "feature-b")

    _, log = logger()
    prepare(env.ws(), feature, log)

    assert (feature / "runConfigurations" / "app.xml").read_text() == "<configuration/>\n"
    assert (feature / "runConfigurations" / "sub" / "nested.xml").read_text() == "<nested/>\n"


def test_prepare_skips_files_that_git_tracks(env: Env) -> None:
    _configure_local_files(env.root, ["pom.xml"])
    feature = _clone(env, "feature-c")

    lines, log = logger()
    prepare(env.ws(), feature, log)

    assert "/pom.xml" not in _exclude_lines(feature)
    assert any("pom.xml" in line and "tracks" in line for line in lines)


def test_prepare_adds_local_files_and_maven_files_to_exclude(env: Env) -> None:
    (env.main / "local.properties").write_text("value\n")
    _configure_local_files(env.root, ["local.properties"])
    feature = _clone(env, "feature-d")

    _, log = logger()
    prepare(env.ws(), feature, log)

    exclude = _exclude_lines(feature)
    assert "/local.properties" in exclude
    assert "/.mvn/maven.config" in exclude
    assert "/.m2repo/" in exclude


def test_prepare_hides_but_does_not_copy_into_main(env: Env) -> None:
    (env.main / "local.properties").write_text("value\n")
    _configure_local_files(env.root, ["local.properties"])

    lines, log = logger()
    prepare(env.ws(), env.main, log)

    assert "/local.properties" in _exclude_lines(env.main)
    assert (env.main / "local.properties").read_text() == "value\n"
    assert not any("Copied" in line for line in lines)


def test_prepare_copies_a_git_hook_without_excluding_it(env: Env) -> None:
    hook = env.main / ".git" / "hooks" / "commit-msg"
    write_executable(hook, "#!/bin/sh\nexit 0\n")
    _configure_local_files(env.root, [".git/hooks/commit-msg"])
    feature = _clone(env, "feature-e")

    _, log = logger()
    prepare(env.ws(), feature, log)

    copied = feature / ".git" / "hooks" / "commit-msg"
    assert copied.is_file()
    assert copied.stat().st_mode & stat.S_IEXEC
    assert not any(".git/hooks/commit-msg" in line for line in _exclude_lines(feature))


def test_prepare_can_run_twice_without_changes(env: Env) -> None:
    (env.main / "local.properties").write_text("value\n")
    _configure_local_files(env.root, ["local.properties"])
    feature = _clone(env, "feature-f")

    _1, log1 = logger()
    prepare(env.ws(), feature, log1)
    exclude_after_first = _exclude_lines(feature)
    config_mtime = (feature / ".mvn" / "maven.config").stat().st_mtime_ns

    _2, log2 = logger()
    prepare(env.ws(), feature, log2)

    assert _exclude_lines(feature) == exclude_after_first
    assert (feature / ".mvn" / "maven.config").stat().st_mtime_ns == config_mtime


def test_prepare_refuses_the_workspace_root(env: Env) -> None:
    lines, log = logger()

    with pytest.raises(OperationError) as exc_info:
        prepare(env.ws(), env.root, log)

    assert str(env.root) in str(exc_info.value)
    assert lines == []


def test_prepare_gives_main_its_own_m2repo(env: Env) -> None:
    _, log = logger()

    prepare(env.ws(), env.main, log)

    assert (env.main / ".m2repo").is_dir()
    config = (env.main / ".mvn" / "maven.config").read_text()
    assert str(env.main / ".m2repo") in config


def test_prepare_refuses_when_maven_config_is_tracked_and_changes_nothing(env: Env) -> None:
    (env.main / "local.properties").write_text("value\n")
    _configure_local_files(env.root, ["local.properties"])
    feature = _clone(env, "feature-g")
    tracked_content = "-Dmaven.repo.local=/somewhere\n"
    (feature / ".mvn").mkdir()
    (feature / ".mvn" / "maven.config").write_text(tracked_content)
    sh(feature, "git", "add", ".mvn/maven.config")
    sh(feature, "git", "commit", "-q", "-m", "track maven config")
    exclude_before = _exclude_lines(feature)

    _, log = logger()

    with pytest.raises(OperationError):
        prepare(env.ws(), feature, log)

    assert (feature / ".mvn" / "maven.config").read_text() == tracked_content
    assert not (feature / ".m2repo").exists()
    assert not (feature / "local.properties").exists()
    assert _exclude_lines(feature) == exclude_before


def test_prepare_refuses_an_old_maven_and_changes_nothing(env: Env) -> None:
    write_executable(env.bin / "mvn", '#!/bin/sh\necho "Apache Maven 3.8.1 (old)"\n')
    (env.main / "local.properties").write_text("value\n")
    _configure_local_files(env.root, ["local.properties"])
    feature = _clone(env, "feature-h")
    exclude_before = _exclude_lines(feature)

    _, log = logger()

    with pytest.raises(OperationError) as exc_info:
        prepare(env.ws(), feature, log)

    assert "3.8.1" in str(exc_info.value)
    assert not (feature / "local.properties").exists()
    assert not (feature / ".mvn").exists()
    assert not (feature / ".m2repo").exists()
    assert _exclude_lines(feature) == exclude_before


def test_prepare_skips_maven_when_main_has_no_pom(env: Env) -> None:
    (env.main / "pom.xml").unlink()
    feature = _clone(env, "feature-i")

    _, log = logger()
    prepare(env.ws(), feature, log)

    assert not (feature / ".mvn").exists()
    assert not (feature / ".m2repo").exists()


def test_prepare_sets_up_maven_in_a_clone_whose_branch_removed_the_pom(env: Env) -> None:
    feature = _clone(env, "feature-j")
    (feature / "pom.xml").unlink()
    sh(feature, "git", "add", "-A")
    sh(feature, "git", "commit", "-q", "-m", "remove pom")

    _, log = logger()
    prepare(env.ws(), feature, log)

    assert (feature / ".mvn" / "maven.config").is_file()
    assert (feature / ".m2repo").is_dir()


def test_prepare_skips_maven_in_a_clone_whose_branch_added_a_pom(env: Env) -> None:
    (env.main / "pom.xml").unlink()
    feature = _clone(env, "feature-k")
    assert (feature / "pom.xml").is_file()

    _, log = logger()
    prepare(env.ws(), feature, log)

    assert not (feature / ".mvn").exists()
    assert not (feature / ".m2repo").exists()


def test_init_clones_main_and_writes_the_workspace_files(env: Env) -> None:
    root = env.tmp / "fresh-workspace"
    root.mkdir()
    _, log = logger()

    ws = init(root, str(env.remote), log)

    assert ws.root == root
    assert ws.main == root / "main"
    assert (root / "main" / ".git").is_dir()
    assert (root / "main" / "pom.xml").is_file()
    gitignore = (root / ".gitignore").read_text()
    assert "/*" in gitignore
    assert "!/draupnir.toml" in gitignore
    assert (root / "draupnir.toml").is_file()


def test_init_writes_the_repository_url_into_the_config(env: Env) -> None:
    root = env.tmp / "fresh-workspace-config"
    root.mkdir()
    _, log = logger()

    init(root, str(env.remote), log)

    content = (root / "draupnir.toml").read_text()
    assert f'repository = "{env.remote}"' in content


def test_init_in_a_cloned_workspace_uses_the_url_from_the_config(env: Env) -> None:
    cloned = env.tmp / "cloned-workspace"
    cloned.mkdir()
    (cloned / "draupnir.toml").write_text(f'[workspace]\nrepository = "{env.remote}"\n')
    sh(cloned, "git", "init", "-q")
    sh(cloned, "git", "add", "draupnir.toml")
    sh(cloned, "git", "commit", "-q", "-m", "workspace config")
    _, log = logger()

    ws = init(cloned, None, log)

    assert ws.main == cloned / "main"
    assert (cloned / "main" / ".git").is_dir()


def test_init_adopts_an_existing_main_folder(env: Env) -> None:
    root = env.tmp / "adopt-workspace"
    root.mkdir()
    main = root / "main"
    sh(root, "git", "clone", "-q", str(env.remote), str(main))
    before_sha = sh(main, "git", "rev-parse", "HEAD")
    _, log = logger()

    ws = init(root, None, log)

    assert ws.main == main
    assert sh(main, "git", "rev-parse", "HEAD") == before_sha
    assert f'repository = "{env.remote}"' in (root / "draupnir.toml").read_text()


def test_init_refuses_a_url_that_differs_from_main(env: Env) -> None:
    root = env.tmp / "mismatch-workspace"
    root.mkdir()
    main = root / "main"
    sh(root, "git", "clone", "-q", str(env.remote), str(main))
    other_source = env.tmp / "other-source"
    other_source.mkdir()
    sh(other_source, "git", "init", "-q")
    commit(other_source, "file.txt", "x\n", "initial")
    other_remote = env.tmp / "other.git"
    sh(env.tmp, "git", "clone", "-q", "--bare", str(other_source), str(other_remote))
    _, log = logger()

    with pytest.raises(OperationError) as exc_info:
        init(root, str(other_remote), log)

    assert str(root) in str(exc_info.value)


def test_init_refuses_to_run_inside_a_project_clone(env: Env) -> None:
    _, log = logger()

    with pytest.raises(OperationError) as exc_info:
        init(env.main, None, log)

    assert str(env.main) in str(exc_info.value)


def test_init_sets_origin_head_when_it_is_missing(env: Env) -> None:
    sh(env.main, "git", "symbolic-ref", "-d", "refs/remotes/origin/HEAD")
    _, log = logger()

    init(env.root, None, log)

    assert (
        sh(env.main, "git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD") == "origin/main"
    )


def _push_develop(env: Env) -> None:
    other = env.tmp / "pusher-develop"
    sh(env.tmp, "git", "clone", "-q", str(env.remote), str(other))
    sh(other, "git", "push", "-q", "origin", "main:develop")


def test_init_writes_githubs_default_branch_as_the_main_branch(env: Env) -> None:
    root = env.tmp / "default-branch-workspace"
    root.mkdir()
    _, log = logger()

    ws = init(root, str(env.remote), log)

    assert 'main-branch = "main"' in (root / "draupnir.toml").read_text()
    assert ws.main_branch() == "main"


def test_init_with_a_main_branch_checks_it_out_in_a_new_main(env: Env) -> None:
    _push_develop(env)
    root = env.tmp / "develop-workspace"
    root.mkdir()
    _, log = logger()

    ws = init(root, str(env.remote), log, main_branch="develop")

    content = (root / "draupnir.toml").read_text()
    assert 'main-branch = "develop"' in content
    assert "# main-branch" not in content
    assert ws.main_branch() == "develop"
    assert sh(ws.main, "git", "branch", "--show-current") == "develop"


def test_init_with_a_main_branch_leaves_an_adopted_main_on_its_branch(env: Env) -> None:
    _push_develop(env)
    lines, log = logger()

    ws = init(env.root, None, log, main_branch="develop")

    assert ws.main_branch() == "develop"
    assert 'main-branch = "develop"' in (env.root / "draupnir.toml").read_text()
    assert sh(env.main, "git", "branch", "--show-current") == "main"
    assert any("switch develop" in line for line in lines)


def test_init_refuses_a_main_branch_that_github_does_not_have(env: Env) -> None:
    _, log = logger()

    with pytest.raises(OperationError) as exc_info:
        init(env.root, None, log, main_branch="no-such-branch")

    assert "no-such-branch" in str(exc_info.value)
    assert "no-such-branch" not in (env.root / "draupnir.toml").read_text()


def test_init_can_run_twice(env: Env) -> None:
    _, log = logger()

    ws = init(env.root, str(env.remote), log)

    assert ws.root == env.root
    assert ws.main == env.main
    assert (env.main / ".git").is_dir()


def test_workspace_gitignore_hides_main_and_clones(env: Env) -> None:
    root = env.tmp / "gitignore-workspace"
    root.mkdir()
    _, log = logger()
    init(root, str(env.remote), log)

    sh(root, "git", "init", "-q")
    status = sh(root, "git", "status", "--porcelain")

    names = {line.split(maxsplit=1)[-1] for line in status.splitlines()}
    assert names == {".gitignore", "draupnir.toml"}


def _push_new_branch(env: Env, branch: str, base: str = "main") -> None:
    other = env.tmp / f"pusher-{folder_for_branch(branch)}"
    sh(env.tmp, "git", "clone", "-q", str(env.remote), str(other))
    sh(other, "git", "checkout", "-q", "-b", branch, f"origin/{base}")
    commit(other, "feature.txt", "feature work\n", "feature commit")
    sh(other, "git", "push", "-q", "origin", branch)


def test_new_clone_tracks_a_branch_that_exists_on_github(env: Env) -> None:
    _push_new_branch(env, "feature/tracked")
    _, log = logger()

    folder = new_clone(env.ws(), "feature/tracked", None, log)

    assert folder == env.root / "feature-tracked"
    assert sh(folder, "git", "branch", "--show-current") == "feature/tracked"
    assert sh(folder, "git", "rev-parse", "feature/tracked@{upstream}") == sh(
        folder, "git", "rev-parse", "origin/feature/tracked"
    )
    assert (folder / "feature.txt").is_file()


def test_new_clone_checks_out_a_branch_named_like_a_folder_in_the_project(env: Env) -> None:
    other = env.tmp / "pusher-docs"
    sh(env.tmp, "git", "clone", "-q", str(env.remote), str(other))
    (other / "docs").mkdir()
    commit(other, "docs/readme.txt", "docs\n", "add docs folder")
    sh(other, "git", "push", "-q", "origin", "main")
    sh(other, "git", "push", "-q", "origin", "main:docs")
    sh(env.main, "git", "pull", "-q")
    _, log = logger()

    folder = new_clone(env.ws(), "docs", None, log)

    assert sh(folder, "git", "branch", "--show-current") == "docs"
    assert sh(folder, "git", "rev-parse", "docs@{upstream}") == sh(
        folder, "git", "rev-parse", "origin/docs"
    )


def test_new_clone_removes_the_folder_when_the_checkout_fails(env: Env) -> None:
    real_git = shutil.which("git")
    write_executable(
        env.bin / "git",
        f'#!/bin/sh\n[ "$1" = checkout ] && exit 1\nexec {real_git} "$@"\n',
    )
    _, log = logger()

    with pytest.raises(OperationError):
        new_clone(env.ws(), "feature/broken-checkout", None, log)

    assert not (env.root / "feature-broken-checkout").exists()


def test_new_clone_creates_a_branch_from_the_base(env: Env) -> None:
    _, log = logger()

    folder = new_clone(env.ws(), "feature/from-base", None, log)

    assert sh(folder, "git", "branch", "--show-current") == "feature/from-base"
    assert sh(folder, "git", "rev-parse", "HEAD") == sh(env.main, "git", "rev-parse", "origin/main")
    with pytest.raises(subprocess.CalledProcessError):
        sh(folder, "git", "rev-parse", "feature/from-base@{upstream}")


def test_new_clone_refuses_a_base_that_is_not_on_github(env: Env) -> None:
    _, log = logger()

    with pytest.raises(OperationError) as exc_info:
        new_clone(env.ws(), "feature/x", "no-such-base", log)

    assert "no-such-base" in str(exc_info.value)


def test_new_clone_points_origin_at_github_not_at_main(env: Env) -> None:
    _, log = logger()

    folder = new_clone(env.ws(), "feature/origin-check", None, log)

    assert sh(folder, "git", "remote", "get-url", "origin") == str(env.remote)


def test_new_clone_does_not_get_local_only_branches_from_main(env: Env) -> None:
    sh(env.main, "git", "branch", "local-only-branch")
    _, log = logger()

    folder = new_clone(env.ws(), "feature/no-local-only", None, log)

    refs = sh(folder, "git", "for-each-ref", "--format=%(refname)")
    assert "local-only-branch" not in refs


def test_new_clone_has_only_its_own_local_branch(env: Env) -> None:
    _, log = logger()

    folder = new_clone(env.ws(), "feature/only-branch", None, log)

    branches = sh(folder, "git", "branch", "--format=%(refname:short)").splitlines()
    assert branches == ["feature/only-branch"]


def test_new_clone_works_after_main_is_moved_away(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/independent", None, log)

    shutil.move(str(env.main), str(env.tmp / "main-moved-away"))

    sh(folder, "git", "log", "--oneline", "-1")
    sh(folder, "git", "fsck")


def test_new_clone_refuses_an_existing_folder(env: Env) -> None:
    (env.root / "feature-taken").mkdir()
    _, log = logger()

    with pytest.raises(OperationError) as exc_info:
        new_clone(env.ws(), "feature/taken", None, log)

    assert "feature-taken" in str(exc_info.value)


def test_new_clone_refuses_a_bad_branch_name(env: Env) -> None:
    _, log = logger()

    with pytest.raises(OperationError) as exc_info:
        new_clone(env.ws(), "bad..branch", None, log)

    assert "bad..branch" in str(exc_info.value)


def test_new_clone_folder_name_replaces_slashes(env: Env) -> None:
    _, log = logger()

    folder = new_clone(env.ws(), "feature/vessel-owner", None, log)

    assert folder == env.root / "feature-vessel-owner"
    assert folder.is_dir()


def test_new_clone_keeps_the_folder_and_explains_when_prepare_fails(env: Env) -> None:
    write_executable(env.bin / "mvn", '#!/bin/sh\necho "Apache Maven 3.8.1 (old)"\n')
    _, log = logger()

    with pytest.raises(OperationError) as exc_info:
        new_clone(env.ws(), "feature/broken-setup", None, log)

    message = str(exc_info.value)
    assert "was created" in message
    assert "draupnir prepare" in message
    folder = env.root / "feature-broken-setup"
    assert folder.is_dir()
    assert (folder / ".git").is_dir()


def _refused_removal(env: Env, folder: Path) -> str:
    """Calls remove_clone without force, checks that it refused, and returns the message."""
    _, log = logger()
    with pytest.raises(OperationError) as exc_info:
        remove_clone(env.ws(), folder, False, log)
    assert folder.is_dir()
    return str(exc_info.value)


def test_remove_deletes_a_clean_clone_with_its_m2repo(env: Env) -> None:
    lines, log = logger()
    folder = new_clone(env.ws(), "feature/clean", None, log)
    artifact = folder / ".m2repo" / "com" / "example" / "shop" / "shop-1.0.jar"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("built\n")

    assert unsaved_work(env.ws(), folder, log) == []
    remove_clone(env.ws(), folder, False, log)

    assert not folder.exists()
    assert (env.main / ".git").is_dir()
    assert any(line.startswith(f"Deleting {folder}.") for line in lines)


def test_remove_refuses_uncommitted_changes(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/uncommitted", None, log)

    (folder / "App.java").write_text("changed but not committed\n")
    message = _refused_removal(env, folder)
    assert "not committed" in message

    sh(folder, "git", "checkout", "--", "App.java")
    (folder / "notes.txt").write_text("a file that git does not track\n")
    message = _refused_removal(env, folder)
    assert "1 file has changes that are not committed" in message


def test_remove_refuses_when_a_stash_exists(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/stashed", None, log)
    (folder / "App.java").write_text("stashed change\n")
    sh(folder, "git", "stash", "-q")
    assert sh(folder, "git", "status", "--porcelain") == ""

    message = _refused_removal(env, folder)

    assert "1 stash" in message


def test_remove_refuses_unpushed_commits_on_any_branch(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/checked-out", None, log)
    sh(folder, "git", "checkout", "-q", "-b", "side-work")
    commit(folder, "side.txt", "side work\n", "work on a branch that is not checked out")
    sh(folder, "git", "checkout", "-q", "feature/checked-out")

    message = _refused_removal(env, folder)

    assert "Branch side-work has 1 commit that is not on GitHub" in message
    assert "Branch feature/checked-out" not in message


def test_remove_accepts_commits_that_were_rebase_merged_and_whose_branch_was_deleted(
    env: Env,
) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/rebase-merged", None, log)
    commit(folder, "first.txt", "first\n", "first feature commit")
    commit(folder, "second.txt", "second\n", "second feature commit")
    sh(folder, "git", "push", "-q", "-u", "origin", "feature/rebase-merged")
    push_from_other_clone(env, "main")

    new_commits = rebase_merge_on_github(env, "feature/rebase-merged")

    assert len(new_commits) == 2
    assert sh(env.remote, "git", "branch", "--list", "feature/rebase-merged") == ""
    assert unsaved_work(env.ws(), folder, log) == []
    # The check of the prototype only looked for commits that are on no remote branch. It
    # would find both commits here, because GitHub gave them new shas.
    assert sh(folder, "git", "rev-list", "--count", "--branches", "--not", "--remotes") == "2"

    remove_clone(env.ws(), folder, False, log)

    assert not folder.exists()


def test_remove_refuses_an_unpushed_merge_commit_whose_parents_are_on_github(env: Env) -> None:
    _, log = logger()
    branch = "feature/merged-main"
    folder = new_clone(env.ws(), branch, None, log)
    commit(folder, "App.java", "line 1\nfeature change\nline 3\n", "change line 2 on the branch")
    sh(folder, "git", "push", "-q", "-u", "origin", branch)

    teammate = env.tmp / "teammate"
    sh(env.tmp, "git", "clone", "-q", str(env.remote), str(teammate))
    commit(teammate, "App.java", "line 1\nmain change\nline 3\n", "change line 2 on main")
    sh(teammate, "git", "push", "-q", "origin", "main")

    sh(folder, "git", "fetch", "-q", "origin")
    merge = subprocess.run(
        ["git", "merge", "--no-edit", "origin/main"], cwd=folder, capture_output=True, text=True
    )
    assert merge.returncode != 0
    (folder / "App.java").write_text("line 1\nfeature change and main change\nline 3\n")
    sh(folder, "git", "add", "App.java")
    sh(folder, "git", "commit", "-q", "--no-edit")

    parents = sh(folder, "git", "rev-list", "--parents", "-n", "1", "HEAD").split()[1:]
    assert parents == [
        sh(env.remote, "git", "rev-parse", f"refs/heads/{branch}"),
        sh(env.remote, "git", "rev-parse", "refs/heads/main"),
    ]

    message = _refused_removal(env, folder)

    assert f"Branch {branch} has 1 commit that is not on GitHub, and it is a merge commit." in (
        message
    )
    assert "Push the branch first, or use --force when the merge is no longer needed." in message


def test_remove_refuses_when_the_fetch_fails(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/offline", None, log)
    env.remote.rename(env.tmp / "project-moved-away.git")

    message = _refused_removal(env, folder)

    first_line = message.splitlines()[0]
    assert "could not fetch" in first_line
    assert str(folder) in first_line
    assert message.endswith(NETWORK_HINT)


def test_remove_message_lists_every_reason_and_the_next_step(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/many-reasons", None, log)
    commit(folder, "unpushed.txt", "not pushed\n", "commit that is not pushed")
    (folder / "App.java").write_text("stashed change\n")
    sh(folder, "git", "stash", "-q")
    (folder / "notes.txt").write_text("not committed\n")

    reasons = unsaved_work(env.ws(), folder, log)
    message = _refused_removal(env, folder)

    assert len(reasons) == 3
    for reason in reasons:
        assert f"- {reason}" in message
    assert str(folder) in message.splitlines()[0]
    assert "Branch feature/many-reasons" in message
    assert "stash" in message
    assert "not committed" in message
    assert "Commit and push" in message
    assert "--force" in message


def test_remove_with_force_deletes_anyway(env: Env) -> None:
    _, clone_log = logger()
    folder = new_clone(env.ws(), "feature/forced", None, clone_log)
    commit(folder, "unpushed.txt", "not pushed\n", "commit that is not pushed")
    (folder / "notes.txt").write_text("not committed\n")
    env.remote.rename(env.tmp / "project-offline.git")

    lines, log = logger()
    remove_clone(env.ws(), folder, True, log)

    assert not folder.exists()
    assert not any(line.startswith("$ git fetch") for line in lines)


def test_remove_refuses_main_and_folders_outside_the_workspace(env: Env) -> None:
    _, log = logger()
    ws = env.ws()
    clone = new_clone(ws, "feature/keep-me", None, log)
    nested = clone / "src"
    nested.mkdir()
    outside = env.tmp / "outside-clone"
    sh(env.tmp, "git", "clone", "-q", str(env.remote), str(outside))
    plain = env.root / "plain-folder"
    plain.mkdir()
    other_project = env.root / "other-project"
    other_project.mkdir()
    sh(other_project, "git", "init", "-q")
    sh(other_project, "git", "remote", "add", "origin", "git@github.example.com:acme/other.git")

    for target in (env.main, env.root, outside, nested, plain, other_project):
        with pytest.raises(OperationError) as exc_info:
            remove_clone(ws, target, True, log)
        assert str(target) in str(exc_info.value).splitlines()[0]
        assert target.is_dir()

    assert (env.main / ".git").is_dir()
    assert (clone / ".git").is_dir()


def test_remove_refuses_commits_on_a_detached_head(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/detached", None, log)
    sh(folder, "git", "checkout", "-q", "--detach")
    commit(folder, "detached.txt", "on no branch\n", "commit on a detached HEAD")

    message = _refused_removal(env, folder)

    assert "The detached HEAD has 1 commit that is not on GitHub" in message


def test_remove_refuses_while_a_rebase_is_in_progress(env: Env) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/rebasing", None, log)
    commit(folder, "work.txt", "work\n", "work to rebase")
    sh(folder, "git", "push", "-q", "-u", "origin", "feature/rebasing")
    rebase = subprocess.run(
        ["git", "rebase", "--force-rebase", "--exec", "false", "origin/main"],
        cwd=folder,
        capture_output=True,
        text=True,
    )
    assert rebase.returncode != 0

    message = _refused_removal(env, folder)

    assert "A rebase is in progress" in message


def test_remove_command_asks_before_deleting(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _, log = logger()
    folder = new_clone(env.ws(), "feature/ask-first", None, log)
    monkeypatch.chdir(env.root)

    monkeypatch.setattr(sys, "stdin", FakeStdin(is_a_tty=False))
    code = main(["remove", folder.name])
    assert code == 1
    assert "--yes" in capsys.readouterr().err
    assert folder.is_dir()

    monkeypatch.setattr(sys, "stdin", FakeStdin(is_a_tty=True))
    questions: list[str] = []

    def answer_no(prompt: str = "") -> str:
        questions.append(prompt)
        return "n"

    monkeypatch.setattr("builtins.input", answer_no)
    code = main(["remove", folder.name])
    assert code == 0
    assert questions == [f"Delete {folder} with its .m2repo and local files? [y/N] "]
    assert "Kept the clone" in capsys.readouterr().out
    assert folder.is_dir()

    monkeypatch.setattr(sys, "stdin", FakeStdin(is_a_tty=False))
    code = main(["remove", folder.name, "--yes"])
    assert code == 0
    assert f"Deleted {folder}." in capsys.readouterr().out
    assert not folder.exists()
