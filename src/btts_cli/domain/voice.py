"""Voice profiles: identities that can be replayed by ``say --voice``.

A design profile is a replayable recipe (instruction + seed + cfg). A clone
profile is a reference WAV plus its exact transcript, which is the only stable
identity anchor Breeze offers across texts.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Annotated, Any, Literal

import platformdirs
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from btts_cli import LICENSE_NOTICE, __version__
from btts_cli.audio.hash import prefixed, sha256_file
from btts_cli.domain.design import NAME_RE
from btts_cli.output.errors import IntegrityError, UsageError
from btts_cli.output.json import read_json, write_json_atomic, write_text_atomic

PROFILE_SCHEMA_VERSION = 1
LICENSE_MODEL = "breezeblue-research-non-commercial"
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

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


class VoiceStore:
    def __init__(self, root: Path | None = None):
        self.root = (root or (data_dir() / "voices")).expanduser()

    def path_for(self, name: str) -> Path:
        validate_name(name)
        return self.root / name

    def exists(self, name: str) -> bool:
        return (self.path_for(name) / "profile.json").is_file()

    def load(self, name: str) -> VoiceProfile:
        directory = self.path_for(name)
        profile_path = directory / "profile.json"
        if not profile_path.is_file():
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

    def list(self) -> list[VoiceProfile]:
        if not self.root.is_dir():
            return []
        profiles: list[VoiceProfile] = []
        for directory in sorted(self.root.iterdir()):
            profile_path = directory / "profile.json"
            if not profile_path.is_file():
                continue
            try:
                profile = VoiceProfile.model_validate(read_json(profile_path))
                if profile.kind == "clone":
                    self.reference_paths(directory.name, profile)
                profiles.append(profile)
            except (OSError, ValueError, IntegrityError):
                continue
        return profiles

    def profile_sha256(self, name: str) -> str:
        return prefixed(sha256_file(self.path_for(name) / "profile.json"))

    def save(self, profile: VoiceProfile, files: dict[str, Path], *, force: bool = False) -> Path:
        """Create a voice directory from already-materialised source files."""
        validate_name(profile.name)
        directory = self.path_for(profile.name)
        if directory.exists() and not force:
            raise UsageError(
                f"voice already exists: {profile.name}",
                hint="choose another name or pass --force to replace it",
            )
        if profile.reference is not None:
            for relative in (profile.reference.audio, profile.reference.transcript):
                _assert_safe_basename(relative, "reference filename")
                if relative not in files:
                    raise UsageError(
                        f"required reference file is not being saved: {relative}"
                    )
        for target_name in files:
            _assert_safe_basename(target_name, "voice file")
        directory.mkdir(parents=True, exist_ok=True)
        for target_name, source in files.items():
            _atomic_copy(source, directory / target_name)
        if profile.reference is not None:
            self.reference_paths(profile.name, profile)
        write_text_atomic(directory / "LICENSE.txt", LICENSE_FILE_TEXT)
        write_json_atomic(directory / "profile.json", profile.model_dump(mode="json"))
        return directory


def _resolve_inside(directory: Path, relative: str, label: str) -> Path:
    _assert_safe_basename(relative, label)
    base = directory.resolve()
    try:
        candidate = (base / relative).resolve(strict=True)
    except OSError as exc:
        raise IntegrityError(f"{label} file is missing: {relative}") from exc
    if not candidate.is_relative_to(base):
        raise IntegrityError(
            f"{label} escapes the voice directory: {relative}",
            hint="voice profiles may only reference files inside their own directory",
        )
    if not candidate.is_file():
        raise IntegrityError(f"{label} is not a file: {relative}")
    return candidate


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
