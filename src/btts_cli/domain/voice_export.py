"""Portable export bundles for the local voice store.

``--mode full`` copies the complete voice directory set (profile, license,
preview, and the design recipe or clone reference/transcript). ``--mode wav``
exports only one audible ``<voice>.wav`` per voice. Both modes carry a root
``LICENSE.txt``, a short ``README.txt`` and a ``manifest.json`` with SHA-256
hashes for every exported file.

The destination is never overwritten: the bundle is materialised in a hidden
sibling and published with a no-replace rename. Store validation is strict on
purpose (unlike ``VoiceStore.scan()``), because a partial backup that claims to
be complete is worse than a failed export.

The export runs under the store lock, which serializes cooperating ``btts``
processes. Every copied file is pinned by device/inode/size and re-checked
while streaming, but the recursive walk is path-based: it is not a
transactional snapshot against a non-cooperating writer swapping a parent
directory mid-export.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import stat
import struct
import tempfile
import time
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from btts_cli import __version__
from btts_cli.audio.hash import prefixed, sha256_bytes, sha256_text
from btts_cli.domain.voice import (
    LICENSE_FILE_TEXT,
    LICENSE_MODEL,
    SECURE_DIR_OPS,
    TOMBSTONE_PREFIX,
    VoiceProfile,
    VoiceStore,
    _open_dir_fd,
    _rename_no_replace,
)
from btts_cli.output.errors import BttsError, IntegrityError, UsageError
from btts_cli.output.json import json_dumps, write_text_atomic

ExportMode = Literal["full", "wav"]
EXPORT_SCHEMA_VERSION = 1
CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class _SourceFile:
    """One regular source file, pinned by identity and size at scan time."""

    rel: str
    path: Path
    dev: int
    ino: int
    size: int
    mode: int
    mtime: float

    @classmethod
    def from_stat(cls, rel: str, path: Path, entry_stat: os.stat_result) -> _SourceFile:
        return cls(
            rel=rel,
            path=path,
            dev=entry_stat.st_dev,
            ino=entry_stat.st_ino,
            size=entry_stat.st_size,
            mode=entry_stat.st_mode,
            mtime=entry_stat.st_mtime,
        )


@dataclass(frozen=True)
class _VoiceSnapshot:
    name: str
    directory: Path
    profile: VoiceProfile
    profile_sha256: str
    preview: _SourceFile
    reference_audio: _SourceFile | None
    reference_transcript: _SourceFile | None
    # Raw-byte hash of the transcript used for validation. Profile hashes cover
    # the text with one trailing newline stripped, so this is the only value
    # that can be compared against the bytes copied later.
    reference_transcript_sha256: str | None
    design_yaml_missing: bool


@dataclass(frozen=True)
class ExportResult:
    out: Path
    archive: bool
    mode: ExportMode
    voices: tuple[dict[str, Any], ...]
    files: int
    bytes: int
    manifest_sha256: str
    warnings: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": EXPORT_SCHEMA_VERSION,
            "kind": "btts-voice-export",
            "out": str(self.out),
            "archive": self.archive,
            "mode": self.mode,
            "voices": list(self.voices),
            "files": self.files,
            "bytes": self.bytes,
            "manifest_sha256": self.manifest_sha256,
            "warnings": list(self.warnings),
        }


# --------------------------------------------------------------------------- public


def export_voices(
    store: VoiceStore,
    *,
    names: Sequence[str],
    export_all: bool,
    mode: ExportMode,
    out: Path | None,
    archive: bool,
) -> ExportResult:
    """Export the selected voices and return a summary.

    Selection and source validation happen under the store lock, before any
    destination is created, so a missing, broken or unsafe voice never leaves a
    partial bundle behind.
    """
    if export_all and names:
        raise UsageError("--all cannot be combined with explicit voice names")
    if not export_all and not names:
        raise UsageError(
            "no voices selected",
            hint="pass one or more voice names, or use --all",
        )
    if mode not in ("full", "wav"):
        raise UsageError(f"unsupported export mode: {mode}")

    destination = _resolve_destination(store, out, archive)
    warnings: list[str] = []
    cleaned = _normalise_names(names)

    with store._store_lock():  # noqa: SLF001 - same package, shared store lock
        selected = (
            _enumerate_store(store, warnings) if export_all else cleaned
        )
        if not selected:
            raise UsageError(
                "no voices to export",
                hint=f"add a voice first, or check the store at {store.root}",
            )
        snapshots = _load_selected(store, selected, warnings)
        _reject_casefold_collisions(snapshots)

        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise UsageError(
                f"cannot create export destination: {destination.parent}: {exc}",
                hint="check the parent path, permissions and free disk space",
            ) from exc

        writer: _BundleWriter
        if archive:
            writer = _ZipBundle.create(destination)
        else:
            writer = _DirectoryBundle.create(destination)
        try:
            root_entries, voice_entries, files, total_bytes = _materialise(
                writer, snapshots, mode, warnings
            )
            manifest = _build_manifest(
                mode=mode,
                root_entries=root_entries,
                voice_entries=voice_entries,
                warnings=warnings,
            )
            manifest_sha256 = writer.finish(manifest)
            writer.publish()
        except BaseException:
            writer.discard()
            raise

    return ExportResult(
        out=destination,
        archive=archive,
        mode=mode,
        voices=tuple(voice_entries),
        files=files + 1,  # + manifest.json
        bytes=total_bytes + len(json_dumps(manifest).encode("utf-8")),
        manifest_sha256=manifest_sha256,
        warnings=tuple(warnings),
    )


# ----------------------------------------------------------------------- selection


def _normalise_names(names: Sequence[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in names:
        name = str(raw).removeprefix("@")
        if name not in seen:
            seen.add(name)
            cleaned.append(name)
    return cleaned


def _enumerate_store(store: VoiceStore, warnings: list[str]) -> list[str]:
    """Strict ``--all`` enumeration.

    Unlike ``VoiceStore.scan()``, this never silently drops a broken voice: any
    entry that looks like a voice is returned and then validated. Tombstones and
    non-voice directories are skipped with a warning; hidden entries (the store
    lock file) are ignored. An unreadable store entry is an integrity failure,
    never a silent skip.
    """
    try:
        entries = sorted(store.root.iterdir(), key=lambda path: path.name)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise UsageError(
            f"cannot read voice store: {store.root}: {exc}",
            hint="check the directory permissions or BTTS_DATA_DIR",
        ) from exc

    selected: list[str] = []
    for entry in entries:
        name = entry.name
        if name.startswith(TOMBSTONE_PREFIX):
            warnings.append(f"skipped leftover delete tombstone: {name}")
            continue
        if name.startswith("."):
            continue
        try:
            entry_stat = entry.lstat()
        except OSError as exc:
            raise IntegrityError(
                f"cannot inspect voice store entry {name}: {exc}",
                hint="check the voice store permissions; refusing a partial export",
            ) from exc
        if stat.S_ISLNK(entry_stat.st_mode):
            # Let the strict loader reject the symlink instead of hiding it.
            selected.append(name)
            continue
        if not stat.S_ISDIR(entry_stat.st_mode):
            warnings.append(f"skipped non-voice entry: {name}")
            continue
        try:
            (entry / "profile.json").lstat()
        except FileNotFoundError:
            warnings.append(f"skipped non-voice directory: {name}")
            continue
        except OSError as exc:
            raise IntegrityError(
                f"cannot inspect voice directory {name}: {exc}",
                hint="check the voice store permissions; refusing a partial export",
            ) from exc
        selected.append(name)
    return selected


def _load_selected(
    store: VoiceStore, names: Sequence[str], warnings: list[str]
) -> list[_VoiceSnapshot]:
    snapshots: list[_VoiceSnapshot] = []
    failures: list[str] = []
    hints: list[str] = []
    has_integrity_failure = False
    for name in names:
        try:
            snapshot = _load_snapshot(store, name)
        except BttsError as exc:
            failures.append(f"{name}: {exc.message}")
            if exc.hint:
                hints.append(exc.hint)
            has_integrity_failure = has_integrity_failure or isinstance(
                exc, IntegrityError
            )
            continue
        if snapshot.design_yaml_missing:
            warnings.append(
                f"{name}: design.yaml is missing; the replayable recipe is still "
                "in profile.json"
            )
        snapshots.append(snapshot)
    if failures:
        error_type = IntegrityError if has_integrity_failure else UsageError
        hint = "no output was written"
        hint += (
            "; " + "; ".join(hints)
            if hints
            else "; repair or remove the failing voices and retry"
        )
        raise error_type(
            "cannot export selected voices: " + "; ".join(failures),
            hint=hint,
        )
    return snapshots


def _reject_casefold_collisions(snapshots: Sequence[_VoiceSnapshot]) -> None:
    seen: dict[str, str] = {}
    for snapshot in snapshots:
        folded = snapshot.name.casefold()
        other = seen.get(folded)
        if other is not None:
            raise UsageError(
                f"voice names collide on a case-insensitive filesystem: "
                f"{other!r} and {snapshot.name!r}",
                hint="export them separately or rename one voice",
            )
        seen[folded] = snapshot.name


# --------------------------------------------------------------- source validation


def _load_snapshot(store: VoiceStore, name: str) -> _VoiceSnapshot:
    directory = store._voice_dir_or_raise(name)  # noqa: SLF001 - strict store path
    profile_sha256: str
    if SECURE_DIR_OPS:
        with _open_dir_fd(
            directory, nofollow=True, label=f"voice {name!r}"
        ) as directory_fd:
            payload, profile_sha256 = _read_profile_at(directory_fd, name)
    else:
        payload, profile_sha256 = _read_profile_path(
            directory / "profile.json", name
        )
    try:
        profile = VoiceProfile.model_validate(payload)
    except ValueError as exc:
        raise IntegrityError(f"invalid profile for voice {name}") from exc
    if profile.name != name:
        raise IntegrityError(
            f"voice directory {name!r} contains profile name {profile.name!r}",
            hint=f"re-run `btts voice rename {name} {profile.name}` to repair the store",
        )

    preview = _require_regular(directory, "preview.wav", f"preview for voice {name}")
    reference_audio: _SourceFile | None = None
    reference_transcript: _SourceFile | None = None
    reference_transcript_sha256: str | None = None
    if profile.kind == "clone":
        reference = profile.reference
        if reference is None:  # pragma: no cover - model validator enforces this
            raise IntegrityError(f"clone profile {name} has no reference block")
        reference_audio = _require_regular(
            directory, reference.audio, f"reference audio for voice {name}"
        )
        reference_transcript = _require_regular(
            directory, reference.transcript, f"reference transcript for voice {name}"
        )
        _verify_source_hash(
            reference_audio, reference.audio_sha256, "reference audio"
        )
        reference_transcript_sha256 = _verify_transcript_source(
            reference_transcript, reference.transcript_sha256, "reference transcript"
        )
        _verify_source_hash(preview, reference.audio_sha256, "preview audio")

    return _VoiceSnapshot(
        name=name,
        directory=directory,
        profile=profile,
        profile_sha256=profile_sha256,
        preview=preview,
        reference_audio=reference_audio,
        reference_transcript=reference_transcript,
        reference_transcript_sha256=reference_transcript_sha256,
        design_yaml_missing=(
            profile.kind == "design" and not (directory / "design.yaml").is_file()
        ),
    )


def _read_profile_at(directory_fd: int, name: str) -> tuple[Any, str]:
    flags = os.O_RDONLY | os.O_NOFOLLOW
    try:
        file_fd = os.open("profile.json", flags, dir_fd=directory_fd)
    except OSError as exc:
        raise IntegrityError(f"invalid profile for voice {name}") from exc
    try:
        with os.fdopen(file_fd, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        raise IntegrityError(f"invalid profile for voice {name}") from exc
    return _parse_profile(data, name), prefixed(sha256_bytes(data))


def _read_profile_path(path: Path, name: str) -> tuple[Any, str]:
    try:
        entry = path.lstat()
        if stat.S_ISLNK(entry.st_mode) or not stat.S_ISREG(entry.st_mode):
            raise IntegrityError(f"invalid profile for voice {name}")
        data = path.read_bytes()
    except IntegrityError:
        raise
    except OSError as exc:
        raise IntegrityError(f"invalid profile for voice {name}") from exc
    return _parse_profile(data, name), prefixed(sha256_bytes(data))


def _parse_profile(data: bytes, name: str) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise IntegrityError(f"invalid profile for voice {name}") from exc


def _require_regular(directory: Path, relative: str, label: str) -> _SourceFile:
    path = directory / relative
    try:
        entry = path.lstat()
    except OSError as exc:
        raise IntegrityError(f"{label} is missing or unreadable: {relative}") from exc
    if stat.S_ISLNK(entry.st_mode):
        raise IntegrityError(
            f"refusing symlinked {label}: {relative}",
            hint="voice files must be regular files inside the voice directory",
        )
    if not stat.S_ISREG(entry.st_mode):
        raise IntegrityError(f"{label} is not a regular file: {relative}")
    if entry.st_size == 0:
        raise IntegrityError(f"{label} is empty: {relative}")
    return _SourceFile.from_stat(relative, path, entry)


def _read_source(source: _SourceFile) -> bytes:
    fd = _open_source(source)
    try:
        with os.fdopen(fd, "rb") as handle:
            return handle.read()
    except OSError as exc:
        raise IntegrityError(
            f"cannot read voice file {source.rel}: {exc}",
            hint="another process may be modifying the voice store; retry",
        ) from exc


def _verify_source_hash(source: _SourceFile, expected: str, label: str) -> None:
    actual = prefixed(sha256_bytes(_read_source(source)))
    if actual != expected:
        raise IntegrityError(
            f"{label} hash does not match profile.json: {source.rel}",
            hint="the voice store is inconsistent; regenerate or re-add the voice",
        )


def _verify_transcript_source(
    source: _SourceFile, expected: str, label: str
) -> str:
    """Verify the normalized transcript and return its raw-byte hash.

    ``voice add`` and ``say`` hash the text with one trailing newline stripped;
    the exported ``ref.txt`` keeps that newline. Returning the raw hash lets the
    copy step prove it wrote exactly the bytes that were validated.
    """
    data = _read_source(source)
    try:
        # Match ``Path.read_text`` universal-newline translation used by
        # ``voice add``: a Windows-created ref.txt may store CRLF even though
        # the profile hash covers LF-normalized text.
        text = (
            data.decode("utf-8")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .rstrip("\n")
        )
    except UnicodeDecodeError as exc:
        raise IntegrityError(f"{label} is not valid UTF-8: {source.rel}") from exc
    if prefixed(sha256_text(text)) != expected:
        raise IntegrityError(
            f"{label} hash does not match profile.json: {source.rel}",
            hint="the voice store is inconsistent; regenerate or re-add the voice",
        )
    return prefixed(sha256_bytes(data))


def _walk_voice_files(
    snapshot: _VoiceSnapshot, warnings: list[str]
) -> list[_SourceFile]:
    """List every regular file, rejecting symlinks and special files."""
    result: list[_SourceFile] = []
    _walk_voice_dir(snapshot, snapshot.directory, "", result, warnings)
    return result


def _walk_voice_dir(
    snapshot: _VoiceSnapshot,
    directory: Path,
    prefix: str,
    result: list[_SourceFile],
    warnings: list[str],
) -> None:
    try:
        with os.scandir(directory) as iterator:
            entries = sorted(iterator, key=lambda item: item.name)
    except OSError as exc:
        raise IntegrityError(
            f"cannot read voice directory for {snapshot.name}: {exc}"
        ) from exc
    for entry in entries:
        if entry.name.startswith("."):
            warnings.append(
                f"{snapshot.name}: skipped hidden entry {prefix}{entry.name}"
            )
            continue
        if "/" in entry.name or "\\" in entry.name or "\x00" in entry.name:
            raise IntegrityError(
                f"{snapshot.name}: unsafe filename in voice directory: "
                f"{entry.name!r}"
            )
        relative = f"{prefix}{entry.name}"
        try:
            entry_stat = entry.stat(follow_symlinks=False)
        except OSError as exc:
            raise IntegrityError(
                f"cannot stat voice file {relative}: {exc}"
            ) from exc
        if stat.S_ISLNK(entry_stat.st_mode):
            raise IntegrityError(
                f"{snapshot.name}: refusing symlinked file: {relative}",
                hint="voice directories may only contain regular files",
            )
        if stat.S_ISDIR(entry_stat.st_mode):
            _walk_voice_dir(
                snapshot, Path(entry.path), f"{relative}/", result, warnings
            )
        elif stat.S_ISREG(entry_stat.st_mode):
            result.append(
                _SourceFile.from_stat(relative, Path(entry.path), entry_stat)
            )
        else:
            raise IntegrityError(
                f"{snapshot.name}: unsupported special file: {relative}"
            )


# ------------------------------------------------------------------------ materialise


def _materialise(
    writer: _BundleWriter,
    snapshots: Sequence[_VoiceSnapshot],
    mode: ExportMode,
    warnings: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, int]:
    root_entries: list[dict[str, Any]] = []
    voice_entries: list[dict[str, Any]] = []

    root_entries.append(writer.add_text("LICENSE.txt", LICENSE_FILE_TEXT))
    readme = _readme_text(mode, snapshots)
    root_entries.append(writer.add_text("README.txt", readme))
    files = len(root_entries)
    total_bytes = sum(int(entry["bytes"]) for entry in root_entries)

    for snapshot in snapshots:
        if mode == "full":
            file_entries = [
                _add_voice_file(writer, snapshot, source)
                for source in _walk_voice_files(snapshot, warnings)
            ]
            file_entries.sort(key=lambda entry: entry["path"])
            exported = {
                entry["path"].removeprefix(f"{snapshot.name}/")
                for entry in file_entries
            }
            if "LICENSE.txt" not in exported:
                raise IntegrityError(
                    f"voice {snapshot.name} is missing LICENSE.txt",
                    hint="re-add the voice to restore the model notice",
                )
            if "profile.json" not in exported:  # pragma: no cover - validated earlier
                raise IntegrityError(f"voice {snapshot.name} is missing profile.json")
        else:
            reference = snapshot.profile.reference
            entry = writer.add_file(
                f"{snapshot.name}.wav",
                snapshot.preview,
                expected_sha256=(
                    reference.audio_sha256 if reference is not None else None
                ),
            )
            entry["source"] = "preview.wav"
            file_entries = [entry]

        voice_bytes = sum(int(entry["bytes"]) for entry in file_entries)
        files += len(file_entries)
        total_bytes += voice_bytes
        voice_entries.append(
            {
                "name": snapshot.name,
                "voice_id": snapshot.profile.voice_id,
                "kind": snapshot.profile.kind,
                "language": snapshot.profile.language,
                "tags": list(snapshot.profile.tags),
                "profile_sha256": snapshot.profile_sha256,
                "files": file_entries,
            }
        )

    voice_entries.sort(key=lambda entry: entry["name"])
    return root_entries, voice_entries, files, total_bytes


def _add_voice_file(
    writer: _BundleWriter, snapshot: _VoiceSnapshot, source: _SourceFile
) -> dict[str, Any]:
    expected: str | None = None
    if source.rel == "profile.json":
        expected = snapshot.profile_sha256
    elif snapshot.profile.reference is not None:
        reference = snapshot.profile.reference
        if source.rel == reference.audio:
            expected = reference.audio_sha256
        elif source.rel == reference.transcript:
            expected = snapshot.reference_transcript_sha256
        elif source.rel == "preview.wav":
            expected = reference.audio_sha256
    return writer.add_file(
        f"{snapshot.name}/{source.rel}", source, expected_sha256=expected
    )


def _build_manifest(
    *,
    mode: ExportMode,
    root_entries: Sequence[dict[str, Any]],
    voice_entries: Sequence[dict[str, Any]],
    warnings: Sequence[str],
) -> dict[str, Any]:
    return {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "kind": "btts-voice-export",
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cli_version": __version__,
        "mode": mode,
        "license": {
            "model": LICENSE_MODEL,
            "cli": "Apache-2.0",
            "notice_file": "LICENSE.txt",
        },
        "root_files": sorted(root_entries, key=lambda entry: entry["path"]),
        "voices": list(voice_entries),
        "warnings": list(warnings),
    }


def _readme_text(mode: ExportMode, snapshots: Sequence[_VoiceSnapshot]) -> str:
    lines = [
        "btts voice export bundle",
        "",
        f"mode: {mode}",
        "voices: " + ", ".join(sorted(snapshot.name for snapshot in snapshots)),
        f"generated_by: breeze-tts-cli {__version__}",
        "",
        "Layout:",
        "  manifest.json  voice metadata and SHA-256 hashes",
        "  LICENSE.txt    Breeze TTS 2 self-hosted output notice",
        "  README.txt     this file",
    ]
    if mode == "full":
        lines.extend(
            [
                "  <voice>/       profile.json, LICENSE.txt, preview.wav, and the",
                "                 design recipe or clone reference/transcript",
                "",
                "Full exports can contain reference audio, exact transcripts and",
                "profile metadata. Treat the bundle as personal data and share it",
                "only with the speaker's consent.",
            ]
        )
    else:
        lines.append("  <voice>.wav    audible preview sample for each voice")
        lines.extend(
            [
                "",
                "WAV exports contain only audible preview samples; profiles and",
                "reference data are not included. manifest.json still records each",
                "voice's name, voice_id, kind, language, tags and profile hash.",
            ]
        )
    lines.extend(["", "See LICENSE.txt for the model license and usage terms.", ""])
    return "\n".join(lines)


# -------------------------------------------------------------------------- bundles


class _BundleWriter:
    def add_text(self, relpath: str, text: str) -> dict[str, Any]:
        raise NotImplementedError

    def add_file(
        self,
        relpath: str,
        source: _SourceFile,
        *,
        expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def finish(self, manifest: dict[str, Any]) -> str:
        raise NotImplementedError

    def publish(self) -> None:
        raise NotImplementedError

    def discard(self) -> None:
        raise NotImplementedError


class _DirectoryBundle(_BundleWriter):
    def __init__(self, root: Path, destination: Path):
        self.root = root
        self.destination = destination

    @classmethod
    def create(cls, destination: Path) -> _DirectoryBundle:
        try:
            root = Path(
                tempfile.mkdtemp(
                    prefix=f".{destination.name}.btts-",
                    suffix=".tmp",
                    dir=destination.parent,
                )
            )
        except OSError as exc:
            raise UsageError(
                f"cannot create export directory: {destination.parent}: {exc}",
                hint="check permissions and free disk space",
            ) from exc
        return cls(root, destination)

    def _path(self, relpath: str) -> Path:
        return self.root.joinpath(*relpath.split("/"))

    def add_text(self, relpath: str, text: str) -> dict[str, Any]:
        path = self._path(relpath)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            write_text_atomic(path, text)
        except OSError as exc:
            raise UsageError(f"cannot write export file {relpath}: {exc}") from exc
        data = text.encode("utf-8")
        return {
            "path": relpath,
            "sha256": prefixed(sha256_bytes(data)),
            "bytes": len(data),
        }

    def add_file(
        self,
        relpath: str,
        source: _SourceFile,
        *,
        expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        path = self._path(relpath)
        digest = hashlib.sha256()
        written = 0
        source_fd = -1
        destination_fd = -1

        def cleanup() -> None:
            for fd in (source_fd, destination_fd):
                if fd >= 0:
                    with contextlib.suppress(OSError):
                        os.close(fd)
            with contextlib.suppress(OSError):
                path.unlink()

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            source_fd = _open_source(source)
            destination_fd = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            reader = os.fdopen(source_fd, "rb")
            source_fd = -1
            writer = os.fdopen(destination_fd, "wb")
            destination_fd = -1
            with reader, writer:
                while chunk := reader.read(CHUNK_SIZE):
                    digest.update(chunk)
                    writer.write(chunk)
                    written += len(chunk)
                writer.flush()
                os.fsync(writer.fileno())
                os.fchmod(writer.fileno(), stat.S_IMODE(source.mode))
        except OSError as exc:
            cleanup()
            raise UsageError(
                f"cannot write export file {relpath}: {exc}",
                hint="check free disk space and destination permissions",
            ) from exc
        except BaseException:
            cleanup()
            raise
        if written != source.size:
            raise IntegrityError(
                f"voice file changed while exporting: {source.rel}",
                hint="another process modified the voice store; retry the export",
            )
        return _file_entry(relpath, digest.hexdigest(), written, expected_sha256)

    def finish(self, manifest: dict[str, Any]) -> str:
        text = json_dumps(manifest)
        self.add_text("manifest.json", text)
        _fsync_directory(self.root)
        return prefixed(sha256_text(text))

    def publish(self) -> None:
        try:
            renamed = _rename_no_replace(self.root, self.destination)
        except OSError as exc:
            raise UsageError(
                f"cannot publish export: {self.destination}: {exc}",
                hint="check the destination path and permissions",
            ) from exc
        if not renamed:
            raise UsageError(
                f"export destination already exists: {self.destination}",
                hint="choose a new --out path; exports never overwrite",
            )
        _fsync_directory(self.destination.parent)

    def discard(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class _ZipBundle(_BundleWriter):
    def __init__(self, temp: Path, destination: Path, archive: zipfile.ZipFile):
        self.temp = temp
        self.destination = destination
        self.archive = archive
        self.stem = destination.stem

    @classmethod
    def create(cls, destination: Path) -> _ZipBundle:
        try:
            fd, name = tempfile.mkstemp(
                prefix=f".{destination.name}.btts-",
                suffix=".tmp",
                dir=destination.parent,
            )
        except OSError as exc:
            raise UsageError(
                f"cannot create export archive: {destination.parent}: {exc}",
                hint="check permissions and free disk space",
            ) from exc
        os.close(fd)
        temp = Path(name)
        archive: zipfile.ZipFile | None = None
        try:
            archive = zipfile.ZipFile(
                temp, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
            )
            bundle = cls(temp, destination, archive)
            bundle._add_directory_entry()
            return bundle
        except BaseException as exc:
            if archive is not None:
                with contextlib.suppress(Exception):
                    archive.close()
            with contextlib.suppress(OSError):
                temp.unlink(missing_ok=True)
            if isinstance(exc, OSError):
                raise UsageError(
                    f"cannot create export archive: {destination}: {exc}",
                    hint="check permissions and free disk space",
                ) from exc
            raise

    def _arcname(self, relpath: str) -> str:
        return f"{self.stem}/{relpath}"

    def _add_directory_entry(self) -> None:
        info = zipfile.ZipInfo(f"{self.stem}/")
        info.external_attr = (stat.S_IFDIR | 0o755) << 16
        self.archive.writestr(info, b"")

    def add_text(self, relpath: str, text: str) -> dict[str, Any]:
        data = text.encode("utf-8")
        info = zipfile.ZipInfo(
            self._arcname(relpath), date_time=_zip_time(time.time())
        )
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = (stat.S_IFREG | 0o600) << 16
        try:
            self.archive.writestr(info, data)
        except (OSError, ValueError, struct.error, zipfile.BadZipFile) as exc:
            raise UsageError(f"cannot write export file {relpath}: {exc}") from exc
        return {
            "path": relpath,
            "sha256": prefixed(sha256_bytes(data)),
            "bytes": len(data),
        }

    def add_file(
        self,
        relpath: str,
        source: _SourceFile,
        *,
        expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        info = zipfile.ZipInfo(
            self._arcname(relpath),
            date_time=_zip_time(source.mtime),
        )
        info.compress_type = _compression_for(relpath)
        info.external_attr = (stat.S_IFREG | stat.S_IMODE(source.mode)) << 16
        digest = hashlib.sha256()
        written = 0
        source_fd = _open_source(source)
        try:
            reader = os.fdopen(source_fd, "rb")
            source_fd = -1
            with reader, self.archive.open(info, "w") as writer:
                while chunk := reader.read(CHUNK_SIZE):
                    digest.update(chunk)
                    writer.write(chunk)
                    written += len(chunk)
        except (OSError, ValueError, struct.error, zipfile.BadZipFile) as exc:
            if source_fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(source_fd)
            raise UsageError(f"cannot write export file {relpath}: {exc}") from exc
        if written != source.size:
            raise IntegrityError(
                f"voice file changed while exporting: {source.rel}",
                hint="another process modified the voice store; retry the export",
            )
        return _file_entry(relpath, digest.hexdigest(), written, expected_sha256)

    def finish(self, manifest: dict[str, Any]) -> str:
        text = json_dumps(manifest)
        self.add_text("manifest.json", text)
        try:
            self.archive.close()
            with self.temp.open("rb+") as handle:
                os.fsync(handle.fileno())
        except (OSError, ValueError, struct.error, zipfile.BadZipFile) as exc:
            raise UsageError(f"cannot finalize export archive: {exc}") from exc
        return prefixed(sha256_text(text))

    def publish(self) -> None:
        try:
            renamed = _rename_no_replace(self.temp, self.destination)
        except OSError as exc:
            raise UsageError(
                f"cannot publish export: {self.destination}: {exc}",
                hint="check the destination path and permissions",
            ) from exc
        if not renamed:
            raise UsageError(
                f"export destination already exists: {self.destination}",
                hint="choose a new --out path; exports never overwrite",
            )
        _fsync_directory(self.destination.parent)

    def discard(self) -> None:
        with contextlib.suppress(Exception):
            self.archive.close()
        with contextlib.suppress(OSError):
            self.temp.unlink(missing_ok=True)


def _file_entry(
    relpath: str,
    digest_hex: str,
    written: int,
    expected_sha256: str | None,
) -> dict[str, Any]:
    actual = prefixed(digest_hex)
    if expected_sha256 is not None and actual != expected_sha256:
        raise IntegrityError(
            f"file changed while exporting: {relpath}",
            hint="another process modified the voice store; retry the export",
        )
    return {"path": relpath, "sha256": actual, "bytes": written}


def _open_source(source: _SourceFile) -> int:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(source.path, flags)
    except OSError as exc:
        raise IntegrityError(
            f"cannot read voice file {source.rel}: {exc}",
            hint="another process may be modifying the voice store; retry",
        ) from exc
    try:
        entry = os.fstat(fd)
        if not stat.S_ISREG(entry.st_mode):
            raise IntegrityError(f"voice file is no longer regular: {source.rel}")
        if (entry.st_dev, entry.st_ino, entry.st_size) != (
            source.dev,
            source.ino,
            source.size,
        ):
            raise IntegrityError(
                f"voice file changed during export: {source.rel}",
                hint="another process modified the voice store; retry the export",
            )
    except BaseException:
        os.close(fd)
        raise
    return fd


# ------------------------------------------------------------------------ destination


def _resolve_destination(
    store: VoiceStore, out: Path | None, archive: bool
) -> Path:
    if out is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = Path.cwd() / f"btts-voices-{stamp}"
        if archive:
            destination = destination.with_suffix(".zip")
    else:
        destination = Path(out).expanduser()
        if not destination.is_absolute():
            destination = Path.cwd() / destination
    if archive and destination.suffix.lower() != ".zip":
        raise UsageError(
            f"--zip requires a .zip destination, got: {destination.name}",
            hint="pass -o PATH.zip or omit --out to use the default name",
        )
    try:
        resolved = destination.resolve(strict=False)
        store_root = store.root.resolve()
    except (OSError, RuntimeError) as exc:
        raise UsageError(f"cannot resolve export destination: {destination}") from exc
    if resolved == store_root or resolved.is_relative_to(store_root):
        raise UsageError(
            f"refusing to export inside the voice store: {resolved}",
            hint="choose a destination outside $BTTS_DATA_DIR/voices",
        )
    if os.path.lexists(destination):
        raise UsageError(
            f"export destination already exists: {destination}",
            hint="choose a new --out path; exports never overwrite",
        )
    return destination


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        with contextlib.suppress(OSError):
            os.fsync(fd)
    finally:
        os.close(fd)


def _zip_time(mtime: float) -> tuple[int, int, int, int, int, int]:
    try:
        values = time.localtime(mtime)[:6]
    except (OverflowError, OSError, ValueError):
        values = time.localtime()[:6]
    # DOS timestamps in ZIP support 1980..2107 inclusive.
    return (min(max(values[0], 1980), 2107), *values[1:])


def _compression_for(relpath: str) -> int:
    if relpath.lower().endswith((".wav", ".mp3", ".flac", ".m4a", ".ogg")):
        return zipfile.ZIP_STORED
    return zipfile.ZIP_DEFLATED
