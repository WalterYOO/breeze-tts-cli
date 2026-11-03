"""Shared test helpers."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

from btts_cli.domain.design import design_from_template, dump_design_spec


def make_design(
    path: Path,
    template: str = "zh-late-night-radio/v1",
    name: str = "dusk",
) -> Path:
    spec = design_from_template(template, name)
    dump_design_spec(spec, path)
    return path


def write_wav(
    path: Path,
    seconds: float = 8.0,
    sample_rate: int = 24000,
    frequency: float = 220.0,
    amplitude: float = 0.3,
) -> Path:
    frames = max(1, int(sample_rate * seconds))
    timeline = np.arange(frames, dtype=np.float32) / sample_rate
    audio = (amplitude * np.sin(2.0 * np.pi * frequency * timeline)).astype(np.float32)
    sf.write(path, audio, sample_rate, subtype="PCM_16")
    return path


def write_transcript(path: Path, text: str) -> Path:
    path.write_text(text + "\n", encoding="utf-8")
    return path
