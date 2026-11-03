"""SHA-256 helpers used by recipes, profiles and resume checks."""

from __future__ import annotations

import hashlib
from pathlib import Path


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prefixed(digest: str) -> str:
    """Return the canonical ``sha256:<hex>`` representation."""
    return digest if digest.startswith("sha256:") else f"sha256:{digest}"


def strip_prefix(value: str) -> str:
    return value.removeprefix("sha256:")
