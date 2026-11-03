"""macOS playback after a complete, committed WAV is on disk.

Playback is intentionally all-or-nothing: the caller writes and renames the
file first, then calls :func:`play_file`. A playback failure keeps the file and
raises :class:`PlaybackError` (exit code 4).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from btts_cli.output.errors import PlaybackError


def afplay_path() -> str | None:
    return shutil.which("afplay")


def play_file(path: Path) -> None:
    if sys.platform != "darwin":
        raise PlaybackError(
            "--play is only supported on macOS (afplay)",
            hint=f"audio was written to {path.name}",
        )
    executable = afplay_path()
    if executable is None:
        raise PlaybackError(
            "afplay not found on PATH",
            hint=f"audio was written to {path.name}",
        )
    try:
        subprocess.run([executable, str(path)], check=True)
    except subprocess.CalledProcessError as exc:
        raise PlaybackError(
            f"afplay failed with exit code {exc.returncode}",
            hint=f"audio was written to {path.name}",
        ) from exc
    except OSError as exc:
        raise PlaybackError(
            f"could not start afplay: {exc}",
            hint=f"audio was written to {path.name}",
        ) from exc
