"""
reconstruct/funding.py — Phase 3, the funding side.

Funding is a HOLDING cost: a periodic payment for keeping a perp position open,
structurally different from execution P&L (price movement) and from venue fees
(paid per trade). Conflating the three is a rookie error and, worse, a silent
tax error — their tax treatments may differ (errors_in_plan.md #6). So funding
gets its OWN ledger rows, its OWN category, and is NEVER netted into realized
P&L here. Whether funding is a separate taxable event, a cost of the position,
or a deductible expense is an EXPLICIT parameter in Phase 5, not a choice baked
in at reconstruction time.

Sign convention (verified live 2026-07-22, tests/fixtures.py):
  usdc  + = received by user, - = paid by user.

This module is deliberately thin: the load layer already typed the funding
stream. Its job is only to project that stream onto the canonical funding-ledger
shape the interpret/present layers consume, and to keep it pure and testable.
"""

from __future__ import annotations

import pandas as pd

FUNDING_LEDGER_COLUMNS = [
    "ts", "asset", "asset_class", "usdc", "funding_rate", "szi",
]


def reconstruct_funding(funding: pd.DataFrame) -> pd.DataFrame:
    """Loaded funding DataFrame -> canonical funding-ledger rows, ascending by
    time. One row per funding settlement; `usdc` is signed (+ received /
    - paid). Computes nothing new — funding is already realized at settlement.
    """
    cols = {
        "ts": "int64", "asset": "object", "asset_class": "object",
        "usdc": "float64", "funding_rate": "float64", "szi": "float64",
    }
    if not len(funding):
        return pd.DataFrame({c: pd.Series([], dtype=dt) for c, dt in cols.items()})

    out = pd.DataFrame({
        "ts": funding["time_ms"].astype("int64"),
        "asset": funding["coin"],
        "asset_class": funding["asset_class"],
        "usdc": funding["usdc"].astype("float64"),
        "funding_rate": funding["funding_rate"].astype("float64"),
        "szi": funding["szi"].astype("float64"),
    }, columns=FUNDING_LEDGER_COLUMNS)
    return out.sort_values("ts", kind="stable").reset_index(drop=True)


def total_funding_usd(funding_ledger: pd.DataFrame) -> float:
    """Signed sum: net funding (+ net received, - net paid)."""
    if not len(funding_ledger):
        return 0.0
    return float(funding_ledger["usdc"].sum())
