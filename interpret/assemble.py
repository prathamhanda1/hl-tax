"""
interpret/assemble.py — the bridge from phases 3+4 to phase 5.

The reconstruct layer (Phase 3) emits per-CLOSE events, a funding ledger, and
the load layer carries deposits/withdrawals. The fx layer (Phase 4) knows how
to stamp an event timestamp with the INR rate that prevailed then. What was
missing until here is the connective tissue: a SINGLE pure function that folds
those separate streams into the one INR ledger the interpret layer consumes
(the input contract documented at the top of interpret/treatments.py).

This module is the only place USD becomes INR for the tax layer. It multiplies
amount_usd by the row's fx_rate exactly once, after the fx layer has attached
the rate — so a conversion bug can live in exactly one line, not scattered
across four treatments. It is PURE: frames + a converter in, one frame out.

WHAT MAPS TO WHAT
-----------------
Each CLOSING event becomes TWO ledger rows (they are taxed differently):
  - a `realized_pnl` row  (amount_usd = realized_usd; the price P&L, gross of
    fees, per the reconstruct sign convention). gross_consideration_inr is the
    exit notional (exit_px * size_closed in INR) — the figure a VDA-reading TDS
    of 1% would bite on. A cost-basis-incomplete close carries realized NaN;
    it is kept and flagged, never silently zeroed.
  - a `fee` row           (amount_usd = close_fee_usd; + = paid, - = rebate,
    matching both reconstruct and the interpret fee convention).

Each funding settlement becomes one `funding` row (usdc: + received, - paid).

Each in-scope ledger flow becomes a `deposit` (+usdc) or `withdrawal` (-usdc)
row. These are USDC movements ON the venue — NOT the off-venue INR<->USDC
conversion, so their asset_class is "other", never "stablecoin_leg". The real
stablecoin legs are invisible to this tool (they happened at an Indian
exchange) and are surfaced loudly by interpret.cross_cutting, never fabricated
here. Deposits/withdrawals contribute 0 to every P&L base but are kept so the
ledger reconciles and every rupee is traceable.

SIGN SANITY (carried verbatim from the upstream layers, never re-derived here):
  realized_usd    + = gain,     - = loss
  close_fee_usd   + = fee paid, - = maker rebate
  funding usdc    + = received, - = paid
  deposit usdc    + into account   -> amount_usd = +usdc
  withdraw usdc   + leaves account -> amount_usd = -usdc
"""

from __future__ import annotations

import numpy as np
import pandas as pd

import config
from fx.fx import FxConverter, attach_fx_columns, default_converter
from interpret.treatments import (
    CAT_DEPOSIT,
    CAT_FEE,
    CAT_FUNDING,
    CAT_REALIZED,
    CAT_WITHDRAWAL,
    REQUIRED_LEDGER_COLUMNS,
)

# Columns the assembler emits, in order: the required contract plus the two
# optional TDS/audit columns interpret uses where present.
LEDGER_COLUMNS = REQUIRED_LEDGER_COLUMNS + [
    "gross_consideration_inr", "cost_basis_incomplete",
]

_ASSET_CLASS_CASH = "other"  # USDC on-venue flows: not a stablecoin conversion leg


def _ms_to_utc(ms) -> pd.Timestamp:
    return pd.Timestamp(int(ms), unit="ms", tz="UTC")


def _blank_ledger() -> pd.DataFrame:
    """A 0-row ledger with the contract's columns, so an empty address flows
    through interpret without dtype surprises."""
    cols = {c: [] for c in LEDGER_COLUMNS}
    df = pd.DataFrame(cols)
    df["ts_utc"] = pd.Series([], dtype="datetime64[ns, UTC]")
    df["acq_ts"] = pd.Series([], dtype="datetime64[ns, UTC]")
    return df


def _rows_from_closes(closing_events: pd.DataFrame) -> list[dict]:
    rows: list[dict] = []
    for e in closing_events.itertuples(index=False):
        realized = float(e.realized_usd)
        incomplete = bool(e.cost_basis_incomplete)
        # Exit notional in USD; NaN when we never saw the entry (incomplete).
        exit_notional_usd = (
            np.nan if incomplete else abs(float(e.exit_px) * float(e.size_closed))
        )
        rows.append({
            "event_id": f"rp:{int(e.close_tid)}",
            "ts": _ms_to_utc(e.close_ts),
            "acq_ts": _ms_to_utc(e.open_ts),  # episode open -> Schedule VDA acq date
            "category": CAT_REALIZED,
            "asset": e.asset,
            "asset_class": e.asset_class,
            "episode_id": int(e.episode),
            "amount_usd": realized,
            "gross_consideration_usd": exit_notional_usd,
            "is_liquidation": bool(e.is_liquidation),
            "cost_basis_incomplete": incomplete,
        })
        # The closing fill's own fee, as its own row.
        rows.append({
            "event_id": f"fee:{int(e.close_tid)}",
            "ts": _ms_to_utc(e.close_ts),
            "category": CAT_FEE,
            "asset": e.asset,
            "asset_class": e.asset_class,
            "episode_id": int(e.episode),
            "amount_usd": float(e.close_fee_usd),
            "gross_consideration_usd": np.nan,
            "is_liquidation": bool(e.is_liquidation),
            "cost_basis_incomplete": incomplete,
        })
    return rows


def _rows_from_funding(funding_ledger: pd.DataFrame) -> list[dict]:
    rows: list[dict] = []
    for i, f in enumerate(funding_ledger.itertuples(index=False)):
        rows.append({
            "event_id": f"fund:{int(f.ts)}:{f.asset}:{i}",
            "ts": _ms_to_utc(f.ts),
            "category": CAT_FUNDING,
            "asset": f.asset,
            "asset_class": f.asset_class,
            "episode_id": pd.NA,
            "amount_usd": float(f.usdc),
            "gross_consideration_usd": np.nan,
            "is_liquidation": False,
            "cost_basis_incomplete": False,
        })
    return rows


def _rows_from_cashflows(ledger: pd.DataFrame) -> list[dict]:
    """Deposits and withdrawals (the in-scope ledger flows). Money in is +USD,
    money out is -USD. asset_class is 'other' — these are USDC flows on the
    venue, not the invisible off-venue INR<->USDC conversion."""
    rows: list[dict] = []
    if not len(ledger):
        return rows
    scoped = ledger[ledger["in_scope"].astype(bool)]
    for i, r in enumerate(scoped.itertuples(index=False)):
        usdc = float(r.usdc) if not pd.isna(r.usdc) else 0.0
        if r.type == "deposit":
            category, signed = CAT_DEPOSIT, +usdc
        elif r.type == "withdraw":
            category, signed = CAT_WITHDRAWAL, -usdc
        else:
            continue  # only deposit/withdraw are in scope (config)
        tag = r.hash if getattr(r, "hash", None) else f"idx{i}"
        rows.append({
            "event_id": f"led:{category}:{tag}:{i}",
            "ts": r.ts,
            "category": category,
            "asset": "USDC",
            "asset_class": _ASSET_CLASS_CASH,
            "episode_id": pd.NA,
            "amount_usd": signed,
            "gross_consideration_usd": np.nan,
            "is_liquidation": False,
            "cost_basis_incomplete": False,
        })
    return rows


def build_inr_ledger(
    closing_events: pd.DataFrame,
    funding_ledger: pd.DataFrame,
    ledger: pd.DataFrame,
    converter: FxConverter | None = None,
) -> pd.DataFrame:
    """Assemble the single INR ledger interpret() consumes.

    Inputs are the raw upstream frames (reconstruct closing events, the funding
    ledger, the loaded non-funding ledger) and an FxConverter. Output is one
    row per economic event in the Phase 5 input-contract shape, sorted by time,
    with amount_inr = amount_usd * fx_rate computed exactly once here.
    """
    converter = converter or default_converter()

    rows = (
        _rows_from_closes(closing_events)
        + _rows_from_funding(funding_ledger)
        + _rows_from_cashflows(ledger)
    )
    if not rows:
        return _blank_ledger()

    df = pd.DataFrame(rows)
    df = df.sort_values("ts", kind="stable").reset_index(drop=True)

    # Stamp each row with the INR rate that prevailed at its timestamp, then do
    # the one and only USD->INR multiplication.
    df = attach_fx_columns(df, converter, ts_col="ts")
    df["ts_utc"] = df["ts"]
    df["amount_inr"] = df["amount_usd"] * df["fx_rate"]
    df["gross_consideration_inr"] = (
        df["gross_consideration_usd"] * df["fx_rate"]
    )

    if "acq_ts" not in df.columns:
        df["acq_ts"] = pd.NaT
    return df[LEDGER_COLUMNS + [
        "ts", "acq_ts", "fx_source", "fx_convention", "fx_stale_grace",
    ]].reset_index(drop=True)
