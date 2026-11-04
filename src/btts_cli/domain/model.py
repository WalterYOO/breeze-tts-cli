"""Model acquisition and verification.

This module is deliberately torch-free and does not import
``huggingface_hub`` at import time: the provider methods import it lazily.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from btts_cli import __version__
from btts_cli.audio.hash import sha256_file
from btts_cli.engine.base import (
    REQUIRED_MODEL_FILES,
    compute_model_digest,
    missing_model_files,
)
from btts_cli.output.errors import BttsError, EngineError, IntegrityError, UsageError
from btts_cli.output.json import read_json, write_json_atomic

MARKER_NAME = ".btts-model.json"
INCOMPLETE_NAME = ".btts-model.incomplete.json"
MARKER_SCHEMA_VERSION = 1
DEFAULT_REPO = "BreezeBlue/breeze-tts-2"
HF_REVISION = "main"
MODELSCOPE_REVISION = "master"
MODELSCOPE_BASE_URL = "https://modelscope.cn"
USER_AGENT = f"btts-cli/{__version__}"
DISK_HEADROOM = 1.05
CHUNK_SIZE = 1024 * 1024
HTTP_TIMEOUT = 60

KNOWN_AUDIO_TOKENIZER_FILES = (
    "audio_tokenizer/config.json",
    "audio_tokenizer/model.safetensors",
)
KNOWN_REPO_FILES = frozenset(
    {
        ".gitattributes",
        "LICENSE",
        "README.md",
        "configuration.json",
        "special_tokens_map.json",
    }
)
KNOWN_MODEL_FILES = frozenset((*REQUIRED_MODEL_FILES, *KNOWN_AUDIO_TOKENIZER_FILES))

Progress = Callable[[str], None]


@dataclass(frozen=True)
class ModelFile:
    path: str
    size: int
    sha256: str | None = None


class MarkerEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    size: int
    sha256: str


class ModelMarker(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = MARKER_SCHEMA_VERSION
    source: str
    repo: str
    revision: str
    downloaded_at: str
    files: dict[str, MarkerEntry] = Field(default_factory=dict)


class IncompleteMarker(BaseModel):
    """State file for a download that started and has not succeeded yet."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = MARKER_SCHEMA_VERSION
    source: str
    repo: str
    revision: str
    started_at: str
    error: str | None = None


@dataclass
class DownloadResult:
    source: str
    repo: str
    revision: str
    model_dir: str
    files: int
    total_bytes: int
    skipped: int
    verified: int

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "repo": self.repo,
            "revision": self.revision,
            "model_dir": self.model_dir,
            "files": self.files,
            "total_bytes": self.total_bytes,
            "skipped": self.skipped,
            "verified": self.verified,
        }


@dataclass
class VerificationResult:
    model_dir: str
    complete: bool
    missing: list[str]
    files_checked: int
    deep: bool
    digest: str | None
    source: str | None
    repo: str | None
    revision: str | None
    downloaded_at: str | None
    warnings: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "model_dir": self.model_dir,
            "complete": self.complete,
            "missing": self.missing,
            "files_checked": self.files_checked,
            "deep": self.deep,
            "digest": self.digest,
            "source": self.source,
            "repo": self.repo,
            "revision": self.revision,
            "downloaded_at": self.downloaded_at,
            "warnings": self.warnings,
        }


class ModelProvider(Protocol):
    name: str
    default_revision: str

    def list_files(self, repo: str, revision: str) -> list[ModelFile]: ...

    def download(
        self,
        repo: str,
        revision: str,
        files: list[ModelFile],
        target: Path,
        *,
        progress: Progress,
    ) -> None: ...


def get_provider(source: str) -> ModelProvider:
    if source == "huggingface":
        return HuggingFaceProvider()
    if source == "modelscope":
        return ModelScopeProvider()
    raise UsageError(
        f"unsupported model source: {source}",
        hint="choose huggingface or modelscope",
    )


def _safe_relative(path: str) -> str:
    if not path or "\\" in path or "\x00" in path:
        raise EngineError(f"provider returned an invalid path: {path!r}")
    pure = Path(path)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise EngineError(f"provider returned an unsafe path: {path!r}")
    relative = pure.as_posix()
    if relative in {"", "."}:
        raise EngineError(f"provider returned an unsafe path: {path!r}")
    return relative


def _normalise_files(files: list[ModelFile]) -> list[ModelFile]:
    normalised: list[ModelFile] = []
    seen: set[str] = set()
    for item in files:
        relative = _safe_relative(item.path)
        if relative in seen:
            raise EngineError(f"provider returned a duplicate path: {relative}")
        seen.add(relative)
        if item.size < 0:
            raise EngineError(f"provider returned a negative size for {relative}")
        normalised.append(
            ModelFile(
                path=relative,
                size=int(item.size),
                sha256=(item.sha256 or "").lower() or None,
            )
        )
    if not normalised:
        raise EngineError("provider returned an empty file list")
    return sorted(normalised, key=lambda item: item.path)


def _assert_safe_target(target: Path) -> None:
    try:
        is_link = target.is_symlink()
        exists = target.exists()
        is_dir = target.is_dir()
    except OSError as exc:
        raise EngineError(f"cannot inspect model directory {target}: {exc}") from exc
    if is_link:
        raise IntegrityError(
            f"model directory must not be a symlink: {target}",
            hint="choose a real directory and retry",
        )
    if exists and not is_dir:
        raise UsageError(f"model path is not a directory: {target}")


def _assert_path_within_target(target: Path, path: Path) -> None:
    """Reject symlinked components and paths that resolve outside ``target``."""
    target_abs = Path(os.path.abspath(target))
    path_abs = Path(os.path.abspath(path))
    _assert_safe_target(target_abs)
    try:
        relative = path_abs.relative_to(target_abs)
    except ValueError as exc:
        raise IntegrityError(
            f"model path escapes the target directory: {path}"
        ) from exc
    current = target_abs
    try:
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise IntegrityError(
                    f"model path contains a symlink: {current}",
                    hint="remove the symlink and retry",
                )
        resolved_target = target_abs.resolve(strict=False)
        resolved_path = path_abs.resolve(strict=False)
    except RuntimeError as exc:
        raise IntegrityError(
            f"model path contains a symlink loop: {path}",
            hint="remove the looping symlink and retry",
        ) from exc
    except OSError as exc:
        raise EngineError(f"cannot resolve model path {path}: {exc}") from exc
    if not resolved_path.is_relative_to(resolved_target):
        raise IntegrityError(
            f"model path escapes the target directory: {path}",
            hint="remove the symlink and retry",
        )


def _reject_tree_symlinks(target: Path) -> None:
    """Reject any pre-existing symlink before a third-party tree download."""
    _assert_safe_target(target)
    try:
        if not target.exists():
            return
        for root, dir_names, file_names in os.walk(target, followlinks=False):
            for name in [*dir_names, *file_names]:
                path = Path(root) / name
                if path.is_symlink():
                    raise IntegrityError(
                        f"model directory contains a symlink: {path}",
                        hint="remove the symlink and retry",
                    )
    except OSError as exc:
        raise EngineError(f"cannot scan model directory {target}: {exc}") from exc


def _remove_marker(target: Path) -> None:
    """Atomically invalidate a previous success marker before overwriting files."""
    path = marker_path(target)
    _assert_path_within_target(target, path)
    try:
        if not path.exists() and not path.is_symlink():
            return
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise EngineError(f"cannot invalidate model marker: {exc}") from exc


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _discard_untrusted_files(
    target: Path,
    files: list[ModelFile],
    trusted_hashes: dict[str, str] | None = None,
) -> None:
    """Best-effort removal of files that are not provably from this download.

    Files with no remote SHA-256 are only kept when a previous completion
    marker recorded a matching hash; otherwise a same-size corrupt file from
    a failed provider could be mistaken for a completed download.
    """
    trusted = trusted_hashes or {}
    for item in files:
        final = target / Path(item.path)
        try:
            if final.is_symlink():
                _unlink_quietly(final)
                continue
            if not final.is_file():
                continue
            if final.stat().st_size != item.size:
                _unlink_quietly(final)
                continue
            if item.sha256:
                if sha256_file(final).lower() != item.sha256.lower():
                    _unlink_quietly(final)
                continue
            expected = trusted.get(item.path)
            if not expected or sha256_file(final).lower() != expected.lower():
                _unlink_quietly(final)
        except OSError:
            continue


class HuggingFaceProvider:
    name = "huggingface"
    default_revision = HF_REVISION

    def list_files(self, repo: str, revision: str) -> list[ModelFile]:
        try:
            from huggingface_hub import HfApi
        except ImportError as exc:  # pragma: no cover - base install
            raise EngineError(
                "huggingface-hub is not installed",
                hint="install with: pip install 'breeze-tts-cli'",
            ) from exc
        try:
            info = HfApi().model_info(
                repo_id=repo, revision=revision, files_metadata=True
            )
        except Exception as exc:  # noqa: BLE001 - provider/network boundary
            raise EngineError(f"cannot list Hugging Face repository {repo}: {exc}") from exc
        files: list[ModelFile] = []
        for sibling in getattr(info, "siblings", None) or []:
            path = getattr(sibling, "rfilename", None)
            size = getattr(sibling, "size", None)
            if path is None or size is None:
                raise EngineError(
                    "Hugging Face did not return file sizes for this repository"
                )
            lfs = getattr(sibling, "lfs", None)
            checksum = None
            if isinstance(lfs, dict):
                checksum = lfs.get("sha256")
            elif lfs is not None:
                checksum = getattr(lfs, "sha256", None)
            files.append(ModelFile(path=path, size=int(size), sha256=checksum))
        return _normalise_files(files)

    def download(
        self,
        repo: str,
        revision: str,
        files: list[ModelFile],
        target: Path,
        *,
        progress: Progress,
    ) -> None:
        try:
            from huggingface_hub import snapshot_download
        except ImportError as exc:  # pragma: no cover - base install
            raise EngineError(
                "huggingface-hub is not installed",
                hint="install with: pip install 'breeze-tts-cli'",
            ) from exc
        progress(f"downloading {len(files)} file(s) from Hugging Face")
        _reject_tree_symlinks(target)
        try:
            target.mkdir(parents=True, exist_ok=True)
            snapshot_download(repo_id=repo, revision=revision, local_dir=str(target))
        except Exception as exc:  # noqa: BLE001 - provider/network boundary
            raise EngineError(f"Hugging Face download failed: {exc}") from exc


class ModelScopeProvider:
    name = "modelscope"
    default_revision = MODELSCOPE_REVISION

    def __init__(
        self,
        *,
        base_url: str = MODELSCOPE_BASE_URL,
        timeout: int = HTTP_TIMEOUT,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def list_files(self, repo: str, revision: str) -> list[ModelFile]:
        url = (
            f"{self.base_url}/api/v1/models/{urllib.parse.quote(repo, safe='/')}"
            f"/repo/files?Revision={urllib.parse.quote(revision, safe='')}"
            "&Recursive=true"
        )
        payload = self._get_json(url)
        data = payload.get("Data") if isinstance(payload, dict) else None
        entries = data.get("Files") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            raise EngineError("ModelScope returned an unexpected file list")
        files: list[ModelFile] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            file_type = entry.get("Type")
            path = entry.get("Path")
            size = entry.get("Size")
            if file_type not in (None, "blob"):
                continue
            if not path or size is None:
                continue
            try:
                size_value = int(size)
            except (TypeError, ValueError):
                continue
            if size_value <= 0:
                continue
            files.append(
                ModelFile(
                    path=str(path),
                    size=size_value,
                    sha256=entry.get("Sha256") or None,
                )
            )
        return _normalise_files(files)

    def download(
        self,
        repo: str,
        revision: str,
        files: list[ModelFile],
        target: Path,
        *,
        progress: Progress,
    ) -> None:
        _assert_safe_target(target)
        candidates: list[tuple[ModelFile, Path, Path]] = []
        for item in files:
            destination = target / Path(item.path)
            part = destination.with_name(f"{destination.name}.part")
            _assert_path_within_target(target, destination)
            _assert_path_within_target(target, part)
            candidates.append((item, destination, part))
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise EngineError(f"cannot create model directory {target}: {exc}") from exc
        for index, (item, destination, part) in enumerate(candidates, start=1):
            for _item, all_final, all_part in candidates:
                _assert_path_within_target(target, all_final)
                _assert_path_within_target(target, all_part)
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise EngineError(
                    f"cannot create model directory {destination.parent}: {exc}"
                ) from exc
            if self._file_matches(destination, item):
                progress(f"[{index}/{len(files)}] {item.path} already complete")
                continue
            progress(f"[{index}/{len(files)}] {item.path} ({item.size} bytes)")
            self._download_one(repo, revision, item, target, part, destination)

    def _download_one(
        self,
        repo: str,
        revision: str,
        item: ModelFile,
        target: Path,
        part: Path,
        destination: Path,
    ) -> None:
        _assert_path_within_target(target, destination)
        _assert_path_within_target(target, part)
        try:
            offset = part.stat().st_size if part.is_file() else 0
        except OSError as exc:
            raise EngineError(f"cannot inspect partial file {item.path}: {exc}") from exc
        if part.is_file() and offset == item.size:
            try:
                checksum = sha256_file(part)
            except OSError as exc:
                raise EngineError(f"cannot hash partial file {item.path}: {exc}") from exc
            if not item.sha256 or checksum.lower() == item.sha256.lower():
                _assert_path_within_target(target, destination)
                try:
                    part.replace(destination)
                except OSError as exc:
                    raise EngineError(
                        f"cannot commit model file {item.path}: {exc}"
                    ) from exc
                return
            _unlink_quietly(part)
            offset = 0
        if offset > item.size:
            _unlink_quietly(part)
            offset = 0
        url = (
            f"{self.base_url}/api/v1/models/{urllib.parse.quote(repo, safe='/')}"
            f"/repo?Revision={urllib.parse.quote(revision, safe='')}"
            f"&FilePath={urllib.parse.quote(item.path, safe='')}"
        )
        headers = {"User-Agent": USER_AGENT}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        request = urllib.request.Request(url, headers=headers)
        response = self._open(request)
        try:
            status = getattr(response, "status", 200)
            if offset and status == 206:
                mode = "ab"
            else:
                mode = "wb"
            try:
                with part.open(mode) as handle:
                    while True:
                        chunk = response.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as exc:
                raise EngineError(
                    f"cannot write model file {item.path}: {exc}"
                ) from exc
        finally:
            response.close()

        try:
            actual_size = part.stat().st_size if part.is_file() else 0
        except OSError as exc:
            raise EngineError(f"cannot inspect model file {item.path}: {exc}") from exc
        if actual_size != item.size:
            _unlink_quietly(part)
            raise IntegrityError(
                f"size mismatch for {item.path}: expected {item.size}, got {actual_size}"
            )
        try:
            actual_sha = sha256_file(part)
        except OSError as exc:
            raise EngineError(f"cannot hash model file {item.path}: {exc}") from exc
        if item.sha256 and actual_sha.lower() != item.sha256.lower():
            _unlink_quietly(part)
            raise IntegrityError(
                f"SHA-256 mismatch for {item.path}",
                hint="the mirror returned a different file; retry or use another source",
            )
        _assert_path_within_target(target, destination)
        _assert_path_within_target(target, part)
        try:
            part.replace(destination)
        except OSError as exc:
            raise EngineError(f"cannot commit model file {item.path}: {exc}") from exc

    @staticmethod
    def _file_matches(path: Path, item: ModelFile) -> bool:
        try:
            if not path.is_file() or path.stat().st_size != item.size:
                return False
            if item.sha256:
                return sha256_file(path).lower() == item.sha256.lower()
            return True
        except OSError as exc:
            raise EngineError(f"cannot inspect model file {path.name}: {exc}") from exc

    def _get_json(self, url: str) -> object:
        request = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        )
        response = self._open(request)
        try:
            raw = response.read()
        except OSError as exc:
            raise EngineError(f"cannot read ModelScope response: {exc}") from exc
        finally:
            response.close()
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EngineError(f"ModelScope returned invalid JSON: {exc}") from exc

    def _open(self, request: urllib.request.Request):
        try:
            return urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            raise EngineError(
                f"ModelScope HTTP error {exc.code} for {request.full_url}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise EngineError(
                f"cannot reach ModelScope: {exc}",
                hint="check the network connection and retry",
            ) from exc


def _is_known_model_path(relative: str) -> bool:
    if relative in KNOWN_MODEL_FILES or relative in KNOWN_REPO_FILES:
        return True
    if relative in {MARKER_NAME, INCOMPLETE_NAME} or relative.startswith("assets/"):
        return True
    path = Path(relative)
    if path.parent.as_posix() == "audio_tokenizer":
        return True
    name = path.name
    return name.startswith("model-") and name.endswith(".safetensors")


def marker_path(model_dir: Path) -> Path:
    return model_dir / MARKER_NAME


def incomplete_path(model_dir: Path) -> Path:
    return model_dir / INCOMPLETE_NAME


def read_marker(model_dir: Path) -> ModelMarker | None:
    path = marker_path(model_dir)
    if path.is_symlink():
        raise IntegrityError(f"model marker must not be a symlink: {path}")
    if not path.is_file():
        return None
    try:
        marker = ModelMarker.model_validate(read_json(path))
    except (OSError, ValueError, ValidationError) as exc:
        raise IntegrityError(f"invalid model marker: {path}") from exc
    if marker.schema_version != MARKER_SCHEMA_VERSION:
        raise IntegrityError(
            f"unsupported model marker schema: {marker.schema_version}"
        )
    for relative in marker.files:
        try:
            if _safe_relative(relative) != relative:
                raise IntegrityError(f"invalid model marker path: {relative!r}")
        except EngineError as exc:
            raise IntegrityError(
                f"invalid model marker path: {relative!r}"
            ) from exc
    return marker


def read_incomplete_marker(model_dir: Path) -> IncompleteMarker | None:
    path = incomplete_path(model_dir)
    if path.is_symlink():
        raise IntegrityError(f"model download state must not be a symlink: {path}")
    if not path.is_file():
        return None
    try:
        marker = IncompleteMarker.model_validate(read_json(path))
    except (OSError, ValueError, ValidationError) as exc:
        raise IntegrityError(f"invalid model download state: {path}") from exc
    if marker.schema_version != MARKER_SCHEMA_VERSION:
        raise IntegrityError(
            f"unsupported model download state schema: {marker.schema_version}"
        )
    return marker


def marker_matches(
    marker: ModelMarker | IncompleteMarker | None,
    source: str,
    repo: str,
    revision: str,
) -> bool:
    return bool(
        marker
        and marker.source == source
        and marker.repo == repo
        and marker.revision == revision
    )


def _write_incomplete(
    target: Path,
    *,
    source: str,
    repo: str,
    revision: str,
    started_at: str,
    error: str | None = None,
) -> None:
    state = IncompleteMarker(
        source=source,
        repo=repo,
        revision=revision,
        started_at=started_at,
        error=error,
    )
    path = incomplete_path(target)
    _assert_path_within_target(target, path)
    try:
        write_json_atomic(path, state.model_dump(mode="json"))
    except OSError as exc:
        raise EngineError(f"cannot write model download state: {exc}") from exc


def _record_download_failure(
    target: Path,
    *,
    source: str,
    repo: str,
    revision: str,
    started_at: str,
    error: str,
) -> None:
    """Best-effort failure detail; the state file already exists."""
    try:
        _write_incomplete(
            target,
            source=source,
            repo=repo,
            revision=revision,
            started_at=started_at,
            error=error,
        )
    except BttsError:
        pass


def _remove_incomplete(target: Path) -> None:
    path = incomplete_path(target)
    _assert_path_within_target(target, path)
    _unlink_quietly(path)


def recognizable_partial(
    target: Path, source: str, repo: str, revision: str
) -> bool:
    """True when a non-empty directory is an interrupted download we may resume."""
    try:
        marker = read_marker(target)
    except IntegrityError:
        marker = None
    if marker_matches(marker, source, repo, revision):
        return True
    try:
        incomplete = read_incomplete_marker(target)
    except IntegrityError:
        incomplete = None
    if marker_matches(incomplete, source, repo, revision):
        return True
    for path in target.rglob("*"):
        if path.is_dir():
            continue
        relative = path.relative_to(target).as_posix()
        if relative.startswith(".cache/huggingface/"):
            continue
        base = relative[: -len(".part")] if relative.endswith(".part") else relative
        if not _is_known_model_path(base):
            return False
    return True


def ensure_disk_space(target: Path, total_bytes: int) -> None:
    required = math.ceil(total_bytes * DISK_HEADROOM)
    probe = target
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError as exc:
        raise EngineError(f"cannot check disk space for {probe}: {exc}") from exc
    if usage.free < required:
        raise UsageError(
            f"not enough disk space: need {_gb(required)}, "
            f"available {_gb(usage.free)} at {probe}",
            hint="free some space or choose another --to directory",
        )


def download_model(
    *,
    provider: ModelProvider,
    repo: str,
    revision: str,
    target: Path,
    files: list[ModelFile],
    progress: Progress,
) -> DownloadResult:
    files = _normalise_files(files)
    _assert_safe_target(target)
    for item in files:
        _assert_path_within_target(target, target / Path(item.path))

    trusted_hashes: dict[str, str] = {}
    try:
        previous = read_marker(target)
    except IntegrityError:
        previous = None
    if previous is not None:
        trusted_hashes = {
            path: entry.sha256 for path, entry in previous.files.items()
        }

    total_bytes = sum(item.size for item in files)
    skipped = 0
    for item in files:
        final = target / Path(item.path)
        try:
            if final.is_file() and final.stat().st_size == item.size:
                skipped += 1
        except OSError as exc:
            raise EngineError(f"cannot inspect model file {item.path}: {exc}") from exc

    # Record an explicit in-progress state, then invalidate the previous
    # success marker before any file can be overwritten. The state file is
    # only removed after every new file validates and the success marker is
    # written, so doctor/verify keep reporting failure after any crash.
    started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise EngineError(f"cannot create model directory {target}: {exc}") from exc
    _write_incomplete(
        target,
        source=provider.name,
        repo=repo,
        revision=revision,
        started_at=started_at,
    )
    _remove_marker(target)
    try:
        provider.download(repo, revision, files, target, progress=progress)
    except KeyboardInterrupt:
        raise
    except BttsError as exc:
        _discard_untrusted_files(target, files, trusted_hashes)
        _record_download_failure(
            target,
            source=provider.name,
            repo=repo,
            revision=revision,
            started_at=started_at,
            error=exc.message,
        )
        raise
    except OSError as exc:
        _discard_untrusted_files(target, files, trusted_hashes)
        _record_download_failure(
            target,
            source=provider.name,
            repo=repo,
            revision=revision,
            started_at=started_at,
            error=str(exc),
        )
        raise EngineError(f"model download failed: {exc}") from exc
    except Exception as exc:
        _discard_untrusted_files(target, files, trusted_hashes)
        _record_download_failure(
            target,
            source=provider.name,
            repo=repo,
            revision=revision,
            started_at=started_at,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise EngineError(
            f"model download failed: {type(exc).__name__}: {exc}"
        ) from exc

    progress(f"computing checksums for {len(files)} file(s)")
    entries: dict[str, MarkerEntry] = {}
    for index, item in enumerate(files, start=1):
        final = target / Path(item.path)
        _assert_path_within_target(target, final)
        try:
            is_file = final.is_file()
            actual_size = final.stat().st_size if is_file else None
        except OSError as exc:
            raise EngineError(f"cannot inspect model file {item.path}: {exc}") from exc
        if not is_file:
            raise IntegrityError(f"download is missing {item.path}")
        if actual_size != item.size:
            _unlink_quietly(final)
            raise IntegrityError(
                f"size mismatch for {item.path}: expected {item.size}, got {actual_size}"
            )
        try:
            actual_sha = sha256_file(final)
        except OSError as exc:
            raise EngineError(f"cannot hash model file {item.path}: {exc}") from exc
        if item.sha256 and actual_sha.lower() != item.sha256.lower():
            _unlink_quietly(final)
            raise IntegrityError(
                f"SHA-256 mismatch for {item.path}",
                hint="delete the file and retry, or use another source",
            )
        entries[item.path] = MarkerEntry(size=actual_size, sha256=actual_sha)
        if index == len(files) or index % 10 == 0:
            progress(f"checksums {index}/{len(files)}")

    for item in files:
        _assert_path_within_target(target, target / Path(item.path))
    try:
        missing = missing_model_files(target)
    except OSError as exc:
        raise EngineError(f"cannot inspect model checkpoint: {exc}") from exc
    if missing:
        raise IntegrityError(
            "download is incomplete: " + ", ".join(missing),
            hint="rerun model download; completed files will be reused",
        )

    marker = ModelMarker(
        source=provider.name,
        repo=repo,
        revision=revision,
        downloaded_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        files=entries,
    )
    _assert_path_within_target(target, marker_path(target))
    try:
        write_json_atomic(marker_path(target), marker.model_dump(mode="json"))
    except OSError as exc:
        raise EngineError(f"cannot write model marker: {exc}") from exc
    try:
        for item in files:
            final = target / Path(item.path)
            part = final.with_name(f"{final.name}.part")
            _assert_path_within_target(target, final)
            _assert_path_within_target(target, part)
            try:
                part.unlink(missing_ok=True)
            except OSError as exc:
                raise EngineError(
                    f"cannot clean partial file {item.path}: {exc}"
                ) from exc
    except Exception:
        # A failed cleanup must not leave a marker claiming success.
        _unlink_quietly(marker_path(target))
        raise
    _remove_incomplete(target)

    return DownloadResult(
        source=provider.name,
        repo=repo,
        revision=revision,
        model_dir=str(target),
        files=len(files),
        total_bytes=total_bytes,
        skipped=skipped,
        verified=len(entries),
    )


def _required_nonempty(model_dir: Path) -> list[str]:
    broken: list[str] = []
    for relative in KNOWN_MODEL_FILES:
        path = model_dir / relative
        if path.is_file() and path.stat().st_size == 0:
            broken.append(relative)
    index_path = model_dir / "model.safetensors.index.json"
    if index_path.is_file():
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
            for shard in sorted(set(index.get("weight_map", {}).values())):
                path = model_dir / shard
                if path.is_file() and path.stat().st_size == 0:
                    broken.append(str(shard))
        except (OSError, ValueError):
            pass
    return broken


def verify_model(model_dir: Path, *, deep: bool = False) -> VerificationResult:
    warnings: list[str] = []
    missing: list[str] = []
    failed = False
    marker: ModelMarker | None = None
    incomplete: IncompleteMarker | None = None

    if not model_dir.is_dir():
        warnings.append("model directory does not exist")
        failed = True
    else:
        try:
            marker = read_marker(model_dir)
        except IntegrityError as exc:
            warnings.append(exc.message)
            failed = True
        if marker is None:
            try:
                incomplete = read_incomplete_marker(model_dir)
            except IntegrityError as exc:
                warnings.append(exc.message)
                failed = True
            if incomplete is not None:
                detail = f": {incomplete.error}" if incomplete.error else ""
                warnings.append(
                    "previous model download did not finish"
                    f"{detail}; rerun `btts model download`"
                )
                failed = True

    missing.extend(missing_model_files(model_dir))
    for relative in _required_nonempty(model_dir):
        missing.append(f"{relative} (empty)")
    missing = list(dict.fromkeys(missing))
    if missing:
        failed = True

    if marker is not None:
        for relative, entry in marker.files.items():
            path = model_dir / relative
            if not path.is_file():
                missing.append(relative)
                failed = True
                continue
            if path.stat().st_size != entry.size:
                warnings.append(
                    f"size mismatch for {relative}: expected {entry.size}, "
                    f"got {path.stat().st_size}"
                )
                failed = True
        missing = list(dict.fromkeys(missing))

    files_checked = (
        len(marker.files)
        if marker is not None
        else len(
            [name for name in KNOWN_MODEL_FILES if (model_dir / name).is_file()]
        )
    )

    if deep:
        if marker is None:
            warnings.append("no completion marker; deep hash comparison skipped")
        else:
            files_checked = len(marker.files)
            for relative, entry in marker.files.items():
                path = model_dir / relative
                if not path.is_file():
                    continue
                try:
                    actual = sha256_file(path)
                except OSError as exc:
                    warnings.append(f"cannot hash {relative}: {exc}")
                    failed = True
                    continue
                if actual.lower() != entry.sha256.lower():
                    warnings.append(f"SHA-256 mismatch for {relative}")
                    failed = True

    digest = None
    if model_dir.is_dir():
        try:
            digest = compute_model_digest(model_dir)
        except OSError as exc:
            warnings.append(f"cannot compute model digest: {exc}")
            failed = True
    return VerificationResult(
        model_dir=str(model_dir),
        complete=not failed,
        missing=missing,
        files_checked=files_checked,
        deep=deep,
        digest=digest,
        source=marker.source if marker else None,
        repo=marker.repo if marker else None,
        revision=marker.revision if marker else None,
        downloaded_at=marker.downloaded_at if marker else None,
        warnings=warnings,
    )


def _gb(value: int) -> str:
    return f"{value / 1_000_000_000:.2f} GB"
