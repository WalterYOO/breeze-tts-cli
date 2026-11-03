"""Real Apple-MPS smoke test. Enabled only with ``BTTS_RUN_MPS=1``."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import soundfile as sf

pytestmark = pytest.mark.mps

RUN_MPS = os.environ.get("BTTS_RUN_MPS") == "1"


def _model_dir() -> Path:
    env = os.environ.get("BTTS_MODEL_DIR")
    if env:
        return Path(env)
    return (
        Path(__file__).resolve().parents[2]
        / "breeze-tts"
        / "ms-models"
        / "Breeze-TTS-2"
    )


@pytest.mark.skipif(not RUN_MPS, reason="set BTTS_RUN_MPS=1 to run the real MPS test")
def test_mps_load_and_three_routes(tmp_path: Path) -> None:
    from btts_cli.engine.base import GenerationRequest
    from btts_cli.engine.mps import MpsEngine, mps_available

    if not mps_available():
        pytest.skip("MPS is not available on this machine")
    model_dir = _model_dir()
    if not model_dir.is_dir():
        pytest.skip(f"model directory not found: {model_dir}")

    engine = MpsEngine.load(model_dir, device="mps")
    try:
        reference_text = "你好，这是参考音频。"
        design = engine.generate(
            GenerationRequest(
                text=reference_text,
                instruction="一位三十多岁的女性，音色温暖干净，音高中低，语速中慢，语气平静。",
                cfg_scale=4.0,
                seed=42,
                max_new_tokens=120,
            )
        )
        assert design.route == "design"
        assert design.template == "tts_instruction"
        assert design.sample_rate == 24000
        assert design.audio.size > 0
        assert 0.2 < design.duration_s < 30

        reference = tmp_path / "ref.wav"
        sf.write(reference, design.audio, design.sample_rate, subtype="PCM_16")

        clone = engine.generate(
            GenerationRequest(
                text="欢迎收听今晚的节目。",
                ref_audio=reference,
                ref_text=reference_text,
                cfg_scale=1.0,
                seed=42,
                max_new_tokens=120,
            )
        )
        assert clone.route == "clone"
        assert clone.template == "ref_clone_tata"
        assert clone.audio.size > 0

        direction = engine.generate(
            GenerationRequest(
                text="欢迎收听今晚的节目。",
                instruction="更轻、更慢，带一点笑意",
                ref_audio=reference,
                ref_text=reference_text,
                cfg_scale=4.0,
                seed=42,
                max_new_tokens=120,
            )
        )
        assert direction.route == "direction"
        assert direction.template == "ref_edit_tata"
        assert direction.audio.size > 0
    finally:
        engine.close()
