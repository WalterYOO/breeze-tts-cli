from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from btts_cli.audio import metrics as metrics_module
from btts_cli.audio.metrics import compute_metrics, metric_columns


def _set_versions(
    monkeypatch: pytest.MonkeyPatch, librosa: str | None, parselmouth: str | None
) -> None:
    versions = {"librosa": librosa, "parselmouth": parselmouth}
    monkeypatch.setattr(
        metrics_module, "_optional_version", lambda module, distribution: versions[module]
    )


def test_capabilities_full_when_both_backends_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_versions(monkeypatch, "1.0.0", "0.4.7")

    capabilities = metrics_module.metrics_capabilities()

    assert capabilities["level"] == "full"
    assert capabilities["missing"] == []
    assert capabilities["hint"] is None


def test_capabilities_partial_names_the_missing_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_versions(monkeypatch, "1.0.0", None)

    capabilities = metrics_module.metrics_capabilities()

    assert capabilities["level"] == "partial"
    assert capabilities["missing"] == ["praat-parselmouth"]
    assert "breeze-tts-cli[metrics]" in capabilities["hint"]


def test_capabilities_basic_when_no_optional_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_versions(monkeypatch, None, None)

    capabilities = metrics_module.metrics_capabilities()

    assert capabilities["level"] == "basic"
    assert capabilities["missing"] == ["librosa", "praat-parselmouth"]


def test_doctor_json_includes_metrics_capabilities(run, isolated: Path) -> None:
    result = run("doctor", "--json", "--model", str(isolated / "missing"))

    assert result.exit_code == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["metrics"]["level"] in {"basic", "partial", "full"}
    assert isinstance(report["metrics"]["missing"], list)
    assert "hint" in report["metrics"]


def test_doctor_human_output_shows_metrics_row(run, isolated: Path) -> None:
    result = run("doctor", "--model", str(isolated / "missing"))

    assert result.exit_code == 0, result.stderr
    assert "metrics" in result.output


def test_doctor_human_hint_keeps_metrics_extra(
    run, isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from btts_cli.commands import doctor as doctor_module

    monkeypatch.setattr(
        doctor_module,
        "metrics_capabilities",
        lambda: {
            "level": "partial",
            "librosa": "1.0.0",
            "parselmouth": None,
            "missing": ["praat-parselmouth"],
            "hint": metrics_module.METRICS_EXTRA_HINT,
        },
    )

    result = run("doctor", "--model", str(isolated / "missing"))

    assert result.exit_code == 0, result.stderr
    # Rich parses ``[metrics]`` as markup and silently drops it unless escaped.
    assert "breeze-tts-cli[metrics]" in result.output


def _band_noise(
    rng: np.random.Generator, frames: int, sample_rate: int, band: tuple[float, float]
) -> np.ndarray:
    noise = rng.standard_normal(frames)
    spectrum = np.fft.rfft(noise)
    freqs = np.fft.rfftfreq(frames, d=1.0 / sample_rate)
    low, high = band
    spectrum[(freqs < low) | (freqs >= high)] = 0
    filtered = np.fft.irfft(spectrum, frames)
    peak = float(np.max(np.abs(filtered)))
    return filtered / peak if peak else filtered


def _write_mix(
    path: Path,
    *,
    low_gain: float,
    high_gain: float,
    sample_rate: int = 24000,
    seconds: float = 1.5,
) -> None:
    rng = np.random.default_rng(7)
    frames = int(sample_rate * seconds)
    low = _band_noise(rng, frames, sample_rate, metrics_module.LOW_BAND_HZ)
    high = _band_noise(rng, frames, sample_rate, metrics_module.HIGH_BAND_HZ)
    audio = 0.4 * (low_gain * low + high_gain * high)
    sf.write(path, audio.astype(np.float32), sample_rate)


def _write_harmonic_tone(path: Path, f0: float, sample_rate: int = 24000) -> None:
    seconds = 1.5
    frames = int(sample_rate * seconds)
    timeline = np.arange(frames) / sample_rate
    audio = np.zeros(frames, dtype=np.float64)
    for harmonic, amplitude in ((1, 0.2), (2, 0.1), (3, 0.07), (4, 0.05)):
        audio += amplitude * np.sin(2 * np.pi * f0 * harmonic * timeline)
    sf.write(path, audio.astype(np.float32), sample_rate)


def test_metric_columns_include_spectral_shape_fields() -> None:
    columns = metric_columns()

    for field in (
        "f0_p10_hz",
        "f0_p90_hz",
        "f0_std_hz",
        "spectral_slope_db_per_oct",
        "band_ratio_high_low_db",
    ):
        assert field in columns


def test_full_metrics_reports_f0_distribution(tmp_path: Path) -> None:
    pytest.importorskip("librosa")
    path = tmp_path / "tone.wav"
    _write_harmonic_tone(path, 120.0)

    metrics, _ = compute_metrics(path, level="full")

    assert metrics["f0_median_hz"] == pytest.approx(120.0, abs=3.0)
    assert metrics["f0_p10_hz"] <= metrics["f0_median_hz"] <= metrics["f0_p90_hz"]
    assert metrics["f0_std_hz"] is not None
    assert metrics["f0_std_hz"] < 6.0


def test_full_metrics_skips_spectral_shape_for_short_clip(tmp_path: Path) -> None:
    pytest.importorskip("librosa")
    path = tmp_path / "short.wav"
    _write_harmonic_tone(path, 200.0)
    audio, sample_rate = sf.read(path)
    sf.write(path, audio[: sample_rate // 50], sample_rate)

    metrics, _ = compute_metrics(path, level="full")

    assert metrics["abnormal_short"] is True
    assert metrics["spectral_slope_db_per_oct"] is None
    assert metrics["band_ratio_high_low_db"] is None


def test_full_metrics_separates_low_and_high_frequency_balance(
    tmp_path: Path,
) -> None:
    pytest.importorskip("librosa")
    low_path = tmp_path / "low.wav"
    high_path = tmp_path / "high.wav"
    _write_mix(low_path, low_gain=1.0, high_gain=0.02)
    _write_mix(high_path, low_gain=0.02, high_gain=1.0)

    low, _ = compute_metrics(low_path, level="full")
    high, _ = compute_metrics(high_path, level="full")

    assert low["band_ratio_high_low_db"] is not None
    assert high["band_ratio_high_low_db"] is not None
    assert (
        high["band_ratio_high_low_db"] > low["band_ratio_high_low_db"] + 30.0
    )
    assert low["spectral_slope_db_per_oct"] is not None
    assert high["spectral_slope_db_per_oct"] is not None
    assert high["spectral_slope_db_per_oct"] > low["spectral_slope_db_per_oct"] + 1.0
