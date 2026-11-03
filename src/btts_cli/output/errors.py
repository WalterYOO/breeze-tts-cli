"""Typed CLI errors with stable exit codes.

Exit codes are part of the P0 contract:

===== ==========================================================
code  meaning
===== ==========================================================
1     generic / unexpected btts error
2     usage or input validation
3     engine / model / device failure
4     playback failure (the generated file is kept)
5     data integrity failure (hash, profile, take validation)
130   interrupted (Ctrl-C)
===== ==========================================================
"""

from __future__ import annotations


class BttsError(Exception):
    """Base class for user-facing errors."""

    exit_code = 1
    code = "btts-error"

    def __init__(self, message: str, *, hint: str | None = None, code: str | None = None):
        super().__init__(message)
        self.message = message
        self.hint = hint
        if code is not None:
            self.code = code

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


class UsageError(BttsError):
    exit_code = 2
    code = "usage"


class EngineError(BttsError):
    exit_code = 3
    code = "engine"


class PlaybackError(BttsError):
    exit_code = 4
    code = "playback"


class IntegrityError(BttsError):
    exit_code = 5
    code = "integrity"
