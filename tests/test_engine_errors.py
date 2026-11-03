from __future__ import annotations

import json
from pathlib import Path

import pytest

from btts_cli.commands._common import generate_or_raise_engine_error
from btts_cli.output.errors import EngineError, IntegrityError
from tests.fake_engine import FakeEngine
from tests.helpers import make_design


class ExplodingEngine(FakeEngine):
    def generate(self, request):
        raise RuntimeError("simulated MPS generation failure")


@pytest.fixture
def exploding(monkeypatch: pytest.MonkeyPatch) -> ExplodingEngine:
    engine = ExplodingEngine()
    monkeypatch.setattr("btts_cli.engine.load_engine", lambda **kwargs: engine)
    return engine


def test_say_generation_failure_exits_engine_error(
    run, exploding: ExplodingEngine, isolated: Path
) -> None:
    design = make_design(isolated / "dusk.yaml")
    out = isolated / "failed.wav"

    result = run("say", "你好。", "--design", str(design), "--out", str(out))
    assert result.exit_code == 3
    assert "generation failed" in result.stderr
    assert "simulated MPS generation failure" in result.stderr
    assert "Traceback" not in result.stderr
    assert not out.exists()
    assert not out.with_name("failed.recipe.json").exists()


def test_explore_generation_failure_exits_engine_error(
    run, exploding: ExplodingEngine, isolated: Path
) -> None:
    design = make_design(isolated / "dusk.yaml")
    run_dir = isolated / "run"

    result = run("design", "explore", str(design), "--out", str(run_dir))
    assert result.exit_code == 3
    assert "generation for take c001 failed" in result.stderr
    assert "simulated MPS generation" in result.stderr
    assert "Traceback" not in result.stderr

    manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert manifest["completed"] == []
    assert list((run_dir / "candidates").glob("*.wav")) == []
    assert list((run_dir / "candidates").glob("*.recipe.json")) == []


def test_generation_helper_preserves_domain_and_interrupt_errors() -> None:
    class Raises:
        def __init__(self, exc: BaseException):
            self.exc = exc

        def generate(self, request):
            raise self.exc

    with pytest.raises(IntegrityError):
        generate_or_raise_engine_error(Raises(IntegrityError("bad data")), object())
    with pytest.raises(KeyboardInterrupt):
        generate_or_raise_engine_error(Raises(KeyboardInterrupt()), object())
    with pytest.raises(EngineError) as info:
        generate_or_raise_engine_error(Raises(RuntimeError("boom")), object())
    assert "RuntimeError: boom" in info.value.message
