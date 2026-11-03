from __future__ import annotations

import json
from pathlib import Path

import pytest

from btts_cli.audio.hash import sha256_file
from btts_cli.domain.design import dump_design_spec, load_design_spec
from tests.helpers import make_design


def test_resume_only_regenerates_missing_and_corrupt(
    run, fake, isolated: Path
) -> None:
    design = make_design(isolated / "dusk.yaml")
    run_dir = isolated / "run"
    first = run("design", "explore", str(design), "--out", str(run_dir))
    assert first.exit_code == 0, first.output
    assert len(fake.calls) == 4

    candidates = run_dir / "candidates"
    untouched_hash = sha256_file(candidates / "c001.wav")
    (candidates / "c002.wav").unlink()
    (candidates / "c003.wav").write_bytes(b"corrupted")
    fake.calls.clear()

    resumed = run("design", "explore", str(design), "--out", str(run_dir), "--resume")
    assert resumed.exit_code == 0, resumed.output
    assert len(fake.calls) == 2
    assert sha256_file(candidates / "c001.wav") == untouched_hash

    manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert sorted(manifest["completed"]) == ["c001", "c002", "c003", "c004"]

    for take in ("c002", "c003"):
        recipe = json.loads(
            (candidates / f"{take}.recipe.json").read_text(encoding="utf-8")
        )
        expected = sha256_file(candidates / f"{take}.wav")
        assert recipe["output"]["sha256"] == f"sha256:{expected}"


def test_resume_complete_run_does_not_load_engine(run, fake, isolated: Path) -> None:
    design = make_design(isolated / "dusk.yaml")
    run_dir = isolated / "run"
    assert run("design", "explore", str(design), "--out", str(run_dir)).exit_code == 0
    calls_before = len(fake.calls)

    result = run("design", "explore", str(design), "--out", str(run_dir), "--resume")
    assert result.exit_code == 0, result.output
    assert len(fake.calls) == calls_before
    assert "nothing to do" in result.output.lower()


def test_resume_refuses_changed_design(run, fake, isolated: Path) -> None:
    design = make_design(isolated / "dusk.yaml")
    run_dir = isolated / "run"
    assert run("design", "explore", str(design), "--out", str(run_dir)).exit_code == 0

    spec = load_design_spec(design)
    spec.identity.timbre = ["bright", "forward"]
    dump_design_spec(spec, design)

    result = run("design", "explore", str(design), "--out", str(run_dir), "--resume")
    assert result.exit_code == 2
    assert "changed" in result.stderr


def test_resume_refuses_changed_covers(run, fake, isolated: Path) -> None:
    design = make_design(isolated / "dusk.yaml")
    run_dir = isolated / "run"
    assert run("design", "explore", str(design), "--out", str(run_dir)).exit_code == 0

    spec = load_design_spec(design)
    spec.covers = ["全新的开场白，内容和之前完全不同。"]
    dump_design_spec(spec, design)

    result = run("design", "explore", str(design), "--out", str(run_dir), "--resume")
    assert result.exit_code == 2
    assert "changed" in result.stderr


def test_resume_refuses_changed_model_digest(
    run, fake, isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_dir = isolated / "ms-models" / "Breeze-TTS-2"
    model_dir.mkdir(parents=True)
    fake.model_digest = "sha256:" + "a" * 64
    monkeypatch.setattr(
        "btts_cli.commands.design.compute_model_digest",
        lambda path: fake.model_digest,
    )
    design = make_design(isolated / "dusk.yaml")
    run_dir = isolated / "run"
    assert run("design", "explore", str(design), "--out", str(run_dir)).exit_code == 0
    calls = len(fake.calls)

    fake.model_digest = "sha256:" + "b" * 64
    result = run("design", "explore", str(design), "--out", str(run_dir), "--resume")
    assert result.exit_code == 2
    assert "model checkpoint changed" in result.stderr
    assert len(fake.calls) == calls


def test_resume_regenerates_tampered_recipe(run, fake, isolated: Path) -> None:
    design = make_design(isolated / "dusk.yaml")
    run_dir = isolated / "run"
    assert run("design", "explore", str(design), "--out", str(run_dir)).exit_code == 0

    recipe_path = run_dir / "candidates" / "c002.recipe.json"
    recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
    recipe["operation"]["instruction"] = "tampered instruction"
    recipe["model"]["digest"] = "sha256:" + "f" * 64
    recipe_path.write_text(json.dumps(recipe), encoding="utf-8")
    fake.calls.clear()

    result = run("design", "explore", str(design), "--out", str(run_dir), "--resume")
    assert result.exit_code == 0, result.output
    assert len(fake.calls) == 1

    regenerated = json.loads(recipe_path.read_text(encoding="utf-8"))
    assert regenerated["operation"]["instruction"] == fake.calls[0].instruction
    assert regenerated["model"]["digest"] == fake.model_digest
