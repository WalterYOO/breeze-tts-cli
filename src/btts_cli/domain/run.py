"""Run directories: candidate matrix, manifest and resume validation."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from btts_cli import __version__
from btts_cli.audio.hash import prefixed, sha256_file, sha256_text
from btts_cli.domain.design import CompiledDesign, DesignSpec
from btts_cli.domain.recipe import Recipe, read_recipe, recipe_path_for, validate_recipe_output
from btts_cli.output.errors import IntegrityError, UsageError
from btts_cli.output.json import read_json, write_json_atomic, write_text_atomic

RUN_SCHEMA_VERSION = 1
DEFAULT_SEEDS = (42, 43)
DEFAULT_CFGS = (2.5, 4.0)
DEFAULT_MAX_NEW_TOKENS = 300
MAX_TOKENS = 1500
WIDE_LIMIT = 8
LARGE_MATRIX_LIMIT = 16
RTF_BASELINE = 9.0
BYTES_PER_SECOND = 48000  # 24 kHz mono PCM16
MAX_CFG_SCALE = 20.0
TAKE_ID_RE = re.compile(r"^c[0-9]{3,}$")
DESIGN_MODEL_TEMPLATE = "tts_instruction"
CfgScale = Annotated[float, Field(gt=0, le=20, allow_inf_nan=False)]

CJK_RE = re.compile(r"[一-鿿]")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class DesignSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    language: str
    template: str
    compiler_version: str
    rendered_instruction: str
    structured: dict[str, Any]
    covers: list[str]
    cli_version: str = __version__


class MatrixSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seeds: list[int]
    cfgs: list[CfgScale]
    covers: list[str]
    wide: bool = False


class CandidatePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    take_id: str
    seed: int
    cfg_scale: CfgScale
    cover_index: int
    text: str
    text_sha256: str
    estimated_seconds: float
    max_new_tokens: int

    @field_validator("take_id")
    @classmethod
    def _check_take_id(cls, value: str) -> str:
        if not TAKE_ID_RE.fullmatch(value):
            raise ValueError("take_id must match ^c[0-9]{3,}$")
        return value


class ModelInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = "BreezeBlue/breeze-tts-2"
    digest: str
    api_protocol: str = "local-mps/v1"
    sample_rate: int = 24000


class RunManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = RUN_SCHEMA_VERSION
    run_id: str
    name: str
    created_at: str
    updated_at: str
    command: Literal["design.explore"] = "design.explore"
    design: DesignSnapshot
    matrix: MatrixSpec
    model: ModelInfo
    candidates: list[CandidatePlan] = Field(default_factory=list)
    completed: list[str] = Field(default_factory=list)
    interrupted: bool = False
    warnings: list[str] = Field(default_factory=list)


@dataclass
class RunState:
    run_dir: Path
    manifest: RunManifest

    @property
    def candidates_dir(self) -> Path:
        return candidates_dir(self.run_dir)

    @property
    def report_dir(self) -> Path:
        return report_dir(self.run_dir)


def candidates_dir(run_dir: Path) -> Path:
    return run_dir / "candidates"


def report_dir(run_dir: Path) -> Path:
    return run_dir / "report"


def manifest_path(run_dir: Path) -> Path:
    return run_dir / "run.json"


def new_run_id(name: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-") or "run"
    return f"{safe}-{stamp}"


def estimate_audio_seconds(text: str, language: str) -> float:
    if language == "zh":
        units = len(CJK_RE.findall(text))
        return round(0.4 + 0.22 * max(units, 1), 2)
    words = len(re.findall(r"\S+", text))
    return round(0.4 + words / 2.6, 2)


def estimate_max_new_tokens(seconds: float) -> int:
    return max(DEFAULT_MAX_NEW_TOKENS, min(MAX_TOKENS, int(round(seconds * 120))))


def parse_float_list(value: str | None) -> list[float] | None:
    if value is None:
        return None
    parts = [part.strip() for part in value.split(",") if part.strip()]
    try:
        numbers = [float(part) for part in parts]
    except ValueError as exc:
        raise UsageError(f"invalid number list: {value}") from exc
    if not numbers or any(
        not math.isfinite(number) or number <= 0 or number > MAX_CFG_SCALE
        for number in numbers
    ):
        raise UsageError(
            f"all cfg values must be finite, greater than 0 and at most "
            f"{MAX_CFG_SCALE:g}: {value}"
        )
    return numbers


def parse_int_list(value: str | None) -> list[int] | None:
    if value is None:
        return None
    parts = [part.strip() for part in value.split(",") if part.strip()]
    try:
        numbers = [int(part) for part in parts]
    except ValueError as exc:
        raise UsageError(f"invalid integer list: {value}") from exc
    if not numbers or any(number < 0 for number in numbers):
        raise UsageError(f"seeds must be non-negative integers: {value}")
    return numbers


def build_plans(
    compiled: CompiledDesign,
    *,
    wide: bool = False,
    seeds: list[int] | None = None,
    cfgs: list[float] | None = None,
    covers: list[str] | None = None,
    allow_large: bool = False,
) -> tuple[list[CandidatePlan], MatrixSpec]:
    seed_values = seeds or list(DEFAULT_SEEDS)
    cfg_values = cfgs or list(DEFAULT_CFGS)
    if covers:
        cover_values = list(covers)
    elif wide:
        cover_values = compiled.covers[:2]
    else:
        cover_values = compiled.covers[:1]
    if not cover_values:
        raise UsageError("design has no cover script; add covers to the YAML or use --template")
    if wide and len(cover_values) < 2:
        raise UsageError(
            "--wide requires at least two cover scripts",
            hint="add a second cover to the design YAML, or drop --wide",
        )

    total = len(seed_values) * len(cfg_values) * len(cover_values)
    if total > LARGE_MATRIX_LIMIT and not allow_large:
        raise UsageError(
            f"matrix would generate {total} takes (limit {LARGE_MATRIX_LIMIT} without --yes)",
            hint="reduce --seeds/--cfgs, or pass --yes after checking the estimate",
        )

    plans: list[CandidatePlan] = []
    index = 1
    for cover_index, cover in enumerate(cover_values):
        for cfg in cfg_values:
            for seed in seed_values:
                seconds = estimate_audio_seconds(cover, compiled.language)
                plans.append(
                    CandidatePlan(
                        take_id=f"c{index:03d}",
                        seed=int(seed),
                        cfg_scale=round(float(cfg), 6),
                        cover_index=cover_index,
                        text=cover,
                        text_sha256=prefixed(sha256_text(cover)),
                        estimated_seconds=seconds,
                        max_new_tokens=estimate_max_new_tokens(seconds),
                    )
                )
                index += 1
    matrix = MatrixSpec(
        seeds=[int(seed) for seed in seed_values],
        cfgs=[round(float(cfg), 6) for cfg in cfg_values],
        covers=cover_values,
        wide=wide,
    )
    return plans, matrix


def summarise_plans(plans: list[CandidatePlan]) -> dict[str, float]:
    audio_seconds = sum(plan.estimated_seconds for plan in plans)
    return {
        "takes": float(len(plans)),
        "audio_seconds": round(audio_seconds, 1),
        "compute_seconds": round(audio_seconds * RTF_BASELINE, 0),
        "megabytes": round(audio_seconds * BYTES_PER_SECOND / 1_000_000, 2),
    }


def create_run(
    run_dir: Path,
    *,
    compiled: CompiledDesign,
    spec: DesignSpec,
    plans: list[CandidatePlan],
    matrix: MatrixSpec,
    model: ModelInfo,
    run_id: str | None = None,
) -> RunState:
    run_dir.mkdir(parents=True, exist_ok=True)
    candidates_dir(run_dir).mkdir(parents=True, exist_ok=True)
    now = utc_now()
    manifest = RunManifest(
        run_id=run_id or new_run_id(compiled.spec.name),
        name=compiled.spec.name,
        created_at=now,
        updated_at=now,
        design=DesignSnapshot(
            name=compiled.spec.name,
            language=compiled.language,
            template=compiled.template,
            compiler_version=compiled.compiler_version,
            rendered_instruction=compiled.rendered_instruction,
            structured=compiled.structured,
            covers=compiled.covers,
        ),
        matrix=matrix,
        model=model,
        candidates=plans,
    )
    snapshot = spec.model_dump(mode="json", exclude_none=True)
    write_text_atomic(
        run_dir / "design.yaml",
        yaml.safe_dump(snapshot, sort_keys=False, allow_unicode=True),
    )
    write_manifest(run_dir, manifest)
    return RunState(run_dir=run_dir, manifest=manifest)


def write_manifest(run_dir: Path, manifest: RunManifest) -> None:
    manifest.updated_at = utc_now()
    write_json_atomic(manifest_path(run_dir), manifest.model_dump(mode="json"))


def load_run(run_dir: Path) -> RunState:
    path = manifest_path(run_dir)
    if not path.is_file():
        raise UsageError(
            f"not a btts run directory: {run_dir}",
            hint="expected run.json (create one with `btts design explore`)",
        )
    try:
        manifest = RunManifest.model_validate(read_json(path))
    except (OSError, ValueError, ValidationError) as exc:
        raise IntegrityError(f"invalid run manifest: {path}") from exc
    return RunState(run_dir=run_dir, manifest=manifest)


def take_wav_path(run_dir: Path, take_id: str) -> Path:
    """Resolve a take WAV path, refusing ids that could leave ``candidates/``."""
    if not TAKE_ID_RE.fullmatch(take_id):
        raise IntegrityError(
            f"invalid take id: {take_id!r}",
            hint="take ids must match ^c[0-9]{3,}$",
        )
    directory = candidates_dir(run_dir)
    base = directory.resolve()
    path = directory / f"{take_id}.wav"
    if not path.resolve().is_relative_to(base):
        raise IntegrityError(f"take path escapes the candidates directory: {take_id}")
    return path


def find_plan(state: RunState, take_id: str) -> CandidatePlan:
    for plan in state.manifest.candidates:
        if plan.take_id == take_id:
            return plan
    raise UsageError(f"unknown take {take_id}", hint="see run.json for candidate ids")


def candidate_status(
    run_dir: Path, plan: CandidatePlan, manifest: RunManifest
) -> tuple[str, str | None]:
    """Return ``(status, reason)`` where status is complete/missing/corrupt.

    Every identity-bearing recipe field is checked against the plan and the
    manifest. A resume must never treat a take generated with another model or
    instruction as complete.
    """
    wav_path = take_wav_path(run_dir, plan.take_id)
    sidecar = recipe_path_for(wav_path)
    if not wav_path.is_file():
        return "missing", "audio missing"
    if not sidecar.is_file():
        return "corrupt", "recipe sidecar missing"
    try:
        recipe = read_recipe(sidecar)
        validate_recipe_output(recipe, wav_path)
    except IntegrityError as exc:
        return "corrupt", exc.message

    mismatches = [
        (recipe.take_id == plan.take_id, "recipe take_id differs from plan"),
        (recipe.run_id == manifest.run_id, "recipe run_id differs from manifest"),
        (recipe.command == "design.explore", "recipe command is not design.explore"),
        (recipe.output.file == wav_path.name, "recipe output file differs from take"),
        (recipe.operation.kind == "design", "recipe operation is not design"),
        (
            recipe.operation.template == DESIGN_MODEL_TEMPLATE,
            "recipe template differs from design route",
        ),
        (
            recipe.operation.instruction == manifest.design.rendered_instruction,
            "recipe instruction differs from manifest",
        ),
        (recipe.operation.seed == plan.seed, "recipe seed differs from plan"),
        (
            abs(recipe.operation.cfg_scale - plan.cfg_scale) <= 1e-6,
            "recipe cfg differs from plan",
        ),
        (recipe.input.sha256 == plan.text_sha256, "recipe text hash differs from plan"),
        (
            recipe.input.language == manifest.design.language,
            "recipe language differs from manifest",
        ),
        (
            recipe.model.id == manifest.model.id,
            "recipe model id differs from manifest",
        ),
        (
            recipe.model.digest == manifest.model.digest,
            "recipe model digest differs from manifest",
        ),
        (
            recipe.model.api_protocol == manifest.model.api_protocol,
            "recipe protocol differs from manifest",
        ),
        (
            recipe.model.sample_rate == manifest.model.sample_rate,
            "recipe sample rate differs from manifest",
        ),
    ]
    for ok, reason in mismatches:
        if not ok:
            return "corrupt", reason
    return "complete", None


def completed_takes(run_dir: Path, manifest: RunManifest) -> list[str]:
    result = []
    for plan in manifest.candidates:
        status, _ = candidate_status(run_dir, plan, manifest)
        if status == "complete":
            result.append(plan.take_id)
    return result


def candidate_recipe(run_dir: Path, take_id: str) -> Recipe:
    wav_path = take_wav_path(run_dir, take_id)
    return read_recipe(recipe_path_for(wav_path))


def audio_digest(run_dir: Path, take_id: str) -> str:
    return prefixed(sha256_file(take_wav_path(run_dir, take_id)))
