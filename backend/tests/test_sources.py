"""Year-range validation for team sources."""

from datetime import date

import pytest
from fastapi import HTTPException


def _validate(kind, start, end):
    from app.api.sources import _validate_years
    from app.models import SourceKind

    _validate_years(SourceKind(kind), start, end)


def test_team_range_accepts_bounded_and_ongoing():
    _validate("team", 2022, 2026)
    _validate("team", 2018, 2018)
    _validate("team", 1992, None)
    _validate("team", None, None)


def test_team_range_rejects_years_before_frc():
    with pytest.raises(HTTPException) as exc:
        _validate("team", 1980, None)
    assert exc.value.status_code == 400


def test_team_range_rejects_end_before_start_and_future_years():
    with pytest.raises(HTTPException):
        _validate("team", 2024, 2022)
    with pytest.raises(HTTPException):
        _validate("team", 1992, date.today().year + 1)


def test_non_team_sources_reject_a_year_range():
    with pytest.raises(HTTPException):
        _validate("season", 2022, 2026)
