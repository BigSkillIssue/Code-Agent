"""Shared fixtures: tmp_project (a temp git repo), fake_provider and ctx."""

from pathlib import Path

import pytest

from forge.ctx import Ctx
from forge.providers.fake import FakeProvider
from support import init_repo, make_ctx


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    """An empty git repository in a temporary folder."""
    return init_repo(tmp_path / "project")


@pytest.fixture
def fake_provider() -> FakeProvider:
    """A FakeProvider with an empty script; tests add turns as needed."""
    return FakeProvider()


@pytest.fixture
def ctx(tmp_project: Path) -> Ctx:
    """A Ctx over in-memory ports, rooted at `tmp_project`."""
    return make_ctx(tmp_project)
