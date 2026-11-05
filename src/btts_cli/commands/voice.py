"""``btts voice``: add, list and show local profiles."""

from __future__ import annotations

import tempfile
from pathlib import Path

import click
from rich.panel import Panel
from rich.table import Table

from btts_cli.audio.hash import prefixed, sha256_file, sha256_text
from btts_cli.audio.io import read_wav
from btts_cli.commands._common import with_error_handling
from btts_cli.domain.design import has_inline_event
from btts_cli.domain.voice import (
    DefaultsBlock,
    IdentityBlock,
    ProvenanceBlock,
    ReferenceBlock,
    VoiceProfile,
    VoiceStore,
    new_voice_id,
)
from btts_cli.output.console import console, print_license_notice
from btts_cli.output.errors import UsageError
from btts_cli.output.json import json_dumps


@click.group("voice")
def voice() -> None:
    """Manage local voice profiles."""


@voice.command("add")
@click.argument("name")
@click.option(
    "--ref",
    "ref_path",
    required=True,
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    help="Authorized reference WAV (6–20s recommended).",
)
@click.option(
    "--text",
    "text_file",
    required=True,
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    help="Exact transcript of the reference audio.",
)
@click.option(
    "--confirm-transcript",
    is_flag=True,
    help="Confirm the transcript matches the reference word-for-word.",
)
@click.option("--language", type=click.Choice(["zh", "en"]), default="zh")
@click.option("--allow-long-ref", is_flag=True)
@click.option("--force", is_flag=True)
@click.option("--tag", "tags", multiple=True)
@with_error_handling
def voice_add(
    name: str,
    ref_path: Path,
    text_file: Path,
    confirm_transcript: bool,
    language: str,
    allow_long_ref: bool,
    force: bool,
    tags: tuple[str, ...],
) -> None:
    """Add a clone profile from authorized reference audio + exact transcript."""
    transcript = text_file.read_text(encoding="utf-8").rstrip("\n")
    if not transcript.strip():
        raise UsageError(f"transcript file is empty: {text_file.name}")
    if has_inline_event(transcript):
        raise UsageError(
            "reference transcript contains inline event markers; it must match the "
            "audio exactly"
        )
    if not confirm_transcript:
        console.print(Panel(transcript, title="reference transcript"))
        raise UsageError(
            "adding a clone profile requires --confirm-transcript",
            hint="verify the transcript above word-for-word, then re-run",
        )

    audio, sample_rate = read_wav(ref_path)
    seconds = len(audio) / sample_rate if sample_rate else 0.0
    if seconds < 1.0:
        raise UsageError(f"reference audio is too short ({seconds:.2f}s)")
    if seconds < 6.0:
        console.print(
            f"[yellow]warning[/yellow] reference audio is {seconds:.1f}s; "
            "6–20s is recommended for stable cloning"
        )
    if seconds > 30.0 and not allow_long_ref:
        raise UsageError(
            f"reference audio is {seconds:.1f}s (over 30s)",
            hint="trim it, or pass --allow-long-ref explicitly",
        )

    store = VoiceStore()
    profile = VoiceProfile(
        voice_id=new_voice_id(),
        name=name,
        kind="clone",
        language=language,
        identity=IdentityBlock(structured={}, rendered_instruction=""),
        design=None,
        reference=ReferenceBlock(
            audio_sha256=prefixed(sha256_file(ref_path)),
            transcript_sha256=prefixed(sha256_text(transcript)),
            transcript_confirmed=True,
            duration_s=round(seconds, 3),
            sample_rate=sample_rate,
        ),
        defaults=DefaultsBlock(cfg_scale=1.0, seed=19),
        provenance=ProvenanceBlock(source="reference_audio"),
        tags=list(tags),
    )
    with tempfile.TemporaryDirectory() as tmp:
        transcript_path = Path(tmp) / "ref.txt"
        transcript_path.write_text(transcript + "\n", encoding="utf-8")
        directory = store.save(
            profile,
            {
                "ref.wav": ref_path,
                "ref.txt": transcript_path,
                "preview.wav": ref_path,
            },
            force=force,
        )

    console.print(f"[green]added[/green] clone voice [bold]{name}[/bold] → {directory}")
    console.print(f"voice_id: {profile.voice_id}")
    console.print(f"reference: {seconds:.2f}s · {sample_rate} Hz")
    print_license_notice()


@voice.command("list")
@with_error_handling
def voice_list() -> None:
    """List local voice profiles."""
    store = VoiceStore()
    profiles = store.list()
    if not profiles:
        console.print("no voices yet.")
        console.print(
            "add one with: btts voice add NAME --ref REF.wav --text REF.txt "
            "--confirm-transcript"
        )
        return
    table = Table(title="voices", show_header=True)
    for column in ("name", "kind", "language", "voice_id", "cfg", "tags"):
        table.add_column(column)
    for profile in profiles:
        cfg = profile.design.cfg_scale if profile.design else profile.defaults.cfg_scale
        table.add_row(
            profile.name,
            profile.kind,
            profile.language,
            profile.voice_id,
            f"{cfg:g}",
            ", ".join(profile.tags),
        )
    console.print(table)
    console.print(f"[dim]store: {store.root}[/dim]")


@voice.command("show")
@click.argument("name")
@with_error_handling
def voice_show(name: str) -> None:
    """Show one voice profile and its files."""
    store = VoiceStore()
    profile = store.load(name.removeprefix("@"))
    click.echo(json_dumps(profile.model_dump(mode="json")), nl=False)
    directory = store.path_for(profile.name)
    console.print(f"\n[dim]directory: {directory}[/dim]")
    for path in sorted(directory.iterdir()):
        console.print(f"  {path.name} · {path.stat().st_size} bytes")
    print_license_notice()
