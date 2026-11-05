from __future__ import annotations

from typing import Any

from btts_cli.audio.recommend import (
    choose_recommendation,
    rank_key,
    technical_flags,
)


def row(take_id: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "take_id": take_id,
        "abnormal_short": False,
        "abnormal_long": False,
        "truncated": False,
        "clipping_ratio": 0.0,
        "silence_ratio": 0.0,
        "hnr_db": 15.0,
        "voiced_ratio": 0.8,
    }
    base.update(overrides)
    return base


def test_clean_candidate_with_higher_hnr_wins() -> None:
    rows = [row("c001", hnr_db=10.0), row("c002", hnr_db=20.0)]
    recommendation = choose_recommendation(rows)
    assert recommendation is not None
    assert recommendation.take_id == "c002"
    assert recommendation.degraded is False


def test_technical_flags_cover_all_failure_modes() -> None:
    assert technical_flags(row("c001", abnormal_short=True)) == ["short"]
    assert technical_flags(row("c001", abnormal_long=True)) == ["long"]
    assert technical_flags(row("c001", truncated=True)) == ["truncated"]
    assert technical_flags(row("c001", clipping_ratio=0.01)) == ["clipping"]
    assert technical_flags(row("c001", silence_ratio=0.9)) == ["silence"]
    assert technical_flags(row("c001")) == []


def test_truncated_take_loses_to_clean_take() -> None:
    rows = [
        row("c001", truncated=True, hnr_db=30.0),
        row("c002", hnr_db=10.0),
    ]
    recommendation = choose_recommendation(rows)
    assert recommendation is not None
    assert recommendation.take_id == "c002"


def test_all_degraded_returns_least_bad() -> None:
    rows = [
        row("c001", clipping_ratio=0.01),
        row("c002", silence_ratio=0.9),
        row("c003", abnormal_short=True),
    ]
    recommendation = choose_recommendation(rows)
    assert recommendation is not None
    assert recommendation.degraded is True
    # short still counts as one flag; clipping/silence break the tie in its favour.
    assert recommendation.take_id == "c003"


def test_missing_full_metrics_sort_after_measured_values() -> None:
    measured = row("c001", hnr_db=1.0, voiced_ratio=0.1)
    missing = row("c002", hnr_db=None, voiced_ratio=None)
    assert rank_key(measured) < rank_key(missing)
    recommendation = choose_recommendation([missing, measured])
    assert recommendation is not None
    assert recommendation.take_id == "c001"


def test_missing_metrics_are_not_technical_failures() -> None:
    rows = [row("c001", hnr_db=None, voiced_ratio=None)]
    recommendation = choose_recommendation(rows)
    assert recommendation is not None
    assert recommendation.degraded is False


def test_row_order_does_not_change_recommendation() -> None:
    rows = [
        row("c001", hnr_db=10.0),
        row("c002", hnr_db=20.0),
        row("c003", hnr_db=20.0, silence_ratio=0.01),
    ]
    forward = choose_recommendation(rows)
    backward = choose_recommendation(list(reversed(rows)))
    assert forward == backward
    assert forward is not None
    assert forward.take_id == "c002"


def test_take_id_is_the_final_tie_break() -> None:
    rows = [row("c002"), row("c001")]
    recommendation = choose_recommendation(rows)
    assert recommendation is not None
    assert recommendation.take_id == "c001"


def test_empty_rows_have_no_recommendation() -> None:
    assert choose_recommendation([]) is None
