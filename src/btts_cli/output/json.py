"""Deterministic JSON serialisation and atomic writes."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def json_dumps(data: Any) -> str:
    text = json.dumps(
        data,
        indent=2,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
    )
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        # Hand-edited stores can contain lone surrogates (for example from
        # undecodable filesystem bytes). Keep the document writable instead of
        # crashing the command; ASCII escaping still produces valid JSON.
        text = json.dumps(
            data,
            indent=2,
            ensure_ascii=True,
            sort_keys=True,
            allow_nan=False,
        )
    return text + "\n"


def write_json_atomic(path: Path, data: Any) -> None:
    """Write JSON via a sibling temporary file followed by ``os.replace``."""
    write_text_atomic(path, json_dumps(data))


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
