"""
reconstruct/positions.py — Phase 3, the hard core.

Replays a perp fill stream per asset in time order, maintaining a single net
position and its size-weighted average entry price, and emits ONE row per
CLOSING event with realized P&L. Pure: DataFrames in, DataFrames out, no I/O,
no network — so it is unit-testable on the hand-built paper fixtures.

WHY AVERAGE-ENTRY, AND WHAT COULD GO WRONG (errors_in_plan.md #7)
----------------------------------------------------------------
Hyperliquid nets a SINGLE position per asset per account, so "which lot did
this close?" reduces to episode accounting. v1 uses average-entry-price within
an episode (config.COST_BASIS_CONVENTION). The claim that this matches HL's own
`closedPnl` is NOT assumed true — it is MEASURED by Gate 3b (venue
reconciliation). If they diverge, Gate 3b reports the deviation rather than the
tool silently trusting an unverified convention.

SIGN CONVENTIONS (each verified against live data — see tests/fixtures.py)
--------------------------------------------------------------------------
  side "B" = buy  -> position change +sz   (increases a long / covers a short)
  side "A" = sell -> position change -sz   (increases a short / reduces a long)
  net position   : + = net long, - = net short.
  startPosition  : the venue's signed net position BEFORE this fill. We use it
                   as a LIVE INVARIANT: our running net position, just before
                   applying a fill, must equal that fill's startPosition. A
                   mismatch means a missing fill or a reconstruction bug — a
                   loud warning, never a silent wrong number.
  realized P&L   : long  close -> (exit_px - entry_px) * size_closed
                   short close -> (entry_px - exit_px) * size_closed
                   GROSS of fees (matches HL's closedPnl, which reports the
                   price P&L; the fee is a separate field and a separate
                   ledger line). Never net fees into realized here.
  fee            : + = paid, - = maker rebate. The CLOSING fill's own fee is
                   attached to its closing event. A flip fill's FULL fee lands
                   on the close leg it causes; the episode it opens inherits
                   none (config fee-attribution convention).

TRUNCATION / PRE-EXISTING POSITIONS (the #1 real-data hazard)
-------------------------------------------------------------
HL's userFillsByTime exposes only the ~10k most recent fills. A heavy account's
history is therefore truncated: the first retained fill for an asset can have a
NON-ZERO startPosition — the account was already in a position whose entry price
we never saw. We CANNOT compute a correct realized P&L for closes of such a
pre-existing position, so those closing events are flagged
`cost_basis_incomplete=True` with realized_usd = NaN, rather than emitting a
confident wrong number. An episode opened cleanly (from a flat position, inside
our window) is complete and trustworthy.

FLIPS
-----
A single fill that closes a long AND opens a short (dir "Long > Short", or the
reverse) is split into two economic events: (a) close the entire existing
position — one closing event; (b) open a new episode with the remainder.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

import config

# Positions within this magnitude are treated as flat (float dust guard).
_FLAT_EPS = 1e-9
# Tolerance for the running-position vs startPosition invariant.
_START_POS_EPS = 1e-6

CLOSING_EVENT_COLUMNS = [
    "episode", "asset", "asset_class", "direction",
    "open_ts", "close_ts", "size_closed", "entry_px", "exit_px",
    "realized_usd", "close_fee_usd", "is_liquidation",
    "cost_basis_incomplete", "close_tid",
]

OPEN_POSITION_COLUMNS = [
    "episode", "asset", "asset_class", "direction", "open_ts",
    "size", "entry_px", "cost_basis_incomplete",
]


@dataclass
class _State:
    """Live per-asset position state during the replay."""
    net_pos: float = 0.0
    avg_entry: float = float("nan")
    episode_id: int | None = None
    open_ts: int | None = None
    cost_basis_incomplete: bool = False
    bootstrapped: bool = False  # seen at least one fill for this asset


@dataclass
class Reconstruction:
    closing_events: pd.DataFrame
    open_positions: pd.DataFrame
    warnings: list[str] = field(default_factory=list)


def _direction(net_pos: float) -> str:
    return "long" if net_pos > 0 else "short"


def reconstruct_positions(fills: pd.DataFrame) -> Reconstruction:
    """Replay the (perp) fills into closing events + residual open positions.

    Spot fills (is_perp == False) are excluded — spot is out of scope for perp
    P&L (plan 0.4); the load layer already warned about them. Fills are
    processed in global time order so episode ids increase in open-time order
    across assets.
    """
    if len(fills):
        perp = fills[fills["is_perp"]].sort_values(
            "time_ms", kind="stable"
        ).reset_index(drop=True)
    else:
        perp = fills

    states: dict[str, _State] = {}
    episode_counter = 0
    events: list[dict] = []
    warnings: list[str] = []

    has_liq_col = "is_liquidation" in perp.columns

    for row in perp.itertuples(index=False):
        coin = row.coin
        st = states.setdefault(coin, _State())
        signed = row.sz if row.side == "B" else -row.sz
        start_pos = row.start_position
        is_liq = bool(getattr(row, "is_liquidation")) if has_liq_col else False

        # --- Bootstrap / invariant check against the venue's startPosition ---
        if not st.bootstrapped:
            st.bootstrapped = True
            if abs(start_pos) > _FLAT_EPS:
                # Pre-existing position from before our data window: entry
                # unknown -> this episode's cost basis is incomplete.
                episode_counter += 1
                st.net_pos = start_pos
                st.avg_entry = float("nan")
                st.episode_id = episode_counter
                st.open_ts = int(row.time_ms)  # proxy; true open predates window
                st.cost_basis_incomplete = True
                warnings.append(
                    f"{coin}: first retained fill has startPosition "
                    f"{start_pos:g} (not flat) - position pre-existed the "
                    f"~{config.USERFILLS_BY_TIME_MAX_RECENT}-fill history "
                    f"window. Its entry price is unknown; realized P&L for "
                    f"closing this position is NOT computable and is emitted "
                    f"as NaN (cost_basis_incomplete)."
                )
        elif abs(st.net_pos - start_pos) > _START_POS_EPS:
            warnings.append(
                f"{coin}: position desync at tid {row.tid} - reconstructed "
                f"net {st.net_pos:g} != venue startPosition {start_pos:g}. A "
                f"fill may be missing or mis-ordered; downstream numbers for "
                f"this asset are suspect."
            )
            # Trust the venue: resync, and mark incomplete (we lost the thread).
            st.net_pos = start_pos
            st.cost_basis_incomplete = True

        new_pos = st.net_pos + signed

        # --- Case A: opening from flat ---
        if abs(st.net_pos) <= _FLAT_EPS:
            episode_counter += 1
            st.episode_id = episode_counter
            st.open_ts = int(row.time_ms)
            st.avg_entry = row.px
            st.net_pos = signed
            st.cost_basis_incomplete = False
            continue

        same_sign = (st.net_pos > 0) == (signed > 0)

        # --- Case B: adding to the position (same direction) ---
        if same_sign:
            total = abs(st.net_pos) + abs(signed)
            if not st.cost_basis_incomplete:
                st.avg_entry = (
                    st.avg_entry * abs(st.net_pos) + row.px * abs(signed)
                ) / total
            st.net_pos = new_pos
            continue

        # --- Opposite direction: a reduce/close, possibly a flip ---
        closing_size = min(abs(signed), abs(st.net_pos))
        was_long = st.net_pos > 0
        if st.cost_basis_incomplete:
            realized = float("nan")
            entry = float("nan")
        elif was_long:
            realized = (row.px - st.avg_entry) * closing_size
            entry = st.avg_entry
        else:
            realized = (st.avg_entry - row.px) * closing_size
            entry = st.avg_entry

        events.append({
            "episode": st.episode_id,
            "asset": coin,
            "asset_class": row.asset_class,
            "direction": _direction(st.net_pos),
            "open_ts": st.open_ts,
            "close_ts": int(row.time_ms),
            "size_closed": closing_size,
            "entry_px": entry,
            "exit_px": row.px,
            "realized_usd": realized,
            "close_fee_usd": row.fee,  # full fill fee lands on the close leg
            "is_liquidation": is_liq,
            "cost_basis_incomplete": st.cost_basis_incomplete,
            "close_tid": int(row.tid),
        })

        is_flip = abs(signed) > abs(st.net_pos) + _FLAT_EPS
        if is_flip:
            # (b) open a fresh episode with the remainder, at this fill's price.
            remainder = abs(signed) - abs(st.net_pos)
            episode_counter += 1
            st.episode_id = episode_counter
            st.open_ts = int(row.time_ms)
            st.avg_entry = row.px
            st.net_pos = remainder if signed > 0 else -remainder
            st.cost_basis_incomplete = False
        else:
            st.net_pos = new_pos
            if abs(st.net_pos) <= _FLAT_EPS:
                # Episode fully closed; next fill opens a fresh one.
                st.net_pos = 0.0
                st.avg_entry = float("nan")
                st.episode_id = None
                st.open_ts = None
                st.cost_basis_incomplete = False

    closing_events = _events_frame(events)
    open_positions = _open_frame(states)
    return Reconstruction(closing_events, open_positions, warnings)


def _events_frame(events: list[dict]) -> pd.DataFrame:
    if not events:
        return pd.DataFrame({c: pd.Series([], dtype=_event_dtype(c))
                             for c in CLOSING_EVENT_COLUMNS})
    df = pd.DataFrame(events, columns=CLOSING_EVENT_COLUMNS)
    for c in ("episode", "open_ts", "close_ts", "close_tid"):
        df[c] = df[c].astype("int64")
    for c in ("size_closed", "entry_px", "exit_px", "realized_usd",
              "close_fee_usd"):
        df[c] = df[c].astype("float64")
    for c in ("is_liquidation", "cost_basis_incomplete"):
        df[c] = df[c].astype("bool")
    return df


def _open_frame(states: dict[str, _State]) -> pd.DataFrame:
    rows = []
    for coin, st in states.items():
        if abs(st.net_pos) > _FLAT_EPS:
            rows.append({
                "episode": st.episode_id,
                "asset": coin,
                "asset_class": None,  # filled below if we have it
                "direction": _direction(st.net_pos),
                "open_ts": st.open_ts,
                "size": st.net_pos,
                "entry_px": st.avg_entry,
                "cost_basis_incomplete": st.cost_basis_incomplete,
            })
    if not rows:
        return pd.DataFrame({c: pd.Series([], dtype=_open_dtype(c))
                             for c in OPEN_POSITION_COLUMNS})
    return pd.DataFrame(rows, columns=OPEN_POSITION_COLUMNS)


def _event_dtype(col: str) -> str:
    if col in ("episode", "open_ts", "close_ts", "close_tid"):
        return "int64"
    if col in ("size_closed", "entry_px", "exit_px", "realized_usd",
               "close_fee_usd"):
        return "float64"
    if col in ("is_liquidation", "cost_basis_incomplete"):
        return "bool"
    return "object"


def _open_dtype(col: str) -> str:
    if col in ("episode", "open_ts"):
        return "int64"
    if col in ("size", "entry_px"):
        return "float64"
    if col == "cost_basis_incomplete":
        return "bool"
    return "object"


# ---------------------------------------------------------------------------
# Convenience aggregates (used by the reconciliation gates and the report)
# ---------------------------------------------------------------------------

def total_realized_usd(closing_events: pd.DataFrame) -> float:
    """Sum of realized P&L over COMPLETE closing events (NaN/incomplete rows
    excluded — they are not computable, not zero)."""
    if not len(closing_events):
        return 0.0
    return float(np.nansum(closing_events["realized_usd"].to_numpy()))
