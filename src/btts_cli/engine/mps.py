"""Local Apple-Silicon engine built on the vendored Breeze inference code.

This is the only module allowed to import ``torch``. The environment variable
``PYTORCH_ENABLE_MPS_FALLBACK`` is set before that import so unimplemented MPS
operators fall back to CPU instead of failing mid-generation.
"""

from __future__ import annotations

import gc
import os
import time
from pathlib import Path
from typing import Any

os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

import numpy as np  # noqa: E402
import torch  # noqa: E402

from btts_cli.engine.base import (  # noqa: E402
    GenerationRequest,
    GenerationResult,
    check_config_supported,
    compute_model_digest,
    missing_model_files,
)
from btts_cli.output.errors import EngineError  # noqa: E402

MODEL_ID = "BreezeBlue/breeze-tts-2"
REPETITION_PENALTY = 1.1


def mps_available() -> bool:
    return bool(torch.backends.mps.is_built() and torch.backends.mps.is_available())


def resolve_device(explicit: str | None) -> str:
    if explicit:
        normalized = explicit.strip().lower()
        if normalized not in {"mps", "cpu"}:
            raise EngineError(
                f"unsupported device: {explicit}",
                hint="P0 supports --device mps (default) or --device cpu (debug only)",
            )
        if normalized == "mps" and not mps_available():
            raise EngineError("MPS is not available on this machine")
        return normalized
    if mps_available():
        return "mps"
    raise EngineError(
        "MPS is not available; refusing to fall back to CPU silently",
        hint="run `btts_cli doctor` for details, or pass --device cpu only for debugging",
    )


class MpsEngine:
    """One process = one model load; candidates share the loaded model."""

    model_id = MODEL_ID

    def __init__(
        self,
        *,
        tokenizer: Any,
        model: Any,
        audio_tokenizer: Any,
        device: str,
        model_digest: str,
        sample_rate: int,
    ):
        self.tokenizer = tokenizer
        self.model = model
        self.audio_tokenizer = audio_tokenizer
        self.device = device
        self.model_digest = model_digest
        self.sample_rate = sample_rate
        self.load_seconds = 0.0

    @classmethod
    def load(cls, model_dir: Path, device: str | None = None) -> MpsEngine:
        from btts_cli._vendor.breeze_tts.breeze_infer.runtime import (
            load_runtime,
            update_generation_config_for_breeze,
        )

        if not model_dir.is_dir():
            raise EngineError(
                f"model directory not found: {model_dir}",
                hint="pass --model or set BTTS_MODEL_DIR",
            )
        missing = missing_model_files(model_dir)
        if missing:
            raise EngineError(
                "model directory is incomplete: " + ", ".join(missing),
                hint="the checkpoint must include weights, tokenizer files and audio_tokenizer/",
            )
        check_config_supported(model_dir / "config.json")
        resolved_device = resolve_device(device)
        started = time.perf_counter()
        try:
            tokenizer, model, audio_tokenizer = load_runtime(
                model_dir,
                device=resolved_device,
                attn_implementation="eager",
            )
            update_generation_config_for_breeze(model)
        except Exception as exc:  # noqa: BLE001 - surface as engine failure
            raise EngineError(f"failed to load Breeze model: {exc}") from exc
        elapsed = time.perf_counter() - started
        sample_rate = int(getattr(model.config.codec_config, "sampling_rate", 24000))
        engine = cls(
            tokenizer=tokenizer,
            model=model,
            audio_tokenizer=audio_tokenizer,
            device=resolved_device,
            model_digest=compute_model_digest(model_dir),
            sample_rate=sample_rate,
        )
        engine.load_seconds = elapsed
        return engine

    def generate(self, request: GenerationRequest) -> GenerationResult:
        from btts_cli._vendor.breeze_tts.breeze_infer.runtime import set_all_seeds
        from btts_cli._vendor.breeze_tts.breeze_infer.templates import (
            get_template,
            prepare_inputs,
            select_template_name,
        )
        from btts_cli._vendor.breeze_tts.models.logits_process import (
            GeneratedTokenRepetitionPenaltyLogitsProcessor,
        )

        request_dict = request.as_request_dict()
        template_name = select_template_name(request_dict)
        template = get_template(template_name)
        set_all_seeds(request.seed)
        inputs = prepare_inputs(
            self.tokenizer,
            self.audio_tokenizer,
            self.model,
            [request_dict],
            template,
            guidance_scale=request.cfg_scale,
            guidance_scale_ref=None,
            guidance_scale_ins=None,
        )
        logits_processor = _logits_processor(GeneratedTokenRepetitionPenaltyLogitsProcessor)

        started = time.perf_counter()
        with torch.inference_mode():
            output = self.model.generate(
                **inputs,
                output_audio=True,
                audio_tokenizer=self.audio_tokenizer,
                logits_processor=logits_processor,
                max_new_tokens=request.max_new_tokens,
                return_dict_in_generate=True,
            )
        elapsed = time.perf_counter() - started

        audio = _decode_output_audio(output)
        finish_reason, truncated = _finish_reason(output, request.max_new_tokens)
        warnings: list[str] = []
        if truncated:
            warnings.append(
                f"generation reached max_new_tokens={request.max_new_tokens}; "
                "audio may be truncated"
            )
        return GenerationResult(
            audio=audio,
            sample_rate=self.sample_rate,
            elapsed_s=elapsed,
            route=request.route,
            template=template_name,
            finish_reason=finish_reason,
            warnings=tuple(warnings),
        )

    def close(self) -> None:
        self.model = None
        self.tokenizer = None
        self.audio_tokenizer = None
        gc.collect()
        if self.device.startswith("mps") and hasattr(torch, "mps"):
            try:
                torch.mps.empty_cache()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass


def _logits_processor(processor_cls: Any) -> Any:
    from transformers.generation.logits_process import LogitsProcessorList

    return LogitsProcessorList([processor_cls(REPETITION_PENALTY)])


def _decode_output_audio(output: Any) -> np.ndarray:
    audio = getattr(output, "audio", None)
    if audio is None:
        raise EngineError("model returned no audio")
    first = audio[0] if isinstance(audio, (list, tuple)) else audio
    if isinstance(first, torch.Tensor):
        first = first.detach().to("cpu", dtype=torch.float32).numpy()
    array = np.asarray(first, dtype=np.float32).reshape(-1)
    if array.size == 0:
        raise EngineError("model returned empty audio")
    return array


def _finish_reason(output: Any, max_new_tokens: int) -> tuple[str, bool]:
    sequences = getattr(output, "sequences", None)
    if sequences is None:
        return "unknown", False
    generated = int(sequences.shape[-1])
    if generated >= max_new_tokens:
        return "length", True
    return "stop", False


def mps_memory_bytes() -> int | None:
    if not mps_available():
        return None
    try:
        return int(torch.mps.driver_allocated_memory())
    except Exception:  # noqa: BLE001
        return None
