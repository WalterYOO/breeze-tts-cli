"""``btts say``: design, clone and direction routes."""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import click

import btts_cli.engine as engine_module
from btts_cli.audio.hash import prefixed, sha256_file, sha256_text
from btts_cli.audio.io import write_wav_atomic
from btts_cli.audio.play import play_file
from btts_cli.commands._common import (
    device_option,
    generate_or_raise_engine_error,
    with_error_handling,
)
from btts_cli.commands._complete import complete_voice_reference
from btts_cli.domain.design import (
    compile_design,
    has_inline_event,
    lint_design,
    load_design_spec,
)
from btts_cli.domain.recipe import RecipeVoice, build_recipe, recipe_path_for, write_recipe
from btts_cli.domain.run import estimate_audio_seconds, estimate_max_new_tokens
from btts_cli.domain.voice import VoiceProfile, VoiceStore
from btts_cli.engine.base import MAX_CFG_SCALE, GenerationRequest
from btts_cli.output.console import console, print_license_notice
from btts_cli.output.errors import IntegrityError, UsageError

RouteKind = Literal["design", "clone", "direction", "plain"]


@click.command("say")
@click.argument("text")
@click.option(
    "--design",
    "design_file",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Design YAML → tts_instruction (default cfg 4.0).",
)
@click.option(
    "--voice",
    "voice_ref",
    default=None,
    shell_complete=complete_voice_reference,
    help="@NAME from the voice store → clone / design recipe.",
)
@click.option("--direction", default=None, help="One-off performance note (clone only).")
@click.option("--seed", type=int, default=None)
@click.option("--cfg", type=float, default=None)
@click.option("--out", "out_path", type=click.Path(path_type=Path, dir_okay=False))
@click.option(
    "--play", "play_audio", is_flag=True, help="Call afplay after the WAV is committed."
)
@click.option("--model", "model_dir", type=click.Path(path_type=Path), default=None)
@device_option
@click.option("--max-new-tokens", type=int, default=None)
@with_error_handling
def say(
    text: str,
    design_file: Path | None,
    voice_ref: str | None,
    direction: str | None,
    seed: int | None,
    cfg: float | None,
    out_path: Path | None,
    play_audio: bool,
    model_dir: Path | None,
    device: str | None,
    max_new_tokens: int | None,
) -> None:
    """Synthesize one line through exactly one of the three routes."""
    if bool(design_file) == bool(voice_ref):
        raise UsageError("provide exactly one of --design FILE or --voice @NAME")
    if cfg is not None and (
        not math.isfinite(cfg) or cfg <= 0 or cfg > MAX_CFG_SCALE
    ):
        raise UsageError(
            f"--cfg must be finite, greater than 0 and at most {MAX_CFG_SCALE:g}"
        )

    instruction: str | None
    ref_audio: Path | None = None
    ref_text: str | None = None
    voice_meta = RecipeVoice()

    if design_file is not None:
        if direction:
            raise UsageError("--direction requires --voice @NAME")
        spec = load_design_spec(design_file)
        compiled = compile_design(spec)
        lint = lint_design(compiled)
        if lint.blocked:
            blocking = [issue.code for issue in lint.issues if issue.severity == "block"]
            raise UsageError(
                "design has blocking lint issues: " + ", ".join(blocking),
                hint=f"run `btts design lint {design_file}` for details",
            )
        instruction = compiled.rendered_instruction
        language = compiled.language
        cfg_value = cfg if cfg is not None else 4.0
        seed_value = seed if seed is not None else 19
        kind: RouteKind = "design"
        label = compiled.spec.name
    else:
        assert voice_ref is not None
        store = VoiceStore()
        name, profile = store.load_by_reference(voice_ref)
        language = profile.language
        voice_meta = RecipeVoice(
            profile_id=profile.voice_id,
            profile_sha256=store.profile_sha256(name),
        )
        if direction:
            if profile.kind != "clone":
                raise UsageError(
                    "--direction requires a clone profile",
                    hint="freeze the take with `design freeze --as clone`, "
                    "or use `say --design FILE` for an instruction route",
                )
            ref_audio, ref_text = _load_reference(store, name, profile)
            instruction = direction.strip()
            if not instruction:
                raise UsageError("--direction must not be empty")
            if has_inline_event(instruction):
                raise UsageError(
                    "--direction contains inline event markers; describe the "
                    "delivery instead of placing events in the instruction"
                )
            cfg_value = cfg if cfg is not None else 4.0
            seed_value = seed if seed is not None else profile.defaults.seed
            kind = "direction"
            voice_meta = _with_reference_hashes(voice_meta, profile)
        elif profile.kind == "clone":
            ref_audio, ref_text = _load_reference(store, name, profile)
            instruction = None
            cfg_value = cfg if cfg is not None else profile.defaults.cfg_scale
            seed_value = seed if seed is not None else profile.defaults.seed
            kind = "clone"
            voice_meta = _with_reference_hashes(voice_meta, profile)
        else:
            if profile.design is None:
                raise IntegrityError(f"design profile {name} has no design block")
            instruction = profile.design.instruction
            cfg_value = cfg if cfg is not None else profile.design.cfg_scale
            seed_value = seed if seed is not None else profile.design.seed
            kind = "design"
        label = name

    if seed_value < 0:
        raise UsageError("--seed must be non-negative")
    if max_new_tokens is None:
        estimated = estimate_audio_seconds(text, language)
        max_new_tokens = estimate_max_new_tokens(estimated)
    elif max_new_tokens <= 0:
        raise UsageError("--max-new-tokens must be greater than 0")

    target = out_path or _default_output(label, language)

    console.print(
        f"route [bold]{_route_label(kind)}[/bold] · cfg {cfg_value} · seed {seed_value}"
    )
    engine = engine_module.load_engine(model_dir=model_dir, device=device)
    try:
        result = generate_or_raise_engine_error(
            engine,
            GenerationRequest(
                text=text,
                instruction=instruction,
                ref_audio=ref_audio,
                ref_text=ref_text,
                cfg_scale=cfg_value,
                seed=seed_value,
                max_new_tokens=max_new_tokens,
            ),
        )
        write_wav_atomic(target, result.audio, result.sample_rate)
        recipe = build_recipe(
            command="say",
            wav_path=target,
            text=text,
            language=language,
            kind=kind,
            template=result.template,
            instruction=instruction,
            cfg_scale=cfg_value,
            seed=seed_value,
            model_digest=engine.model_digest,
            sample_rate=result.sample_rate,
            duration_s=result.duration_s,
            elapsed_s=result.elapsed_s,
            voice=voice_meta,
            warnings=list(result.warnings),
            finish_reason=result.finish_reason,
        )
        write_recipe(target, recipe)
    finally:
        engine.close()

    rtf = result.elapsed_s / result.duration_s if result.duration_s else None
    console.print(
        f"[green]wrote[/green] {target} · {result.duration_s:.2f}s audio in "
        f"{result.elapsed_s:.1f}s" + (f" · RTF {rtf:.1f}" if rtf else "")
    )
    console.print(f"recipe: {recipe_path_for(target)}")
    for warning in result.warnings:
        console.print(f"[yellow]warning[/yellow] {warning}")
    print_license_notice(compact=True)

    if play_audio:
        play_file(target)


def _load_reference(
    store: VoiceStore, name: str, profile: VoiceProfile
) -> tuple[Path, str]:
    reference = profile.reference
    if reference is None:
        raise IntegrityError(f"clone profile {name} has no reference block")
    if not reference.transcript_confirmed:
        raise UsageError(
            f"reference transcript for {name} is not confirmed",
            hint="clone routes refuse unconfirmed transcripts; no automatic transcription",
        )
    audio_path, transcript_path = store.reference_paths(name, profile)
    if prefixed(sha256_file(audio_path)) != reference.audio_sha256:
        raise IntegrityError(f"reference audio hash mismatch for {name}")
    transcript = transcript_path.read_text(encoding="utf-8").rstrip("\n")
    if not transcript:
        raise IntegrityError(f"reference transcript for {name} is empty")
    if has_inline_event(transcript):
        raise UsageError(
            f"reference transcript for {name} contains inline event markers; "
            "the transcript must match the audio exactly"
        )
    if prefixed(sha256_text(transcript)) != reference.transcript_sha256:
        raise IntegrityError(f"reference transcript hash mismatch for {name}")
    return audio_path, transcript


def _with_reference_hashes(
    voice_meta: RecipeVoice, profile: VoiceProfile
) -> RecipeVoice:
    reference = profile.reference
    if reference is None:
        return voice_meta
    return voice_meta.model_copy(
        update={
            "ref_audio_sha256": reference.audio_sha256,
            "ref_text_sha256": reference.transcript_sha256,
        }
    )


def _default_output(label: str, language: str) -> Path:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", label).strip("-") or "say"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path("outputs") / f"say-{slug}-{stamp}.wav"


def _route_label(kind: RouteKind) -> str:
    return {
        "design": "tts_instruction",
        "clone": "ref_clone_tata",
        "direction": "ref_edit_tata",
        "plain": "tts_plain",
    }[kind]
