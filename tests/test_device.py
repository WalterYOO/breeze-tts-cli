"""Device resolution: auto prefers MPS then CUDA, CPU is explicit only."""

from __future__ import annotations

import pytest

from btts_cli.engine.mps import resolve_device
from btts_cli.output.errors import EngineError


def test_cpu_only_when_explicit() -> None:
    assert resolve_device("cpu") == "cpu"
    assert resolve_device(" CPU ") == "cpu"


def test_auto_prefers_mps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("btts_cli.engine.mps.mps_available", lambda: True)
    monkeypatch.setattr("btts_cli.engine.mps.cuda_available", lambda: True)
    assert resolve_device(None) == "mps"
    assert resolve_device("auto") == "mps"


def test_auto_falls_to_cuda_with_local_rank(monkeypatch: pytest.MonkeyPatch) -> None:
    import torch

    monkeypatch.setattr("btts_cli.engine.mps.mps_available", lambda: False)
    monkeypatch.setattr("btts_cli.engine.mps.cuda_available", lambda: True)
    monkeypatch.setattr(torch.cuda, "device_count", lambda: 4)
    monkeypatch.setenv("LOCAL_RANK", "2")
    assert resolve_device(None) == "cuda:2"


def test_auto_without_gpu_refuses_to_silently_use_cpu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("btts_cli.engine.mps.mps_available", lambda: False)
    monkeypatch.setattr("btts_cli.engine.mps.cuda_available", lambda: False)
    with pytest.raises(EngineError, match="no GPU backend"):
        resolve_device(None)


def test_cuda_index_is_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    import torch

    monkeypatch.setattr("btts_cli.engine.mps.cuda_available", lambda: True)
    monkeypatch.setattr(torch.cuda, "device_count", lambda: 2)
    assert resolve_device("cuda") == "cuda"
    assert resolve_device("cuda:1") == "cuda:1"
    with pytest.raises(EngineError, match="invalid CUDA device"):
        resolve_device("cuda:2")
    with pytest.raises(EngineError, match="invalid CUDA device"):
        resolve_device("cuda:x")


def test_cuda_without_cuda_build_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("btts_cli.engine.mps.cuda_available", lambda: False)
    with pytest.raises(EngineError, match="CUDA is not available"):
        resolve_device("cuda:0")


def test_unknown_device_is_rejected() -> None:
    with pytest.raises(EngineError, match="unsupported device"):
        resolve_device("tpu")
