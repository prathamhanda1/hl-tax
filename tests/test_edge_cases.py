"""
tests/test_edge_cases.py — Phase 7 hardening sweep.

The master plan's Phase 7 names the edge cases explicitly: empty address,
address with one fill, address with ONLY spot activity (must warn, not crash),
address with activity predating the FX series, and a malformed address (must
fail loudly with a clear message, not a stack trace). The enormous-address
case is a pagination/memory concern exercised by the fetch layer against real
data (Gate 1) rather than a pure unit here.

These are pure, no-network tests: they drive the load / reconstruct / fx /
normalize entry points on hand-built inputs and assert the tool degrades the
way the doctrine requires — loudly and correctly — instead of silently
producing a wrong tax number wearing a green checkmark.
"""

from __future__ import annotations

import pytest

import config
from load.load import load_raw
from reconstruct.positions import reconstruct_positions
from reconstruct.funding import reconstruct_funding


# ---------------------------------------------------------------------------
# Helpers — raw-fill dicts in exactly the shape fetch writes / fixtures hold.
# ---------------------------------------------------------------------------

def _fill(coin, px, sz, side, tid, *, start="0.0", dir_="Open Long",
          closed="0.0", fee="0.0", time=1735779600000):
    return {
        "coin": coin, "px": px, "sz": sz, "side": side,
        "time": time, "startPosition": start, "dir": dir_,
        "closedPnl": closed,
        "hash": f"0x{tid:064x}",
        "oid": 100000000000 + tid, "crossed": True, "fee": fee,
        "tid": tid, "feeToken": "USDC", "twapId": None,
    }


# ---------------------------------------------------------------------------
# 1 — Empty address: no history at all.
# ---------------------------------------------------------------------------

def test_empty_address_loads_and_reconstructs_without_crashing():
    loaded = load_raw({})  # no fills / funding / ledger keys at all
    assert len(loaded.fills) == 0
    assert len(loaded.funding) == 0
    assert len(loaded.ledger) == 0
    assert loaded.warnings == []  # nothing present -> nothing to warn about

    # Empty frames still carry their typed schema, so nothing downstream sees
    # an object-dtype numeric column.
    assert str(loaded.fills["px"].dtype) == "float64"
    assert str(loaded.fills["ts"].dtype) == "datetime64[ns, UTC]"

    rec = reconstruct_positions(loaded.fills)
    assert len(rec.closing_events) == 0
    assert len(rec.open_positions) == 0
    fund = reconstruct_funding(loaded.funding)
    assert len(fund) == 0


# ---------------------------------------------------------------------------
# 2 — One fill: an open with no matching close.
# ---------------------------------------------------------------------------

def test_single_open_fill_yields_open_position_no_closing_event():
    raw = {"fills": [_fill("BTC", "100000.0", "0.5", "B", 1,
                           dir_="Open Long", fee="2.0")]}
    loaded = load_raw(raw)
    assert len(loaded.fills) == 1

    rec = reconstruct_positions(loaded.fills)
    # Nothing closed -> zero realized episodes, but the residual long is
    # surfaced as an open position rather than dropped.
    assert len(rec.closing_events) == 0
    assert len(rec.open_positions) == 1
    assert rec.open_positions.iloc[0]["asset"] == "BTC"


# ---------------------------------------------------------------------------
# 3 — Only spot activity: must WARN and EXCLUDE, never crash or silently mix.
# ---------------------------------------------------------------------------

def test_only_spot_activity_warns_and_is_excluded_from_perp_pnl():
    # "@107" is a spot asset in index form (plan / load.classify_asset).
    raw = {"fills": [
        _fill("@107", "1.0", "10.0", "B", 1, dir_="Buy"),
        _fill("@107", "1.1", "10.0", "A", 2, dir_="Sell", closed="1.0"),
    ]}
    loaded = load_raw(raw)

    # Load tags it spot and announces it — a skipped event must never be silent.
    assert (~loaded.fills["is_perp"]).all()
    assert any("SPOT" in w for w in loaded.warnings)

    # Reconstruction excludes spot (plan 0.4): no perp episodes, no crash.
    rec = reconstruct_positions(loaded.fills)
    assert len(rec.closing_events) == 0
    assert len(rec.open_positions) == 0


# ---------------------------------------------------------------------------
# 4 — Malformed address: fail loudly BEFORE any network call.
# ---------------------------------------------------------------------------

def test_bad_address_raises_before_any_network_call():
    from fetch.fetch_user import normalize_address, BadAddressError

    with pytest.raises(BadAddressError) as exc:
        normalize_address("not-an-address")
    assert "No API call was made." in str(exc.value)

    # A valid address normalises to lowercase, 0x-prefixed.
    good = "0x" + "AbCd" * 10  # 40 hex chars
    assert normalize_address(good) == good.lower()
    # Bare (no 0x) but otherwise valid is accepted and prefixed.
    assert normalize_address("aB" * 20) == ("0x" + "ab" * 20)


# ---------------------------------------------------------------------------
# 5 — Event predating the cached FX series: refuse, do not guess.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not config.FX_SERIES_FILE.exists(),
    reason="cached FBIL/RBI series not present; FX edge case needs it",
)
def test_event_predating_fx_series_fails_loud_not_silent():
    import pandas as pd
    from fx.fx import default_converter, RateUnavailableError

    conv = default_converter()
    ancient = pd.Timestamp("1990-01-01", tz="UTC")  # long before the series
    with pytest.raises(RateUnavailableError) as exc:
        conv.rate_for_event(ancient)
    # The message must point the user at a fix, not just fail.
    assert "predates the cached FX series" in str(exc.value)
