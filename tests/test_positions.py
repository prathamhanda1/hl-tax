"""
tests/test_positions.py — GATE 3a: reconstruction vs the hand-built fixtures.

Every number here was computed BY HAND in tests/fixtures.py before this code
existed. If the reconstruction and the hand arithmetic disagree, the code is
wrong until proven otherwise (troubleshooting doctrine #1). This exercises:
open+close long, open+close short (the sign test), a partial close with
average entry, a flip split into close+open, a liquidation flag, funding in
both directions, and a HIP-3 namespaced asset.
"""

from __future__ import annotations

import math

import pytest

from load.load import load_fills, load_funding
from reconstruct.positions import reconstruct_positions, total_realized_usd
from reconstruct.funding import reconstruct_funding, total_funding_usd
from tests import fixtures as fx

TOL = 1e-6


def _fills_df():
    """Load the fixture fills and inject liquidation knowledge by tid — the
    fixtures predate (and so do not carry) the real `liquidation` field; the
    production path tags is_liquidation in the load layer from that field."""
    df = load_fills(fx.FILLS)
    df["is_liquidation"] = df["tid"].isin(fx.LIQUIDATION_TIDS)
    return df


@pytest.fixture(scope="module")
def reconstruction():
    return reconstruct_positions(_fills_df())


def test_closing_event_count(reconstruction):
    assert len(reconstruction.closing_events) == len(fx.EXPECTED_CLOSING_EVENTS)
    assert len(reconstruction.closing_events) == fx.EXPECTED_TOTALS["n_closing_events"]


def test_no_truncation_warnings_on_clean_fixture(reconstruction):
    # The fixture opens every episode from flat, inside the window.
    assert reconstruction.warnings == []
    assert not reconstruction.closing_events["cost_basis_incomplete"].any()


def test_account_flat_at_end(reconstruction):
    # Every fixture episode closes; no residual open position.
    assert len(reconstruction.open_positions) == 0


@pytest.mark.parametrize("i", range(len(fx.EXPECTED_CLOSING_EVENTS)))
def test_closing_event_matches_hand_computed(reconstruction, i):
    got = reconstruction.closing_events.iloc[i]
    exp = fx.EXPECTED_CLOSING_EVENTS[i]
    assert got["episode"] == exp["episode"]
    assert got["asset"] == exp["asset"]
    assert got["direction"] == exp["direction"]
    assert got["open_ts"] == exp["open_ts"]
    assert got["close_ts"] == exp["close_ts"]
    assert got["is_liquidation"] == exp["is_liquidation"]
    for k in ("size_closed", "entry_px", "exit_px", "realized_usd",
              "close_fee_usd"):
        assert math.isclose(got[k], exp[k], abs_tol=TOL), (
            f"event {i} field {k}: got {got[k]}, expected {exp[k]}"
        )


def test_realized_total(reconstruction):
    assert math.isclose(
        total_realized_usd(reconstruction.closing_events),
        fx.EXPECTED_TOTALS["total_realized_usd"], abs_tol=TOL,
    )


def test_realized_matches_venue_closedpnl_sum(reconstruction):
    """Our realized total must equal the venue's closedPnl summed over the
    same fills — the fixture-level version of Gate 3b."""
    venue = sum(float(f["closedPnl"]) for f in fx.FILLS)
    assert math.isclose(
        total_realized_usd(reconstruction.closing_events), venue, abs_tol=TOL,
    )


def test_short_profits_when_price_falls(reconstruction):
    """The sign test: episode 2 is a short opened at 3000, closed at 2900 —
    a short must PROFIT when price falls."""
    ev = reconstruction.closing_events
    short = ev[(ev["episode"] == 2)].iloc[0]
    assert short["direction"] == "short"
    assert short["realized_usd"] > 0
    assert math.isclose(short["realized_usd"], 200.0, abs_tol=TOL)


def test_partial_close_uses_average_entry(reconstruction):
    """Episode 3: entries at 100k and 102k -> avg 101k, applied to both the
    winning and losing partial close."""
    ev = reconstruction.closing_events
    ep3 = ev[ev["episode"] == 3]
    assert len(ep3) == 2
    assert all(math.isclose(x, 101000.0, abs_tol=TOL) for x in ep3["entry_px"])


def test_flip_split_into_two_episodes(reconstruction):
    """The flip fill closes episode 4 (long) and opens episode 5 (short)."""
    ev = reconstruction.closing_events
    ep4 = ev[ev["episode"] == 4].iloc[0]
    ep5 = ev[ev["episode"] == 5].iloc[0]
    assert ep4["direction"] == "long"
    assert ep5["direction"] == "short"
    # The flip fill's full fee lands on the close leg (episode 4).
    assert math.isclose(ep4["close_fee_usd"], 0.372, abs_tol=TOL)
    assert ep4["close_ts"] == ep5["open_ts"]  # same fill


def test_liquidation_flagged(reconstruction):
    ev = reconstruction.closing_events
    liq = ev[ev["is_liquidation"]]
    assert len(liq) == 1
    assert liq.iloc[0]["asset"] == "SOL"
    assert liq.iloc[0]["realized_usd"] < 0


# ---------------------------------------------------------------------------
# Funding
# ---------------------------------------------------------------------------

def test_funding_rows_match_fixture():
    fl = reconstruct_funding(load_funding(fx.FUNDING_EVENTS))
    assert len(fl) == len(fx.EXPECTED_FUNDING_ROWS)
    for i, exp in enumerate(fx.EXPECTED_FUNDING_ROWS):
        got = fl.iloc[i]
        assert got["ts"] == exp["ts"]
        assert got["asset"] == exp["asset"]
        assert math.isclose(got["usdc"], exp["usdc"], abs_tol=TOL)


def test_funding_signed_total():
    fl = reconstruct_funding(load_funding(fx.FUNDING_EVENTS))
    assert math.isclose(
        total_funding_usd(fl), fx.EXPECTED_TOTALS["total_funding_usd"],
        abs_tol=TOL,
    )
    # Both directions present: one paid (-), one received (+).
    assert (fl["usdc"] < 0).any() and (fl["usdc"] > 0).any()


# ---------------------------------------------------------------------------
# The equity identity on the fixture (the fixture-level Gate 3c: flat account,
# unrealized = 0, all flows known).
# ---------------------------------------------------------------------------

def test_fixture_equity_identity():
    recon = reconstruct_positions(_fills_df())
    fills = load_fills(fx.FILLS)
    funding = reconstruct_funding(load_funding(fx.FUNDING_EVENTS))

    realized = total_realized_usd(recon.closing_events)
    fees = float(fills["fee"].sum())
    fund = total_funding_usd(funding)
    deposits = fx.EXPECTED_TOTALS["total_deposits_usd"]
    withdrawals = fx.EXPECTED_TOTALS["total_withdrawals_usd"]

    equity = deposits - withdrawals + realized + fund - fees  # unrealized = 0
    assert math.isclose(
        equity, fx.EXPECTED_TOTALS["final_equity_usd"], abs_tol=1e-4
    )
