"""``btts design``: init, lint, explore, report, freeze."""

from __future__ import annotations

import csv
import io
import math
import shlex
import signal
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from rich.panel import Panel
from rich.table import Table

import btts_cli.engine as engine_module
from btts_cli import LICENSE_NOTICE
from btts_cli.audio.hash import prefixed, sha256_file, sha256_text
from btts_cli.audio.io import concat_with_gap, read_wav, write_wav_atomic
from btts_cli.audio.metrics import METRICS_EXTRA_HINT, compute_metrics
from btts_cli.audio.recommend import (
    Recommendation,
    choose_recommendation,
    technical_flags,
)
from btts_cli.commands._common import (
    generate_or_raise_engine_error,
    with_error_handling,
)
from btts_cli.domain.design import (
    NAME_RE,
    RESOURCE_DIR,
    compile_design,
    design_from_template,
    dump_design_spec,
    has_inline_event,
    lint_design,
    load_design_spec,
    template_names,
)
from btts_cli.domain.recipe import build_recipe, read_recipe, recipe_path_for, write_recipe
from btts_cli.domain.run import (
    ModelInfo,
    build_plans,
    candidate_recipe,
    candidate_status,
    create_run,
    find_plan,
    load_run,
    parse_float_list,
    parse_int_list,
    report_dir,
    summarise_plans,
    take_wav_path,
    write_manifest,
)
from btts_cli.domain.voice import (
    DefaultsBlock,
    DesignBlock,
    IdentityBlock,
    ProvenanceBlock,
    ReferenceBlock,
    VoiceProfile,
    VoiceStore,
    new_voice_id,
)
from btts_cli.engine.base import (
    GenerationRequest,
    compute_model_digest,
    resolve_model_dir,
)
from btts_cli.output.console import console, print_license_notice
from btts_cli.output.errors import IntegrityError, UsageError
from btts_cli.output.json import write_text_atomic


@click.group("design")
def design() -> None:
    """Design workbench: write, lint, explore, analyse and freeze a voice."""


@design.command("init")
@click.argument("file", type=click.Path(path_type=Path, dir_okay=False))
@click.option(
    "--template",
    "template_name",
    type=click.Choice(template_names()),
    default=None,
    help="Design template (default: zh-late-night-radio/v1).",
)
@click.option("--name", default=None, help="Voice name (default: file stem).")
@click.option("--language", type=click.Choice(["zh", "en"]), default=None)
@click.option("--force", is_flag=True, help="Overwrite an existing file.")
@with_error_handling
def design_init(
    file: Path,
    template_name: str | None,
    name: str | None,
    language: str | None,
    force: bool,
) -> None:
    """Write an editable design YAML."""
    if file.exists() and not force:
        raise UsageError(f"{file} already exists", hint="pass --force to overwrite")
    spec = design_from_template(template_name, name or file.stem, language)
    dump_design_spec(spec, file)
    compiled = compile_design(spec)
    console.print(f"[green]wrote[/green] {file} (template {spec.template})")
    console.print(Panel(compiled.rendered_instruction, title="rendered instruction"))
    console.print(f"next: btts design lint {file}")


@design.command("lint")
@click.argument("file", type=click.Path(path_type=Path, exists=True, dir_okay=False))
@click.option("--show-rendered", is_flag=True, help="Print the compiled instruction.")
@with_error_handling
def design_lint(file: Path, show_rendered: bool) -> None:
    """Offline, deterministic Instruction Readiness Score."""
    spec = load_design_spec(file)
    compiled = compile_design(spec)
    report = lint_design(compiled)

    colour = "green" if report.score >= 85 else ("yellow" if report.score >= 70 else "red")
    console.print(
        f"[bold]{spec.name}[/bold] · template {spec.template} · "
        f"Instruction Readiness Score [{colour}]{report.score}/100[/{colour}]"
    )
    table = Table(show_header=True, box=None)
    table.add_column("dimension")
    table.add_column("score", justify="right")
    for name, value in report.dimensions.items():
        table.add_row(name.replace("_", " "), str(value))
    console.print(table)

    if report.issues:
        issues = Table(title="issues", show_header=True)
        issues.add_column("severity")
        issues.add_column("code")
        issues.add_column("message")
        severity_style = {"block": "bold red", "warn": "yellow", "info": "dim"}
        for issue in report.issues:
            issues.add_row(
                f"[{severity_style[issue.severity]}]{issue.severity}[/]",
                issue.code,
                issue.message,
            )
        console.print(issues)
    else:
        console.print("[green]no issues[/green]")

    if show_rendered:
        console.print(Panel(report.rendered_instruction, title="rendered instruction"))
    console.print(
        "[dim]This score measures instruction readiness (specificity, executability, "
        "consistency, language match, testability), not voice quality.[/dim]"
    )
    if report.blocked:
        raise click.exceptions.Exit(1)


@design.command("explore")
@click.argument("file", type=click.Path(path_type=Path, exists=True, dir_okay=False))
@click.option(
    "--out", "out_dir", type=click.Path(path_type=Path, file_okay=False), required=True
)
@click.option("--resume", is_flag=True, help="Only fill missing or hash-mismatched takes.")
@click.option("--wide", is_flag=True, help="Use both cover scripts (8 takes by default).")
@click.option(
    "--seeds",
    default=None,
    help="Comma-separated seeds (default: 2 random primes below 100).",
)
@click.option("--cfgs", default=None, help="Comma-separated cfg scales (explicit matrix).")
@click.option(
    "--yes", "allow_large", is_flag=True, help="Allow a matrix larger than 16 takes."
)
@click.option("--max-new-tokens", type=int, default=None)
@click.option("--model", "model_dir", type=click.Path(path_type=Path), default=None)
@click.option("--device", type=click.Choice(["mps", "cpu"]), default=None)
@with_error_handling
def design_explore(
    file: Path,
    out_dir: Path,
    resume: bool,
    wide: bool,
    seeds: str | None,
    cfgs: str | None,
    allow_large: bool,
    max_new_tokens: int | None,
    model_dir: Path | None,
    device: str | None,
) -> None:
    """Generate the candidate matrix with one model load."""
    spec = load_design_spec(file)
    compiled = compile_design(spec)
    lint = lint_design(compiled)
    if lint.blocked:
        blocking = [issue.code for issue in lint.issues if issue.severity == "block"]
        raise UsageError(
            "design has blocking lint issues: " + ", ".join(blocking),
            hint=f"run `btts design lint {file}` for details",
        )

    plans, matrix = build_plans(
        compiled,
        wide=wide,
        seeds=parse_int_list(seeds),
        cfgs=parse_float_list(cfgs),
        allow_large=allow_large,
    )
    if max_new_tokens is not None:
        if max_new_tokens <= 0:
            raise UsageError("--max-new-tokens must be greater than 0")
        for plan in plans:
            plan.max_new_tokens = max_new_tokens

    existing = out_dir.exists() and any(out_dir.iterdir())
    if existing and not resume:
        raise UsageError(
            f"{out_dir} is not empty",
            hint="pass --resume to continue an interrupted run, or choose a new --out",
        )
    if resume and not out_dir.is_dir():
        raise UsageError(f"--resume requires an existing run directory: {out_dir}")

    if existing:
        state = load_run(out_dir)
        if (
            state.manifest.design.rendered_instruction != compiled.rendered_instruction
            or state.manifest.design.template != compiled.template
            or state.manifest.matrix.covers != matrix.covers
        ):
            raise UsageError(
                "the design file changed since this run was created",
                hint="use a new --out directory; recipes must stay reproducible",
            )
        resolved_model = resolve_model_dir(model_dir)
        if resolved_model.is_dir():
            current_digest = compute_model_digest(resolved_model)
            if (
                state.manifest.model.digest != "sha256:pending"
                and state.manifest.model.digest != current_digest
            ):
                raise UsageError(
                    "the model checkpoint changed since this run was created",
                    hint="use a new --out directory; takes from different models "
                    "must not be mixed in one run",
                )
        manifest = state.manifest
        plans = manifest.candidates
        matrix = manifest.matrix
        if max_new_tokens is not None:
            for plan in plans:
                plan.max_new_tokens = max_new_tokens
    else:
        state = create_run(
            out_dir,
            compiled=compiled,
            spec=spec,
            plans=plans,
            matrix=matrix,
            model=ModelInfo(digest="sha256:pending"),
        )
        manifest = state.manifest
        console.print(f"[green]run[/green] {manifest.run_id} → {out_dir}")

    summary = summarise_plans(plans)
    console.print(
        f"matrix: seeds {matrix.seeds} × cfg {matrix.cfgs} × "
        f"{len(matrix.covers)} cover(s) = [bold]{len(plans)}[/bold] takes"
    )
    console.print(
        f"estimated audio {summary['audio_seconds']}s · "
        f"generation ~{_minutes(summary['compute_seconds'])} (RTF≈9) · "
        f"~{summary['megabytes']} MB"
    )

    statuses = {
        plan.take_id: candidate_status(out_dir, plan, manifest) for plan in plans
    }
    manifest.completed = [
        plan.take_id for plan in plans if statuses[plan.take_id][0] == "complete"
    ]
    pending = [plan for plan in plans if statuses[plan.take_id][0] != "complete"]
    if manifest.completed:
        console.print(
            f"[dim]{len(manifest.completed)} take(s) already complete; skipping[/dim]"
        )
    for plan in pending:
        status, reason = statuses[plan.take_id]
        if status == "corrupt":
            console.print(f"[yellow]{plan.take_id}: {reason}; regenerating[/yellow]")
    if not pending:
        write_manifest(out_dir, manifest)
        console.print("[green]all takes complete — nothing to do[/green]")
        return

    console.print("loading model (one load for every candidate)...")
    engine = engine_module.load_engine(model_dir=model_dir, device=device)
    try:
        manifest.model = ModelInfo(digest=engine.model_digest, sample_rate=engine.sample_rate)
        write_manifest(out_dir, manifest)

        stop = {"requested": False, "hits": 0}

        def _handle_interrupt(signum: int, frame: Any) -> None:
            stop["hits"] += 1
            if stop["hits"] >= 2:
                raise KeyboardInterrupt
            stop["requested"] = True
            console.print("[yellow]stopping after the current take...[/yellow]")

        previous = signal.signal(signal.SIGINT, _handle_interrupt)
        durations: list[float] = []
        try:
            for index, plan in enumerate(pending, start=1):
                if stop["requested"]:
                    break
                console.print(
                    f"generating {index}/{len(pending)} · [bold]{plan.take_id}[/bold] · "
                    f"seed={plan.seed} cfg={plan.cfg_scale}"
                )
                result = generate_or_raise_engine_error(
                    engine,
                    GenerationRequest(
                        text=plan.text,
                        instruction=compiled.rendered_instruction,
                        cfg_scale=plan.cfg_scale,
                        seed=plan.seed,
                        max_new_tokens=plan.max_new_tokens,
                    ),
                    stage=f"generation for take {plan.take_id}",
                )
                wav_path = take_wav_path(out_dir, plan.take_id)
                write_wav_atomic(wav_path, result.audio, result.sample_rate)
                recipe = build_recipe(
                    command="design.explore",
                    wav_path=wav_path,
                    text=plan.text,
                    language=compiled.language,
                    kind="design",
                    template=result.template,
                    instruction=compiled.rendered_instruction,
                    cfg_scale=plan.cfg_scale,
                    seed=plan.seed,
                    model_digest=engine.model_digest,
                    sample_rate=result.sample_rate,
                    duration_s=result.duration_s,
                    elapsed_s=result.elapsed_s,
                    take_id=plan.take_id,
                    run_id=manifest.run_id,
                    warnings=list(result.warnings),
                    finish_reason=result.finish_reason,
                )
                write_recipe(wav_path, recipe)
                manifest.completed = sorted({*manifest.completed, plan.take_id})
                write_manifest(out_dir, manifest)
                durations.append(result.elapsed_s)
                eta = (sum(durations) / len(durations)) * (len(pending) - index)
                console.print(
                    f"  saved {wav_path.name} · {result.duration_s:.2f}s audio in "
                    f"{result.elapsed_s:.1f}s · ETA {eta:.0f}s"
                )
                for warning in result.warnings:
                    console.print(f"  [yellow]warning[/yellow] {warning}")
        finally:
            signal.signal(signal.SIGINT, previous)

        if stop["requested"]:
            manifest.interrupted = True
            write_manifest(out_dir, manifest)
            console.print(
                f"[yellow]stopped[/yellow] after {len(durations)} take(s); "
                f"continue with: btts design explore {file} --out {out_dir} --resume"
            )
    finally:
        engine.close()

    console.print(f"next: btts design report {out_dir}")


@design.command("report")
@click.argument("run_dir", type=click.Path(path_type=Path, file_okay=False))
@click.option(
    "--metrics", "metrics_level", type=click.Choice(["basic", "full"]), default="basic"
)
@click.option("--csv/--no-csv", "csv_enabled", default=True)
@click.option("--html/--no-html", "html_enabled", default=True)
@click.option("--reel/--no-reel", "reel_enabled", default=True)
@click.option("--gap", type=float, default=1.0, show_default=True)
@with_error_handling
def design_report(
    run_dir: Path,
    metrics_level: str,
    csv_enabled: bool,
    html_enabled: bool,
    reel_enabled: bool,
    gap: float,
) -> None:
    """Write metrics, CSV, Markdown, one reel and an offline audition page."""
    if not math.isfinite(gap) or gap < 0 or gap > 30:
        raise UsageError("--gap must be finite and between 0 and 30 seconds")
    state = load_run(run_dir)
    manifest = state.manifest
    complete = []
    skipped = []
    for plan in manifest.candidates:
        status, reason = candidate_status(run_dir, plan, manifest)
        if status == "complete":
            complete.append(plan)
        else:
            skipped.append((plan.take_id, reason or status))
    if not complete:
        raise UsageError("no complete takes in this run", hint="run design explore first")

    rows: list[dict[str, Any]] = []
    metric_warnings: list[str] = []
    for plan in complete:
        wav_path = take_wav_path(run_dir, plan.take_id)
        metrics, warnings = compute_metrics(
            wav_path, level=metrics_level, sample_rate=manifest.model.sample_rate
        )
        metric_warnings.extend(f"{plan.take_id}: {warning}" for warning in warnings)
        recipe = candidate_recipe(run_dir, plan.take_id)
        rows.append(
            {
                "take_id": plan.take_id,
                "seed": plan.seed,
                "cfg_scale": plan.cfg_scale,
                "cover_index": plan.cover_index,
                "text": plan.text,
                "truncated": recipe.finish_reason == "length"
                or any("truncated" in warning for warning in recipe.warnings),
                "inline_event": has_inline_event(plan.text),
                **metrics,
            }
        )

    recommendation = choose_recommendation(rows)

    out_dir = report_dir(run_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    columns = [
        "take_id",
        "seed",
        "cfg_scale",
        "cover_index",
        "duration_s",
        "peak_dbfs",
        "rms_dbfs",
        "clipping_ratio",
        "silence_ratio",
        "f0_median_hz",
        "voiced_ratio",
        "hnr_db",
        "spectral_centroid_hz",
        "abnormal_short",
        "abnormal_long",
    ]

    if csv_enabled:
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        write_text_atomic(out_dir / "metrics.csv", buffer.getvalue())
        console.print(f"wrote {out_dir / 'metrics.csv'}")

    write_text_atomic(
        out_dir / "report.md",
        _render_markdown(manifest, rows, skipped, metric_warnings, metrics_level, recommendation),
    )
    console.print(f"wrote {out_dir / 'report.md'}")

    if reel_enabled:
        paths = [take_wav_path(run_dir, plan.take_id) for plan in complete]
        reel = concat_with_gap(paths, gap_s=gap, sample_rate=manifest.model.sample_rate)
        write_wav_atomic(out_dir / "all_candidates.wav", reel, manifest.model.sample_rate)
        console.print(f"wrote {out_dir / 'all_candidates.wav'}")

    if html_enabled:
        html = _render_audition(
            manifest, run_dir, rows, skipped, metric_warnings, metrics_level, recommendation
        )
        write_text_atomic(out_dir / "audition.html", html)
        console.print(f"wrote {out_dir / 'audition.html'}")

    _print_metric_table(rows, metrics_level)
    if metric_warnings:
        for warning in metric_warnings:
            console.print(f"[yellow]warning[/yellow] {warning}")
        if metrics_level == "full":
            console.print(f"[dim]{METRICS_EXTRA_HINT}[/dim]")
    if skipped:
        console.print(
            "[yellow]incomplete takes skipped:[/yellow] "
            + ", ".join(f"{take} ({reason})" for take, reason in skipped)
        )
    if recommendation is not None:
        suffix = " (no candidate passed technical checks)" if recommendation.degraded else ""
        console.print(f"[bold]metric pick (not aesthetic):[/bold] {recommendation.take_id}{suffix}")
    console.print(
        "[dim]Metrics are for eliminating technical failures and sorting — "
        "they are not an aesthetic judgement. Pick the final take by ear.[/dim]"
    )


@design.command("freeze")
@click.argument("run_dir", type=click.Path(path_type=Path, file_okay=False))
@click.option("--take", "take_id", required=True)
@click.option("--name", required=True)
@click.option(
    "--as",
    "kind",
    type=click.Choice(["design", "clone"]),
    default="design",
    show_default=True,
)
@click.option("--confirm-transcript", is_flag=True)
@click.option("--allow-long-ref", is_flag=True)
@click.option("--force", is_flag=True)
@click.option("--tag", "tags", multiple=True)
@with_error_handling
def design_freeze(
    run_dir: Path,
    take_id: str,
    name: str,
    kind: str,
    confirm_transcript: bool,
    allow_long_ref: bool,
    force: bool,
    tags: tuple[str, ...],
) -> None:
    """Freeze one take into a design or clone voice profile."""
    state = load_run(run_dir)
    manifest = state.manifest
    plan = find_plan(state, take_id)
    status, reason = candidate_status(run_dir, plan, manifest)
    if status != "complete":
        raise IntegrityError(f"take {take_id} is {status}: {reason}")

    wav_path = take_wav_path(run_dir, take_id)
    recipe = read_recipe(recipe_path_for(wav_path))
    if recipe.finish_reason == "length" or any(
        "truncated" in warning for warning in recipe.warnings
    ):
        raise IntegrityError(
            f"take {take_id} reached max_new_tokens and may be truncated",
            hint="regenerate with a larger --max-new-tokens before freezing",
        )

    audio, sample_rate = read_wav(wav_path)
    seconds = len(audio) / sample_rate if sample_rate else 0.0
    if seconds < 1.0:
        raise IntegrityError(f"take {take_id} is too short to freeze ({seconds:.2f}s)")

    store = VoiceStore()
    snapshot = manifest.design
    identity = IdentityBlock(
        structured=snapshot.structured,
        rendered_instruction=snapshot.rendered_instruction,
        template=snapshot.template,
        compiler_version=snapshot.compiler_version,
    )
    profile_kwargs: dict[str, Any] = {
        "voice_id": new_voice_id(),
        "name": name,
        "language": snapshot.language,
        "identity": identity,
        "tags": list(tags),
    }

    if kind == "design":
        profile = VoiceProfile(
            kind="design",
            design=DesignBlock(
                seed=recipe.operation.seed,
                cfg_scale=recipe.operation.cfg_scale,
                instruction=recipe.operation.instruction
                or snapshot.rendered_instruction,
                max_new_tokens=plan.max_new_tokens,
            ),
            reference=None,
            defaults=DefaultsBlock(
                cfg_scale=recipe.operation.cfg_scale, seed=recipe.operation.seed
            ),
            provenance=ProvenanceBlock(
                source="designed",
                source_take_id=plan.take_id,
                source_session_id=manifest.run_id,
                model_digest=recipe.model.digest,
            ),
            **profile_kwargs,
        )
        directory = store.save(
            profile,
            {"preview.wav": wav_path, "design.yaml": run_dir / "design.yaml"},
            force=force,
        )
    else:
        source_text = recipe.input.text
        console.print(Panel(source_text, title="source text — verify word-for-word"))
        console.print(
            f"source text: {len(source_text)} characters · {seconds:.2f}s audio"
        )
        if not confirm_transcript:
            raise UsageError(
                "freezing as clone requires --confirm-transcript",
                hint="check the text above for missing or added words, then re-run",
            )
        if has_inline_event(source_text):
            raise UsageError(
                "source text contains inline event markers; a clone reference must "
                "match the audio exactly"
            )
        if seconds < 6.0:
            console.print(
                f"[yellow]warning[/yellow] reference audio is {seconds:.1f}s; "
                "6–20s is recommended for stable cloning"
            )
        if seconds > 30.0 and not allow_long_ref:
            raise UsageError(
                f"reference audio is {seconds:.1f}s (over 30s)",
                hint="pick a shorter take, or pass --allow-long-ref explicitly",
            )
        with tempfile.TemporaryDirectory() as tmp:
            transcript_path = Path(tmp) / "ref.txt"
            transcript_path.write_text(source_text + "\n", encoding="utf-8")
            profile = VoiceProfile(
                kind="clone",
                design=None,
                reference=ReferenceBlock(
                    audio_sha256=prefixed(sha256_file(wav_path)),
                    transcript_sha256=prefixed(sha256_text(source_text)),
                    transcript_confirmed=True,
                    duration_s=round(seconds, 3),
                    sample_rate=sample_rate,
                ),
                defaults=DefaultsBlock(cfg_scale=1.0, seed=recipe.operation.seed),
                provenance=ProvenanceBlock(
                    source="designed",
                    source_take_id=plan.take_id,
                    source_session_id=manifest.run_id,
                    model_digest=recipe.model.digest,
                ),
                **profile_kwargs,
            )
            directory = store.save(
                profile,
                {
                    "ref.wav": wav_path,
                    "ref.txt": transcript_path,
                    "preview.wav": wav_path,
                },
                force=force,
            )

    console.print(
        f"[green]frozen[/green] voice [bold]{name}[/bold] as {kind} → {directory}"
    )
    console.print(f"voice_id: {profile.voice_id}")
    if kind == "design":
        console.print(
            "[dim]design profiles replay a recipe; the same instruction may drift "
            "across texts. Freeze --as clone for a stable identity.[/dim]"
        )
    else:
        console.print(
            "[dim]clone profiles use ref.wav + ref.txt as the stable identity anchor.[/dim]"
        )
    print_license_notice(compact=True)


# --------------------------------------------------------------------------- helpers


def _minutes(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    return f"{seconds / 60:.1f} min"


def _md(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    return str(value)


def _flags_text(row: dict[str, Any]) -> str:
    flags = technical_flags(row)
    return ", ".join(flags) if flags else "none"


def _render_markdown(
    manifest: Any,
    rows: list[dict[str, Any]],
    skipped: list[tuple[str, str]],
    warnings: list[str],
    metrics_level: str,
    recommendation: Recommendation | None,
) -> str:
    header = (
        "| take | seed | cfg | cover | duration_s | peak_dBFS | rms_dBFS | clip | "
        "silence | F0_med | HNR | centroid |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|---|\n"
    )
    body = "".join(
        f"| {row['take_id']} | {row['seed']} | {row['cfg_scale']} | {row['cover_index']} | "
        f"{_md(row['duration_s'])} | {_md(row['peak_dbfs'])} | {_md(row['rms_dbfs'])} | "
        f"{_md(row['clipping_ratio'])} | {_md(row['silence_ratio'])} | "
        f"{_md(row['f0_median_hz'])} | {_md(row['hnr_db'])} | "
        f"{_md(row['spectral_centroid_hz'])} |\n"
        for row in rows
    )
    lines = [
        f"# Design report — {manifest.run_id}",
        "",
        f"- takes: {len(rows)} complete, {len(skipped)} skipped",
        f"- metrics: {metrics_level}",
        f"- template: `{manifest.design.template}` (compiler {manifest.design.compiler_version})",
        f"- model digest: `{manifest.model.digest}`",
        "",
    ]
    if recommendation is not None:
        chosen = next(row for row in rows if row["take_id"] == recommendation.take_id)
        lines += [
            "## Recommendation",
            "",
            f"- recommended: `{recommendation.take_id}` (metric-based, not aesthetic)",
            f"- flags: {_flags_text(chosen)}",
            f"- clipping: {_md(chosen['clipping_ratio'])}; "
            f"silence: {_md(chosen['silence_ratio'])}; "
            f"HNR: {_md(chosen['hnr_db'])}",
        ]
        if chosen["inline_event"]:
            lines.append(
                "- inline event in script: clone freeze is rejected; the page "
                "generates an `--as design` command for this take"
            )
        if recommendation.degraded:
            lines.append(
                "- every candidate has a technical flag; this is the least-bad "
                "pick, not a passing take"
            )
        lines.append("")
    lines += [
        "## Instruction",
        "",
        f"> {manifest.design.rendered_instruction}",
        "",
        "## Metrics",
        "",
        "Metrics are for eliminating technical failures and sorting. "
        "They are not an aesthetic judgement; choose the final take by ear.",
        "",
        header + body,
    ]
    if skipped:
        lines += ["## Skipped", ""]
        lines += [f"- {take}: {reason}" for take, reason in skipped]
        lines.append("")
    if warnings:
        lines += ["## Warnings", ""]
        lines += [f"- {warning}" for warning in warnings]
        lines.append("")
    lines += [f"- license: {LICENSE_NOTICE}", ""]
    return "\n".join(lines)


def _page_value(value: Any) -> str:
    return "n/a" if value is None else _md(value)


def _freeze_command(run_dir: Path, take_id: str, name: str, as_kind: str) -> str:
    quoted_run = shlex.quote(str(Path(run_dir).resolve()))
    name_token = shlex.quote(name) if NAME_RE.fullmatch(name) else "YOUR_VOICE_NAME"
    return f"btts design freeze {quoted_run} --take {take_id} --name {name_token} --as {as_kind}"


def _render_audition(
    manifest: Any,
    run_dir: Path,
    rows: list[dict[str, Any]],
    skipped: list[tuple[str, str]],
    warnings: list[str],
    metrics_level: str,
    recommendation: Recommendation | None,
) -> str:
    env = Environment(
        loader=FileSystemLoader(str(RESOURCE_DIR)),
        undefined=StrictUndefined,
        autoescape=True,
    )
    template = env.get_template("audition.html.j2")
    labels = [chr(ord("A") + index) for index in range(len(rows))]
    recommended_id = recommendation.take_id if recommendation else None
    prepared = []
    for label, row in zip(labels, rows, strict=True):
        flags = technical_flags(row)
        # ``--as clone`` rejects inline event markers; fall back to a design
        # profile so the copied command is actually runnable.
        as_kind = "design" if row["inline_event"] else "clone"
        prepared.append(
            {
                "label": label,
                "take_id": row["take_id"],
                "audio": f"../candidates/{row['take_id']}.wav",
                "text": row["text"],
                "flags": flags,
                "flag_text": ", ".join(flags) if flags else "none",
                "inline_event": row["inline_event"],
                "recommended": row["take_id"] == recommended_id,
                "freeze_command": _freeze_command(run_dir, row["take_id"], manifest.name, as_kind),
                "duration_s": _md(row["duration_s"]),
                "peak_dbfs": _page_value(row["peak_dbfs"]),
                "rms_dbfs": _page_value(row["rms_dbfs"]),
                "clipping_ratio": _page_value(row["clipping_ratio"]),
                "silence_ratio": _page_value(row["silence_ratio"]),
                "f0_median_hz": _page_value(row["f0_median_hz"]),
                "voiced_ratio": _page_value(row["voiced_ratio"]),
                "hnr_db": _page_value(row["hnr_db"]),
                "spectral_centroid_hz": _page_value(row["spectral_centroid_hz"]),
                "seed": row["seed"],
                "cfg_scale": row["cfg_scale"],
            }
        )
    payload = {
        "recommended": recommended_id,
        "degraded": bool(recommendation and recommendation.degraded),
        "candidates": [
            {
                "take_id": candidate["take_id"],
                "label": candidate["label"],
                "text": candidate["text"],
                "freeze_command": candidate["freeze_command"],
            }
            for candidate in prepared
        ],
    }
    return template.render(
        run_id=manifest.run_id,
        name=manifest.name,
        instruction=manifest.design.rendered_instruction,
        template_name=manifest.design.template,
        model_digest=manifest.model.digest,
        metrics_level=metrics_level,
        candidates=prepared,
        payload=payload,
        skipped=skipped,
        warnings=warnings,
        license_notice=LICENSE_NOTICE,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def _print_metric_table(rows: list[dict[str, Any]], metrics_level: str) -> None:
    table = Table(title=f"candidate metrics ({metrics_level})", show_header=True)
    for column in ("take", "dur", "peak", "clip", "silence"):
        table.add_column(column, justify="right")
    if metrics_level == "full":
        for column in ("F0", "HNR", "centroid"):
            table.add_column(column, justify="right")
    for row in rows:
        cells = [
            row["take_id"],
            _md(row["duration_s"]),
            _md(row["peak_dbfs"]),
            _md(row["clipping_ratio"]),
            _md(row["silence_ratio"]),
        ]
        if metrics_level == "full":
            cells += [
                _md(row["f0_median_hz"]),
                _md(row["hnr_db"]),
                _md(row["spectral_centroid_hz"]),
            ]
        table.add_row(*cells)
    console.print(table)
