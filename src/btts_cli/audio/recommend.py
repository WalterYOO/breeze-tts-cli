"""Deterministic, metric-based candidate ranking for auditions.

The recommendation only eliminates technically failed takes and orders the
rest. It is **not** an aesthetic judgement: the final take is chosen by a
human listening to ``audition.html``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

CLIP_FAIL = 0.001
SILENCE_FAIL = 0.6
MISSING = float("inf")


@dataclass(frozen=True)
class Recommendation:
    take_id: str
    degraded: bool


def technical_flags(row: dict[str, Any]) -> list[str]:
    """Return short machine-readable reasons a take is technically unusable."""
    flags: list[str] = []
    if row.get("abnormal_short"):
        flags.append("short")
    if row.get("abnormal_long"):
        flags.append("long")
    if row.get("truncated"):
        flags.append("truncated")
    clipping = row.get("clipping_ratio")
    if clipping is not None and clipping > CLIP_FAIL:
        flags.append("clipping")
    silence = row.get("silence_ratio")
    if silence is not None and silence > SILENCE_FAIL:
        flags.append("silence")
    return flags


def rank_key(row: dict[str, Any]) -> tuple[Any, ...]:
    """Total order for candidates; smaller is better.

    Missing optional metrics sort after measured values but are not failures.
    ``take_id`` is the final tie-break so the result never depends on row order.
    """
    hnr = row.get("hnr_db")
    voiced = row.get("voiced_ratio")
    return (
        len(technical_flags(row)),
        row.get("clipping_ratio") if row.get("clipping_ratio") is not None else MISSING,
        row.get("silence_ratio") if row.get("silence_ratio") is not None else MISSING,
        -hnr if hnr is not None else MISSING,
        -voiced if voiced is not None else MISSING,
        row["take_id"],
    )


def choose_recommendation(rows: list[dict[str, Any]]) -> Recommendation | None:
    """Pick the best-ranked candidate, flagging a fully degraded matrix."""
    if not rows:
        return None
    best = min(rows, key=rank_key)
    return Recommendation(
        take_id=best["take_id"],
        degraded=bool(technical_flags(best)),
    )
