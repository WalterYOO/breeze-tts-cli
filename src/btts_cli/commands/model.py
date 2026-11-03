"""``btts_cli model``: download and verify the Breeze TTS 2 checkpoint."""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

import click
from rich.table import Table

from btts_cli import LICENSE_NOTICE
from btts_cli.commands._common import with_error_handling
from btts_cli.domain.model import (
    DEFAULT_REPO,
    download_model,
    ensure_disk_space,
    get_provider,
    recognizable_partial,
    verify_model,
)
from btts_cli.engine.base import resolve_model_dir
from btts_cli.output.console import console, err_console
from btts_cli.output.errors import EngineError, IntegrityError, UsageError
from btts_cli.output.json import json_dumps


def _is_interactive() -> bool:
    return bool(sys.stdin.isatty() and sys.stdout.isatty())


def _gb(value: int) -> str:
    return f"{value / 1_000_000_000:.2f} GB"


def _emit_plan(
    *, source: str, repo: str, revision: str, files: int, total: int, as_json: bool
) -> None:
    target_console = err_console if as_json else console
    target_console.print(f"source:   {source}")
    target_console.print(f"repo:     {repo}")
    target_console.print(f"revision: {revision}")
    target_console.print(f"files:    {files}")
    target_console.print(f"size:     {_gb(total)}")
    target_console.print(f"license:  {LICENSE_NOTICE}")


@click.group("model")
def model() -> None:
    """Download and verify the model checkpoint."""


@model.command("download")
@click.option(
    "--source",
    type=click.Choice(["huggingface", "modelscope"]),
    default="huggingface",
    show_default=True,
    help="Model mirror to download from.",
)
@click.option("--repo", default=DEFAULT_REPO, show_default=True, help="Repository ID.")
@click.option("--revision", default=None, help="Branch, tag or commit.")
@click.option(
    "--to",
    "target_dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Destination directory (default: $BTTS_MODEL_DIR or ./ms-models/Breeze-TTS-2).",
)
@click.option(
    "--force",
    is_flag=True,
    help="Allow a non-empty directory; only same-name model files are overwritten.",
)
@click.option("--yes", is_flag=True, help="Skip the interactive confirmation.")
@click.option("--json", "as_json", is_flag=True, help="Emit one machine-readable JSON document.")
@with_error_handling
def model_download(
    source: str,
    repo: str,
    revision: str | None,
    target_dir: Path | None,
    force: bool,
    yes: bool,
    as_json: bool,
) -> None:
    """Download (or resume) the model files and write a completion marker."""
    if not repo.strip():
        raise UsageError("--repo must not be empty")
    if revision is not None and not revision.strip():
        raise UsageError("--revision must not be empty")

    target = resolve_model_dir(target_dir)
    try:
        is_symlink = target.is_symlink()
        exists = target.exists()
    except OSError as exc:
        raise EngineError(f"cannot inspect model directory {target}: {exc}") from exc
    if is_symlink:
        raise IntegrityError(
            f"model directory must not be a symlink: {target}",
            hint="choose a real directory and retry",
        )
    if exists and not target.is_dir():
        raise UsageError(f"model path is not a directory: {target}")

    provider = get_provider(source)
    resolved_revision = revision or provider.default_revision

    is_non_empty = target.is_dir() and any(target.iterdir())
    if (
        is_non_empty
        and not force
        and not recognizable_partial(target, source, repo, resolved_revision)
    ):
        raise UsageError(
            f"model directory is not empty and is not a recognizable download: {target}",
            hint="pass --force to overwrite same-name model files; other files are kept",
        )

    if not yes and not _is_interactive():
        raise UsageError(
            "confirmation is required in a non-interactive session",
            hint="pass --yes to confirm the download",
        )

    # Nothing above this point touches the network. Provider discovery and
    # third-party progress output are kept away from stdout in JSON mode.
    try:
        with contextlib.redirect_stdout(sys.stderr):
            files = provider.list_files(repo, resolved_revision)
    except OSError as exc:
        raise EngineError(f"cannot list model files: {exc}") from exc
    total_bytes = sum(item.size for item in files)

    _emit_plan(
        source=provider.name,
        repo=repo,
        revision=resolved_revision,
        files=len(files),
        total=total_bytes,
        as_json=as_json,
    )
    if not yes:
        if not click.confirm(
            f"Download {_gb(total_bytes)} into {target}?", default=False, err=True
        ):
            raise UsageError("download cancelled")

    ensure_disk_space(target, total_bytes)

    def progress(message: str) -> None:
        err_console.print(message, style="dim", highlight=False, markup=False)

    try:
        with contextlib.redirect_stdout(sys.stderr):
            result = download_model(
                provider=provider,
                repo=repo,
                revision=resolved_revision,
                target=target,
                files=files,
                progress=progress,
            )
    except OSError as exc:
        raise EngineError(f"model download failed: {exc}") from exc

    if as_json:
        click.echo(json_dumps(result.to_dict()), nl=False)
        return
    console.print(f"[green]model ready[/green] {result.model_dir}")
    console.print(
        f"source {result.source} · {result.repo} @ {result.revision}; "
        f"{result.files} files, {_gb(result.total_bytes)}; "
        f"skipped {result.skipped}, verified {result.verified}"
    )
    console.print(f"[dim]{LICENSE_NOTICE}[/dim]")


@model.command("verify")
@click.option(
    "--model",
    "model_dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Checkpoint directory (default: $BTTS_MODEL_DIR or ./ms-models/Breeze-TTS-2).",
)
@click.option(
    "--deep",
    is_flag=True,
    help="Recompute SHA-256 for every file recorded in the completion marker.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit one machine-readable JSON document.")
@with_error_handling
def model_verify(model_dir: Path | None, deep: bool, as_json: bool) -> None:
    """Check required files, sizes and, with --deep, hashes."""
    target = resolve_model_dir(model_dir)
    if target.exists() and not target.is_dir():
        raise UsageError(f"model path is not a directory: {target}")
    try:
        result = verify_model(target, deep=deep)
    except OSError as exc:
        raise EngineError(f"cannot verify model directory: {exc}") from exc

    if as_json:
        click.echo(json_dumps(result.to_dict()), nl=False)
    else:
        table = Table(title="btts_cli model verify", show_header=False, box=None)
        table.add_column("key", style="bold")
        table.add_column("value")
        table.add_row("model", result.model_dir)
        table.add_row("complete", "yes" if result.complete else "[red]no[/red]")
        table.add_row("files checked", str(result.files_checked))
        table.add_row("deep", "yes" if result.deep else "no")
        table.add_row("digest", result.digest or "unknown")
        if result.source is not None:
            table.add_row(
                "download",
                f"{result.source} · {result.repo} @ {result.revision}",
            )
            table.add_row("downloaded", result.downloaded_at or "unknown")
        if result.missing:
            table.add_row("missing", ", ".join(result.missing))
        console.print(table)
        for warning in result.warnings:
            console.print(f"[yellow]warning[/yellow] {warning}")

    if not result.complete:
        detail = ", ".join(result.missing) if result.missing else "see warnings"
        raise IntegrityError(
            f"model verification failed: {detail}",
            hint="rerun model download or pass --model with a complete checkpoint",
        )
