"""Voice profiles: identities that can be replayed by ``say --voice``.

A design profile is a replayable recipe (instruction + seed + cfg). A clone
profile is a reference WAV plus its exact transcript, which is the only stable
identity anchor Breeze offers across texts.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import json
import os
import secrets
import shutil
import stat
import sys
import tempfile
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Annotated, Any, Literal

import platformdirs
from filelock import FileLock, Timeout
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from btts_cli import LICENSE_NOTICE, __version__
from btts_cli.audio.hash import prefixed, sha256_file
from btts_cli.domain.design import NAME_RE
from btts_cli.output.errors import BttsError, IntegrityError, UsageError
from btts_cli.output.json import (
    json_dumps,
    read_json,
    write_json_atomic,
    write_text_atomic,
)

PROFILE_SCHEMA_VERSION = 1
LICENSE_MODEL = "breezeblue-research-non-commercial"
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
MAX_TAG_LENGTH = 64

STORE_LOCK_FILENAME = ".btts-voices.lock"
STORE_LOCK_TIMEOUT = 10.0
TOMBSTONE_PREFIX = ".deleting-"
# Directory-relative, no-follow operations are available on POSIX (macOS/Linux).
# Windows lacks O_DIRECTORY/O_NOFOLLOW and dir_fd; callers fall back to the
# path-based implementations, which are re-validated but cannot be fully atomic
# against a concurrent symlink swap.
SECURE_DIR_OPS = (
    os.name == "posix"
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and os.open in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.unlink in os.supports_dir_fd
    and os.rmdir in os.supports_dir_fd
    and os.rename in os.supports_dir_fd
)

CfgScale = Annotated[float, Field(gt=0, le=20, allow_inf_nan=False)]

LICENSE_FILE_TEXT = f"""Breeze TTS 2 — self-hosted output notice

{LICENSE_NOTICE}

Model: BreezeBlue/breeze-tts-2
Model license: BreezeBlue Research and Non-Commercial License 1.1
CLI: breeze-tts-cli {__version__} (Apache-2.0)

This voice profile and any audio generated with it are intended for research,
evaluation and non-commercial prototyping. Commercial use requires a separate
written license from BreezeBlue. Do not remove this notice when redistributing
the profile within the terms of the model license.
"""


class IdentityBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    structured: dict[str, Any] = Field(default_factory=dict)
    rendered_instruction: str
    template: str | None = None
    compiler_version: str | None = None


class DesignBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seed: int
    cfg_scale: CfgScale
    instruction: str
    max_new_tokens: int = 300


def _assert_safe_basename(value: str, field_name: str) -> None:
    """Reject absolute paths, dot entries and anything containing a separator."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty filename")
    if value in {".", ".."}:
        raise ValueError(f"{field_name} must not be '.' or '..'")
    if "\x00" in value or "/" in value or "\\" in value:
        raise ValueError(f"{field_name} must be a plain filename, not a path")
    if Path(value).name != value or Path(value).is_absolute():
        raise ValueError(f"{field_name} must be a plain filename, not a path")


class ReferenceBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audio: str = "ref.wav"
    audio_sha256: str
    transcript: str = "ref.txt"
    transcript_sha256: str
    transcript_confirmed: bool
    duration_s: float
    sample_rate: int

    @field_validator("audio", "transcript")
    @classmethod
    def _check_safe_name(cls, value: str, info: Any) -> str:
        _assert_safe_basename(value, info.field_name)
        return value


class DefaultsBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cfg_scale: CfgScale
    seed: int = 19
    direction: str | None = None


class ProvenanceBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["designed", "reference_audio"]
    source_take_id: str | None = None
    source_session_id: str | None = None
    model_digest: str | None = None
    model_id: str = "BreezeBlue/breeze-tts-2"
    api_protocol: str = "local-mps/v1"
    cli_version: str = __version__


class LicenseBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = LICENSE_MODEL


class VoiceProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = PROFILE_SCHEMA_VERSION
    voice_id: str
    name: str
    kind: Literal["design", "clone"]
    language: str
    identity: IdentityBlock
    design: DesignBlock | None = None
    reference: ReferenceBlock | None = None
    defaults: DefaultsBlock
    provenance: ProvenanceBlock
    license: LicenseBlock = Field(default_factory=LicenseBlock)
    consent: dict[str, Any] | None = None
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_kind(self) -> VoiceProfile:
        if self.kind == "design" and self.design is None:
            raise ValueError("kind=design requires a design block")
        if self.kind == "clone":
            if self.reference is None:
                raise ValueError("kind=clone requires a reference block")
            if self.design is not None:
                raise ValueError("kind=clone must not store a design block")
        return self


def data_dir() -> Path:
    override = os.environ.get("BTTS_DATA_DIR")
    if override:
        return Path(override).expanduser()
    return Path(platformdirs.user_data_dir("btts_cli", appauthor=False))


def new_voice_id() -> str:
    timestamp = int(time.time() * 1000)
    randomness = int.from_bytes(os.urandom(10), "big")
    return "v_" + _encode(timestamp, 10) + _encode(randomness, 16)


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(ALPHABET[value % 32])
        value //= 32
    return "".join(reversed(chars))


def validate_name(name: str) -> None:
    if not NAME_RE.match(name):
        raise UsageError(
            f"invalid voice name: {name!r}",
            hint="use letters, digits, '-' or '_' (max 64 characters)",
        )


def normalize_tag(tag: str) -> str:
    """Trim and validate one user-supplied tag."""
    cleaned = tag.strip()
    if not cleaned:
        raise UsageError(
            "tag must not be empty or whitespace",
            hint="pass a short label such as 'narrator' or 'calm'",
        )
    if len(cleaned) > MAX_TAG_LENGTH:
        raise UsageError(
            f"tag is too long ({len(cleaned)} characters; max {MAX_TAG_LENGTH})"
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in cleaned):
        raise UsageError("tag must not contain control characters")
    return cleaned


def _dedupe(values: Iterable[str]) -> list[str]:
    """Remove duplicates while keeping the first occurrence order."""
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def normalize_tags(values: Iterable[str]) -> list[str]:
    """Normalize and de-duplicate user-supplied tags."""
    return _dedupe(normalize_tag(value) for value in values)


class VoiceStore:
    def __init__(self, root: Path | None = None):
        self.root = (root or (data_dir() / "voices")).expanduser()

    def path_for(self, name: str) -> Path:
        validate_name(name)
        return self.root / name

    def exists(self, name: str) -> bool:
        try:
            return (self.path_for(name) / "profile.json").is_file()
        except (OSError, RuntimeError):
            # is_file() re-raises EACCES (and resolve-style loops raise
            # RuntimeError); a missing/unreadable profile simply is not a voice.
            return False

    def load(self, name: str) -> VoiceProfile:
        directory = self.path_for(name)
        profile_path = directory / "profile.json"
        try:
            present = profile_path.is_file()
        except (OSError, RuntimeError) as exc:
            raise UsageError(f"cannot access voice {name!r}: {exc}") from exc
        if not present:
            raise UsageError(
                f"voice not found: {name}",
                hint=f"stored voices live in {self.root}",
            )
        try:
            profile = VoiceProfile.model_validate(read_json(profile_path))
        except (OSError, ValueError) as exc:
            raise IntegrityError(f"invalid profile for voice {name}") from exc
        if profile.kind == "clone":
            try:
                self.reference_paths(name, profile)
            except IntegrityError as exc:
                raise IntegrityError(
                    f"invalid profile for voice {name}: {exc.message}",
                    hint=exc.hint,
                ) from exc
        return profile

    def reference_paths(
        self, name: str, profile: VoiceProfile
    ) -> tuple[Path, Path]:
        """Resolve and confine the audio/transcript files to the voice directory."""
        reference = profile.reference
        if reference is None:
            raise IntegrityError(f"clone profile {name} has no reference block")
        directory = self.path_for(name)
        return (
            _resolve_inside(directory, reference.audio, "reference audio"),
            _resolve_inside(directory, reference.transcript, "reference transcript"),
        )

    def load_by_reference(self, reference: str) -> tuple[str, VoiceProfile]:
        name = reference[1:] if reference.startswith("@") else reference
        return name, self.load(name)

    def scan(
        self,
    ) -> tuple[list[VoiceProfile], list[Path], list[tuple[str, str]]]:
        """Return ``(profiles, tombstones, mismatches)`` for ``voice list``.

        One unreadable or invalid directory must never make the whole listing
        fail. Delete tombstones are skipped and returned separately so the CLI
        can warn about leftovers; a profile whose ``name`` disagrees with its
        directory (an interrupted rename) is displayed under the directory name
        and reported as a mismatch.
        """
        profiles: list[VoiceProfile] = []
        tombstones: list[Path] = []
        mismatches: list[tuple[str, str]] = []
        try:
            # is_dir() re-raises EACCES (e.g. an unreadable BTTS_DATA_DIR
            # parent), so it must be inside the same controlled mapping.
            if not self.root.is_dir():
                return [], [], []
            entries = sorted(self.root.iterdir())
        except FileNotFoundError:
            # The store disappeared between is_dir() and iterdir().
            return [], [], []
        except OSError as exc:
            # An unreadable store (e.g. chmod 000) must not leak a traceback.
            raise UsageError(
                f"cannot read voice store: {self.root}: {exc}",
                hint="check the directory permissions or BTTS_DATA_DIR",
            ) from exc
        for directory in entries:
            try:
                name = directory.name
                if name.startswith(TOMBSTONE_PREFIX):
                    tombstones.append(directory)
                    continue
                # Voice names cannot start with '.', so hidden entries (the lock
                # file, editor debris) are never voices.
                if name.startswith(".") or not directory.is_dir():
                    continue
                profile_path = directory / "profile.json"
                if not profile_path.is_file():
                    continue
                profile = VoiceProfile.model_validate(read_json(profile_path))
                if profile.kind == "clone":
                    self.reference_paths(name, profile)
            except (OSError, ValueError, RuntimeError, BttsError):
                # is_file()/resolve() can raise PermissionError or raise
                # RuntimeError on a symlink loop; reference_paths can raise
                # IntegrityError. One bad entry must not break the listing.
                continue
            if profile.name != name:
                mismatches.append((name, profile.name))
                # Show the addressable directory name, not the "future" name.
                profile = profile.model_copy(update={"name": name})
            profiles.append(profile)
        return profiles, tombstones, mismatches

    def list(self) -> list[VoiceProfile]:
        return self.scan()[0]

    def profile_sha256(self, name: str) -> str:
        return prefixed(sha256_file(self.path_for(name) / "profile.json"))

    @contextlib.contextmanager
    def _store_lock(self) -> Iterator[None]:
        """Serialize mutating commands with a store-level lock file.

        The lock only coordinates cooperating ``btts`` processes; it is not a
        security boundary. The directory-relative operations below defend
        against non-cooperating writers as well.
        """
        if not self.root.is_dir():
            # No lock file can exist; _voice_dir_or_raise reports the real reason.
            yield
            return
        lock = FileLock(str(self.root / STORE_LOCK_FILENAME))
        try:
            lock.acquire(timeout=STORE_LOCK_TIMEOUT)
        except Timeout as exc:
            raise IntegrityError(
                "timed out waiting for the voice store lock",
                hint="another btts process is modifying the voice store; retry",
            ) from exc
        except OSError as exc:
            raise UsageError(
                f"cannot lock voice store: {self.root}: {exc}"
            ) from exc
        try:
            yield
        finally:
            lock.release()

    def _require_store_root(self, not_found: UsageError) -> None:
        try:
            root_stat = self.root.stat()
        except FileNotFoundError:
            raise not_found from None
        except OSError as exc:
            raise UsageError(
                f"cannot access voice store: {self.root}: {exc}"
            ) from exc
        if not stat.S_ISDIR(root_stat.st_mode):
            raise UsageError(
                f"voice store is not a directory: {self.root}",
                hint="check $BTTS_DATA_DIR; remove any file occupying the voices path",
            )

    def _voice_dir_or_raise(self, name: str) -> Path:
        validate_name(name)
        not_found = UsageError(
            f"voice not found: {name}",
            hint=f"stored voices live in {self.root}",
        )
        self._require_store_root(not_found)
        directory = self.root / name
        if directory.parent != self.root or directory == self.root:
            raise IntegrityError(f"voice path escapes the store: {directory}")
        try:
            entry = directory.lstat()
        except FileNotFoundError:
            raise not_found from None
        except OSError as exc:
            raise UsageError(f"cannot access voice {name!r}: {exc}") from exc
        if stat.S_ISLNK(entry.st_mode):
            raise IntegrityError(
                f"refusing symlinked voice directory: {directory}",
                hint="voice directories must be real directories inside the store",
            )
        if not stat.S_ISDIR(entry.st_mode):
            raise not_found
        profile_path = directory / "profile.json"
        try:
            profile_entry = profile_path.lstat()
        except FileNotFoundError:
            raise not_found from None
        except OSError as exc:
            raise UsageError(f"cannot access voice {name!r}: {exc}") from exc
        if stat.S_ISLNK(profile_entry.st_mode):
            raise IntegrityError(
                f"refusing symlinked profile: {profile_path}",
                hint="profile.json must be a regular file inside the voice directory",
            )
        if not stat.S_ISREG(profile_entry.st_mode):
            raise not_found
        try:
            resolved = directory.resolve(strict=True)
            root_resolved = self.root.resolve()
        except (OSError, RuntimeError) as exc:
            raise IntegrityError(f"cannot resolve voice directory: {directory}") from exc
        if not resolved.is_relative_to(root_resolved):
            raise IntegrityError(
                f"voice directory escapes the store: {directory}",
                hint="voice profiles must live directly inside the store root",
            )
        return directory

    def _validate_payload(
        self, name: str, payload: Any, *, check_reference: bool = True
    ) -> VoiceProfile:
        if not isinstance(payload, dict):
            raise IntegrityError(f"invalid profile for voice {name}")
        try:
            profile = VoiceProfile.model_validate(payload)
        except ValueError as exc:
            raise IntegrityError(f"invalid profile for voice {name}") from exc
        if check_reference and profile.kind == "clone":
            try:
                self.reference_paths(name, profile)
            except IntegrityError as exc:
                raise IntegrityError(
                    f"invalid profile for voice {name}: {exc.message}", hint=exc.hint
                ) from exc
        return profile

    def _read_payload(
        self, name: str, directory: Path, *, check_reference: bool = True
    ) -> tuple[dict[str, Any], VoiceProfile]:
        """Read the raw JSON payload and validate it before mutating the store."""
        try:
            payload = read_json(directory / "profile.json")
        except (OSError, ValueError) as exc:
            raise IntegrityError(f"invalid profile for voice {name}") from exc
        return payload, self._validate_payload(
            name, payload, check_reference=check_reference
        )

    def _read_payload_at(
        self, name: str, directory_fd: int
    ) -> tuple[dict[str, Any], VoiceProfile]:
        payload = _read_json_at(directory_fd, "profile.json", name=name)
        return payload, self._validate_payload(name, payload)

    def tags_for(self, name: str) -> list[str]:
        """Return the stored tags without writing or checking reference files."""
        directory = self._voice_dir_or_raise(name)
        _, profile = self._read_payload(name, directory, check_reference=False)
        return list(profile.tags)

    def rename(self, old: str, new: str) -> Path:
        """Rename a voice, updating both the directory and ``profile.json``.

        Ordering matters for crash safety: the profile is re-written (atomically)
        while the directory still has its old name, then the directory is moved
        with a single same-filesystem ``os.rename``. A crash between the two
        steps leaves ``root/old`` holding ``name: new``; re-running the same
        command repairs it. The reverse order would leave no command able to
        find the half-renamed voice. If the directory rename fails we restore
        the original profile payload before surfacing the error.

        On POSIX the profile and directory are handled through an
        ``O_DIRECTORY|O_NOFOLLOW`` descriptor, so a path swapped for a symlink
        after validation cannot redirect the write outside the store.
        """
        validate_name(old)
        validate_name(new)
        with self._store_lock():
            source = self._voice_dir_or_raise(old)
            target = self.root / new
            if target != source and os.path.lexists(target):
                # lstat first: a symlink pointing at the source is still an
                # existing target, not a case-only rename.
                if not target.is_symlink() and _same_file(source, target):
                    raise UsageError(
                        f"cannot rename {old!r} to {new!r}: the target differs only "
                        "by case on a case-insensitive filesystem",
                        hint=(
                            "use an intermediate name: "
                            f"`btts voice rename {old} {old}-tmp` then "
                            f"`btts voice rename {old}-tmp {new}`"
                        ),
                    )
                raise UsageError(
                    f"voice already exists: {new}",
                    hint="rename never overwrites an existing voice; pick another name",
                )
            if not SECURE_DIR_OPS:
                return self._rename_path_based(old, new, source, target)
            with _open_dir_fd(source, nofollow=True, label=f"voice {old!r}") as source_fd:
                payload, _ = self._read_payload_at(old, source_fd)
                updated = dict(payload)
                updated["name"] = new
                try:
                    _write_json_at(source_fd, "profile.json", updated)
                except OSError as exc:
                    raise IntegrityError(
                        f"cannot update profile during rename: {exc}",
                        hint=f"the voice is unchanged at {source}",
                    ) from exc
                if target == source:
                    return source
                with _open_dir_fd(
                    self.root, nofollow=False, label="voice store"
                ) as root_fd:
                    try:
                        _assert_entry_matches(
                            root_fd, old, source_fd, f"voice {old!r}"
                        )
                    except IntegrityError:
                        _restore_profile_at(source_fd, payload, old=old, new=new)
                        raise
                    try:
                        renamed = _rename_no_replace(source, target)
                    except OSError as exc:
                        _restore_profile_at(source_fd, payload, old=old, new=new)
                        raise IntegrityError(
                            f"cannot rename voice directory: {exc}",
                            hint=(
                                "the profile was restored; check directory "
                                "permissions and retry"
                            ),
                        ) from exc
                    if not renamed:
                        _restore_profile_at(source_fd, payload, old=old, new=new)
                        raise UsageError(
                            f"voice already exists: {new}",
                            hint=(
                                "rename never overwrites an existing voice; "
                                "pick another name"
                            ),
                        )
                    try:
                        _assert_entry_matches(
                            root_fd, new, source_fd, f"voice {new!r}"
                        )
                    except IntegrityError as exc:
                        with contextlib.suppress(OSError):
                            _rename_no_replace(target, source)
                        _restore_profile_at(source_fd, payload, old=old, new=new)
                        raise IntegrityError(
                            f"voice directory changed during rename: {exc.message}",
                            hint=(
                                "the profile was restored; retry "
                                f"`btts voice rename {old} {new}`"
                            ),
                        ) from exc
            return target

    def _rename_path_based(
        self, old: str, new: str, source: Path, target: Path
    ) -> Path:
        """Fallback for platforms without dir_fd/O_NOFOLLOW (re-validated)."""
        payload, _ = self._read_payload(old, source)
        updated = dict(payload)
        updated["name"] = new
        try:
            write_json_atomic(source / "profile.json", updated)
        except OSError as exc:
            raise IntegrityError(
                f"cannot update profile during rename: {exc}",
                hint=f"the voice is unchanged at {source}",
            ) from exc
        if target == source:
            return source
        try:
            renamed = _rename_no_replace(source, target)
        except OSError as exc:
            try:
                write_json_atomic(source / "profile.json", payload)
            except OSError as rollback_exc:
                raise IntegrityError(
                    f"rename failed ({exc}) and profile rollback failed ({rollback_exc})",
                    hint=(
                        f"{source / 'profile.json'} now names voice {new!r}; re-run "
                        f"`btts voice rename {old} {new}` after fixing permissions"
                    ),
                ) from exc
            raise IntegrityError(
                f"cannot rename voice directory: {exc}",
                hint="the profile was restored; check directory permissions and retry",
            ) from exc
        if not renamed:
            try:
                write_json_atomic(source / "profile.json", payload)
            except OSError as rollback_exc:
                raise IntegrityError(
                    f"rename target appeared and profile rollback failed: {rollback_exc}",
                    hint=(
                        f"re-run `btts voice rename {old} {new}` after removing "
                        "the conflicting target"
                    ),
                ) from rollback_exc
            raise UsageError(
                f"voice already exists: {new}",
                hint="rename never overwrites an existing voice; pick another name",
            )
        return target

    def update_tags(
        self,
        name: str,
        *,
        add: Iterable[str] = (),
        remove: Iterable[str] = (),
        replace: Iterable[str] | None = None,
        clear: bool = False,
    ) -> list[str]:
        """Update only the ``tags`` field, preserving every other field."""
        validate_name(name)
        with self._store_lock():
            directory = self._voice_dir_or_raise(name)
            if SECURE_DIR_OPS:
                with _open_dir_fd(
                    directory, nofollow=True, label=f"voice {name!r}"
                ) as directory_fd:
                    payload, profile = self._read_payload_at(name, directory_fd)
                    tags = _compute_tags(
                        profile.tags,
                        add=add,
                        remove=remove,
                        replace=replace,
                        clear=clear,
                    )
                    updated = dict(payload)
                    updated["tags"] = tags
                    try:
                        _write_json_at(directory_fd, "profile.json", updated)
                    except OSError as exc:
                        raise IntegrityError(
                            f"cannot update tags for voice {name}: {exc}"
                        ) from exc
                    return tags
            payload, profile = self._read_payload(name, directory)
            tags = _compute_tags(
                profile.tags, add=add, remove=remove, replace=replace, clear=clear
            )
            updated = dict(payload)
            updated["tags"] = tags
            try:
                write_json_atomic(directory / "profile.json", updated)
            except OSError as exc:
                raise IntegrityError(
                    f"cannot update tags for voice {name}: {exc}"
                ) from exc
            return tags

    def summarize(self, name: str) -> tuple[Path, int, int]:
        """Return ``(directory, file_count, total_bytes)`` without following symlinks."""
        directory = self._voice_dir_or_raise(name)
        try:
            if SECURE_DIR_OPS:
                with _open_dir_fd(
                    directory, nofollow=True, label=f"voice {name!r}"
                ) as directory_fd:
                    files, total = _tree_summary_at(directory_fd)
            else:
                files, total = _tree_summary(directory)
        except IntegrityError:
            raise
        except OSError as exc:
            raise IntegrityError(f"cannot inspect voice {name}: {exc}") from exc
        return directory, files, total

    def delete(self, name: str) -> Path:
        """Delete one voice directory, refusing symlinks and escape paths.

        On POSIX the directory is opened once with ``O_NOFOLLOW`` and every
        unlink is made relative to that descriptor, so replacing the path with
        a symlink after validation cannot delete store-external files. The
        directory is first atomically moved to a ``.deleting-*`` tombstone so
        an interrupted delete is discoverable instead of looking like a valid
        voice.
        """
        validate_name(name)
        with self._store_lock():
            directory = self._voice_dir_or_raise(name)
            try:
                root_resolved = self.root.resolve()
            except (OSError, RuntimeError) as exc:
                raise IntegrityError(
                    f"cannot resolve voice store: {self.root}"
                ) from exc
            if directory.resolve() == root_resolved:
                raise UsageError("refusing to delete the voice store root")
            if not SECURE_DIR_OPS:
                try:
                    _remove_tree_no_follow(directory)
                except OSError as exc:
                    raise IntegrityError(
                        f"cannot delete voice {name}: {exc}",
                        hint=f"inspect {directory} and retry, or remove it manually",
                    ) from exc
                return directory
            with _open_dir_fd(
                directory, nofollow=True, label=f"voice {name!r}"
            ) as directory_fd:
                with _open_dir_fd(
                    self.root, nofollow=False, label="voice store"
                ) as root_fd:
                    _assert_entry_matches(
                        root_fd, name, directory_fd, f"voice {name!r}"
                    )
                    tombstone = f"{TOMBSTONE_PREFIX}{name}-{secrets.token_hex(4)}"
                    try:
                        os.rename(
                            name,
                            tombstone,
                            src_dir_fd=root_fd,
                            dst_dir_fd=root_fd,
                        )
                    except OSError as exc:
                        raise IntegrityError(
                            f"cannot prepare voice for deletion: {exc}"
                        ) from exc
                    try:
                        _assert_entry_matches(
                            root_fd, tombstone, directory_fd, f"voice {name!r}"
                        )
                    except IntegrityError:
                        with contextlib.suppress(OSError):
                            os.rename(
                                tombstone,
                                name,
                                src_dir_fd=root_fd,
                                dst_dir_fd=root_fd,
                            )
                        raise
                    try:
                        _remove_tree_at(directory_fd, remove_profile_first=True)
                        os.rmdir(tombstone, dir_fd=root_fd)
                    except OSError as exc:
                        leftover = self.root / tombstone
                        raise IntegrityError(
                            f"cannot delete voice {name}: {exc}",
                            hint=(
                                f"leftover files were moved to {leftover}; "
                                "remove them manually"
                            ),
                        ) from exc
            return directory

    def save(self, profile: VoiceProfile, files: dict[str, Path], *, force: bool = False) -> Path:
        """Create a voice directory from already-materialised source files.

        Runs under the same store lock as rename/tag/delete; the non-force
        ``mkdir`` is the atomic name claim, so a concurrent rename either wins
        and makes this add fail with "voice already exists", or loses and
        leaves the newly added voice untouched.
        """
        validate_name(profile.name)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise UsageError(
                f"voice store is not a directory: {self.root}"
            ) from exc
        with self._store_lock():
            directory = self.path_for(profile.name)
            if directory.exists() and not force:
                raise UsageError(
                    f"voice already exists: {profile.name}",
                    hint="choose another name or pass --force to replace it",
                )
            if profile.reference is not None:
                for relative in (
                    profile.reference.audio,
                    profile.reference.transcript,
                ):
                    _assert_safe_basename(relative, "reference filename")
                    if relative not in files:
                        raise UsageError(
                            f"required reference file is not being saved: {relative}"
                        )
            for target_name in files:
                _assert_safe_basename(target_name, "voice file")
            if force:
                directory.mkdir(parents=True, exist_ok=True)
            else:
                try:
                    directory.mkdir(parents=True, exist_ok=False)
                except FileExistsError as exc:
                    raise UsageError(
                        f"voice already exists: {profile.name}",
                        hint="choose another name or pass --force to replace it",
                    ) from exc
            for target_name, source in files.items():
                _atomic_copy(source, directory / target_name)
            if profile.reference is not None:
                self.reference_paths(profile.name, profile)
            write_text_atomic(directory / "LICENSE.txt", LICENSE_FILE_TEXT)
            write_json_atomic(
                directory / "profile.json", profile.model_dump(mode="json")
            )
            return directory


def _resolve_inside(directory: Path, relative: str, label: str) -> Path:
    _assert_safe_basename(relative, label)
    try:
        base = directory.resolve()
        candidate = (base / relative).resolve(strict=True)
        is_file = candidate.is_file()
    except RuntimeError as exc:
        # pathlib raises RuntimeError (not OSError) on ELOOP in Python 3.12.
        raise IntegrityError(
            f"{label} file is missing or unresolvable: {relative}"
        ) from exc
    except OSError as exc:
        raise IntegrityError(
            f"{label} file is missing or unreadable: {relative}"
        ) from exc
    if not candidate.is_relative_to(base):
        raise IntegrityError(
            f"{label} escapes the voice directory: {relative}",
            hint="voice profiles may only reference files inside their own directory",
        )
    if not is_file:
        raise IntegrityError(f"{label} is not a file: {relative}")
    return candidate


def _compute_tags(
    existing: Iterable[str],
    *,
    add: Iterable[str],
    remove: Iterable[str],
    replace: Iterable[str] | None,
    clear: bool,
) -> list[str]:
    if replace is not None:
        return normalize_tags(replace)
    normalized_existing: list[str] = []
    for tag in existing:
        try:
            normalized_existing.append(normalize_tag(tag))
        except UsageError:
            # Legacy unnormalized tag (from before `voice add` validated
            # --tag). Drop it on the next mutation so the store converges.
            continue
    tags = [] if clear else _dedupe(normalized_existing)
    removals = set(normalize_tags(remove))
    if not clear and removals:
        tags = [tag for tag in tags if tag not in removals]
    for tag in normalize_tags(add):
        if tag not in tags:
            tags.append(tag)
    return tags


@contextlib.contextmanager
def _open_dir_fd(path: Path, *, nofollow: bool, label: str) -> Iterator[int]:
    flags = os.O_RDONLY | os.O_DIRECTORY
    if nofollow:
        flags |= os.O_NOFOLLOW
    try:
        directory_fd = os.open(path, flags)
    except OSError as exc:
        if nofollow and exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise IntegrityError(
                f"refusing symlinked or non-directory {label}: {path}",
                hint="the voice directory changed while the command was running",
            ) from exc
        raise IntegrityError(f"cannot open {label}: {path}: {exc}") from exc
    try:
        yield directory_fd
    finally:
        os.close(directory_fd)


def _read_json_at(directory_fd: int, filename: str, *, name: str) -> Any:
    flags = os.O_RDONLY | os.O_NOFOLLOW
    try:
        file_fd = os.open(filename, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise IntegrityError(f"invalid profile for voice {name}") from exc
    try:
        with os.fdopen(file_fd, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        raise IntegrityError(f"invalid profile for voice {name}") from exc


def _write_json_at(directory_fd: int, filename: str, data: Any) -> None:
    """Atomically write JSON inside an open directory without following symlinks."""
    text = json_dumps(data)
    for _ in range(16):
        tmp_name = f".{filename}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
        try:
            file_fd = os.open(
                tmp_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            break
        except FileExistsError:
            continue
    else:  # pragma: no cover - astronomically unlikely collision
        raise IntegrityError(f"cannot allocate a temporary file for {filename}")
    try:
        with os.fdopen(file_fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(
            tmp_name,
            filename,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name, dir_fd=directory_fd)
        raise


def _assert_entry_matches(
    parent_fd: int, entry_name: str, child_fd: int, label: str
) -> None:
    try:
        entry = os.stat(entry_name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise IntegrityError(
            f"{label} disappeared during the operation",
            hint="another process modified the voice store; retry the command",
        ) from exc
    child = os.fstat(child_fd)
    if not stat.S_ISDIR(entry.st_mode) or (entry.st_dev, entry.st_ino) != (
        child.st_dev,
        child.st_ino,
    ):
        raise IntegrityError(
            f"{label} changed during the operation",
            hint="another process modified the voice store; retry the command",
        )


def _rename_no_replace(source: Path, target: Path) -> bool:
    """Rename ``source`` to ``target`` without replacing any existing target.

    Returns ``False`` when the target already exists. Uses macOS
    ``renamex_np(RENAME_EXCL)`` or Linux ``renameat2(RENAME_NOREPLACE)`` when
    available; otherwise falls back to ``lexists`` + ``os.rename`` under the
    store lock (documented degradation).
    """
    source_bytes = os.fsencode(source)
    target_bytes = os.fsencode(target)
    if sys.platform == "darwin":
        renamex_np = getattr(ctypes.CDLL(None, use_errno=True), "renamex_np", None)
        if renamex_np is not None:
            renamex_np.argtypes = [
                ctypes.c_char_p,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            renamex_np.restype = ctypes.c_int
            rename_excl = 0x00000004
            if renamex_np(source_bytes, target_bytes, rename_excl) == 0:
                return True
            error = ctypes.get_errno()
            if error in (errno.EEXIST, errno.ENOTEMPTY):
                return False
            if error not in (errno.EINVAL, errno.ENOTSUP):
                raise OSError(
                    error, os.strerror(error), str(source), str(target)
                )
    elif sys.platform.startswith("linux"):
        renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
        if renameat2 is not None:
            renameat2.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            renameat2.restype = ctypes.c_int
            at_fdcwd = -100
            rename_noreplace = 1
            if (
                renameat2(
                    at_fdcwd,
                    source_bytes,
                    at_fdcwd,
                    target_bytes,
                    rename_noreplace,
                )
                == 0
            ):
                return True
            error = ctypes.get_errno()
            if error in (errno.EEXIST, errno.ENOTEMPTY):
                return False
            if error != errno.ENOSYS:
                raise OSError(
                    error, os.strerror(error), str(source), str(target)
                )
    # Degraded path: cooperative processes are serialized by the store lock.
    if os.path.lexists(target):
        return False
    os.rename(source, target)
    return True


def _same_file(left: Path, right: Path) -> bool:
    try:
        left_stat = os.stat(left)
        right_stat = os.stat(right)
    except OSError:
        return False
    return (left_stat.st_dev, left_stat.st_ino) == (
        right_stat.st_dev,
        right_stat.st_ino,
    )


def _restore_profile_at(
    directory_fd: int, payload: Any, *, old: str, new: str
) -> None:
    try:
        _write_json_at(directory_fd, "profile.json", payload)
    except OSError as exc:
        raise IntegrityError(
            f"rename failed and profile rollback failed: {exc}",
            hint=(
                f"the profile now names voice {new!r}; re-run "
                f"`btts voice rename {old} {new}` after fixing permissions"
            ),
        ) from exc


def _tree_summary_at(directory_fd: int) -> tuple[int, int]:
    """Count files and bytes below an open directory without following symlinks."""
    files = 0
    total = 0
    with os.scandir(directory_fd) as entries:
        for entry in entries:
            entry_stat = entry.stat(follow_symlinks=False)
            if entry.is_dir(follow_symlinks=False):
                try:
                    child_fd = os.open(
                        entry.name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=directory_fd,
                    )
                except OSError:
                    files += 1
                    total += entry_stat.st_size
                    continue
                try:
                    sub_files, sub_total = _tree_summary_at(child_fd)
                finally:
                    os.close(child_fd)
                files += sub_files
                total += sub_total
            else:
                files += 1
                total += entry_stat.st_size
    return files, total


def _remove_tree_at(directory_fd: int, *, remove_profile_first: bool) -> None:
    """Recursively delete an open directory via dir_fd (never follows symlinks)."""
    if remove_profile_first:
        with contextlib.suppress(FileNotFoundError):
            os.unlink("profile.json", dir_fd=directory_fd)
    with os.scandir(directory_fd) as iterator:
        entries = list(iterator)
    for entry in entries:
        if entry.is_dir(follow_symlinks=False):
            try:
                child_fd = os.open(
                    entry.name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
            except OSError:
                # The entry was swapped for a symlink/non-directory; unlink
                # that entry itself, never its target.
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(entry.name, dir_fd=directory_fd)
                continue
            try:
                _remove_tree_at(child_fd, remove_profile_first=False)
            finally:
                os.close(child_fd)
            os.rmdir(entry.name, dir_fd=directory_fd)
        else:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(entry.name, dir_fd=directory_fd)


def _tree_summary(directory: Path) -> tuple[int, int]:
    """Count files and bytes below ``directory`` without following symlinks."""
    files = 0
    total = 0
    with os.scandir(directory) as entries:
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                sub_files, sub_total = _tree_summary(Path(entry.path))
                files += sub_files
                total += sub_total
            else:
                files += 1
                total += entry.stat(follow_symlinks=False).st_size
    return files, total


def _remove_tree_no_follow(directory: Path) -> None:
    """Recursively delete ``directory`` without ever following symlinks.

    ``profile.json`` is removed last: if the process is interrupted, the
    remaining directory is still recognizable (and deletable) as a voice.
    """
    profile_path = directory / "profile.json"
    with os.scandir(directory) as entries:
        for entry in entries:
            entry_path = Path(entry.path)
            if entry_path == profile_path:
                continue
            if entry.is_dir(follow_symlinks=False):
                _remove_tree_no_follow(entry_path)
            else:
                entry_path.unlink()
    if os.path.lexists(profile_path):
        profile_path.unlink()
    directory.rmdir()


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        shutil.copyfile(source, tmp_path)
        tmp_path.replace(destination)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
