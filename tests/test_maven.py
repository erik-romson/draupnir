from __future__ import annotations

from pathlib import Path

import pytest
from conftest import Env
from helpers import logger, write_executable

from draupnir.errors import OperationError
from draupnir.maven import version, write_config


def test_maven_config_has_absolute_paths_for_head_and_tail(env: Env) -> None:
    shared = env.tmp / "shared-repo"
    lines, log = logger()

    write_config(env.main, shared, log)

    content = (env.main / ".mvn" / "maven.config").read_text()
    assert content == (
        f"-Dmaven.repo.local={env.main / '.m2repo'}\n-Dmaven.repo.local.tail={shared}\n"
    )
    assert (env.main / ".m2repo").is_dir()
    assert str(env.main) in lines[0]


def test_maven_shared_repository_comes_from_config_and_environment(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    (env.root / "draupnir.toml").write_text('[maven]\nshared-repository = "~/from-file"\n')
    _, log = logger()

    write_config(env.main, env.ws().config.maven.shared_repository, log)

    content = (env.main / ".mvn" / "maven.config").read_text()
    assert str(Path("~/from-file").expanduser()) in content

    monkeypatch.setenv("DRAUPNIR_MAVEN_SHARED_REPO", "~/from-env")
    write_config(env.main, env.ws().config.maven.shared_repository, log)

    content = (env.main / ".mvn" / "maven.config").read_text()
    assert str(Path("~/from-env").expanduser()) in content


def test_maven_prefers_mvnw_over_mvn(env: Env) -> None:
    write_executable(env.main / "mvnw", '#!/bin/sh\necho "Apache Maven 3.9.1 (mvnw)"\n')

    found = version(env.main)

    assert found == (3, 9, 1)


def test_maven_older_than_3_9_0_is_refused(env: Env) -> None:
    write_executable(env.bin / "mvn", '#!/bin/sh\necho "Apache Maven 3.8.7 (test fixture)"\n')

    with pytest.raises(OperationError) as exc_info:
        version(env.main)

    message = str(exc_info.value)
    assert "3.8.7" in message
    assert "3.9.0" in message


def test_maven_that_cannot_run_gives_a_message(env: Env) -> None:
    write_executable(env.bin / "mvn", "#!/bin/sh\nexit 1\n")

    with pytest.raises(OperationError) as exc_info:
        version(env.main)

    assert str(env.main) in str(exc_info.value)
