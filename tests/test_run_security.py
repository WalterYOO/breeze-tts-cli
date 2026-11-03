from __future__ import annotations

import json
from pathlib import Path

import pytest

from btts_cli.domain.run import take_wav_path
from btts_cli.domain.voice import VoiceStore
from btts_cli.output.errors import IntegrityError
from tests.helpers import make_design


@pytest.mark.parametrize(
    "take_id",
    ["../../outside", "c001/../../outside", "c1", "c001 ", "/abs", "../c001"],
)
def test_take_wav_path_rejects_traversal(tmp_path: Path, take_id: str) -> None:
    (tmp_path / "candidates").mkdir()
    with pytest.raises(IntegrityError):
        take_wav_path(tmp_path, take_id)


def test_freeze_rejects_traversal_take_id(run, fake, isolated: Path) -> None:
    design = make_design(isolated / "dusk.yaml")
    run_dir = isolated / "run"
    assert run("design", "explore", str(design), "--out", str(run_dir)).exit_code == 0

    manifest_path = run_dir / "run.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["candidates"][0]["take_id"] = "../../outside"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = run(
        "design",
        "freeze",
        str(run_dir),
        "--take",
        "../../outside",
        "--name",
        "pwned",
    )
    assert result.exit_code == 5
    assert "invalid run manifest" in result.stderr
    assert not VoiceStore().exists("pwned")
    assert not (isolated / "outside.wav").exists()
