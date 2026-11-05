from __future__ import annotations

import json
from pathlib import Path

import pytest

from btts_cli.domain.design import (
    DeliveryBaseline,
    Identity,
    Scene,
    design_from_template,
    dump_design_spec,
)
from btts_cli.domain.voice import VoiceStore
from btts_cli.engine.base import GenerationRequest
from btts_cli.output.errors import PlaybackError, UsageError
from btts_cli.output.json import json_dumps
from tests.helpers import make_design, write_transcript, write_wav


def _add_clone(run, isolated: Path, name: str = "dusk") -> tuple[Path, str]:
    ref = write_wav(isolated / f"{name}-ref.wav", seconds=8.0)
    text = "这是一个用于克隆的参考音频。"
    transcript = write_transcript(isolated / f"{name}-ref.txt", text)
    result = run(
        "voice",
        "add",
        name,
        "--ref",
        str(ref),
        "--text",
        str(transcript),
        "--confirm-transcript",
    )
    assert result.exit_code == 0, result.output
    return ref, text


def test_say_design_route(run, fake, isolated: Path) -> None:
    design = make_design(isolated / "dusk.yaml")
    out = isolated / "out.wav"

    result = run("say", "欢迎收听今晚的节目。", "--design", str(design), "--out", str(out))
    assert result.exit_code == 0, result.output
    assert out.is_file()

    call = fake.calls[-1]
    assert call.route == "design"
    assert call.instruction
    assert call.cfg_scale == 4.0
    assert call.seed == 19
    assert call.ref_audio is None

    recipe = json.loads((isolated / "out.recipe.json").read_text(encoding="utf-8"))
    assert recipe["command"] == "say"
    assert recipe["operation"]["kind"] == "design"
    assert recipe["operation"]["template"] == "tts_instruction"
    assert recipe["operation"]["cfg_scale"] == 4.0
    assert recipe["output"]["file"] == "out.wav"
    assert str(isolated) not in json.dumps(recipe)


def test_say_clone_route(run, fake, isolated: Path) -> None:
    _, reference_text = _add_clone(run, isolated)
    out = isolated / "clone.wav"

    result = run("say", "今晚我们聊点轻松的。", "--voice", "@dusk", "--out", str(out))
    assert result.exit_code == 0, result.output

    call = fake.calls[-1]
    assert call.route == "clone"
    assert call.instruction is None
    assert call.cfg_scale == 1.0
    assert call.ref_text == reference_text
    assert call.ref_audio is not None and call.ref_audio.is_file()

    recipe = json.loads((isolated / "clone.recipe.json").read_text(encoding="utf-8"))
    assert recipe["operation"]["kind"] == "clone"
    assert recipe["operation"]["template"] == "ref_clone_tata"
    assert recipe["operation"]["cfg_scale"] == 1.0
    assert recipe["voice"]["profile_id"].startswith("v_")
    assert recipe["voice"]["ref_audio_sha256"].startswith("sha256:")
    assert str(isolated) not in json.dumps(recipe)


def test_say_direction_route(run, fake, isolated: Path) -> None:
    _add_clone(run, isolated)
    out = isolated / "direction.wav"

    result = run(
        "say",
        "今晚我们聊点轻松的。",
        "--voice",
        "@dusk",
        "--direction",
        "更轻、更慢，带一点笑意",
        "--out",
        str(out),
    )
    assert result.exit_code == 0, result.output

    call = fake.calls[-1]
    assert call.route == "direction"
    assert call.instruction == "更轻、更慢，带一点笑意"
    assert call.cfg_scale == 4.0
    assert call.ref_text

    recipe = json.loads((isolated / "direction.recipe.json").read_text(encoding="utf-8"))
    assert recipe["operation"]["template"] == "ref_edit_tata"
    assert recipe["operation"]["cfg_scale"] == 4.0


def test_say_design_profile_via_voice(run, fake, isolated: Path) -> None:
    design = make_design(isolated / "dusk.yaml")
    run_dir = isolated / "run"
    assert run("design", "explore", str(design), "--out", str(run_dir)).exit_code == 0
    assert (
        run("design", "freeze", str(run_dir), "--take", "c001", "--name", "dusk").exit_code
        == 0
    )

    result = run("say", "晚上好。", "--voice", "@dusk", "--out", str(isolated / "o.wav"))
    assert result.exit_code == 0, result.output
    call = fake.calls[-1]
    profile = VoiceStore().load("dusk")
    assert call.route == "design"
    assert call.cfg_scale == 2.5
    assert profile.design is not None
    assert call.seed == profile.design.seed


def test_say_refuses_unconfirmed_clone(run, fake, isolated: Path) -> None:
    _add_clone(run, isolated)
    store = VoiceStore()
    profile_path = store.path_for("dusk") / "profile.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["reference"]["transcript_confirmed"] = False
    profile_path.write_text(json.dumps(profile), encoding="utf-8")

    result = run("say", "你好。", "--voice", "@dusk", "--out", str(isolated / "o.wav"))
    assert result.exit_code == 2
    assert "not confirmed" in result.output
    assert fake.calls == []


def test_say_refuses_tampered_reference(run, fake, isolated: Path) -> None:
    _add_clone(run, isolated)
    store = VoiceStore()
    stored_ref = store.path_for("dusk") / "ref.wav"
    stored_ref.write_bytes(stored_ref.read_bytes() + b"\x00")

    result = run("say", "你好。", "--voice", "@dusk", "--out", str(isolated / "o.wav"))
    assert result.exit_code == 5
    assert "hash mismatch" in result.stderr


def test_say_play_runs_after_commit_and_keeps_file_on_failure(
    run, fake, isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    design = make_design(isolated / "dusk.yaml")
    out = isolated / "played.wav"

    def fail_play(path: Path) -> None:
        raise PlaybackError("afplay failed", hint=f"audio was written to {path.name}")

    monkeypatch.setattr("btts_cli.commands.say.play_file", fail_play)
    result = run(
        "say", "晚上好。", "--design", str(design), "--out", str(out), "--play"
    )
    assert result.exit_code == 4
    assert out.is_file()
    assert (isolated / "played.recipe.json").is_file()


def test_say_requires_exactly_one_route(run, fake, isolated: Path) -> None:
    assert run("say", "你好。").exit_code == 2
    design = make_design(isolated / "dusk.yaml")
    assert (
        run("say", "你好。", "--design", str(design), "--voice", "@dusk").exit_code == 2
    )
    assert (
        run("say", "你好。", "--design", str(design), "--direction", "更慢").exit_code
        == 2
    )


def test_say_design_rejects_blocking_lint_event(run, fake, isolated: Path) -> None:
    design = isolated / "bad.yaml"
    spec = design_from_template("zh-late-night-radio/v1", "bad")
    spec.identity.timbre = ["[笑]"]
    dump_design_spec(spec, design)
    out = isolated / "o.wav"

    result = run("say", "你好。", "--design", str(design), "--out", str(out))
    assert result.exit_code == 2
    assert "inline-event" in result.stderr
    assert "design lint" in result.stderr
    assert fake.calls == []
    assert not out.exists()
    assert not out.with_name("o.recipe.json").exists()


def test_say_design_rejects_no_anchors(run, fake, isolated: Path) -> None:
    design = isolated / "empty.yaml"
    spec = design_from_template("zh-late-night-radio/v1", "empty")
    spec.identity = Identity()
    spec.delivery_baseline = DeliveryBaseline()
    spec.scene = Scene()
    dump_design_spec(spec, design)

    result = run(
        "say", "你好。", "--design", str(design), "--out", str(isolated / "o.wav")
    )
    assert result.exit_code == 2
    assert "no-anchors" in result.stderr
    assert fake.calls == []


def test_say_direction_rejects_inline_event(run, fake, isolated: Path) -> None:
    _add_clone(run, isolated)

    result = run(
        "say",
        "你好。",
        "--voice",
        "@dusk",
        "--direction",
        "更慢一点[笑]",
        "--out",
        str(isolated / "o.wav"),
    )
    assert result.exit_code == 2
    assert "inline event" in result.stderr.lower()
    assert fake.calls == []


@pytest.mark.parametrize("cfg", ["nan", "inf", "-inf", "0", "20.1"])
def test_say_rejects_invalid_cfg(run, fake, isolated: Path, cfg: str) -> None:
    design = make_design(isolated / "dusk.yaml")
    out = isolated / "bad.wav"

    result = run(
        "say", "你好。", "--design", str(design), "--cfg", cfg, "--out", str(out)
    )
    assert result.exit_code == 2
    assert "cfg" in result.stderr.lower()
    assert fake.calls == []
    assert not out.exists()
    assert not out.with_name("bad.recipe.json").exists()


def test_generation_request_and_json_reject_non_finite_values() -> None:
    with pytest.raises(UsageError):
        GenerationRequest(text="你好。", cfg_scale=float("nan"))
    with pytest.raises(UsageError):
        GenerationRequest(text="你好。", cfg_scale=float("inf"))
    with pytest.raises(UsageError):
        GenerationRequest(text="你好。", cfg_scale=21.0)
    with pytest.raises(ValueError):
        json_dumps({"cfg_scale": float("nan")})
