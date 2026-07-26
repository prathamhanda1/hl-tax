"""
tests/test_reconciliation.py — GATE 3b and GATE 3c against cached REAL data.

This is the "reconcile against reality" heart of the project (doctrine #2):
our numbers are checked against two independent sources HL itself provides.

  Gate 3b — venue reconciliation. For every closing event we FULLY captured
  (cost_basis_incomplete == False), our independently reconstructed realized
  P&L must match the venue's own `closedPnl` for that same closing fill,
  within config.TOL_VENUE_RECONCILE_USD. This is what proves the average-entry
  + gross-of-fees convention is the one HL actually uses (errors_in_plan.md #7)
  rather than an assumption we hoped was true.

  Gate 3c — the equity identity. On a clean, full-history account the entire
  ledger of economic events must reconcile to the account's current USDC:
      deposits - withdrawals - withdraw_fees
        + SUM(realized) + SUM(funding, signed) - SUM(fees) - SUM(builder_fees)
        + unrealized_now
      ~= perp accountValue_now + spot USDC_now
  (see config.TOL_EQUITY_IDENTITY_USD for the full derivation and the two
  terms — builderFee and spot USDC — found only by reconciling against real
  data). If a sign is backwards or a flow is missed, this does NOT close.

Both gates SKIP CLEANLY on a fresh clone with no cached real pulls, and
ENFORCE once a cache exists (plan, Part 3). Fixture-only checks live in
test_positions.py; this file needs the real cache.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

import config
from load.load import load_address
from reconstruct.positions import reconstruct_positions, total_realized_usd
from reconstruct.funding import reconstruct_funding, total_funding_usd

# The address whose fixtures/empty roles make them unsuitable as reconciliation
# targets are simply not "reconcilable" and are skipped by the guards below;
# no address is hard-coded as required.
_RAW = config.RAW_CACHE_DIR


def _cached_addresses() -> list[str]:
    if not _RAW.exists():
        return []
    out = []
    for d in sorted(_RAW.iterdir()):
        if d.is_dir() and d.name.startswith("0x") \
                and (d / "manifest.json").exists():
            out.append(d.name)
    return out


_ADDRS = _cached_addresses()
pytestmark = pytest.mark.skipif(
    not _ADDRS, reason="no cached real address pulls (fresh clone) - skipping "
                       "reconciliation gates until `python -m fetch.fetch_user "
                       "<address>` has been run"
)


def _spot_usdc(address: str) -> float | None:
    p = _RAW / address / "spot_clearinghouse_state.json"
    if not p.exists():
        return None
    spot = json.loads(p.read_text(encoding="utf-8"))
    for b in spot.get("balances", []):
        if b.get("coin") == "USDC":
            return float(b["total"])
    return 0.0


# ---------------------------------------------------------------------------
# GATE 3b — venue reconciliation (runs on ANY cached address with complete
# episodes; the more truncated the address, the fewer events, but those we DID
# capture must still match).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("address", _ADDRS)
def test_gate3b_venue_reconciliation(address):
    loaded = load_address(address)
    recon = reconstruct_positions(loaded.fills)
    ce = recon.closing_events
    complete = ce[~ce["cost_basis_incomplete"]]
    if len(complete) == 0:
        pytest.skip(f"{address}: no fully-captured episodes to reconcile "
                    f"(history truncated) - reconstruction correctly declined")

    venue = loaded.fills[["tid", "closed_pnl"]].rename(
        columns={"tid": "close_tid", "closed_pnl": "venue_pnl"}
    )
    m = complete.merge(venue, on="close_tid", how="left", validate="one_to_one")
    assert not m["venue_pnl"].isna().any(), "a closing fill had no venue closedPnl"

    dev = (m["realized_usd"] - m["venue_pnl"]).abs()
    max_dev = float(dev.max())
    total_dev = float((m["realized_usd"] - m["venue_pnl"]).sum())
    print(f"\n[Gate 3b] {address}: {len(m)} complete events | "
          f"our realized {m['realized_usd'].sum():.4f} vs venue "
          f"{m['venue_pnl'].sum():.4f} | max per-event dev {max_dev:.4f} | "
          f"total dev {total_dev:.4f}")
    assert max_dev <= config.TOL_VENUE_RECONCILE_USD, (
        f"{address}: max per-event deviation {max_dev:.4f} exceeds tolerance "
        f"{config.TOL_VENUE_RECONCILE_USD} - the reconstruction convention may "
        f"not match HL's closedPnl (errors_in_plan.md #7)"
    )


# ---------------------------------------------------------------------------
# GATE 3c — the equity identity, on addresses clean enough for it to hold.
# ---------------------------------------------------------------------------

def _is_reconcilable(address: str, loaded, recon) -> tuple[bool, str]:
    """The identity holds only for a self-contained account: full history (no
    incomplete episodes), no USDC-moving ledger flows we don't model (only
    deposit/withdraw), no spot trades converting USDC<->token, and a cached
    spot balance. Otherwise Gate 3c is not applicable - skip, don't fail."""
    if recon.closing_events["cost_basis_incomplete"].any():
        return False, "history truncated (incomplete episodes)"
    led_types = set(loaded.ledger["type"]) if len(loaded.ledger) else set()
    if not led_types <= {"deposit", "withdraw"}:
        return False, f"ledger has flows beyond deposit/withdraw: {sorted(led_types - {'deposit','withdraw'})}"
    if len(loaded.fills) and (~loaded.fills["is_perp"]).any():
        return False, "has spot fills (USDC<->token) not modeled by the identity"
    if _spot_usdc(address) is None:
        return False, "no cached spotClearinghouseState (re-fetch with --refresh)"
    return True, ""


@pytest.mark.parametrize("address", _ADDRS)
def test_gate3c_equity_identity(address):
    loaded = load_address(address)
    recon = reconstruct_positions(loaded.fills)
    ok, why = _is_reconcilable(address, loaded, recon)
    if not ok:
        pytest.skip(f"{address}: not a clean reconciliation target - {why}")

    led = loaded.ledger
    deposits = float(led.loc[led["type"] == "deposit", "usdc"].sum())
    withdrawals = float(led.loc[led["type"] == "withdraw", "usdc"].sum())
    wd_fees = float(led.loc[led["type"] == "withdraw", "fee"].fillna(0).sum())

    realized = total_realized_usd(recon.closing_events)
    funding = total_funding_usd(reconstruct_funding(loaded.funding))
    perp = loaded.fills[loaded.fills["is_perp"]]
    fees = float(perp["fee"].sum())
    builder_fees = float(perp["builder_fee"].sum())

    chs = json.loads((_RAW / address / "clearinghouse_state.json")
                     .read_text(encoding="utf-8"))
    account_value = float(chs["marginSummary"]["accountValue"])
    unrealized = sum(
        float(p["position"]["unrealizedPnl"])
        for p in chs.get("assetPositions", [])
    )
    spot_usdc = _spot_usdc(address)

    lhs = (deposits - withdrawals - wd_fees + realized + funding
           - fees - builder_fees + unrealized)
    rhs = account_value + spot_usdc
    residual = lhs - rhs
    print(f"\n[Gate 3c] {address}: LHS {lhs:.4f} vs RHS (perp {account_value:.2f} "
          f"+ spot {spot_usdc:.4f}) {rhs:.4f} | residual {residual:.4f} "
          f"(builder_fees {builder_fees:.4f})")
    assert abs(residual) <= config.TOL_EQUITY_IDENTITY_USD, (
        f"{address}: equity identity residual {residual:.4f} exceeds tolerance "
        f"{config.TOL_EQUITY_IDENTITY_USD} - a sign is backwards or a flow is "
        f"missing somewhere (Gate 3c, the whole reason the tool can be trusted)"
    )


def test_at_least_one_address_reconciled_each_gate():
    """Guard against silent all-skip: with a real cache, at least one address
    must actually exercise each gate (not merely skip)."""
    any_3b = any_3c = False
    for address in _ADDRS:
        loaded = load_address(address)
        recon = reconstruct_positions(loaded.fills)
        if len(recon.closing_events[~recon.closing_events["cost_basis_incomplete"]]):
            any_3b = True
        if _is_reconcilable(address, loaded, recon)[0]:
            any_3c = True
    assert any_3b, "no cached address exercised Gate 3b (venue reconciliation)"
    assert any_3c, ("no cached address exercised Gate 3c (equity identity) - "
                    "fetch a clean full-history perp-only account")
