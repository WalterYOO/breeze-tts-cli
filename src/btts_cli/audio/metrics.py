"""Objective audio metrics.

``basic`` needs only numpy + soundfile. ``full`` additionally uses librosa (F0,
spectral centroid) and praat-parselmouth (HNR) when installed; missing optional
dependencies degrade to ``None`` with an actionable warning rather than a
failure.

These numbers exist to **eliminate technical failures and support sorting**.
They are not an aesthetic score, and the CLI never selects a take automatically.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from btts_cli.audio.io import read_wav

METRIC_LEVELS = ("basic", "full")

BASIC_FIELDS = (
    "duration_s",
    "sample_rate",
    "peak_dbfs",
    "rms_dbfs",
    "clipping_ratio",
    "silence_ratio",
    "abnormal_short",
    "abnormal_long",
)
FULL_FIELDS = ("f0_median_hz", "voiced_ratio", "hnr_db", "spectral_centroid_hz")

SILENCE_FLOOR_DBFS = -50.0
SILENCE_FRAME_S = 0.03
CLIP_THRESHOLD = 0.999
SHORT_TAKE_S = 1.0
LONG_TAKE_S = 30.0

METRICS_EXTRA_HINT = (
    "install optional metrics with: pip install 'breeze-tts-cli[metrics]' "
    "(librosa + praat-parselmouth)"
)


def compute_metrics(
    wav_path: Path,
    *,
    level: str = "basic",
    sample_rate: int | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Return ``(metrics, warnings)`` for one WAV file."""
    if level not in METRIC_LEVELS:
        raise ValueError(f"unknown metrics level: {level}")

    audio, sr = read_wav(wav_path)
    if sample_rate is not None and sr != sample_rate:
        raise ValueError(f"{wav_path.name}: sample rate {sr} != expected {sample_rate}")

    # ``read_wav`` returns float32 samples decoded by libsndfile, i.e. already in
    # [-1, 1], so clipping is detected on the decoded float values.
    metrics: dict[str, Any] = {
        "duration_s": round(len(audio) / sr, 4) if sr else 0.0,
        "sample_rate": sr,
        "peak_dbfs": _dbfs(float(np.max(np.abs(audio))) if audio.size else 0.0),
        "rms_dbfs": _dbfs(float(np.sqrt(np.mean(np.square(audio)))) if audio.size else 0.0),
        "clipping_ratio": _clipping_ratio(audio),
        "silence_ratio": _silence_ratio(audio, sr),
        "abnormal_short": bool(audio.size / sr < SHORT_TAKE_S) if sr else True,
        "abnormal_long": bool(audio.size / sr > LONG_TAKE_S) if sr else False,
    }

    warnings: list[str] = []
    if level == "full":
        full_metrics, full_warnings = _full_metrics(audio, sr)
        metrics.update(full_metrics)
        warnings.extend(full_warnings)
    else:
        metrics.update({field: None for field in FULL_FIELDS})
    return metrics, warnings


def _dbfs(amplitude: float) -> float | None:
    if amplitude <= 0:
        return None
    return round(20.0 * np.log10(amplitude), 2)


def _clipping_ratio(audio: np.ndarray) -> float:
    if audio.size == 0:
        return 0.0
    return round(float(np.mean(np.abs(audio) >= CLIP_THRESHOLD)), 6)


def _silence_ratio(audio: np.ndarray, sample_rate: int) -> float:
    if audio.size == 0 or sample_rate <= 0:
        return 1.0
    frame = max(1, int(round(SILENCE_FRAME_S * sample_rate)))
    usable = audio[: audio.size - (audio.size % frame)]
    if usable.size == 0:
        return 0.0
    frames = usable.reshape(-1, frame)
    rms = np.sqrt(np.mean(np.square(frames), axis=1))
    floor = 10.0 ** (SILENCE_FLOOR_DBFS / 20.0)
    return round(float(np.mean(rms < floor)), 6)


def _full_metrics(audio: np.ndarray, sample_rate: int) -> tuple[dict[str, Any], list[str]]:
    values: dict[str, Any] = {field: None for field in FULL_FIELDS}
    warnings: list[str] = []
    if audio.size == 0 or sample_rate <= 0:
        return values, ["full metrics skipped: empty audio"]

    try:
        import librosa
    except ImportError:
        warnings.append(
            f"F0 and spectral centroid unavailable: librosa is not installed; {METRICS_EXTRA_HINT}"
        )
    else:
        try:
            f0, voiced_flag, _ = librosa.pyin(
                audio.astype(np.float64),
                fmin=60.0,
                fmax=400.0,
                sr=sample_rate,
                frame_length=1024,
            )
            finite = f0[np.isfinite(f0)]
            if finite.size:
                values["f0_median_hz"] = round(float(np.median(finite)), 2)
            values["voiced_ratio"] = round(float(np.mean(voiced_flag)), 4)
            centroid = librosa.feature.spectral_centroid(
                y=audio.astype(np.float64), sr=sample_rate
            )
            values["spectral_centroid_hz"] = round(float(np.mean(centroid)), 2)
        except Exception as exc:  # noqa: BLE001 - metrics must never abort a report
            warnings.append(f"librosa metrics failed ({type(exc).__name__}: {exc})")

    try:
        import parselmouth
    except ImportError:
        warnings.append(
            f"HNR unavailable: praat-parselmouth is not installed; {METRICS_EXTRA_HINT}"
        )
    else:
        try:
            sound = parselmouth.Sound(audio.astype(np.float64), sampling_frequency=sample_rate)
            harmonicity = sound.to_harmonicity()
            samples = np.asarray(harmonicity.values, dtype=np.float64).reshape(-1)
            # Praat encodes "undefined" as a large negative sentinel.
            usable = samples[np.isfinite(samples) & (samples > -100.0)]
            if usable.size:
                values["hnr_db"] = round(float(np.mean(usable)), 2)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"HNR metric failed ({type(exc).__name__}: {exc})")

    return values, warnings


def metric_columns() -> list[str]:
    return ["take_id", *BASIC_FIELDS, *FULL_FIELDS]
