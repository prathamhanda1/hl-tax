"""
present/ca_report.py — Phase 9. The financial-year CSV bundle for a CA.

    from present.ca_report import available_fys, build_ca_report
    zip_path = build_ca_report(rec.closing_events, rec.open_positions,
                               ledger, meta, out_dir, fy="2025-26")

WHY THIS EXISTS, GIVEN summary.html ALREADY DOES
------------------------------------------------
`summary.html` argues the tax question: four readings side by side, section
references, unsettled-law caveats. That is the right artifact for a taxpayer
deciding how to file. It is the wrong artifact to hand a Chartered Accountant.
A CA does not want our reading of the statute; they want THE NUMBERS,
STRUCTURED, FOR ONE FINANCIAL YEAR, in the genre they already process from
Indian exchanges — one sheet per product, one row per transaction, every column
a fact (pair, side, quantity, price, gross, fees, net, TDS), and no tax
computation anywhere. CoinDCX's Trade Report (docs/phase5data_dcx.md) is that
genre, and it is explicit that it computes no liability. This module is the
equivalent for a Hyperliquid address.

Because the venue is perps-only, sheet 01 is modelled on CoinDCX's *Futures
Orders* sheet (realised P&L per transaction), not their Spot sheet: there is no
per-leg quantity-of-crypto-transferred here, only closes of a net position.
CoinDCX's own FAQ is the authority for two facts this bundle asserts: no
deduction at source applies to futures and options, because no ownership of a
crypto asset is transferred; and futures P&L carries no fixed rate — which is
precisely why this bundle applies none.

LAYER DISCIPLINE — THIS FILE COMPUTES NOTHING NEW
-------------------------------------------------
Every number already exists in the INR ledger or the reconstruction. This module
only JOINS, FILTERS BY FY, ORDERS, LABELS, AGGREGATES and WRITES. A bug here can
never change a tax number. Two consequences, both load-bearing:

  * The ledger is the ONLY source of `fx_rate` / `amount_inr`. This module never
    imports `fx` and never multiplies a USD figure by a rate. The join back to
    `closing_events` exists solely to recover the execution facts the ledger
    flattens away (entry_px, exit_px, size_closed, direction, open_ts).
  * `turnover_inr` is imported from `interpret.treatments`, not reimplemented,
    so this bundle and the four-reading analysis can never disagree about what
    turnover means.

NEUTRALITY IS THE POINT, NOT A DISCLAIMER (brief non-negotiable #2)
------------------------------------------------------------------
No rate, no named reading, no liability figure, no "recommended" anything
appears anywhere in this bundle — sheets 00 and 06 both say so verbatim. The
four-reading analysis stays in summary.html, a separate document with a separate
purpose. `tests/test_ca_report.py::test_bundle_asserts_no_rate_and_names_no_reading`
is the enforcement mechanism; it greps the whole bundle. Keep it.

THINGS THAT LOOK LIKE ROUNDING ERRORS AND ARE NOT
-------------------------------------------------
  * A close whose entry predates the ~10k-fill history window has NO computable
    cost basis. Its money cells are BLANK, never 0, it is excluded from every
    total (np.nansum, mirroring reconstruct.total_realized_usd), and it is
    counted in sheet 00 and explained in sheet 06. A silent zero is a wrong tax
    number wearing a green checkmark.
  * FY membership is decided by the REALISATION date (`close_ts`) in IST. An
    episode opened in FY 2024-25 and closed in FY 2025-26 belongs to 2025-26;
    the row's `open_fy` column makes that visible rather than surprising.
  * Deposits and withdrawals are USDC movements ON the venue, not the INR<->USDC
    conversion. They contribute ZERO to every total and are present only so the
    bundle reconciles.
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

import config
from interpret.treatments import (
    CAT_DEPOSIT,
    CAT_FEE,
    CAT_FUNDING,
    CAT_REALIZED,
    CAT_WITHDRAWAL,
    turnover_inr,
)

# The seven sheets, in the order a reader should meet them.
FY_SHEETS = [
    "00_summary",
    "01_futures_trades",
    "02_funding",
    "03_fees",
    "04_cashflows",
    "05_open_positions",
    "06_assumptions_and_notes",
]

# The one paragraph that makes this bundle honest. Reproduced verbatim in both
# sheet 00 and sheet 06 — a CA may read either one first.
NEUTRALITY_STATEMENT = (
    "This report contains transaction data and arithmetic totals only. No tax "
    "rate has been applied and no tax treatment has been selected. The tax "
    "treatment of perpetual-futures P&L on a foreign venue is unsettled in "
    "Indian law; determining it is the responsibility of a qualified Chartered "
    "Accountant."
)

_ALL = "all"                      # fy=None -> every event, labelled "all"
_FY_RE = re.compile(r"^(\d{4})-(\d{2})$")
_QUOTE_CURRENCY = "USDC"          # the venue's settlement/margin currency


class FyFormatError(ValueError):
    """The FY string is not a well-formed Indian financial year label.

    A well-formed FY with no activity is NOT this error — it is a valid, empty
    bundle (see the module docstring). Only unparseable input lands here, so the
    HTTP layer can map it to 400 and nothing else.
    """


# ===========================================================================
# Financial-year arithmetic. Get this right before anything else: every filter
# in this file depends on it, and an off-by-one here silently moves income from
# one tax year into another.
# ===========================================================================

def _fy_start_year(fy: str) -> int:
    """'2025-26' -> 2025, validating that the halves are consecutive years."""
    m = _FY_RE.match(str(fy).strip())
    if not m:
        raise FyFormatError(
            f"Not a financial year: {fy!r}. Expected 'YYYY-YY' with "
            f"consecutive years, e.g. '2025-26'."
        )
    start = int(m.group(1))
    if int(m.group(2)) != (start + 1) % 100:
        raise FyFormatError(
            f"Not a financial year: {fy!r}. An Indian FY spans two consecutive "
            f"years, so {start} must be followed by {(start + 1) % 100:02d}."
        )
    return start


def fy_label(start_year: int) -> str:
    """2025 -> '2025-26'."""
    return f"{int(start_year)}-{(int(start_year) + 1) % 100:02d}"


def fy_bounds(fy: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    """FY label -> (start, end_exclusive) as UTC instants, derived in IST.

    Half-open on purpose: `[1 Apr 00:00:00 IST, next 1 Apr 00:00:00 IST)` is
    exactly the closed range ending 31 Mar 23:59:59.999999999 IST, without
    inventing a last-representable-nanosecond constant that a future pandas
    could redefine under us.
    """
    y = _fy_start_year(fy)
    tz = config.FY_TIMEZONE
    start = pd.Timestamp(year=y, month=config.FY_START_MONTH, day=1, tz=tz)
    end = pd.Timestamp(year=y + 1, month=config.FY_START_MONTH, day=1, tz=tz)
    return start.tz_convert("UTC"), end.tz_convert("UTC")


def fy_of(ts_utc) -> str | None:
    """UTC instant -> the Indian FY it falls in, evaluated in IST.

    Naive input is assumed UTC (every timestamp in this engine is tz-aware UTC
    by the time it reaches the presentation layer). Missing -> None.
    """
    if ts_utc is None or pd.isna(ts_utc):
        return None
    t = pd.Timestamp(ts_utc)
    t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
    t = t.tz_convert(config.FY_TIMEZONE)
    start = t.year if t.month >= config.FY_START_MONTH else t.year - 1
    return fy_label(start)


def _fy_series(ts: pd.Series) -> pd.Series:
    """Vectorised `fy_of` over a UTC datetime Series. A heavy account's ledger
    runs to tens of thousands of rows; a Python loop here is felt."""
    if not len(ts):
        return pd.Series([], dtype="object")
    ist = pd.to_datetime(ts, utc=True).dt.tz_convert(config.FY_TIMEZONE)
    start = ist.dt.year.where(ist.dt.month >= config.FY_START_MONTH,
                              ist.dt.year - 1)
    return start.map(lambda y: fy_label(y) if pd.notna(y) else None)


def available_fys(ledger: pd.DataFrame) -> list[str]:
    """Every FY in which this address had ANY ledger event, most recent first.

    "Any" means exactly that — one fill in a year makes that year available. The
    dropdown this feeds must never hide a year that contains a transaction, and
    the current, incomplete FY is offered like any other (sheet 00 states
    whether the window has closed).
    """
    if ledger is None or not len(ledger):
        return []
    labels = _fy_series(ledger["ts_utc"]).dropna().unique().tolist()
    return sorted(labels, reverse=True)


def fy_mask(ledger: pd.DataFrame, fy: str | None) -> pd.Series:
    """Boolean mask over an ALREADY-ASSEMBLED ledger.

    Filtering upstream instead would change which events the FX layer is asked
    to price, and could turn a loud coverage error into a silent omission — so
    the FY filter is always the last thing that happens, never the first.
    """
    if not len(ledger):
        return pd.Series([], dtype=bool)
    if fy is None:
        return pd.Series(True, index=ledger.index)
    start, end = fy_bounds(fy)
    ts = pd.to_datetime(ledger["ts_utc"], utc=True)
    return (ts >= start) & (ts < end)


# ===========================================================================
# Small display / aggregation helpers.
# ===========================================================================

def _ist(ts) -> str:
    """UTC instant -> 'YYYY-MM-DD HH:MM:SS' in IST: the Indian wall-clock time a
    CA files against."""
    if ts is None or pd.isna(ts):
        return ""
    t = pd.Timestamp(ts)
    t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
    return t.tz_convert(config.FY_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")


def _iso(ts) -> str:
    if ts is None or pd.isna(ts):
        return ""
    t = pd.Timestamp(ts)
    t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _date(v) -> str:
    if v is None or pd.isna(v):
        return ""
    if isinstance(v, str):
        return v
    return pd.Timestamp(v).strftime("%Y-%m-%d")


def _f(v) -> float:
    """Anything -> float, with every flavour of missing becoming NaN, never 0."""
    if v is None:
        return float("nan")
    try:
        if pd.isna(v):
            return float("nan")
    except (TypeError, ValueError):
        pass
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


def _inr(v) -> float:
    """An INR figure, rounded for readability if configured. NaN stays NaN —
    rounding a missing number into existence is the one thing this must not do."""
    x = _f(v)
    if config.CA_REPORT_ROUND_INR and x == x:
        return round(x, config.REPORT_INR_DECIMALS)
    return x


def _nansum(values) -> float:
    """Sum that SKIPS the incomputable rather than treating it as zero — the same
    choice reconstruct.total_realized_usd makes, for the same reason."""
    arr = np.asarray([_f(v) for v in values], dtype="float64")
    if not arr.size:
        return 0.0
    return float(np.nansum(arr))


def _positive(values) -> float:
    arr = np.asarray([_f(v) for v in values], dtype="float64")
    if not arr.size:
        return 0.0
    return float(np.nansum(np.where(arr > 0, arr, 0.0)))


def _negative(values) -> float:
    """Kept NEGATIVE. A loss shown as a positive number is a loss waiting to be
    added to a gain by a tired human."""
    arr = np.asarray([_f(v) for v in values], dtype="float64")
    if not arr.size:
        return 0.0
    return float(np.nansum(np.where(arr < 0, arr, 0.0)))


def _pair(asset) -> str:
    """An Indian exchange futures report names a row by its contract pair; so do
    we, in a form that cannot be mistaken for a spot pair."""
    a = "" if asset is None else str(asset)
    return f"{a}-PERP/{_QUOTE_CURRENCY}" if a else ""


def _blank(columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series([], dtype="object") for c in columns})


def _tid_of(event_id) -> int | None:
    """'rp:12345' / 'fee:12345' -> 12345. The ledger's traceability id is the
    join key back to the closing event that produced it."""
    try:
        return int(str(event_id).split(":", 1)[1])
    except (IndexError, ValueError):
        return None


# ===========================================================================
# Sheet 01 — one row per closing episode (the futures-report genre).
# ===========================================================================

TRADE_COLUMNS = [
    "sr_no", "transaction_id", "asset", "crypto_pair", "asset_class",
    "base_currency", "direction", "type_of_transaction",
    "open_ts_utc", "open_ts_ist", "open_fy", "close_ts_utc", "close_ts_ist",
    "size_closed", "entry_px_usd", "exit_px_usd",
    "gross_notional_usd", "gross_notional_inr",
    "realized_pnl_usd", "realized_pnl_inr",
    "fee_usd", "fee_inr", "net_pnl_inr",
    "fx_rate", "fx_rate_date", "fx_stale_grace",
    "is_liquidation", "cost_basis_incomplete", "episode_id", "event_id",
]


def _trades_frame(closing_events: pd.DataFrame,
                  ledger_fy: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Join this window's realised-P&L ledger rows to their closing events.

    Driven from the LEDGER, not from `closing_events`: the ledger is what every
    total is computed over, so driving from it makes "exactly one row per
    realised ledger row" true by construction, and a dropped or duplicated trade
    impossible. Returns (frame, join_misses); a miss is a ledger row whose
    closing event cannot be found, which would be an upstream inconsistency, so
    it is counted and reported in sheet 06 rather than swallowed.
    """
    rp = ledger_fy[ledger_fy["category"] == CAT_REALIZED]
    if not len(rp):
        return _blank(TRADE_COLUMNS), 0

    # Execution facts by close_tid, and the matching fee row by the same tid.
    ev_by_tid: dict[int, object] = {}
    if closing_events is not None and len(closing_events):
        for e in closing_events.itertuples(index=False):
            ev_by_tid[int(e.close_tid)] = e
    fee_by_tid: dict[int, object] = {}
    for r in ledger_fy[ledger_fy["category"] == CAT_FEE].itertuples(index=False):
        tid = _tid_of(r.event_id)
        if tid is not None:
            fee_by_tid[tid] = r

    rows: list[dict] = []
    misses = 0
    for i, r in enumerate(rp.sort_values("ts_utc", kind="stable")
                          .itertuples(index=False), start=1):
        tid = _tid_of(r.event_id)
        e = ev_by_tid.get(tid)
        if e is None:
            misses += 1
        fee = fee_by_tid.get(tid)

        incomplete = bool(getattr(r, "cost_basis_incomplete", False))
        # A close with no computable cost basis carries BLANK money columns.
        # Not zero. See the module docstring.
        realized_usd = np.nan if incomplete else _f(r.amount_usd)
        realized_inr = np.nan if incomplete else _inr(r.amount_inr)
        fee_usd = _f(getattr(fee, "amount_usd", np.nan))
        fee_inr = _inr(getattr(fee, "amount_inr", np.nan))
        if realized_inr != realized_inr:            # NaN -> stays blank
            net_inr = np.nan
        else:
            net_inr = _inr(realized_inr -
                           (0.0 if fee_inr != fee_inr else fee_inr))
        open_ts = (None if e is None
                   else pd.Timestamp(int(e.open_ts), unit="ms", tz="UTC"))

        rows.append({
            "sr_no": i,
            "transaction_id": tid,
            "asset": r.asset,
            "crypto_pair": _pair(r.asset),
            "asset_class": r.asset_class,
            "base_currency": _QUOTE_CURRENCY,
            "direction": "" if e is None else e.direction,
            "type_of_transaction": "realized_pnl_on_close",
            "open_ts_utc": _iso(open_ts),
            "open_ts_ist": _ist(open_ts),
            "open_fy": "" if open_ts is None else (fy_of(open_ts) or ""),
            "close_ts_utc": _iso(r.ts_utc),
            "close_ts_ist": _ist(r.ts_utc),
            "size_closed": np.nan if e is None else _f(e.size_closed),
            "entry_px_usd": np.nan if e is None else _f(e.entry_px),
            "exit_px_usd": np.nan if e is None else _f(e.exit_px),
            "gross_notional_usd": _f(getattr(r, "gross_consideration_usd",
                                             np.nan)),
            "gross_notional_inr": _inr(getattr(r, "gross_consideration_inr",
                                               np.nan)),
            "realized_pnl_usd": realized_usd,
            "realized_pnl_inr": realized_inr,
            "fee_usd": fee_usd,
            "fee_inr": fee_inr,
            "net_pnl_inr": net_inr,
            "fx_rate": _f(r.fx_rate),
            "fx_rate_date": _date(r.fx_rate_date),
            "fx_stale_grace": bool(getattr(r, "fx_stale_grace", False)),
            "is_liquidation": bool(r.is_liquidation),
            "cost_basis_incomplete": incomplete,
            "episode_id": r.episode_id if pd.notna(r.episode_id) else "",
            "event_id": r.event_id,
        })
    return pd.DataFrame(rows, columns=TRADE_COLUMNS), misses


# ===========================================================================
# Sheets 02-05.
# ===========================================================================

FUNDING_COLUMNS = [
    "sr_no", "ts_utc", "ts_ist", "asset", "crypto_pair", "asset_class",
    "funding_usd", "fx_rate", "fx_rate_date", "funding_inr",
    "funding_rate", "position_size_szi", "event_id",
]


def _funding_frame(ledger_fy: pd.DataFrame,
                   funding_ledger: pd.DataFrame | None) -> pd.DataFrame:
    """Funding gets its own sheet because it is its own animal: a HOLDING cost,
    structurally unlike execution P&L and unlike a venue fee, and never netted
    into either (errors_in_plan.md #6). Sign: + = received, - = paid.

    funding_rate and position_size_szi survive only in the upstream funding
    ledger — the INR ledger flattens them away — so they are recovered through
    the positional index the assembler encoded into the event id
    (fund:<ts>:<asset>:<i>). Without that frame the two columns are blank,
    never invented.
    """
    src = ledger_fy[ledger_fy["category"] == CAT_FUNDING]
    if not len(src):
        return _blank(FUNDING_COLUMNS)

    detail: dict[int, tuple[float, float]] = {}
    if funding_ledger is not None and len(funding_ledger):
        for i, f in enumerate(funding_ledger.itertuples(index=False)):
            detail[i] = (_f(getattr(f, "funding_rate", np.nan)),
                         _f(getattr(f, "szi", np.nan)))

    rows: list[dict] = []
    for i, r in enumerate(src.sort_values("ts_utc", kind="stable")
                          .itertuples(index=False), start=1):
        try:
            idx = int(str(r.event_id).rsplit(":", 1)[-1])
        except (ValueError, IndexError):
            idx = -1
        rate, szi = detail.get(idx, (np.nan, np.nan))
        rows.append({
            "sr_no": i,
            "ts_utc": _iso(r.ts_utc),
            "ts_ist": _ist(r.ts_utc),
            "asset": r.asset,
            "crypto_pair": _pair(r.asset),
            "asset_class": r.asset_class,
            "funding_usd": _f(r.amount_usd),
            "fx_rate": _f(r.fx_rate),
            "fx_rate_date": _date(r.fx_rate_date),
            "funding_inr": _inr(r.amount_inr),
            "funding_rate": rate,
            "position_size_szi": szi,
            "event_id": r.event_id,
        })
    return pd.DataFrame(rows, columns=FUNDING_COLUMNS)


FEE_COLUMNS = [
    "sr_no", "ts_utc", "ts_ist", "asset", "crypto_pair", "asset_class",
    "episode_id", "fee_usd", "fx_rate", "fx_rate_date", "fee_inr",
    "is_liquidation", "event_id",
]


def _fees_frame(ledger_fy: pd.DataFrame) -> pd.DataFrame:
    """Every fee event. Deliberately duplicates sheet 01 per-trade fee column: a
    CA wants the total fee line as its own figure, reconcilable on its own,
    without unpicking it out of a trade sheet. Sign: + = paid, - = maker rebate.
    """
    src = ledger_fy[ledger_fy["category"] == CAT_FEE]
    if not len(src):
        return _blank(FEE_COLUMNS)
    rows: list[dict] = []
    for i, r in enumerate(src.sort_values("ts_utc", kind="stable")
                          .itertuples(index=False), start=1):
        rows.append({
            "sr_no": i,
            "ts_utc": _iso(r.ts_utc),
            "ts_ist": _ist(r.ts_utc),
            "asset": r.asset,
            "crypto_pair": _pair(r.asset),
            "asset_class": r.asset_class,
            "episode_id": r.episode_id if pd.notna(r.episode_id) else "",
            "fee_usd": _f(r.amount_usd),
            "fx_rate": _f(r.fx_rate),
            "fx_rate_date": _date(r.fx_rate_date),
            "fee_inr": _inr(r.amount_inr),
            "is_liquidation": bool(r.is_liquidation),
            "event_id": r.event_id,
        })
    return pd.DataFrame(rows, columns=FEE_COLUMNS)


CASHFLOW_COLUMNS = [
    "sr_no", "ts_utc", "ts_ist", "type", "asset", "amount_usd",
    "fx_rate", "fx_rate_date", "amount_inr", "tds_inr", "tx_reference",
    "event_id",
]


def _cashflows_frame(ledger_fy: pd.DataFrame) -> pd.DataFrame:
    """USDC deposits and withdrawals ON THE VENUE — not the INR/USDC conversion,
    which happened at an Indian exchange and is invisible to this tool. They
    contribute ZERO to every total; they are here so the bundle reconciles and
    every rupee is traceable. tds_inr is 0.00 on every row for the reason sheet
    06 gives: a foreign venue has no Indian deductor.
    """
    src = ledger_fy[ledger_fy["category"].isin([CAT_DEPOSIT, CAT_WITHDRAWAL])]
    if not len(src):
        return _blank(CASHFLOW_COLUMNS)
    rows: list[dict] = []
    for i, r in enumerate(src.sort_values("ts_utc", kind="stable")
                          .itertuples(index=False), start=1):
        parts = str(r.event_id).split(":")
        ref = parts[2] if len(parts) > 2 and str(parts[2]).startswith("0x") else ""
        rows.append({
            "sr_no": i,
            "ts_utc": _iso(r.ts_utc),
            "ts_ist": _ist(r.ts_utc),
            "type": r.category,
            "asset": r.asset,
            "amount_usd": _f(r.amount_usd),
            "fx_rate": _f(r.fx_rate),
            "fx_rate_date": _date(r.fx_rate_date),
            "amount_inr": _inr(r.amount_inr),
            "tds_inr": _inr(0.0),
            "tx_reference": ref,
            "event_id": r.event_id,
        })
    return pd.DataFrame(rows, columns=CASHFLOW_COLUMNS)


OPEN_POSITION_COLUMNS = [
    "sr_no", "asset", "crypto_pair", "asset_class", "direction",
    "open_ts_utc", "open_ts_ist", "open_fy", "size", "entry_px_usd",
    "cost_basis_incomplete",
]


def _open_positions_frame(open_positions: pd.DataFrame,
                          fy: str | None,
                          ledger: pd.DataFrame | None = None) -> pd.DataFrame:
    """Positions still open, restricted to those opened on or before the end of
    the window.

    NO UNREALISED P&L IS COMPUTED, deliberately, for two independent reasons: it
    would need a mark price this engine never fetches, and an unrealised amount
    is income only under a mark-to-market reading nobody here has adopted. Sheet
    06 says both.

    Honest limit, stated in sheet 06 rather than papered over: these are the
    positions open at the END OF THE AVAILABLE DATA, not positions reconstructed
    as at the year boundary. A true year-end snapshot is what bounding the run
    with --as-of at the year end is for.
    """
    if open_positions is None or not len(open_positions):
        return _blank(OPEN_POSITION_COLUMNS)
    # reconstruct leaves asset_class unset on this frame; recover it from the
    # ledger, where the same asset already carries it. A lookup, not a guess.
    classes: dict[str, str] = {}
    if ledger is not None and len(ledger):
        for a, k in zip(ledger["asset"], ledger["asset_class"]):
            if a is not None and k and a not in classes:
                classes[str(a)] = str(k)
    end = None if fy is None else fy_bounds(fy)[1]
    rows: list[dict] = []
    for p in open_positions.itertuples(index=False):
        open_ts = pd.Timestamp(int(p.open_ts), unit="ms", tz="UTC")
        if end is not None and open_ts >= end:
            continue                      # opened after this window closed
        rows.append({
            "sr_no": len(rows) + 1,
            "asset": p.asset,
            "crypto_pair": _pair(p.asset),
            "asset_class": (p.asset_class if p.asset_class
                            else classes.get(str(p.asset), "")),
            "direction": p.direction,
            "open_ts_utc": _iso(open_ts),
            "open_ts_ist": _ist(open_ts),
            "open_fy": fy_of(open_ts) or "",
            "size": _f(p.size),
            "entry_px_usd": _f(p.entry_px),
            "cost_basis_incomplete": bool(p.cost_basis_incomplete),
        })
    if not rows:
        return _blank(OPEN_POSITION_COLUMNS)
    return pd.DataFrame(rows, columns=OPEN_POSITION_COLUMNS)


# ===========================================================================
# Sheet 00 — the summary. Key/value rows: no rate, no reading, no liability.
# ===========================================================================

SUMMARY_COLUMNS = ["item", "value", "unit", "note"]


def _summary_frame(trades: pd.DataFrame, funding: pd.DataFrame,
                   fees: pd.DataFrame, cashflows: pd.DataFrame,
                   open_pos: pd.DataFrame, ledger_fy: pd.DataFrame,
                   meta: dict, fy: str | None) -> pd.DataFrame:
    """Identity, counts, and NEUTRAL TOTALS: plain arithmetic over the sheets
    below, with nothing applied to the result."""
    rows: list[tuple] = []

    def add(item, value, unit="", note=""):
        rows.append((item, value, unit, note))

    # --- identity ---------------------------------------------------------
    add("address", meta.get("address", ""), "",
        "Public wallet address. This tool never sees anything else.")
    add("financial_year", fy if fy is not None else _ALL, "",
        "Boundaries evaluated in IST: 1 Apr 00:00:00 to 31 Mar 23:59:59."
        if fy is not None else
        "Every event on record, not scoped to a single year.")
    if fy is not None:
        start, end = fy_bounds(fy)
        add("fy_start_ist", _ist(start), "IST", "")
        add("fy_end_ist", _ist(end - pd.Timedelta(nanoseconds=1)), "IST", "")
        add("fy_window_complete", bool(pd.Timestamp.now(tz="UTC") >= end), "",
            "False means this financial year has not ended yet, so the figures "
            "below describe a year still in progress.")
    add("generated_utc", meta.get("generated_utc", ""), "UTC", "")
    if len(ledger_fy):
        lo = pd.to_datetime(ledger_fy["ts_utc"], utc=True).min()
        hi = pd.to_datetime(ledger_fy["ts_utc"], utc=True).max()
        rng = f"{_ist(lo)[:10]} to {_ist(hi)[:10]}"
    else:
        rng = "no events in this window"
    add("event_range_in_fy", rng, "IST dates", "")
    add("cost_basis_convention",
        meta.get("cost_basis_convention", config.COST_BASIS_CONVENTION), "",
        "Explained in 06_assumptions_and_notes.csv.")
    add("fx_convention", meta.get("fx_convention", config.FX_CONVENTION), "",
        "Explained in 06_assumptions_and_notes.csv.")

    # --- counts -----------------------------------------------------------
    add("closing_trades", int(len(trades)), "rows", "01_futures_trades.csv")
    add("funding_events", int(len(funding)), "rows", "02_funding.csv")
    add("fee_events", int(len(fees)), "rows", "03_fees.csv")
    add("cashflow_events", int(len(cashflows)), "rows", "04_cashflows.csv")
    add("open_positions_at_window_end", int(len(open_pos)), "rows",
        "05_open_positions.csv. Carries no unrealised figure.")
    add("liquidation_closes",
        int(trades["is_liquidation"].sum()) if len(trades) else 0, "rows",
        "Closes flagged as liquidations upstream.")
    n_inc = int(trades["cost_basis_incomplete"].sum()) if len(trades) else 0
    add("closes_with_incomplete_cost_basis", n_inc, "rows",
        "Their money cells are BLANK, not zero, and they are excluded from "
        "every total below. Cause in 06_assumptions_and_notes.csv.")

    # --- neutral totals ---------------------------------------------------
    r_inr = trades["realized_pnl_inr"] if len(trades) else []
    r_usd = trades["realized_pnl_usd"] if len(trades) else []
    f_inr = funding["funding_inr"] if len(funding) else []
    f_usd = funding["funding_usd"] if len(funding) else []
    fee_inr_col = fees["fee_inr"] if len(fees) else []
    fee_usd_col = fees["fee_usd"] if len(fees) else []

    gross_profit = _positive(r_inr)
    gross_loss = _negative(r_inr)
    net_realized = _nansum(r_inr)
    total_fees = _nansum(fee_inr_col)
    net_funding = _nansum(f_inr)

    add("gross_realized_profit_inr", _inr(gross_profit), "INR",
        "Sum of the positive realised amounts only.")
    add("gross_realized_loss_inr", _inr(gross_loss), "INR",
        "Sum of the negative realised amounts, kept negative.")
    add("net_realized_pnl_inr", _inr(net_realized), "INR",
        "Their sum. Incomputable closes are skipped, not zeroed.")
    add("net_realized_pnl_usd", _f(_nansum(r_usd)), "USD", "")
    add("total_fees_inr", _inr(total_fees), "INR",
        "Positive means a net cost after any maker rebates.")
    add("total_fees_usd", _f(_nansum(fee_usd_col)), "USD", "")
    add("funding_received_inr", _inr(_positive(f_inr)), "INR", "")
    add("funding_paid_inr", _inr(_negative(f_inr)), "INR", "Kept negative.")
    add("net_funding_inr", _inr(net_funding), "INR", "")
    add("net_funding_usd", _f(_nansum(f_usd)), "USD", "")
    add("net_pnl_after_fees_and_funding_inr",
        _inr(net_realized + net_funding - total_fees), "INR",
        "net_realized_pnl_inr + net_funding_inr - total_fees_inr. Arithmetic "
        "only. This is not a taxable base and nothing has been applied to it.")
    add("turnover_inr", _inr(turnover_inr(ledger_fy)), "INR",
        "Absolute sum of the per-episode realised amounts, the ICAI guidance "
        "measure of derivative turnover. NOT net P&L and NOT notional.")

    dep = cashflows[cashflows["type"] == CAT_DEPOSIT] if len(cashflows) else []
    wdr = cashflows[cashflows["type"] == CAT_WITHDRAWAL] if len(cashflows) else []
    add("total_deposits_inr",
        _inr(_nansum(dep["amount_inr"] if len(dep) else [])), "INR",
        "USDC moved onto the venue. Contributes 0 to every total above; shown "
        "for reconciliation.")
    add("total_withdrawals_inr",
        _inr(_nansum(wdr["amount_inr"] if len(wdr) else [])), "INR",
        "USDC moved off the venue, kept negative. Contributes 0 to every total "
        "above.")
    add("tds_deducted_on_venue_inr", _inr(0.0), "INR",
        "Zero, and not an omission: this is a foreign venue with no Indian "
        "intermediary to deduct. Anything withheld on your INR/USDC conversion "
        "was withheld at the Indian exchange, appears in Form 26AS, and is "
        "creditable. See 06_assumptions_and_notes.csv.")

    # --- the three rows that keep this bundle honest ----------------------
    add("neutrality", NEUTRALITY_STATEMENT, "", "")
    add("scope", "Hyperliquid perpetual-futures activity for this address only. "
                 "Activity on any other venue, and the INR cost of acquiring "
                 "the USDC used here, are outside what this tool can see.",
        "", "")
    add("not_advice", "Generated by a read-only reporting tool from public "
                      "on-chain data. It is not advice and asserts no "
                      "liability.", "", "")
    return pd.DataFrame(rows, columns=SUMMARY_COLUMNS)


# ===========================================================================
# Sheet 06 — assumptions and notes. Prose, but load-bearing prose.
# ===========================================================================

NOTES_COLUMNS = ["item", "value"]


def _notes_frame(ledger_fy: pd.DataFrame, trades: pd.DataFrame,
                 meta: dict, fy: str | None,
                 warnings: list[str], join_misses: int) -> pd.DataFrame:
    """Every convention the numbers were computed under, and every caveat the
    upstream layers raised, verbatim.

    NEVER LAUNDER A CAVEAT: a warning that reached this layer goes into the
    bundle unedited, because the person who decides what to do about it is the
    CA reading the sheet, not this function.
    """
    rows: list[tuple] = []

    def add(item, value):
        rows.append((item, str(value)))

    add("neutrality", NEUTRALITY_STATEMENT)

    # --- conventions ------------------------------------------------------
    basis = meta.get("cost_basis_convention", config.COST_BASIS_CONVENTION)
    add("cost_basis_convention",
        f"{basis} — the venue nets a single position per asset, so a close is "
        f"matched against the size-weighted average entry price of the episode "
        f"it closes.")
    add("fee_attribution",
        "Fees are tracked per fill and never split within a fill. Where one "
        "fill closes a position and opens the opposite one, that fill carries "
        "its FULL fee on the closing leg; the newly opened episode inherits "
        "none of it.")
    add("funding_treatment",
        "Funding is reported on its own sheet as signed lines and is never "
        "netted into execution P&L. It is a holding cost, structurally "
        "different from both price P&L and venue fees.")
    add("sign_conventions",
        "realized_pnl_inr: + gain, - loss. fee_inr: + paid, - maker rebate. "
        "funding_inr: + received, - paid. amount_inr on a deposit is positive "
        "(onto the venue); on a withdrawal it is negative (off the venue).")
    add("fy_boundary_rule",
        "A trade belongs to the financial year of its REALISATION date, the "
        "close timestamp, evaluated in IST. An episode opened in one year and "
        "closed in the next appears exactly once, in the closing year; the "
        "open_fy column on each row shows where it began.")
    add("episode_granularity",
        "One row per closing episode. That matches the cost-basis unit the "
        "engine works in, and the per-transaction realised-P&L genre an Indian "
        "exchange futures report already uses.")

    # --- FX ---------------------------------------------------------------
    add("fx_convention", meta.get("fx_convention", config.FX_CONVENTION))
    add("fx_source", config.FX_SOURCE_NAME)
    add("fx_provider", config.FX_PROVIDER_NAME)
    add("fx_publication_cutoff_ist",
        f"{config.FX_PUBLICATION_CUTOFF_IST} — a reference rate does not exist "
        f"in the world until it is published, so an event earlier on its own "
        f"publication day uses the previous business day rate.")
    add("fx_staleness_policy",
        f"The rate lookup walks back over weekends and holidays, but refuses "
        f"beyond {config.FX_MAX_STALENESS_DAYS} calendar days rather than "
        f"reusing an old rate for a recent trade. Rows inside that grace "
        f"window are flagged.")
    stale = (int(ledger_fy["fx_stale_grace"].sum())
             if len(ledger_fy) and "fx_stale_grace" in ledger_fy.columns else 0)
    add("fx_rows_flagged_stale_grace",
        f"{stale} of {len(ledger_fy)} events in this window used a rate from "
        f"outside the normal business-day lookback. The fx_rate_date column on "
        f"each row shows exactly which day was used.")

    # --- precision --------------------------------------------------------
    add("rounding",
        f"INR columns are rounded to {config.REPORT_INR_DECIMALS} decimals for "
        f"readability. USD amounts and prices are kept at full precision. The "
        f"rounding happens at presentation only; nothing stored upstream is "
        f"rounded."
        if config.CA_REPORT_ROUND_INR else
        "No rounding is applied; every figure is at full stored precision.")
    add("blank_versus_zero",
        "An empty money cell means NOT COMPUTABLE. It never means zero. Such "
        "cells appear only where the cost basis is genuinely unavailable, and "
        "those rows are excluded from every total in 00_summary.csv and are "
        "counted there.")

    # --- data completeness ------------------------------------------------
    n_inc = int(trades["cost_basis_incomplete"].sum()) if len(trades) else 0
    if n_inc:
        add("incomplete_cost_basis_cause",
            f"{n_inc} close(s) in this window belong to a position that "
            f"pre-existed the roughly "
            f"{config.USERFILLS_BY_TIME_MAX_RECENT}-fill history the venue "
            f"exposes, or to an episode where the reconstruction lost sync "
            f"with the position the venue reported. Their entry price is "
            f"genuinely unknown, so their P&L is blank rather than a confident "
            f"wrong number.")
    add("spot_activity",
        "Spot fills are excluded. This bundle covers perpetual-futures "
        "activity only.")
    add("non_hyperliquid_activity",
        "Activity on any other venue is outside this bundle. If you traded "
        "elsewhere in this year, those figures must be added by hand.")
    add("open_positions_note",
        "05_open_positions.csv lists positions still open, with NO unrealised "
        "figure: computing one would need a mark price this tool does not "
        "fetch, and an unrealised amount is income only under a mark-to-market "
        "reading nobody here has adopted. The list reflects positions open at "
        "the end of the available data, not a position reconstructed as at the "
        "year boundary.")
    add("cashflow_boundary",
        "04_cashflows.csv records USDC movements ON the venue. The INR/USDC "
        "conversion itself happened at an Indian exchange and is invisible to "
        "this tool, so those legs are absent rather than estimated. Deposits "
        "and withdrawals contribute zero to every total in 00_summary.csv.")
    onramp = meta.get("onramp_cost_inr", None)
    add("onramp_usdc_cost_inr",
        f"{onramp} — supplied by you, and used nowhere in the arithmetic of "
        f"this bundle. It is recorded so the figure your CA works from is "
        f"documented." if onramp is not None else
        "Not supplied. The INR cost of acquiring the USDC used on this venue "
        "is not visible to this tool. It is not assumed, estimated, or "
        "defaulted to zero anywhere in this bundle.")
    add("tds_on_this_venue",
        "No deduction at source arises on this venue: the obligation falls on "
        "a resident payer, and a peer-to-peer offshore exchange has no Indian "
        "deductor. Anything withheld when you bought or sold the USDC was "
        "withheld by the Indian exchange, will appear in Form 26AS, and is "
        "creditable. Separately, Indian exchanges do not deduct on futures and "
        "options at all, since no ownership of a crypto asset is transferred.")
    as_of = meta.get("as_of")
    if as_of:
        add("as_of_bound",
            f"The run was bounded to events on or before {as_of}. Anything "
            f"later is excluded by request, not by omission.")

    for i, w in enumerate(list(warnings), start=1):
        add(f"data_completeness_warning_{i}", w)
    if not warnings:
        add("data_completeness_warnings",
            "None. No truncation, desync or out-of-scope-data warning was "
            "raised while building this bundle.")
    if join_misses:
        add("join_integrity_warning",
            f"{join_misses} realised ledger row(s) could not be matched to "
            f"their execution detail. Their money columns are still correct "
            f"(those come from the ledger) but the price and size columns are "
            f"blank. This indicates an internal inconsistency and is worth "
            f"reporting as a bug.")

    add("artifact_scope",
        "This is the data bundle. The separate summary document that sets out "
        "the competing legal readings side by side, ranking none of them, is "
        "not part of this ZIP.")
    return pd.DataFrame(rows, columns=NOTES_COLUMNS)


# ===========================================================================
# Public surface: frames -> ZIP.
# ===========================================================================

def ca_report_frames(
    closing_events: pd.DataFrame,
    open_positions: pd.DataFrame,
    ledger: pd.DataFrame,
    meta: dict,
    fy: str | None = None,
    warnings: list[str] | tuple = (),
    funding_ledger: pd.DataFrame | None = None,
) -> dict[str, pd.DataFrame]:
    """The whole bundle as {sheet_name: DataFrame}. PURE — no I/O, no network,
    and no clock beyond the caller-supplied generated_utc.

    Kept separate from write_ca_report_zip so an on-screen preview and a
    download come from one code path and cannot drift apart.

    A financial year with no activity is a VALID answer: every sheet comes back
    with its headers and no rows, and sheet 00 says the window is empty. This
    never raises and never omits a sheet — a missing file reads as a broken
    tool, while an empty one reads as the true statement it is.
    """
    if fy is not None:
        _fy_start_year(fy)                # fail loudly on a malformed label
    meta = dict(meta or {})
    if ledger is None:
        ledger = _blank(["ts_utc", "category"])

    ledger_fy = (ledger[fy_mask(ledger, fy)].copy() if len(ledger)
                 else ledger.copy())

    trades, join_misses = _trades_frame(closing_events, ledger_fy)
    funding = _funding_frame(ledger_fy, funding_ledger)
    fees = _fees_frame(ledger_fy)
    cashflows = _cashflows_frame(ledger_fy)
    open_pos = _open_positions_frame(open_positions, fy, ledger)

    return {
        "00_summary": _summary_frame(trades, funding, fees, cashflows,
                                     open_pos, ledger_fy, meta, fy),
        "01_futures_trades": trades,
        "02_funding": funding,
        "03_fees": fees,
        "04_cashflows": cashflows,
        "05_open_positions": open_pos,
        "06_assumptions_and_notes": _notes_frame(
            ledger_fy, trades, meta, fy, list(warnings), join_misses),
    }


def ca_report_zip_bytes(frames: dict[str, pd.DataFrame]) -> bytes:
    """The bundle as bytes, so an HTTP response never has to touch disk — which
    matters, because a serverless filesystem is read-only and there would be
    nothing worth keeping anyway."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name in FY_SHEETS:
            df = frames.get(name)
            if df is None:
                continue
            z.writestr(f"{name}.csv", df.to_csv(index=False))
    return buf.getvalue()


def write_ca_report_zip(frames: dict[str, pd.DataFrame], path: Path) -> Path:
    """One CSV per frame into a single ZIP. Computes nothing."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(ca_report_zip_bytes(frames))
    return path


def zip_filename(address: str, fy: str | None) -> str:
    return f"ca_report_{address}_FY{fy or _ALL}.zip"


def build_ca_report(
    closing_events: pd.DataFrame,
    open_positions: pd.DataFrame,
    ledger: pd.DataFrame,
    meta: dict,
    out_dir: Path,
    fy: str | None = None,
    warnings: list[str] | tuple = (),
    funding_ledger: pd.DataFrame | None = None,
) -> Path:
    """frames -> ZIP on disk, in the same out_dir as the other artifacts.
    Returns the path."""
    frames = ca_report_frames(closing_events, open_positions, ledger, meta,
                              fy=fy, warnings=warnings,
                              funding_ledger=funding_ledger)
    name = zip_filename(str(meta.get("address", "address")), fy)
    return write_ca_report_zip(frames, Path(out_dir) / name)
