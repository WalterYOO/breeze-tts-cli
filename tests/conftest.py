from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from btts_cli.cli import cli
from tests.fake_engine import FakeEngine


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("BTTS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "outputs").mkdir(exist_ok=True)
    return tmp_path


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeEngine:
    engine = FakeEngine()
    monkeypatch.setattr("btts_cli.engine.load_engine", lambda **kwargs: engine)
    return engine


@pytest.fixture
def run() -> object:
    runner = CliRunner()

    def invoke(*args: str, **kwargs: object):
        return runner.invoke(cli, [str(arg) for arg in args], **kwargs)

    return invoke
