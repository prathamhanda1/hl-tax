"""
tests/test_fx.py — GATE 4: the FX convention + the sourcing/freshness design.

The six convention scenarios (phase4_fx_spec.md §3) run against a small
hand-built series so every expected rate-date is known exactly and the test is
deterministic. The staleness/validation tests (phase4_fx_spec.md §5, §9) cover
the FBIL-via-Frankfurter design: the two-tier staleness model (local-behind vs
upstream-not-yet-published), the grace flag, and the fetch-time validators.

The fixture series (INR per USD), FBIL administrator, with deliberate gaps:
    2025-01-23 Thu  86.10
    2025-01-24 Fri  86.20
    (2025-01-25 Sat, 26 Sun, 27 Mon-holiday -> ABSENT)
    2025-01-28 Tue  86.55
    2025-01-29 Wed  86.60   <- coverage_end
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

import config
from fx.fx import (
    FxConverter, RateSeries, FXResult, attach_fx_columns,
    RateUnavailableError, StaleSeriesError, UpstreamNotYetPublishedError,
)
from fx.fetch_fx import validate_rows, find_large_gaps, FxFetchError

_SERIES = {
    date(2025, 1, 23): (86.10, "FBIL"),
    date(2025, 1, 24): (86.20, "FBIL"),
    date(2025, 1, 28): (86.55, "FBIL"),
    date(2025, 1, 29): (86.60, "FBIL"),  # coverage_end
}
_COVERAGE_END = date(2025, 1, 29)


@pytest.fixture(scope="module")
def conv():
    return FxConverter(RateSeries(dict(_SERIES)),
                       convention="event_time_prior_business_day",
                       publication_cutoff_ist="13:30")


def _utc(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def _ist_afternoon_utc(d: date) -> datetime:
    # 09:00 UTC = 14:30 IST: after the 13:30 cutoff, so ceiling == d.
    return datetime(d.year, d.month, d.day, 9, 0, tzinfo=timezone.utc)


# ===========================================================================
# The six required convention cases (§3)
# ===========================================================================

def test_normal_weekday_after_publication(conv):
    r = conv.rate_for_event(_utc(2025, 1, 28, 9, 0))  # 14:30 IST
    assert r.rate_date == date(2025, 1, 28)
    assert r.rate == 86.55
    assert r.source == "FBIL"
    assert r.convention == "event_time_prior_business_day"
    assert r.stale_grace is False


def test_saturday_resolves_to_friday(conv):
    r = conv.rate_for_event(_utc(2025, 1, 25, 12, 0))  # Sat 17:30 IST
    assert r.rate_date == date(2025, 1, 24)
    assert r.rate == 86.20


def test_sunday_resolves_to_friday(conv):
    r = conv.rate_for_event(_utc(2025, 1, 26, 12, 0))  # Sun (Republic Day)
    assert r.rate_date == date(2025, 1, 24)


def test_holiday_resolves_to_specific_prior_business_day(conv):
    r = conv.rate_for_event(_utc(2025, 1, 27, 12, 0))  # Mon holiday (absent)
    assert r.rate_date == date(2025, 1, 24)  # walks back over Sun/Sat to Fri
    assert r.rate == 86.20


def test_evening_utc_crosses_into_next_ist_day(conv):
    # 2025-01-23T20:00Z = 2025-01-24 01:30 IST -> IST date 24th, but before the
    # 13:30 cutoff so the 24th isn't published yet -> uses the 23rd.
    r = conv.rate_for_event(_utc(2025, 1, 23, 20, 0))
    assert r.rate_date == date(2025, 1, 23)
    r2 = conv.rate_for_event(_utc(2025, 1, 24, 9, 0))  # 14:30 IST, after cutoff
    assert r2.rate_date == date(2025, 1, 24)


def test_event_before_series_fails_loudly(conv):
    with pytest.raises(RateUnavailableError) as exc:
        conv.rate_for_event(_utc(2020, 1, 1, 12, 0))
    assert "2020" in str(exc.value)


def test_pre_publication_same_day_uses_previous_business_day(conv):
    r = conv.rate_for_event(_utc(2025, 1, 28, 3, 0))  # 08:30 IST, before cutoff
    assert r.rate_date == date(2025, 1, 24)
    r2 = conv.rate_for_event(_utc(2025, 1, 28, 9, 0))  # 14:30 IST, after
    assert r2.rate_date == date(2025, 1, 28)


# ===========================================================================
# Output shape (§3: audit columns) + input hygiene
# ===========================================================================

def test_result_carries_audit_fields(conv):
    r = conv.rate_for_event(_ist_afternoon_utc(date(2025, 1, 29)))
    assert isinstance(r, FXResult)
    assert (r.rate, r.rate_date, r.source, r.convention) == (
        86.60, date(2025, 1, 29), "FBIL", "event_time_prior_business_day")
    assert r.stale_grace is False


def test_naive_timestamp_rejected(conv):
    from fx.fx import FxError
    with pytest.raises(FxError):
        conv.rate_for_event(datetime(2025, 1, 28, 12, 0))  # no tzinfo


# ===========================================================================
# Two-tier staleness (§5) — the crux of foolproofness
# ===========================================================================

def test_within_grace_serves_with_stale_grace_flag(conv):
    # 2 days past coverage_end (2025-01-29): within the default grace window.
    r = conv.rate_for_event(_ist_afternoon_utc(date(2025, 1, 31)))
    assert r.rate_date == _COVERAGE_END          # last known
    assert r.stale_grace is True                 # flagged, not silent


def test_grace_boundary_serves(conv):
    boundary = _COVERAGE_END + timedelta(days=config.FX_MAX_STALENESS_DAYS)
    r = conv.rate_for_event(_ist_afternoon_utc(boundary))
    assert r.rate_date == _COVERAGE_END
    assert r.stale_grace is True


def test_past_grace_local_behind_raises_stale(conv):
    # No upstream_end known -> the safe generic error: refresh your local copy.
    too_far = _COVERAGE_END + timedelta(days=config.FX_MAX_STALENESS_DAYS + 1)
    with pytest.raises(StaleSeriesError) as exc:
        conv.rate_for_event(_ist_afternoon_utc(too_far))
    assert "--update" in str(exc.value)


def test_past_grace_upstream_ahead_raises_stale():
    # Upstream HAS advanced past this event, local copy hasn't -> refresh fixes it.
    c = FxConverter(RateSeries(dict(_SERIES)),
                    upstream_end=date(2025, 3, 1))
    with pytest.raises(StaleSeriesError):
        c.rate_for_event(_ist_afternoon_utc(date(2025, 2, 20)))


def test_past_grace_upstream_also_behind_raises_upstream_not_published():
    # Upstream itself lacks this recent date -> waiting/manual, not a refresh.
    c = FxConverter(RateSeries(dict(_SERIES)),
                    upstream_end=_COVERAGE_END)  # upstream == local, both behind
    with pytest.raises(UpstreamNotYetPublishedError) as exc:
        c.rate_for_event(_ist_afternoon_utc(date(2025, 2, 20)))
    assert "fbil.org.in" in str(exc.value)


def test_interior_hole_walk_back_is_flagged():
    # A real FBIL hole: 2025-01-24 then nothing until 2025-02-20. An event on
    # 2025-02-10 walks back 17 days -> served but flagged stale_grace.
    series = {
        date(2025, 1, 23): (86.10, "FBIL"),
        date(2025, 1, 24): (86.20, "FBIL"),
        date(2025, 2, 20): (86.70, "FBIL"),
    }
    c = FxConverter(RateSeries(series))
    r = c.rate_for_event(_ist_afternoon_utc(date(2025, 2, 10)))
    assert r.rate_date == date(2025, 1, 24)
    assert r.stale_grace is True


# ===========================================================================
# Fetch-time validation (§4, §9) — a bad fetch must never enter the series
# ===========================================================================

def test_validate_rejects_out_of_band_rate():
    with pytest.raises(FxFetchError):
        validate_rows([(date(2025, 1, 2), 86.1), (date(2025, 1, 3), 0.0)])
    with pytest.raises(FxFetchError):
        validate_rows([(date(2025, 1, 2), 9999.0)])  # decimal-shifted


def test_validate_rejects_non_increasing_dates():
    with pytest.raises(FxFetchError):
        validate_rows([(date(2025, 1, 3), 86.1), (date(2025, 1, 3), 86.2)])  # dup
    with pytest.raises(FxFetchError):
        validate_rows([(date(2025, 1, 3), 86.1), (date(2025, 1, 2), 86.2)])  # backwards


def test_validate_accepts_clean_window():
    validate_rows([(date(2025, 1, 2), 86.1), (date(2025, 1, 3), 86.2),
                   (date(2025, 1, 6), 86.3)])  # weekend gap is fine


def test_find_large_gaps_detects_interior_hole():
    rows = [(date(2021, 1, 29), 72.9), (date(2021, 2, 16), 72.7)]
    gaps = find_large_gaps(rows)
    assert gaps == [(date(2021, 1, 29), date(2021, 2, 16), 18)]
    # A normal weekend produces no gap.
    assert find_large_gaps([(date(2025, 1, 24), 86.2),
                            (date(2025, 1, 28), 86.5)]) == []


# ===========================================================================
# attach_fx_columns carries the grace flag through to the ledger
# ===========================================================================

def test_attach_fx_columns_adds_all_columns(conv):
    import pandas as pd
    df = pd.DataFrame({"ts": pd.to_datetime(
        ["2025-01-28T09:00:00Z", "2025-01-31T09:00:00Z"], utc=True)})
    res = attach_fx_columns(df, conv, ts_col="ts")
    for col in ("fx_rate", "fx_rate_date", "fx_source", "fx_convention",
                "fx_stale_grace"):
        assert col in res.columns
    assert res["fx_stale_grace"].tolist() == [False, True]  # 2nd row in grace


# ===========================================================================
# The embedded series copy (fx/embed.py)
#
# The deployed lambda reads the series from the embedded MODULE, not the CSV --
# Vercel's Python builder traces imports and does not copy data files, so the
# CSV never arrives (that is what made every /api/report return a 422 saying
# "FX series file not found: /var/task/fx/data/usdinr_reference.csv"). These
# tests guard the two ways that fallback could rot: drifting from the CSV, and
# not being reachable at all.
# ===========================================================================

def test_embedded_series_matches_csv():
    """The mirror is byte-identical to the CSV it claims to copy.

    Fails if someone refreshes the series and commits only the CSV -- which
    would deploy yesterday's rates while the repo showed today's.
    """
    from fx import embed
    embed.check()          # raises AssertionError with a regenerate hint


def test_embedded_series_is_reachable_and_parses():
    from fx import embed
    from fx.fx import RateSeries

    text = embed.read_embedded()
    assert text, "no embedded series: the deployed lambda would have no rates"
    assert text.startswith("rate_date,rate,")

    series = RateSeries.from_csv(config.FX_SERIES_FILE)
    assert series.max_date() >= date(2026, 7, 17)


def test_from_csv_falls_back_to_embedded_when_file_is_absent(tmp_path):
    """The serverless case: no CSV on disk, series still loads.

    Reproduces the deployed failure exactly -- point the reader at a path that
    does not exist and require a working series rather than an FxError.
    """
    from fx.fx import RateSeries

    missing = tmp_path / "not-deployed" / "usdinr_reference.csv"
    assert not missing.exists()

    series = RateSeries.from_csv(missing)
    on_disk = RateSeries.from_csv(config.FX_SERIES_FILE)
    assert series.max_date() == on_disk.max_date()
    assert series.min_date() == on_disk.min_date()
    assert series.rate_on(series.max_date()) == on_disk.rate_on(on_disk.max_date())


def test_from_csv_still_raises_when_nothing_is_available(tmp_path, monkeypatch):
    """No CSV and no embedded copy is still a loud FxError, not a silent zero."""
    import fx.embed as embed_mod
    from fx.fx import RateSeries, FxError

    monkeypatch.setattr(embed_mod, "_embedded", None)
    with pytest.raises(FxError, match="no embedded copy"):
        RateSeries.from_csv(tmp_path / "nope.csv")
