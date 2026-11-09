"""``btts voice``: add, list, show, rename, tag and delete local profiles."""

from __future__ import annotations

import shlex
import sys
import tempfile
from pathlib import Path

import click
from rich.markup import escape
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
    normalize_tags,
)
from btts_cli.domain.voice_export import export_voices
from btts_cli.domain.voice_filter import (
    MatchMode,
    VoiceFilter,
    is_addressable_name,
    normalize_query,
)
from btts_cli.output.console import console, err_console, print_license_notice
from btts_cli.output.errors import IntegrityError, UsageError
from btts_cli.output.json import json_dumps


def _is_interactive() -> bool:
    return bool(sys.stdin.isatty() and sys.stdout.isatty())


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
        tags=normalize_tags(tags),
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


def _voice_list_options(func):
    """Shared options for ``voice list`` and its ``voice filter`` alias."""
    func = click.argument("query", required=False)(func)
    func = click.option(
        "--match",
        "match_mode",
        type=click.Choice(["fuzzy", "prefix", "exact"]),
        default="fuzzy",
        show_default=True,
        help=(
            "Name match: fuzzy = case-insensitive substring; prefix = "
            "case-insensitive startswith; exact = case-insensitive full name."
        ),
    )(func)
    func = click.option(
        "--tag",
        "tags",
        multiple=True,
        metavar="TAG",
        help="Require every given tag (repeatable, AND).",
    )(func)
    func = click.option(
        "--kind",
        type=click.Choice(["design", "clone"]),
        default=None,
        help="Only this profile kind.",
    )(func)
    func = click.option(
        "--language",
        type=click.Choice(["zh", "en"]),
        default=None,
        help="Only this language.",
    )(func)
    func = click.option(
        "--names-only",
        is_flag=True,
        help="Print matching names one per line for scripting.",
    )(func)
    func = click.option(
        "--json",
        "as_json",
        is_flag=True,
        help="Print one JSON document on stdout (no table).",
    )(func)
    return func


def _voice_cfg(profile: VoiceProfile) -> float:
    return profile.design.cfg_scale if profile.design else profile.defaults.cfg_scale


def _display_text(value: str) -> str:
    """Make store-derived text safe for terminal/table output.

    Tolerant scans accept hand-edited profiles, so metadata can contain lone
    surrogates (undecodable filesystem bytes) or C0/C1 control characters.
    Replace the former and show the latter as visible escapes so a corrupt
    profile cannot inject ANSI sequences or break the terminal encoding.
    Other format characters (for example the ZWJ in emoji) are preserved.
    """
    safe = value.encode("utf-8", "replace").decode("utf-8")
    return "".join(
        f"\\x{ord(ch):02x}"
        if ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F
        else ch
        for ch in safe
    )


def _voice_summary(profile: VoiceProfile) -> dict[str, object]:
    return {
        "name": profile.name,
        "kind": profile.kind,
        "language": profile.language,
        "voice_id": profile.voice_id,
        "cfg_scale": _voice_cfg(profile),
        "tags": list(profile.tags),
    }


def _print_voice_table(profiles: list[VoiceProfile]) -> None:
    table = Table(title="voices", show_header=True)
    for column in ("name", "kind", "language", "voice_id", "cfg", "tags"):
        table.add_column(column)
    for profile in profiles:
        table.add_row(
            escape(_display_text(profile.name)),
            escape(_display_text(profile.kind)),
            escape(_display_text(profile.language)),
            escape(_display_text(profile.voice_id)),
            f"{_voice_cfg(profile):g}",
            escape(_display_text(", ".join(profile.tags))),
        )
    console.print(table)


def _scan_warnings(
    tombstones: list[Path], mismatches: list[tuple[str, str]]
) -> list[str]:
    warnings = [f"leftover delete tombstone: {tombstone}" for tombstone in tombstones]
    for directory_name, profile_name in mismatches:
        warnings.append(
            f"voice directory {directory_name!r} contains profile name "
            f"{profile_name!r}; re-run `btts voice rename "
            f"{shlex.quote(directory_name)} {shlex.quote(profile_name)}` "
            "to repair"
        )
    return warnings


def _emit_warnings(warnings: list[str]) -> None:
    for warning in warnings:
        err_console.print(
            f"[yellow]warning[/yellow] {escape(_display_text(warning))}"
        )


def _is_symlinked_voice(store: VoiceStore, name: str) -> bool:
    directory = store.path_for(name)
    return directory.is_symlink() or (directory / "profile.json").is_symlink()


def _run_voice_list(
    *,
    query: str | None,
    match_mode: MatchMode,
    tags: tuple[str, ...],
    kind: str | None,
    language: str | None,
    names_only: bool,
    as_json: bool,
) -> None:
    if names_only and as_json:
        raise UsageError("--names-only and --json are mutually exclusive")

    filter_spec = VoiceFilter(
        query=normalize_query(query),
        match=match_mode,
        tags=tuple(normalize_tags(tags)),
        kind=kind,
        language=language,
    )
    store = VoiceStore()
    profiles, tombstones, mismatches = store.scan()
    warnings = _scan_warnings(tombstones, mismatches)
    matched = [profile for profile in profiles if filter_spec.matches(profile)]

    if names_only:
        for profile in matched:
            if not is_addressable_name(profile.name):
                warnings.append(
                    f"skipped unaddressable voice directory name: {profile.name!r}"
                )
                continue
            if _is_symlinked_voice(store, profile.name):
                warnings.append(
                    f"skipped symlinked voice (strict commands refuse it): "
                    f"{profile.name!r}"
                )
                continue
            click.echo(profile.name)
        _emit_warnings(warnings)
        return

    for profile in matched:
        if not is_addressable_name(profile.name):
            warnings.append(
                f"voice directory name is not addressable: {profile.name!r}"
            )
        elif _is_symlinked_voice(store, profile.name):
            warnings.append(
                f"voice directory is a symlink (strict commands refuse it): "
                f"{profile.name!r}"
            )

    if as_json:
        payload = {
            "schema_version": 1,
            "kind": "btts-voice-list",
            "query": filter_spec.query,
            "match": filter_spec.match,
            "filters": {
                "tags": list(filter_spec.tags),
                "kind": filter_spec.kind,
                "language": filter_spec.language,
            },
            "count": len(matched),
            "voices": [_voice_summary(profile) for profile in matched],
            "warnings": warnings,
        }
        _emit_warnings(warnings)
        click.echo(json_dumps(payload), nl=False)
        return

    _emit_warnings(warnings)
    if not matched:
        if filter_spec.has_criteria:
            console.print("no matching voices.")
            console.print(
                f"[dim]store: {escape(_display_text(str(store.root)))}[/dim]"
            )
        else:
            console.print("no voices yet.")
            console.print(
                "add one with: btts voice add NAME --ref REF.wav --text REF.txt "
                "--confirm-transcript"
            )
        return
    _print_voice_table(matched)
    console.print(f"[dim]store: {escape(_display_text(str(store.root)))}[/dim]")


@voice.command("list")
@_voice_list_options
@with_error_handling
def voice_list(
    query: str | None,
    match_mode: MatchMode,
    tags: tuple[str, ...],
    kind: str | None,
    language: str | None,
    names_only: bool,
    as_json: bool,
) -> None:
    """List or filter local voice profiles.

    An optional QUERY matches the voice name; --tag/--kind/--language filter
    structured fields. All supplied criteria are AND-ed. No match is a
    successful empty result (exit 0).
    """
    _run_voice_list(
        query=query,
        match_mode=match_mode,
        tags=tags,
        kind=kind,
        language=language,
        names_only=names_only,
        as_json=as_json,
    )


@voice.command("filter")
@_voice_list_options
@with_error_handling
def voice_filter(
    query: str | None,
    match_mode: MatchMode,
    tags: tuple[str, ...],
    kind: str | None,
    language: str | None,
    names_only: bool,
    as_json: bool,
) -> None:
    """Filter local voice profiles (alias for ``voice list`` with filters)."""
    _run_voice_list(
        query=query,
        match_mode=match_mode,
        tags=tags,
        kind=kind,
        language=language,
        names_only=names_only,
        as_json=as_json,
    )


@voice.command("show")
@click.argument("name")
@with_error_handling
def voice_show(name: str) -> None:
    """Show one voice profile and its files."""
    store = VoiceStore()
    requested = name.removeprefix("@")
    profile = store.load(requested)
    # After an interrupted rename the directory name and profile.name can
    # disagree. Fail with a typed integrity error and a repair hint instead of
    # listing the missing profile.name path and leaking a traceback.
    if profile.name != requested:
        raise IntegrityError(
            f"voice directory {requested!r} contains profile name {profile.name!r}",
            hint=(
                "re-run `btts voice rename "
                f"{requested} {profile.name}` to repair the store"
            ),
        )
    directory = store.path_for(requested)
    click.echo(json_dumps(profile.model_dump(mode="json")), nl=False)
    console.print(f"\n[dim]directory: {directory}[/dim]")
    try:
        entries = sorted(directory.iterdir())
    except OSError as exc:
        raise IntegrityError(
            f"cannot list voice directory: {directory}: {exc}"
        ) from exc
    for path in entries:
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise IntegrityError(
                f"cannot stat voice file: {path}: {exc}"
            ) from exc
        console.print(f"  {path.name} · {size} bytes")
    print_license_notice()


@voice.command("export")
@click.argument("names", nargs=-1)
@click.option(
    "--all",
    "export_all",
    is_flag=True,
    help="Export every valid voice in the store (cannot be combined with names).",
)
@click.option(
    "--mode",
    type=click.Choice(["full", "wav"]),
    default="full",
    show_default=True,
    help=(
        "full: profiles, preview, license, and design/clone files; "
        "wav: only one <voice>.wav audio sample per voice."
    ),
)
@click.option(
    "-o",
    "--out",
    "out_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Destination directory (or .zip with --zip); never overwritten.",
)
@click.option(
    "--zip",
    "archive",
    is_flag=True,
    help="Write one .zip archive with a single top-level directory.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Print one JSON summary on stdout (no license panel).",
)
@with_error_handling
def voice_export(
    names: tuple[str, ...],
    export_all: bool,
    mode: str,
    out_path: Path | None,
    archive: bool,
    as_json: bool,
) -> None:
    """Export voices as a portable directory or .zip bundle.

    Pass one or more NAME values, or --all. The bundle always includes
    LICENSE.txt, README.txt and a manifest.json with SHA-256 hashes. Existing
    destinations are refused; nothing is written when any selected voice is
    missing, unsafe or inconsistent.
    """
    result = export_voices(
        VoiceStore(),
        names=names,
        export_all=export_all,
        mode=mode,  # type: ignore[arg-type]
        out=out_path,
        archive=archive,
    )
    for warning in result.warnings:
        err_console.print(f"[yellow]warning[/yellow] {warning}")
    if as_json:
        click.echo(json_dumps(result.as_dict()), nl=False)
        return
    console.print(
        f"[green]exported[/green] {len(result.voices)} voice(s) → {result.out}"
    )
    for voice_entry in result.voices:
        voice_bytes = sum(int(entry["bytes"]) for entry in voice_entry["files"])
        console.print(
            f"  {voice_entry['name']} · {voice_entry['kind']} · "
            f"{len(voice_entry['files'])} file(s) · {voice_bytes} bytes"
        )
    console.print(f"total: {result.files} file(s), {result.bytes} bytes")
    console.print(f"manifest sha256: {result.manifest_sha256}")
    if mode == "full":
        console.print(
            "[yellow]warning[/yellow] full exports can contain reference audio, "
            "exact transcripts and profile metadata; share only with consent."
        )
    print_license_notice()


@voice.command("rename")
@click.argument("old_name")
@click.argument("new_name")
@with_error_handling
def voice_rename(old_name: str, new_name: str) -> None:
    """Rename a voice directory and its profile name (never overwrites)."""
    store = VoiceStore()
    directory = store.rename(old_name, new_name)
    console.print(
        f"[green]renamed[/green] voice [bold]{old_name}[/bold] → "
        f"[bold]{new_name}[/bold]"
    )
    console.print(f"directory: {directory}")


@voice.command("tag")
@click.argument("name")
@click.option("--add", "add_tags", multiple=True, metavar="TAG", help="Add tags.")
@click.option(
    "--remove",
    "remove_tags",
    multiple=True,
    metavar="TAG",
    help="Remove tags (missing tags are ignored).",
)
@click.option(
    "--set",
    "set_tags",
    multiple=True,
    metavar="TAG",
    help="Replace all tags (one flag per tag); cannot be combined with --add/--remove.",
)
@click.option(
    "--clear",
    "clear_tags",
    is_flag=True,
    help="Remove all tags before applying --add; cannot be combined with --set/--remove.",
)
@with_error_handling
def voice_tag(
    name: str,
    add_tags: tuple[str, ...],
    remove_tags: tuple[str, ...],
    set_tags: tuple[str, ...],
    clear_tags: bool,
) -> None:
    """List tags, or add, remove, replace or clear them on a voice profile."""
    if clear_tags and (set_tags or remove_tags):
        raise UsageError("--clear cannot be combined with --set or --remove")
    if set_tags and (add_tags or remove_tags):
        raise UsageError("--set cannot be combined with --add or --remove")
    store = VoiceStore()
    if not (add_tags or remove_tags or set_tags or clear_tags):
        tags = store.tags_for(name)
    else:
        tags = store.update_tags(
            name,
            add=add_tags,
            remove=remove_tags,
            replace=set_tags or None,
            clear=clear_tags,
        )
    console.print(f"voice: {name}")
    console.print(f"tags: {', '.join(tags) if tags else '(none)'}")


@voice.command("delete")
@click.argument("name")
@click.option("--yes", is_flag=True, help="Skip the interactive confirmation.")
@with_error_handling
def voice_delete(name: str, yes: bool) -> None:
    """Delete one voice directory.

    An interactive terminal asks for confirmation; a non-interactive session
    refuses unless --yes is given. It never prompts without a TTY.
    """
    store = VoiceStore()
    directory, files, total_bytes = store.summarize(name)
    console.print(f"about to delete voice [bold]{name}[/bold]")
    console.print(f"path: {directory}")
    console.print(f"contents: {files} file(s), {total_bytes} bytes")
    if not yes:
        if not _is_interactive():
            raise UsageError(
                "confirmation is required in a non-interactive session",
                hint="pass --yes to confirm the delete",
            )
        if not click.confirm(f"Delete {directory}?", default=False, err=True):
            raise UsageError("delete cancelled")
    store.delete(name)
    console.print(f"[green]deleted[/green] voice [bold]{name}[/bold]")
