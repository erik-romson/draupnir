from __future__ import annotations

import pytest
from conftest import Env
from helpers import commit, sh

from draupnir.errors import OperationError
from draupnir.workspace import Workspace


def test_find_workspace_from_inside_a_clone(env: Env) -> None:
    nested = env.main / "src" / "deep"
    nested.mkdir(parents=True)

    ws = Workspace.find(nested)

    assert ws.root == env.root
    assert ws.main == env.main


def test_find_workspace_fails_with_a_helpful_message(env: Env) -> None:
    elsewhere = env.tmp / "elsewhere"
    elsewhere.mkdir()

    with pytest.raises(OperationError) as exc_info:
        Workspace.find(elsewhere)

    message = str(exc_info.value)
    assert str(elsewhere) in message
    assert "draupnir.toml" in message
    assert "main" in message


def test_find_workspace_without_main_says_to_run_init(env: Env) -> None:
    root = env.tmp / "no-main"
    root.mkdir()
    (root / "draupnir.toml").write_text("")

    with pytest.raises(OperationError) as exc_info:
        Workspace.find(root)

    assert "draupnir init" in str(exc_info.value)


def test_clone_for_a_subfolder_gives_the_clone(env: Env) -> None:
    ws = env.ws()
    nested = env.main / "src" / "deep"
    nested.mkdir(parents=True)

    assert ws.clone_for(nested) == env.main
    assert ws.clone_for(env.main) == env.main

    with pytest.raises(OperationError):
        ws.clone_for(env.root)

    with pytest.raises(OperationError):
        ws.clone_for(env.tmp)


def test_resolve_folder_treats_dot_as_a_path_from_cwd(env: Env) -> None:
    ws = env.ws()
    nested = env.main / "src"
    nested.mkdir()

    assert ws.resolve_folder(".", env.main) == env.main
    assert ws.resolve_folder("..", nested) == env.main
    assert ws.resolve_folder("main/", nested) == env.main


def test_remote_branches_lists_the_branches_main_knows_from_github(env: Env) -> None:
    other = env.tmp / "pusher"
    sh(env.tmp, "git", "clone", "-q", str(env.remote), str(other))
    sh(other, "git", "push", "-q", "origin", "main:feature/vessel-owner")
    sh(env.main, "git", "fetch", "-q")

    assert env.ws().remote_branches() == ["feature/vessel-owner", "main"]


def test_project_paths_lists_main_first_then_clones_of_the_same_remote(env: Env) -> None:
    ws = env.ws()
    feature = env.root / "feature-x"
    sh(env.root, "git", "clone", "-q", str(env.remote), str(feature))

    assert ws.project_paths() == [env.main, feature]


def test_project_paths_ignores_a_clone_of_another_repository(env: Env) -> None:
    ws = env.ws()
    other_source = env.tmp / "other-source"
    other_source.mkdir()
    sh(other_source, "git", "init", "-q")
    commit(other_source, "file.txt", "x\n", "initial")
    other_remote = env.tmp / "other.git"
    sh(env.tmp, "git", "clone", "-q", "--bare", str(other_source), str(other_remote))
    unrelated = env.root / "unrelated"
    sh(env.root, "git", "clone", "-q", str(other_remote), str(unrelated))

    assert ws.project_paths() == [env.main]


def test_project_paths_matches_ssh_and_https_urls(env: Env) -> None:
    ws = env.ws()
    feature = env.root / "feature-y"
    sh(env.root, "git", "clone", "-q", str(env.remote), str(feature))
    sh(env.main, "git", "remote", "set-url", "origin", "git@github.example.com:acme/shop.git")
    sh(feature, "git", "remote", "set-url", "origin", "https://github.example.com/acme/shop.git")

    assert ws.project_paths() == [env.main, feature]


def test_main_branch_comes_from_origin_head(env: Env) -> None:
    ws = env.ws()

    assert ws.main_branch() == "main"


def test_main_branch_from_the_config_wins_over_githubs_default(env: Env) -> None:
    config = env.root / "draupnir.toml"
    config.write_text(config.read_text().replace('main-branch = "main"', 'main-branch = "develop"'))

    assert env.ws().main_branch() == "develop"


def test_github_default_branch_without_origin_head_explains_the_fix(env: Env) -> None:
    ws = env.ws()
    sh(env.main, "git", "symbolic-ref", "-d", "refs/remotes/origin/HEAD")

    with pytest.raises(OperationError) as exc_info:
        ws.github_default_branch()

    assert "git -C main remote set-head origin --auto" in str(exc_info.value)
