"""
tests/test_treatments.py — GATE 5 (PHASE5_treatments_guide.md §12).

Paper before code: every expected number is hand-computed in the comments of
its fixture, then asserted. The no-set-off wall (tests 1-3) is the one that must
never silently break — if a slab op and a VDA op ever produce the same number on
the §12.1 fixture, a set-off rule is wired wrong.

RATE ASSUMPTIONS USED (all NEEDS-VERIFICATION, from config.py):
  VDA flat 30%, cess 4%, surcharge 0 below Rs 50,00,000 total income.
  New-regime slab bands: 0-4L 0% | 4-8L 5% | 8-12L 10% | 12-16L 15% |
    16-20L 20% | 20-24L 25% | >24L 30%.  s.87A rebate wipes tax at/below Rs 12L.
  All fixture incomes are kept below the surcharge floor so surcharge = 0 and
  the arithmetic stays hand-checkable; slab-op profit fixtures are kept ABOVE
  the Rs 12L rebate ceiling so the rebate does not mask the slab math.
"""

from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

import config
from interpret.treatments import (
    Assumptions,
    cross_cutting,
    interpret,
    slab_tax,
    tds_summary,
    treatment_futures_inr,
    treatment_non_speculative,
    treatment_speculative,
    treatment_vda,
    turnover_inr,
)

FX = 85.0
_T0 = datetime(2025, 6, 2, 12, 0, tzinfo=timezone.utc)
_RDATE = date(2025, 6, 2)


def make_ledger(rows: list[dict]) -> pd.DataFrame:
    """Build an INR ledger in the Phase 5 input-contract shape. Each row dict
    needs at least category + amount_inr; everything else defaults."""
    out = []
    for i, r in enumerate(rows):
        amount_inr = float(r["amount_inr"])
        out.append({
            "event_id": r.get("event_id", f"e{i}"),
            "ts_utc": r.get("ts_utc", _T0),
            "category": r["category"],
            "asset": r.get("asset", "BTC"),
            "asset_class": r.get("asset_class", "crypto-perp"),
            "episode_id": r.get("episode_id", pd.NA),
            "amount_usd": amount_inr / FX,
            "fx_rate": FX,
            "fx_rate_date": r.get("fx_rate_date", _RDATE),
            "amount_inr": amount_inr,
            "is_liquidation": r.get("is_liquidation", False),
            "gross_consideration_inr": r.get("gross_consideration_inr", np.nan),
            "is_c2c_hop": r.get("is_c2c_hop", False),
        })
    return pd.DataFrame(out)


def rp(amount, episode, **kw):
    return {"category": "realized_pnl", "amount_inr": amount,
            "episode_id": episode, **kw}


# ===========================================================================
# 1. VDA no-set-off — +Rs 250k and -Rs 50k -> base Rs 250k, NOT Rs 200k.
#    tax = 250,000 * 0.30 = 75,000 ; cess 4% -> 3,000 ; total 78,000.
# ===========================================================================

def test_vda_no_set_off_loss_does_not_reduce_base():
    ledger = make_ledger([rp(250_000, 1), rp(-50_000, 2)])
    out = treatment_vda(ledger, Assumptions(vda_loss_scope="no_set_off_at_all"))
    assert out["taxable_base_inr"] == pytest.approx(250_000)
    assert out["taxable_base_inr"] != pytest.approx(200_000)
    assert out["tax_before_cess_inr"] == pytest.approx(75_000)
    assert out["cess_inr"] == pytest.approx(3_000)
    assert out["total_tax_inr"] == pytest.approx(78_000)
    assert out["loss_treatment"] == "none"
    assert out["carry_forward_inr"] == 0.0


# ===========================================================================
# 2. VDA all-losses -> base exactly 0, never negative; carry-forward 0.
# ===========================================================================

def test_vda_all_losses_base_is_zero_not_negative():
    ledger = make_ledger([rp(-30_000, 1), rp(-70_000, 2), rp(-5_000, 3)])
    out = treatment_vda(ledger)
    assert out["taxable_base_inr"] == 0.0
    assert out["total_tax_inr"] == 0.0
    assert out["carry_forward_inr"] == 0.0
    assert out["vda_gross_base_inr"] == 0.0
    assert out["vda_net_base_inr"] == 0.0


# ===========================================================================
# 3. VDA gross-vs-net gap — no_set_off >= intra_vda, and the gap is reported.
#    gross = 250k (loss dropped) ; net = 200k ; gap = 50k.
# ===========================================================================

def test_vda_gross_vs_net_gap_reported():
    ledger = make_ledger([rp(250_000, 1), rp(-50_000, 2)])
    a_gross = Assumptions(vda_loss_scope="no_set_off_at_all")
    a_net = Assumptions(vda_loss_scope="intra_vda_allowed")
    gross = treatment_vda(ledger, a_gross)["taxable_base_inr"]
    net = treatment_vda(ledger, a_net)["taxable_base_inr"]
    assert gross >= net
    assert gross == pytest.approx(250_000)
    assert net == pytest.approx(200_000)
    out = treatment_vda(ledger, a_gross)
    assert out["vda_gross_vs_net_gap_inr"] == pytest.approx(50_000)


# ===========================================================================
# 4. Speculative jail — a speculative net loss does NOT reduce other-head
#    income; only the non-speculative reading sets it off in-year.
#    Fixture: net_pnl -Rs 500k ; other-head income Rs 300k.
# ===========================================================================

def _loss_fixture():
    return make_ledger([rp(200_000, 1), rp(-700_000, 2)])  # net -500,000


def test_speculative_loss_is_ring_fenced():
    ledger = _loss_fixture()
    a = Assumptions(other_head_income_inr=300_000)
    spec = treatment_speculative(ledger, a)
    nonspec = treatment_non_speculative(ledger, a)
    # Speculative loss touches no other head this year; it only carries.
    assert spec["current_year_setoff_inr"] == 0.0
    assert spec["carry_forward_inr"] == pytest.approx(-500_000)
    assert spec["carry_forward_years"] == config.SPECULATIVE_CARRY_FORWARD_YEARS
    assert spec["loss_treatment"] == "ring_fenced_speculative"
    # Non-speculative DOES set off against the other head -> proves divergence.
    assert nonspec["current_year_setoff_inr"] == pytest.approx(300_000)


# ===========================================================================
# 5. Non-speculative set-off — business loss offsets other-head income EXCEPT
#    salary. Fixture: net -500k ; other-head 300k ; salary 800k.
#    setoff = min(500k, 300k) = 300k ; residual carry = -200k ; salary untouched.
# ===========================================================================

def test_non_speculative_setoff_leaves_salary_untouched():
    ledger = _loss_fixture()
    a = Assumptions(other_head_income_inr=300_000, salary_income_inr=800_000)
    out = treatment_non_speculative(ledger, a)
    assert out["current_year_setoff_inr"] == pytest.approx(300_000)
    assert out["other_head_income_after_setoff_inr"] == pytest.approx(0.0)
    assert out["carry_forward_inr"] == pytest.approx(-200_000)
    assert out["carry_forward_years"] == config.BUSINESS_CARRY_FORWARD_YEARS
    # The salary line is NEVER touched by a business-loss set-off.
    assert out["salary_income_after_setoff_inr"] == pytest.approx(800_000)


# ===========================================================================
# 6. Slab ops agree in a net-profit year, then diverge in a loss year.
#    Profit fixture: net +Rs 20,00,000 -> slab tax 200,000 + cess 8,000 = 208,000.
#      4-8L: 20,000 | 8-12L: 40,000 | 12-16L: 60,000 | 16-20L: 80,000 = 200,000.
# ===========================================================================

def test_slab_ops_agree_on_profit_then_diverge_on_loss():
    profit = make_ledger([rp(2_000_000, 1)])
    a = Assumptions(other_head_income_inr=300_000)
    t2 = treatment_futures_inr(profit, a)["total_tax_inr"]
    t3 = treatment_speculative(profit, a)["total_tax_inr"]
    t4 = treatment_non_speculative(profit, a)["total_tax_inr"]
    assert t2 == pytest.approx(208_000)
    assert t2 == pytest.approx(t3) == pytest.approx(t4)  # same headline number

    # A loss year: same headline (all zero tax) but the loss TREATMENT diverges.
    loss = _loss_fixture()
    s3 = treatment_speculative(loss, a)
    s4 = treatment_non_speculative(loss, a)
    assert s3["current_year_setoff_inr"] != s4["current_year_setoff_inr"]
    assert s3["carry_forward_inr"] != s4["carry_forward_inr"]
    assert s3["loss_treatment"] != s4["loss_treatment"]


# ===========================================================================
# 7. Counterfactual flag — real stablecoin_leg rows -> OP 2 premise
#    "counterfactual"; without them -> "live".
# ===========================================================================

def test_op2_counterfactual_when_stablecoin_legs_present():
    with_legs = make_ledger([
        rp(100_000, 1),
        {"category": "stablecoin_leg", "amount_inr": -2_000,
         "asset": "USDC", "asset_class": "stablecoin_leg",
         "gross_consideration_inr": 400_000},
    ])
    without = make_ledger([rp(100_000, 1)])
    assert treatment_futures_inr(with_legs)["premise"] == "counterfactual"
    assert treatment_futures_inr(without)["premise"] == "live"


# ===========================================================================
# 8. Turnover — abs-sum per episode, NOT net P&L.
#    ep1 +250k ; ep2 -50k ; ep3 (+200k, -200k -> net 0).
#    turnover = 250k + 50k + 0 = 300,000 ; net P&L = 200,000.
# ===========================================================================

def test_turnover_is_abs_sum_not_net():
    ledger = make_ledger([
        rp(250_000, 1), rp(-50_000, 2), rp(200_000, 3), rp(-200_000, 3),
    ])
    assert turnover_inr(ledger) == pytest.approx(300_000)
    net = ledger.loc[ledger["category"] == "realized_pnl", "amount_inr"].sum()
    assert net == pytest.approx(200_000)
    assert turnover_inr(ledger) != pytest.approx(net)


def test_audit_flag_trips_above_threshold():
    # Two episodes each Rs 60,00,000 -> turnover 1.2 crore > 1 crore threshold.
    ledger = make_ledger([rp(6_000_000, 1), rp(6_000_000, 2)])
    out = treatment_non_speculative(ledger)
    assert out["turnover_inr"] == pytest.approx(12_000_000)
    assert out["audit_flag"] is True


# ===========================================================================
# 9. TDS split — losing perp P&L but a real USDC->INR off-ramp.
#    realized -100k, gross consideration 500k ; off-ramp gross 400k.
#    tds on P&L: 1% * 500k = 5,000 (OP 1 only) ; slab ops 0.
#    tds on legs: 1% * 400k = 4,000 under ALL ops, independent of the loss.
#    tds actually deducted on-venue = 0.
# ===========================================================================

def test_tds_rides_the_leg_not_the_pnl():
    ledger = make_ledger([
        rp(-100_000, 1, gross_consideration_inr=500_000),
        {"category": "stablecoin_leg", "amount_inr": -2_000,
         "asset": "USDC", "asset_class": "stablecoin_leg",
         "gross_consideration_inr": 400_000},
    ])
    tds = tds_summary(ledger)
    assert tds["tds_expected_on_pnl_by_reading"]["vda"] == pytest.approx(5_000)
    assert tds["tds_expected_on_pnl_by_reading"]["speculative"] == 0.0
    assert tds["tds_expected_on_pnl_by_reading"]["non_speculative"] == 0.0
    # Leg TDS exists under ALL readings and does NOT gate on the P&L loss.
    assert tds["tds_expected_on_legs_inr"] == pytest.approx(4_000)
    assert tds["tds_actually_deducted_on_venue_inr"] == 0.0

    # And it shows up correctly per-treatment.
    vda = treatment_vda(ledger)
    spec = treatment_speculative(ledger)
    assert vda["tds_expected_on_pnl_inr"] == pytest.approx(5_000)
    assert vda["tds_expected_on_legs_inr"] == pytest.approx(4_000)
    assert spec["tds_expected_on_pnl_inr"] == 0.0
    assert spec["tds_expected_on_legs_inr"] == pytest.approx(4_000)
    # OP 2 abstracts the VDA away entirely -> even leg TDS is 0.
    assert treatment_futures_inr(ledger)["tds_expected_on_legs_inr"] == 0.0


# ===========================================================================
# 10. C2C both-sides — a crypto-to-crypto hop counts TDS on BOTH legs.
#     One hop, gross 300k -> leg_count 2 ; tds = 1% * 300k * 2 = 6,000.
# ===========================================================================

def test_c2c_hop_counts_both_sides():
    ledger = make_ledger([
        rp(50_000, 1),
        {"category": "stablecoin_leg", "amount_inr": 0.0, "asset": "BTC",
         "asset_class": "stablecoin_leg", "gross_consideration_inr": 300_000,
         "is_c2c_hop": True},
    ])
    tds = tds_summary(ledger)
    assert tds["tds_leg_count"] == 2
    assert tds["tds_expected_on_legs_inr"] == pytest.approx(6_000)


# ===========================================================================
# 11. Assumptions echoed — changing funding_treatment changes a number AND is
#     reflected in assumptions_used.
#     Fixture: net_pnl +Rs 20,00,000 ; funding -Rs 4,00,000 (paid).
#       separate_taxable_event -> base 2,000,000 - 400,000 = 1,600,000.
#       ignore                 -> base 2,000,000.
# ===========================================================================

def test_funding_treatment_changes_number_and_is_echoed():
    ledger = make_ledger([
        rp(2_000_000, 1),
        {"category": "funding", "amount_inr": -400_000, "episode_id": 1},
    ])
    sep = treatment_futures_inr(ledger, Assumptions(funding_treatment="separate_taxable_event"))
    ign = treatment_futures_inr(ledger, Assumptions(funding_treatment="ignore"))
    assert sep["taxable_base_inr"] == pytest.approx(1_600_000)
    assert ign["taxable_base_inr"] == pytest.approx(2_000_000)
    assert sep["taxable_base_inr"] != ign["taxable_base_inr"]
    assert sep["assumptions_used"]["funding_treatment"] == "separate_taxable_event"
    assert ign["assumptions_used"]["funding_treatment"] == "ignore"


# ===========================================================================
# Structural guards — the non-negotiables (§0, §11).
# ===========================================================================

def test_interpret_declares_no_winner():
    ledger = make_ledger([rp(100_000, 1)])
    result = interpret(ledger)
    assert set(result) == {"vda", "futures_inr", "speculative",
                           "non_speculative", "tds", "cross_cutting"}
    for forbidden in ("best", "recommended", "correct"):
        assert forbidden not in result


def test_slab_op_and_vda_op_differ_on_the_no_setoff_fixture():
    # §12 wall: if a slab op and the VDA op ever match on this fixture, a
    # set-off rule is wired wrong.
    ledger = make_ledger([rp(250_000, 1), rp(-50_000, 2)])
    vda = treatment_vda(ledger)["taxable_base_inr"]        # 250,000 (loss dropped)
    slab = treatment_speculative(ledger)["taxable_base_inr"]  # 200,000 (netted)
    assert vda != slab


def test_slab_tax_helper_matches_hand_figure():
    # Rs 20,00,000 -> 200,000 under the documented bands.
    assert slab_tax(2_000_000, config.SLAB_SCHEDULE_NEW_115BAC) == pytest.approx(200_000)
    assert slab_tax(0, config.SLAB_SCHEDULE_NEW_115BAC) == 0.0
    assert slab_tax(-5, config.SLAB_SCHEDULE_NEW_115BAC) == 0.0


def test_every_treatment_echoes_full_assumptions_and_line_items():
    ledger = make_ledger([rp(100_000, 1),
                          {"category": "fee", "amount_inr": 500, "episode_id": 1}])
    a = Assumptions()
    for fn in (treatment_vda, treatment_futures_inr, treatment_speculative,
               treatment_non_speculative):
        out = fn(ledger, a)
        assert out["assumptions_used"]["vda_flat_rate"] == config.VDA_FLAT_RATE
        assert "line_items" in out and len(out["line_items"]) == len(ledger)
        assert out["section_refs"]["1961"] and out["section_refs"]["2025"]


def test_cross_cutting_states_limitations_at_top():
    ledger = make_ledger([rp(100_000, 1)])
    cc = cross_cutting(ledger)
    assert cc["limitations_top"]
    assert cc["onramp_boundary"]["onramp_cost_supplied"] is False
