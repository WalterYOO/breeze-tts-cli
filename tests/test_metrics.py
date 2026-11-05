from __future__ import annotations

import json
from pathlib import Path

import pytest

from btts_cli.audio import metrics as metrics_module


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
