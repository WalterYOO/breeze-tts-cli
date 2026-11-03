"""Engine protocol and shared request/result types (no torch imports)."""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

import numpy as np

from btts_cli.output.errors import EngineError, UsageError

MODEL_DIR_ENV = "BTTS_MODEL_DIR"
DEFAULT_MODEL_SUBDIR = Path("ms-models") / "Breeze-TTS-2"
ROUTE_KINDS = ("design", "clone", "direction", "plain")
REQUIRED_MODEL_FILES = (
    "config.json",
    "generation_config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "model.safetensors.index.json",
)
REQUIRED_AUDIO_TOKENIZER_FILES = ("config.json", "model.safetensors")
UNSUPPORTED_TEXT_ENCODER_PROJ = "breeze_text_encoder_adapter"
MAX_CFG_SCALE = 20.0

Route = Literal["design", "clone", "direction", "plain"]


@dataclass(frozen=True)
class GenerationRequest:
    text: str
    instruction: str | None = None
    ref_audio: Path | None = None
    ref_text: str | None = None
    cfg_scale: float = 1.0
    seed: int = 42
    max_new_tokens: int = 300

    def __post_init__(self) -> None:
        if not self.text or not self.text.strip():
            raise UsageError("text must not be empty")
        if (
            not math.isfinite(self.cfg_scale)
            or self.cfg_scale <= 0
            or self.cfg_scale > MAX_CFG_SCALE
        ):
            raise UsageError(
                f"cfg_scale must be finite, greater than 0 and at most "
                f"{MAX_CFG_SCALE:g}"
            )
        if self.max_new_tokens <= 0:
            raise UsageError("max_new_tokens must be greater than 0")
        if (self.ref_audio is None) != (self.ref_text is None):
            raise UsageError("ref_audio and ref_text must be provided together")
        if self.ref_audio is not None and not Path(self.ref_audio).is_file():
            raise UsageError(f"reference audio not found: {Path(self.ref_audio).name}")

    @property
    def route(self) -> Route:
        if self.ref_audio is not None:
            return "direction" if self.instruction else "clone"
        return "design" if self.instruction else "plain"

    def as_request_dict(self, request_id: str = "request") -> dict[str, object]:
        payload: dict[str, object] = {"id": request_id, "text": self.text, "speaker": "S0"}
        if self.instruction:
            payload["instruction"] = self.instruction
        if self.ref_audio is not None:
            payload["ref_audio_path"] = str(self.ref_audio)
            payload["ref_text"] = self.ref_text
        return payload


@dataclass(frozen=True)
class GenerationResult:
    audio: np.ndarray
    sample_rate: int
    elapsed_s: float
    route: Route
    template: str
    finish_reason: Literal["stop", "length", "unknown"] = "unknown"
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def duration_s(self) -> float:
        return len(self.audio) / self.sample_rate if self.sample_rate else 0.0


class Engine(Protocol):
    model_id: str
    model_digest: str
    sample_rate: int
    device: str

    def generate(self, request: GenerationRequest) -> GenerationResult: ...

    def close(self) -> None: ...


def resolve_model_dir(explicit: Path | None = None) -> Path:
    """Resolve the checkpoint directory without assuming a sibling checkout."""
    if explicit is not None:
        return Path(explicit).expanduser()
    env_value = os.environ.get(MODEL_DIR_ENV)
    if env_value:
        return Path(env_value).expanduser()
    return Path.cwd() / DEFAULT_MODEL_SUBDIR


def model_dir_hint() -> Path | None:
    """A convenience hint for ``doctor``; never used for inference."""
    for candidate in (
        Path.cwd() / DEFAULT_MODEL_SUBDIR,
        Path.cwd().parent / "breeze-tts" / DEFAULT_MODEL_SUBDIR,
    ):
        if candidate.is_dir():
            return candidate
    return None


def missing_model_files(model_dir: Path) -> list[str]:
    """Return missing checkpoint files (torch-free, safe for ``doctor``)."""
    missing = [name for name in REQUIRED_MODEL_FILES if not (model_dir / name).is_file()]
    index_path = model_dir / "model.safetensors.index.json"
    if index_path.is_file():
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
            for shard in sorted(set(index.get("weight_map", {}).values())):
                if not (model_dir / shard).is_file():
                    missing.append(str(shard))
        except (OSError, ValueError):
            missing.append("model.safetensors.index.json (unreadable)")
    elif not (model_dir / "model.safetensors").is_file():
        missing.append("model.safetensors")
    for name in REQUIRED_AUDIO_TOKENIZER_FILES:
        if not (model_dir / "audio_tokenizer" / name).is_file():
            missing.append(f"audio_tokenizer/{name}")
    return missing


def check_config_supported(config_path: Path) -> dict[str, Any]:
    """Reject checkpoints that need modules excluded from the vendored subset."""
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EngineError(f"cannot read model config: {config_path.name}") from exc

    proj_type = config.get("text_encoder_proj_type")
    if proj_type == UNSUPPORTED_TEXT_ENCODER_PROJ:
        raise EngineError(
            "this checkpoint needs the breeze_text_encoder_adapter module, "
            "which is not vendored",
            hint="use a Breeze-TTS-2 checkpoint with text_encoder_proj_type=linear",
        )
    fast_flags = sorted(
        key
        for key, value in config.items()
        if key.startswith("fast_") and value not in (None, False)
    )
    if fast_flags:
        raise EngineError(
            "this checkpoint enables CUDA fast-path flags that are not vendored: "
            + ", ".join(fast_flags)
        )
    architecture = config.get("architectures") or []
    if architecture and "BreezeForConditionalGeneration" not in architecture:
        raise EngineError(f"unsupported model architecture: {architecture}")
    return config


def compute_model_digest(model_dir: Path) -> str:
    """Hash model metadata plus weight shard names/sizes.

    Hashing 7 GB of weights on every command would cost far more than loading
    them, so the digest covers config/index/audio-tokenizer config and the
    shard inventory. It detects checkpoint swaps without reading every byte.
    """
    digest = hashlib.sha256()
    for relative in (
        "config.json",
        "generation_config.json",
        "model.safetensors.index.json",
        "audio_tokenizer/config.json",
    ):
        path = model_dir / relative
        digest.update(relative.encode("utf-8"))
        if path.is_file():
            digest.update(path.read_bytes())
    for path in sorted(model_dir.glob("*.safetensors")):
        digest.update(path.name.encode("utf-8"))
        digest.update(str(path.stat().st_size).encode("ascii"))
    audio_weights = model_dir / "audio_tokenizer" / "model.safetensors"
    if audio_weights.is_file():
        digest.update(b"audio_tokenizer/model.safetensors")
        digest.update(str(audio_weights.stat().st_size).encode("ascii"))
    return f"sha256:{digest.hexdigest()}"
