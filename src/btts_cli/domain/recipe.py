"""Recipe sidecars: the reproducible description of one generated WAV.

Recipes never contain tokens, private URLs or absolute paths. Every path is
stored as a filename next to the WAV.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from btts_cli.audio.hash import prefixed, sha256_file, sha256_text
from btts_cli.output.errors import IntegrityError
from btts_cli.output.json import read_json, write_json_atomic

RECIPE_SCHEMA_VERSION = 1
MODEL_ID = "BreezeBlue/breeze-tts-2"
API_PROTOCOL = "local-mps/v1"


class RecipeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    sha256: str
    language: str


CfgScale = Annotated[float, Field(gt=0, le=20, allow_inf_nan=False)]


class RecipeOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["design", "clone", "direction", "plain"]
    template: str
    instruction: str | None = None
    cfg_scale: CfgScale
    seed: int


class RecipeVoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: str | None = None
    profile_sha256: str | None = None
    ref_audio_sha256: str | None = None
    ref_text_sha256: str | None = None


class RecipeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = MODEL_ID
    digest: str
    api_protocol: str = API_PROTOCOL
    sample_rate: int


class RecipeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file: str
    format: Literal["wav"] = "wav"
    bytes: int
    duration_s: float
    sha256: str


class RecipeTiming(BaseModel):
    model_config = ConfigDict(extra="forbid")

    elapsed_s: float
    rtf: float | None = None


class Recipe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = RECIPE_SCHEMA_VERSION
    take_id: str | None = None
    run_id: str | None = None
    command: Literal["say", "design.explore"]
    input: RecipeInput
    operation: RecipeOperation
    voice: RecipeVoice = Field(default_factory=RecipeVoice)
    model: RecipeModel
    output: RecipeOutput
    timing: RecipeTiming
    warnings: list[str] = Field(default_factory=list)
    finish_reason: Literal["stop", "length", "unknown"] = "unknown"


def recipe_path_for(wav_path: Path) -> Path:
    return wav_path.with_name(f"{wav_path.stem}.recipe.json")


def build_recipe(
    *,
    command: Literal["say", "design.explore"],
    wav_path: Path,
    text: str,
    language: str,
    kind: Literal["design", "clone", "direction", "plain"],
    template: str,
    instruction: str | None,
    cfg_scale: float,
    seed: int,
    model_digest: str,
    sample_rate: int,
    duration_s: float,
    elapsed_s: float,
    take_id: str | None = None,
    run_id: str | None = None,
    voice: RecipeVoice | None = None,
    warnings: list[str] | None = None,
    finish_reason: Literal["stop", "length", "unknown"] = "unknown",
) -> Recipe:
    """Build a recipe for an already-written WAV file."""
    audio_hash = prefixed(sha256_file(wav_path))
    return Recipe(
        take_id=take_id,
        run_id=run_id,
        command=command,
        input=RecipeInput(text=text, sha256=prefixed(sha256_text(text)), language=language),
        operation=RecipeOperation(
            kind=kind,
            template=template,
            instruction=instruction,
            cfg_scale=round(float(cfg_scale), 6),
            seed=int(seed),
        ),
        voice=voice or RecipeVoice(),
        model=RecipeModel(digest=model_digest, sample_rate=sample_rate),
        output=RecipeOutput(
            file=wav_path.name,
            bytes=wav_path.stat().st_size,
            duration_s=round(float(duration_s), 4),
            sha256=audio_hash,
        ),
        timing=RecipeTiming(
            elapsed_s=round(float(elapsed_s), 3),
            rtf=round(float(elapsed_s) / duration_s, 3) if duration_s > 0 else None,
        ),
        warnings=warnings or [],
        finish_reason=finish_reason,
    )


def write_recipe(wav_path: Path, recipe: Recipe) -> Path:
    path = recipe_path_for(wav_path)
    write_json_atomic(path, recipe.model_dump(mode="json"))
    return path


def read_recipe(path: Path) -> Recipe:
    try:
        return Recipe.model_validate(read_json(path))
    except (OSError, ValueError) as exc:
        raise IntegrityError(f"invalid recipe sidecar: {path.name}") from exc


def validate_recipe_output(recipe: Recipe, wav_path: Path) -> None:
    """Raise when the WAV no longer matches the sidecar."""
    if not wav_path.is_file():
        raise IntegrityError(f"missing audio file: {wav_path.name}")
    actual_hash = prefixed(sha256_file(wav_path))
    if actual_hash != recipe.output.sha256:
        raise IntegrityError(
            f"hash mismatch for {wav_path.name}",
            hint="the take was modified or truncated",
        )
    if wav_path.stat().st_size != recipe.output.bytes:
        raise IntegrityError(f"size mismatch for {wav_path.name}")
