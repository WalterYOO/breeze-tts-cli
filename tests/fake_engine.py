"""Deterministic fake engine used by unit tests (never imports torch)."""

from __future__ import annotations

import numpy as np

from btts_cli.engine.base import GenerationRequest, GenerationResult

ROUTE_TEMPLATES = {
    "design": "tts_instruction",
    "clone": "ref_clone_tata",
    "direction": "ref_edit_tata",
    "plain": "tts_plain",
}


class FakeEngine:
    model_id = "Fake/Breeze-TTS-2"
    model_digest = "sha256:" + "0" * 64
    sample_rate = 24000
    device = "fake"
    load_seconds = 0.01

    def __init__(self, seconds: float = 1.5):
        self.seconds = seconds
        self.calls: list[GenerationRequest] = []
        self.closed = False

    def generate(self, request: GenerationRequest) -> GenerationResult:
        self.calls.append(request)
        frames = max(1, int(self.sample_rate * self.seconds))
        timeline = np.arange(frames, dtype=np.float32) / self.sample_rate
        frequency = 180.0 + 20.0 * (request.seed % 5) + 5.0 * request.cfg_scale
        audio = (0.25 * np.sin(2.0 * np.pi * frequency * timeline)).astype(np.float32)
        audio[: int(0.05 * self.sample_rate)] = 0.0
        return GenerationResult(
            audio=audio,
            sample_rate=self.sample_rate,
            elapsed_s=0.01,
            route=request.route,
            template=ROUTE_TEMPLATES[request.route],
            finish_reason="stop",
            warnings=(),
        )

    def close(self) -> None:
        self.closed = True
