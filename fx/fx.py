"""
fx/fx.py — Phase 4 reader. Event timestamp -> the USD/INR rate to convert it at.

PURE and configurable. A rate-LOOKUP function, not an eager converter
(errors_in_plan.md #11): it answers "what rate, from which date, published by
whom, under which convention?" for one event; the interpret layer injects it.

THE RATE: the FBIL USD/INR reference rate — the benchmark Indian income-tax
foreign-currency conversion relies on — stored as an immutable vendored history
plus a moving tail, both pulled from Frankfurter's FBIL provider (fx/fetch_fx.py
does the pulling; this module only reads). FBIL/RBI is the cited authority;
Frankfurter is transport.

THE CONVENTION (phase4_fx_spec.md; echoed in every report):
  event_time_prior_business_day —
    1. Convert the event's UTC timestamp to IST FIRST, then take the calendar
       date (§2a: 2025-01-02T20:00Z is already 2025-01-03 IST).
    2. A reference rate only exists from ~13:30 IST on its publication day
       (FX_PUBLICATION_CUTOFF_IST), so an event BEFORE the cutoff uses the
       PREVIOUS business day (§2b).
    3. "Business day" is defined by the DATA: any date absent from the series is
       a non-business day, and the lookup walks backwards to the nearest present
       date (§2c) — weekends AND Mumbai holidays self-update from the file.

TWO-TIER STALENESS (phase4_fx_spec.md §5). The series is a snapshot advanced
only by a human running `python -m fx.fetch_fx --update`. "You forgot to
refresh" and "the rate for that date isn't published anywhere yet" have
DIFFERENT fixes and must not wear the same message. Let
gap = event_ceiling_date − coverage_end (the series' last date):
  - gap <= 0                        -> normal lookup.
  - 0 < gap <= FX_MAX_STALENESS_DAYS-> GRACE: serve the last known rate but
                                       stamp fx_stale_grace=True (visible, not
                                       silent). Covers "haven't refreshed today".
  - gap >  FX_MAX_STALENESS_DAYS    -> refuse, and pick the error by WHY:
      * upstream (FBIL) also lacks this date  -> UpstreamNotYetPublishedError
        (fix = wait for the next FBIL publication, or supply it manually).
      * upstream HAS it, only the local copy is behind -> StaleSeriesError
        (fix = `fetch_fx --update`).
  The reader stays PURE: it never calls the network. To distinguish the two it
  takes an optional `upstream_end` (FBIL's real latest, from the manifest or a
  live check done ONCE by orchestration/--status); absent that knowledge it
  raises the safe generic StaleSeriesError.
An event predating the series fails loudly with RateUnavailableError.

Every converted row carries: fx_rate, fx_rate_date, fx_source (the ADMINISTRATOR
— FBIL/RBI — the citable authority), fx_convention, and fx_stale_grace.
"""

from __future__ import annotations

import bisect
import csv
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

import config

IST = ZoneInfo(config.DISPLAY_TIMEZONE)


class FxError(Exception):
    """Base class for FX lookup failures."""


class RateUnavailableError(FxError):
    """No published rate exists on or before the required date (e.g. the event
    predates the cached series). Carries the specific missing date."""


class StaleSeriesError(FxError):
    """The event is past the series' end by more than the grace window, and the
    rate almost certainly EXISTS upstream — the LOCAL copy is just behind.
    Fix: `python -m fx.fetch_fx --update`."""


class UpstreamNotYetPublishedError(FxError):
    """The event is past the series' end by more than the grace window, and
    upstream (FBIL) has not published/propagated that date either. No refresh
    can help yet. Fix: wait for the next FBIL publication (~13:30 IST business
    days) or supply the rate manually from fbil.org.in."""


@dataclass(frozen=True)
class FXResult:
    rate: float          # INR per 1 USD
    rate_date: date      # the IST business date whose published rate was used
    source: str          # ADMINISTRATOR / authority (FBIL or RBI)
    convention: str      # named convention in effect for this run
    stale_grace: bool = False  # served within the grace window (rate_date lags event)


# ---------------------------------------------------------------------------
# The cached rate series
# ---------------------------------------------------------------------------

class RateSeries:
    """A date -> (rate, administrator) series. Presence of a date means "a
    reference rate was published that IST business day"; absence means "not a
    business day" (§2c). `administrator` is the citable authority (FBIL/RBI).
    """

    def __init__(self, rates: dict[date, tuple[float, str]]):
        if not rates:
            raise FxError("rate series is empty - run "
                          "`python -m fx.fetch_fx --reseed` to build it")
        self._rates = rates
        self._dates = sorted(rates)

    @classmethod
    def from_csv(cls, path: Path | str) -> "RateSeries":
        """Load a `rate_date,rate,source,administrator,fetched_at_utc` CSV. Only
        rate_date/rate/administrator are needed for lookup; the rest is
        provenance the reader carries but does not use."""
        path = Path(path)
        if not path.exists():
            raise FxError(
                f"FX series file not found: {path}. Build it with "
                f"`python -m fx.fetch_fx --reseed` (see fx/REFRESH.md)."
            )
        rates: dict[date, tuple[float, str]] = {}
        with path.open("r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                d = date.fromisoformat(row["rate_date"].strip())
                rate = float(row["rate"])
                admin = (row.get("administrator") or "").strip() or "FBIL"
                rates[d] = (rate, admin)
        return cls(rates)

    def min_date(self) -> date:
        return self._dates[0]

    def max_date(self) -> date:
        return self._dates[-1]

    def on_or_before(self, d: date) -> date | None:
        """The most recent series date <= d, or None if d precedes the series."""
        i = bisect.bisect_right(self._dates, d) - 1
        return self._dates[i] if i >= 0 else None

    def rate_on(self, d: date) -> tuple[float, str]:
        return self._rates[d]


# ---------------------------------------------------------------------------
# The converter
# ---------------------------------------------------------------------------

def _parse_cutoff(hhmm: str) -> time:
    h, m = hhmm.split(":")
    return time(int(h), int(m))


def _to_utc_datetime(ts) -> datetime:
    """Accept an epoch-ms int, a (tz-aware) pandas Timestamp, or a datetime;
    return a tz-aware UTC datetime. A naive datetime is rejected loudly — a
    missing tz is exactly how the IST-conversion bug (§2a) sneaks in."""
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
    if isinstance(ts, pd.Timestamp):
        if ts.tzinfo is None:
            raise FxError(f"naive timestamp {ts!r}: FX needs a tz-aware time")
        return ts.to_pydatetime().astimezone(timezone.utc)
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            raise FxError(f"naive datetime {ts!r}: FX needs a tz-aware time")
        return ts.astimezone(timezone.utc)
    raise FxError(f"unsupported timestamp type {type(ts).__name__}: {ts!r}")


class FxConverter:
    """Resolves one event timestamp to an FXResult under a named convention.

    `upstream_end` (optional): FBIL's real latest available date, from the
    manifest or a live check. When known, it lets a past-grace event raise the
    RIGHT error (UpstreamNotYetPublished vs Stale). When None, a past-grace
    event raises the safe generic StaleSeriesError.
    """

    def __init__(
        self,
        series: RateSeries,
        convention: str = config.FX_CONVENTION,
        publication_cutoff_ist: str = config.FX_PUBLICATION_CUTOFF_IST,
        max_staleness_days: int = config.FX_MAX_STALENESS_DAYS,
        upstream_end: date | None = None,
    ):
        self.series = series
        self.convention = convention
        self._cutoff = _parse_cutoff(publication_cutoff_ist)
        self.max_staleness_days = max_staleness_days
        self.upstream_end = upstream_end

    def _asof_ceiling_date(self, ts_utc: datetime) -> date:
        """The latest IST date whose rate could already be published as of this
        event: the event's IST date if at/after the publication cutoff, else the
        day before (§2a + §2b)."""
        ist_dt = ts_utc.astimezone(IST)
        if ist_dt.timetz().replace(tzinfo=None) < self._cutoff:
            return (ist_dt - pd.Timedelta(days=1)).date()
        return ist_dt.date()

    def rate_for_event(self, ts) -> FXResult:
        ts_utc = _to_utc_datetime(ts)
        ceiling = self._asof_ceiling_date(ts_utc)
        coverage_end = self.series.max_date()
        gap = (ceiling - coverage_end).days

        if gap > self.max_staleness_days:
            # Past the grace window: refuse, but say WHY correctly.
            if self.upstream_end is not None and ceiling > self.upstream_end:
                raise UpstreamNotYetPublishedError(
                    f"the FBIL USD/INR reference rate for {ceiling} (IST) is "
                    f"not yet published upstream (FBIL latest: "
                    f"{self.upstream_end}). Recent trades cannot be converted "
                    f"until it publishes - try again after the next FBIL "
                    f"publication (~13:30 IST business days), or supply the "
                    f"rate manually from fbil.org.in. Event: {ts_utc.isoformat()}."
                )
            raise StaleSeriesError(
                f"event {ts_utc.isoformat()} needs a rate for {ceiling} (IST), "
                f"but the local FX series ends {coverage_end} ({gap} days "
                f"stale, beyond the {self.max_staleness_days}-day grace "
                f"window). Run `python -m fx.fetch_fx --update` to refresh; the "
                f"tool will not silently reuse an old rate for a recent event."
            )

        rate_date = self.series.on_or_before(ceiling)
        if rate_date is None:
            raise RateUnavailableError(
                f"no published USD/INR rate on or before {ceiling} (IST) for "
                f"event {ts_utc.isoformat()}: the series begins "
                f"{self.series.min_date()}. This event predates the cached FX "
                f"series - extend the series (see fx/REFRESH.md for pre-2018 "
                f"RBI dates) or exclude the event; the tool will not guess."
            )
        rate, administrator = self.series.rate_on(rate_date)
        # stale_grace flags a rate_date that materially lags the event: either
        # a tail-grace event (past coverage_end but within the window), OR a
        # pathological INTERIOR hole where the walk-back exceeded a normal
        # weekend/holiday cluster (e.g. the real Feb-2021 gap in FBIL). Normal
        # weekends (walk-back <= a few days) are NOT flagged.
        walk_back = (ceiling - rate_date).days
        stale = (gap > 0) or (walk_back > self.max_staleness_days)
        return FXResult(
            rate=rate, rate_date=rate_date, source=administrator,
            convention=self.convention, stale_grace=stale,
        )


def _manifest_upstream_end() -> date | None:
    """FBIL's latest available date recorded at the last fetch, if the manifest
    has it. A reasonable default `upstream_end` for the reader that needs no
    network call (accurate if the series was refreshed recently)."""
    p = config.FX_MANIFEST_FILE
    if not p.exists():
        return None
    try:
        m = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    val = m.get("upstream_end")
    return date.fromisoformat(val) if val else None


def default_converter() -> FxConverter:
    """The converter the pipeline uses by default: the FBIL reference series
    under the event_time_prior_business_day convention, with `upstream_end`
    seeded from the manifest (offline; orchestration may override with a live
    check)."""
    series = RateSeries.from_csv(config.FX_DEFAULT_SERIES_FILE)
    return FxConverter(series, upstream_end=_manifest_upstream_end())


# ---------------------------------------------------------------------------
# Annotating a ledger DataFrame with the audit columns
# ---------------------------------------------------------------------------

def attach_fx_columns(
    df: pd.DataFrame, converter: FxConverter, ts_col: str = "ts"
) -> pd.DataFrame:
    """Return a copy of `df` with fx_rate / fx_rate_date / fx_source /
    fx_convention / fx_stale_grace added, one lookup per row's timestamp in
    `ts_col` (a tz-aware datetime column), memoised by resolved date.

    Adds RATE metadata only; the USD->INR multiplication is the consumer's
    (interpret layer's) job, so a conversion bug can never live in the FX layer.
    """
    if ts_col not in df.columns:
        raise FxError(f"attach_fx_columns: no '{ts_col}' column in DataFrame")

    out = df.copy()
    cache: dict = {}
    rates, rate_dates, sources, graces = [], [], [], []
    for ts in out[ts_col]:
        if pd.isna(ts):
            raise FxError("attach_fx_columns: a row has a null timestamp")
        key = ts.value if isinstance(ts, pd.Timestamp) else ts
        res = cache.get(key)
        if res is None:
            res = converter.rate_for_event(ts)
            cache[key] = res
        rates.append(res.rate)
        rate_dates.append(res.rate_date)
        sources.append(res.source)
        graces.append(res.stale_grace)
    out["fx_rate"] = rates
    out["fx_rate_date"] = rate_dates
    out["fx_source"] = sources
    out["fx_convention"] = converter.convention
    out["fx_stale_grace"] = graces
    return out
