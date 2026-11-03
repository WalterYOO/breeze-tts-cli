from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from btts_cli.domain.voice import DefaultsBlock, DesignBlock, ReferenceBlock, VoiceStore
from tests.helpers import write_transcript, write_wav

BASE_REFERENCE = {
    "audio_sha256": "sha256:" + "0" * 64,
    "transcript_sha256": "sha256:" + "1" * 64,
    "transcript_confirmed": True,
    "duration_s": 8.0,
    "sample_rate": 24000,
}


@pytest.mark.parametrize(
    "unsafe",
    ["../outside.wav", "/tmp/outside.wav", "..", ".", "a/b.wav", "a\\b.wav", ""],
)
def test_reference_block_rejects_unsafe_paths(unsafe: str) -> None:
    with pytest.raises(ValidationError):
        ReferenceBlock(audio=unsafe, **BASE_REFERENCE)
    with pytest.raises(ValidationError):
        ReferenceBlock(transcript=unsafe, **BASE_REFERENCE)


def test_profile_blocks_reject_non_finite_cfg() -> None:
    with pytest.raises(ValidationError):
        DesignBlock(
            seed=42, cfg_scale=float("nan"), instruction="instruction", max_new_tokens=300
        )
    with pytest.raises(ValidationError):
        DefaultsBlock(cfg_scale=float("inf"), seed=42)


def _add_clone(run, isolated: Path, name: str = "dusk") -> VoiceStore:
    ref = write_wav(isolated / f"{name}-ref.wav", seconds=8.0)
    transcript = write_transcript(isolated / f"{name}-ref.txt", "这是一段参考音频。")
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
    return VoiceStore()


def _edit_profile(store: VoiceStore, name: str, **reference: object) -> None:
    path = store.path_for(name) / "profile.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["reference"].update(reference)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_say_rejects_escaped_reference_value(run, fake, isolated: Path) -> None:
    store = _add_clone(run, isolated)
    outside = write_wav(isolated / "outside.wav")
    _edit_profile(store, "dusk", audio="../outside.wav")
    assert outside.is_file()

    result = run("say", "你好。", "--voice", "@dusk", "--out", str(isolated / "o.wav"))
    assert result.exit_code == 5
    assert "invalid profile" in result.stderr
    assert fake.calls == []
    assert not (isolated / "o.recipe.json").exists()


def test_say_rejects_symlink_escape(run, fake, isolated: Path) -> None:
    store = _add_clone(run, isolated)
    outside = write_wav(isolated / "outside.wav")
    stored_ref = store.path_for("dusk") / "ref.wav"
    stored_ref.unlink()
    stored_ref.symlink_to(outside)

    result = run("say", "你好。", "--voice", "@dusk", "--out", str(isolated / "o.wav"))
    assert result.exit_code == 5
    assert "escapes the voice" in result.stderr
    assert fake.calls == []


@pytest.mark.parametrize(
    "marker", ["(clears throat)", "[清嗓子]", "（叹气）", "【咳嗽】"]
)
def test_voice_add_rejects_bracket_events(
    run, isolated: Path, marker: str
) -> None:
    ref = write_wav(isolated / "ref.wav", seconds=8.0)
    transcript = write_transcript(isolated / "ref.txt", f"你好。{marker}")

    result = run(
        "voice",
        "add",
        "evented",
        "--ref",
        str(ref),
        "--text",
        str(transcript),
        "--confirm-transcript",
    )
    assert result.exit_code == 2
    assert "inline event" in result.stderr.lower()
