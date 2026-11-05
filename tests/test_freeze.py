from __future__ import annotations

import json
from pathlib import Path

import pytest

from btts_cli.domain.voice import VoiceStore
from tests.helpers import make_design


@pytest.fixture
def explored(run, fake, isolated: Path) -> tuple[Path, Path]:
    design = make_design(isolated / "dusk.yaml")
    run_dir = isolated / "run"
    result = run("design", "explore", str(design), "--out", str(run_dir))
    assert result.exit_code == 0, result.output
    return design, run_dir


def test_freeze_as_design(run, explored: tuple[Path, Path]) -> None:
    _, run_dir = explored
    result = run(
        "design", "freeze", str(run_dir), "--take", "c001", "--name", "dusk"
    )
    assert result.exit_code == 0, result.output

    recipe = json.loads(
        (run_dir / "candidates" / "c001.recipe.json").read_text(encoding="utf-8")
    )
    store = VoiceStore()
    profile = store.load("dusk")
    assert profile.kind == "design"
    assert profile.design is not None
    assert profile.design.seed == recipe["operation"]["seed"]
    assert profile.design.cfg_scale == 2.5
    assert profile.reference is None
    assert profile.provenance.source_take_id == "c001"
    assert profile.provenance.source_session_id
    assert profile.provenance.model_digest == recipe["model"]["digest"]

    directory = store.path_for("dusk")
    assert (directory / "preview.wav").is_file()
    assert (directory / "design.yaml").is_file()
    assert (directory / "LICENSE.txt").is_file()
    license_text = (directory / "LICENSE.txt").read_text(encoding="utf-8")
    assert "non-commercial" in license_text.lower()
    assert not (directory / "ref.wav").exists()


def test_freeze_as_clone_requires_confirmation(
    run, explored: tuple[Path, Path]
) -> None:
    _, run_dir = explored
    refused = run(
        "design", "freeze", str(run_dir), "--take", "c001", "--name", "cloney", "--as", "clone"
    )
    assert refused.exit_code == 2
    assert "confirm-transcript" in refused.stderr
    assert "晚上好，欢迎收听今天的节目。" in refused.output
    assert not VoiceStore().exists("cloney")


def test_freeze_as_clone(run, explored: tuple[Path, Path]) -> None:
    _, run_dir = explored
    result = run(
        "design",
        "freeze",
        str(run_dir),
        "--take",
        "c001",
        "--name",
        "cloney",
        "--as",
        "clone",
        "--confirm-transcript",
    )
    assert result.exit_code == 0, result.output

    store = VoiceStore()
    profile = store.load("cloney")
    assert profile.kind == "clone"
    assert profile.design is None
    assert profile.reference is not None
    assert profile.reference.transcript_confirmed is True
    assert profile.reference.audio_sha256.startswith("sha256:")
    assert profile.defaults.cfg_scale == 1.0
    directory = store.path_for("cloney")
    assert (directory / "ref.wav").is_file()
    assert (directory / "ref.txt").read_text(encoding="utf-8").strip()


def test_freeze_rejects_hash_mismatch(run, explored: tuple[Path, Path]) -> None:
    _, run_dir = explored
    wav = run_dir / "candidates" / "c001.wav"
    wav.write_bytes(wav.read_bytes() + b"\x00")

    result = run("design", "freeze", str(run_dir), "--take", "c001", "--name", "oops")
    assert result.exit_code == 5


def test_freeze_rejects_truncated_take(run, explored: tuple[Path, Path]) -> None:
    _, run_dir = explored
    recipe_path = run_dir / "candidates" / "c001.recipe.json"
    recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
    recipe["finish_reason"] = "length"
    recipe["warnings"] = ["generation reached max_new_tokens; audio may be truncated"]
    recipe_path.write_text(json.dumps(recipe), encoding="utf-8")

    result = run("design", "freeze", str(run_dir), "--take", "c001", "--name", "oops")
    assert result.exit_code == 5
    assert "truncated" in result.stderr


def test_freeze_existing_name_requires_force(run, explored: tuple[Path, Path]) -> None:
    _, run_dir = explored
    args = ["design", "freeze", str(run_dir), "--take", "c001", "--name", "dusk"]
    assert run(*args).exit_code == 0
    assert run(*args).exit_code == 2
    assert run(*args, "--force").exit_code == 0
