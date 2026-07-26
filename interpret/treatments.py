"""
interpret/treatments.py — Phase 5, the legal heart of the engine.

Reads ONE INR ledger (the output of phases 3+4) and computes the tax position
under FOUR named treatments, side by side, declaring NONE of them correct. Plus
a TDS decomposition and the cross-cutting report sections.

NON-NEGOTIABLES (PHASE5_treatments_guide.md §0; do not violate):
  1. No treatment is labeled "correct." Ordered by section number, not preference.
  2. Every assumption is an explicit `Assumptions` parameter, echoed into output.
  3. Every output number is traceable to the ledger rows in `line_items`.
  4. PURE functions: DataFrames/dicts in, dicts out. No I/O, no network, no print.
  5. The law is unsettled — the tool's only claim is arithmetic under stated
     assumptions. Said out loud in `cross_cutting` and in each treatment's caveats.
  6. Every section number is NEEDS-VERIFICATION, dual-cited (1961 + 2025).

THE TWO ORTHOGONAL QUESTIONS (§2), which is why there are exactly four ops:
  Q1  Is a perp close a "transfer of a VDA"?
        YES -> flat 30% special regime .......................... OP 1  treatment_vda
        NO  -> ordinary income at slab rates, and then Q2:
  Q2  Speculative or not (only if ordinary)?
        INR-margined / VDA abstracted ........................... OP 2  treatment_futures_inr
        cash-settled foreign DEX, losses ring-fenced ............ OP 3  treatment_speculative
        business on the merits, normal loss set-off ............. OP 4  treatment_non_speculative

Settlement rail is the master switch, but (corrected per §2/§14) it governs the
STABLECOIN LEG, not the P&L rate: the prevailing industry reading taxes even a
USDC-settled perp's P&L at slab with no TDS, and the 30%/1% ride the USDC<->INR
conversion. So on a real Hyperliquid address OP 3 / OP 4 are the live P&L
contenders, OP 1 is the conservative bound, and OP 2 (fully INR-margined, zero
VDA anywhere) is COUNTERFACTUAL — flagged whenever real stablecoin legs appear.

----------------------------------------------------------------------------
THE INPUT CONTRACT — the INR ledger (§1). One row per economic event.
----------------------------------------------------------------------------
Required columns:
  event_id            stable unique id, traces back to a raw fill/funding/ledger row
  ts_utc              tz-aware UTC timestamp of the event
  category            realized_pnl | funding | fee | deposit | withdrawal | stablecoin_leg
  asset               e.g. "BTC", "xyz:TSLA", "USDC"
  asset_class         crypto-perp | equity-perp | stablecoin_leg | other
  episode_id          which open->close episode this belongs to (or <NA>)
  amount_usd          signed USD amount
  fx_rate             INR per USD used for this event
  fx_rate_date        the business day whose rate was used (audit trail)
  amount_inr          signed INR amount, computed ONCE upstream, per convention:
                        realized_pnl : + = gain,    - = loss
                        funding      : + = received, - = paid
                        fee          : + = fee paid (a cost), - = maker rebate
                        stablecoin_leg: + = gain,    - = loss on the USDC<->INR move
  is_liquidation      bool, for the closing event

Optional columns (used where present, defaulted where absent):
  gross_consideration_inr  gross INR value of the transfer, for TDS. On
                           realized_pnl closing rows = exit notional; on
                           stablecoin_leg rows = gross off-ramp value. NaN otherwise.
  is_c2c_hop               bool on a stablecoin_leg row: this conversion is a
                           crypto-to-crypto hop, so TDS attaches to BOTH sides.

The ledger does NOT contain the on-ramp cost of the USDC itself (a manual-input
slot, config.ON_RAMP_USDC_COST_INR) nor non-Hyperliquid activity — both are
surfaced loudly by `cross_cutting`, never silently assumed.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

import config

# Ledger categories.
CAT_REALIZED = "realized_pnl"
CAT_FUNDING = "funding"
CAT_FEE = "fee"
CAT_DEPOSIT = "deposit"
CAT_WITHDRAWAL = "withdrawal"
CAT_STABLECOIN_LEG = "stablecoin_leg"

REQUIRED_LEDGER_COLUMNS = [
    "event_id", "ts_utc", "category", "asset", "asset_class", "episode_id",
    "amount_usd", "fx_rate", "fx_rate_date", "amount_inr", "is_liquidation",
]

# Section references, dual-cited (1961 -> 2025), every one NEEDS VERIFICATION.
SECTION_REFS = {
    "vda": {
        "1961": ["s.2(47A) VDA defn", "s.115BBH charge", "s.2(47) transfer",
                 "s.194S TDS"],
        "2025": ["s.2 defns", "s.194 (115B family)", "s.393 TDS"],
    },
    "futures_inr": {
        "1961": ["s.28 PGBP charge", "s.37 deductions", "s.115BAC slab"],
        "2025": ["s.26 PGBP charge", "s.34 deductions", "s.202 slab"],
    },
    "speculative": {
        "1961": ["s.43(5) speculative defn", "s.73 set-off jail", "s.115BAC slab"],
        "2025": ["ss.26-66 PGBP interp", "s.113 set-off", "s.202 slab"],
    },
    "non_speculative": {
        "1961": ["s.28/s.37 business", "s.71 inter-head set-off",
                 "s.72 carry-forward", "s.44AA/s.44AB books/audit"],
        "2025": ["s.26/s.34 business", "s.109 set-off", "s.112 carry-forward",
                 "s.62/s.63 books/audit"],
    },
    "tds": {"1961": ["s.194S"], "2025": ["s.393"]},
}


# ===========================================================================
# Assumptions — every value echoed into every treatment's output (§3).
# ===========================================================================

@dataclass(frozen=True)
class Assumptions:
    # --- rate / regime knobs ---
    vda_flat_rate: float = config.VDA_FLAT_RATE
    cess_rate: float = config.CESS_RATE
    slab_regime: str = config.SLAB_REGIME_DEFAULT
    slab_schedule: tuple = tuple(config.SLAB_SCHEDULE_NEW_115BAC)
    rebate_87a_limit_inr: float = config.REBATE_87A_LIMIT_NEW_INR
    surcharge_schedule: tuple = tuple(config.SURCHARGE_SCHEDULE)

    # --- VDA loss scope: compute BOTH, show the gap (§4) ---
    vda_loss_scope: str = "no_set_off_at_all"   # | "intra_vda_allowed"

    # --- cross-cutting event treatment (applied identically across all ops) ---
    funding_treatment: str = "separate_taxable_event"
        # | "position_cost" | "ignore" — the Act is SILENT; a live CA question
    fee_deductible_slab: bool = True     # fees deduct under business/spec readings
    fee_deductible_vda: bool = False     # under 115BBH only cost of acquisition
    include_stablecoin_legs: bool = True

    # --- settlement premise for OP 2 ---
    treat_settlement_as_inr: bool = False  # models the INR-margined counterfactual

    # --- audit / turnover / TDS ---
    audit_turnover_threshold_inr: float = config.AUDIT_TURNOVER_THRESHOLD_INR
    tds_rate: float = config.TDS_RATE_LEG
    speculative_carry_forward_years: int = config.SPECULATIVE_CARRY_FORWARD_YEARS
    business_carry_forward_years: int = config.BUSINESS_CARRY_FORWARD_YEARS

    # --- other-head income (external to HL), only to DEMONSTRATE set-off under
    #     OP 4. Salary is carried separately because a business loss may NOT be
    #     set off against salary (s.71/s.109). Both default to zero. ---
    other_head_income_inr: float = 0.0   # non-salary, non-business (e.g. interest)
    salary_income_inr: float = 0.0       # protected: business loss cannot touch it


# ===========================================================================
# Rate machinery — pure arithmetic helpers.
# ===========================================================================

def slab_tax(income_inr: float, schedule) -> float:
    """Marginal slab tax over a (upper_bound, rate) schedule; None bound = inf.
    Income at or below zero pays zero (a loss is not a negative tax here — the
    set-off/carry-forward is modeled separately by each treatment)."""
    if income_inr <= 0:
        return 0.0
    tax = 0.0
    lower = 0.0
    for upper, rate in schedule:
        cap = math.inf if upper is None else float(upper)
        if income_inr > lower:
            band = min(income_inr, cap) - lower
            tax += band * rate
            lower = cap
        else:
            break
    return tax


def surcharge_rate(income_inr: float, schedule) -> float:
    """The surcharge rate applicable to a total income, by band."""
    for upper, rate in schedule:
        cap = math.inf if upper is None else float(upper)
        if income_inr <= cap:
            return rate
    return schedule[-1][1]


def _finalize_slab_tax(base_inr: float, a: Assumptions) -> dict:
    """Slab tax before cess, surcharge, cess and total, with the 87A rebate.
    Returns the pieces so each treatment can echo them identically."""
    if base_inr <= 0:
        return {"tax_before_cess_inr": 0.0, "surcharge_inr": 0.0,
                "cess_inr": 0.0, "total_tax_inr": 0.0}
    if base_inr <= a.rebate_87a_limit_inr:
        # s.87A wipes the tax entirely for total income within the ceiling.
        return {"tax_before_cess_inr": 0.0, "surcharge_inr": 0.0,
                "cess_inr": 0.0, "total_tax_inr": 0.0}
    tax = slab_tax(base_inr, a.slab_schedule)
    surch = tax * surcharge_rate(base_inr, a.surcharge_schedule)
    cess = (tax + surch) * a.cess_rate
    return {"tax_before_cess_inr": tax, "surcharge_inr": surch,
            "cess_inr": cess, "total_tax_inr": tax + surch + cess}


def _finalize_flat_tax(base_inr: float, rate: float, a: Assumptions) -> dict:
    """Flat-rate (VDA) tax with surcharge + cess. No 87A rebate on 115BBH income."""
    if base_inr <= 0:
        return {"tax_before_cess_inr": 0.0, "surcharge_inr": 0.0,
                "cess_inr": 0.0, "total_tax_inr": 0.0}
    tax = base_inr * rate
    surch = tax * surcharge_rate(base_inr, a.surcharge_schedule)
    cess = (tax + surch) * a.cess_rate
    return {"tax_before_cess_inr": tax, "surcharge_inr": surch,
            "cess_inr": cess, "total_tax_inr": tax + surch + cess}


# ===========================================================================
# Ledger accessors — pure views over the INR ledger.
# ===========================================================================

def _validate(ledger: pd.DataFrame) -> None:
    missing = [c for c in REQUIRED_LEDGER_COLUMNS if c not in ledger.columns]
    if missing:
        raise ValueError(
            f"INR ledger missing required column(s): {missing}. "
            f"See interpret/treatments.py input contract."
        )


def _rows(ledger: pd.DataFrame, category: str) -> pd.DataFrame:
    return ledger[ledger["category"] == category]


def _sum_inr(ledger: pd.DataFrame, category: str) -> float:
    return float(_rows(ledger, category)["amount_inr"].sum())


def _has_real_stablecoin_legs(ledger: pd.DataFrame) -> bool:
    """A real USDC<->INR leg contradicts the INR-margined premise of OP 2."""
    return bool(
        ((ledger["category"] == CAT_STABLECOIN_LEG)
         | (ledger["asset_class"] == CAT_STABLECOIN_LEG)).any()
    )


def _funding_component(ledger: pd.DataFrame, a: Assumptions) -> float:
    """Signed funding entering the slab base, per funding_treatment.
    "separate_taxable_event" and "position_cost" both fold the signed funding
    into the ordinary base (they differ in LABEL/head, not in the slab number);
    "ignore" drops it. The VDA reading handles funding differently (positives
    only — see treatment_vda)."""
    if a.funding_treatment == "ignore":
        return 0.0
    return _sum_inr(ledger, CAT_FUNDING)


def _fee_component(ledger: pd.DataFrame, deductible: bool) -> float:
    """Total fees available to deduct (positive = paid). Zero when the reading
    disallows fee deduction (VDA)."""
    if not deductible:
        return 0.0
    return float(_rows(ledger, CAT_FEE)["amount_inr"].sum())


def turnover_inr(ledger: pd.DataFrame) -> float:
    """Derivative turnover the ICAI way: sum of the ABSOLUTE per-episode
    realized differences (§7). NOT net P&L, NOT notional."""
    rp = _rows(ledger, CAT_REALIZED)
    if not len(rp):
        return 0.0
    per_ep = rp.groupby("episode_id", dropna=False)["amount_inr"].sum()
    return float(per_ep.abs().sum())


def _turnover_and_audit(ledger: pd.DataFrame, a: Assumptions) -> tuple[float, bool]:
    t = turnover_inr(ledger)
    return t, t > a.audit_turnover_threshold_inr


def _slab_line_items(ledger: pd.DataFrame, a: Assumptions,
                     fee_deductible: bool) -> pd.DataFrame:
    """One traceable row per ledger event with its signed contribution to the
    slab base (realized_pnl + funding - fees). Deposits/withdrawals/legs
    contribute 0 to the P&L base but are kept for traceability."""
    fund_factor = 0.0 if a.funding_treatment == "ignore" else 1.0
    contribs = []
    for r in ledger.itertuples(index=False):
        if r.category == CAT_REALIZED:
            c = r.amount_inr
        elif r.category == CAT_FUNDING:
            c = r.amount_inr * fund_factor
        elif r.category == CAT_FEE:
            c = (-r.amount_inr) if fee_deductible else 0.0
        else:
            c = 0.0
        contribs.append(c)
    out = ledger.copy()
    out["contribution_inr"] = contribs
    return out


# ===========================================================================
# TDS — the 1% question, computed and flagged, never assumed resolved (§8).
# ===========================================================================

def _tds_on_pnl_inr(ledger: pd.DataFrame, a: Assumptions) -> float:
    """1% of the gross consideration on each perp close — ONLY relevant under
    the VDA reading (OP 1). Uses gross_consideration_inr where the ledger
    supplies it; a close with no consideration figure contributes 0 and is a
    known limitation, not a silent zero."""
    rp = _rows(ledger, CAT_REALIZED)
    if not len(rp) or "gross_consideration_inr" not in rp.columns:
        return 0.0
    gross = pd.to_numeric(rp["gross_consideration_inr"], errors="coerce")
    return float(a.tds_rate * np.nansum(gross.to_numpy()))


def _tds_on_legs(ledger: pd.DataFrame, a: Assumptions) -> tuple[float, int]:
    """1% on the gross consideration of each USDC<->INR conversion leg — under
    ALL readings, because converting the stablecoin to INR is a spot VDA sale
    regardless of how the trading P&L was classified. A crypto-to-crypto hop is
    taxed on BOTH sides. Returns (tds_inr, leg_count). Independent of P&L sign."""
    legs = _rows(ledger, CAT_STABLECOIN_LEG)
    if not len(legs) or "gross_consideration_inr" not in legs.columns:
        return 0.0, 0
    tds = 0.0
    count = 0
    has_c2c = "is_c2c_hop" in legs.columns
    for r in legs.itertuples(index=False):
        gross = getattr(r, "gross_consideration_inr")
        if gross is None or (isinstance(gross, float) and math.isnan(gross)):
            continue
        sides = 2 if (has_c2c and bool(getattr(r, "is_c2c_hop"))) else 1
        tds += a.tds_rate * float(gross) * sides
        count += sides
    return tds, count


def tds_summary(ledger: pd.DataFrame, a: Assumptions | None = None) -> dict:
    """The three-line honest decomposition (§8):
       - TDS expected on P&L  (>0 only under OP 1; 0 under the slab ops)
       - TDS expected on stablecoin legs (1% of gross off-ramp, ALL readings)
       - TDS actually deducted on-venue (~0 on a foreign DEX)."""
    a = a or Assumptions()
    _validate(ledger)
    tds_pnl_vda = _tds_on_pnl_inr(ledger, a)
    tds_legs, leg_count = _tds_on_legs(ledger, a)
    return {
        "tds_rate": a.tds_rate,
        # On the trading P&L: only OP 1 (VDA-on-P&L) puts TDS here.
        "tds_expected_on_pnl_by_reading": {
            "vda": tds_pnl_vda,
            "futures_inr": 0.0,
            "speculative": 0.0,
            "non_speculative": 0.0,
        },
        # On the stablecoin conversion legs: exists under ALL readings.
        "tds_expected_on_legs_inr": tds_legs,
        "tds_leg_count": leg_count,
        # Foreign DEX: no Indian intermediary to withhold on-venue.
        "tds_actually_deducted_on_venue_inr": 0.0,
        "thresholds_inr": {
            "specified_person": config.TDS_THRESHOLD_SPECIFIED_PERSON_INR,
            "other": config.TDS_THRESHOLD_OTHER_INR,
        },
        "section_refs": SECTION_REFS["tds"],
        "caveats": [
            "TDS rides the STABLECOIN LEG (USDC<->INR spot disposal), not the "
            "perp P&L, under the prevailing reading — deducted on gross value "
            "whether the trade won or lost. Only OP 1 notionally puts 1% on the "
            "P&L itself.",
            "On a foreign DEX there is no Indian intermediary to deduct; any TDS "
            "actually withheld happens at the Indian-exchange ramp and is "
            "CREDITABLE against final liability, never an extra tax.",
            "Crypto-to-crypto hops are taxed on BOTH sides. s.194S's own text "
            "references perpetual contracts, which cuts against the "
            "'derivatives never involve a VDA' premise — surfaced, not resolved.",
            "Thresholds and 2025-Act equivalents are NEEDS-VERIFICATION.",
        ],
    }


# ===========================================================================
# OP 1 — treatment_vda(): the special 30% regime (§4).
# ===========================================================================

def treatment_vda(ledger: pd.DataFrame, a: Assumptions | None = None) -> dict:
    a = a or Assumptions()
    _validate(ledger)

    # The VDA "buckets" whose gains are charged: realized P&L closes, optionally
    # the stablecoin legs, and (if funding is a separate taxable event) positive
    # funding. Fees, funding paid, gas — none deduct (fee_deductible_vda=False).
    vda_rows = [_rows(ledger, CAT_REALIZED)]
    if a.include_stablecoin_legs:
        vda_rows.append(_rows(ledger, CAT_STABLECOIN_LEG))
    if a.funding_treatment == "separate_taxable_event":
        vda_rows.append(_rows(ledger, CAT_FUNDING))
    vda = pd.concat(vda_rows) if vda_rows else ledger.iloc[0:0]
    amounts = vda["amount_inr"].to_numpy(dtype="float64") if len(vda) else np.array([])

    # No set-off: losing transfers contribute ZERO (gross-of-losses base).
    gross_base = float(amounts[amounts > 0].sum()) if len(amounts) else 0.0
    # Intra-VDA netting (the other reading): net, but never below zero.
    net_base = max(0.0, float(amounts.sum())) if len(amounts) else 0.0
    gap = gross_base - net_base  # the single most shocking number for a churner

    taxable_base = gross_base if a.vda_loss_scope == "no_set_off_at_all" else net_base
    tax = _finalize_flat_tax(taxable_base, a.vda_flat_rate, a)

    turnover, audit = _turnover_and_audit(ledger, a)
    tds_pnl = _tds_on_pnl_inr(ledger, a)
    tds_legs, _ = _tds_on_legs(ledger, a)

    line_items = ledger.copy()
    line_items["vda_charged_inr"] = np.where(
        line_items["event_id"].isin(vda["event_id"])
        & (line_items["amount_inr"] > 0),
        line_items["amount_inr"], 0.0,
    )

    return {
        "treatment": "vda",
        "premise": "live",  # a real contender (conservative bound), never abstracted
        "rate_basis": "flat_30",
        "taxable_base_inr": taxable_base,
        **tax,
        "vda_gross_base_inr": gross_base,
        "vda_net_base_inr": net_base,
        "vda_gross_vs_net_gap_inr": gap,
        "loss_treatment": "none",
        "current_year_setoff_inr": 0.0,
        "carry_forward_inr": 0.0,
        "carry_forward_years": None,
        "tds_expected_inr": tds_pnl + tds_legs,
        "tds_expected_on_pnl_inr": tds_pnl,
        "tds_expected_on_legs_inr": tds_legs,
        "tds_actually_deducted_inr": 0.0,
        "audit_flag": audit,
        "turnover_inr": turnover,
        "assumptions_used": asdict(a),
        "section_refs": SECTION_REFS["vda"],
        "caveats": [
            "Rests on the weakest premise of the four: that a cash-settled "
            "derivative IS a transfer of the underlying VDA, when no underlying "
            "coin is acquired or disposed.",
            "The domestic market actively REJECTS this for the P&L: CoinDCX taxes "
            "even its USDT-settled Global Futures at slab, no TDS on P&L. Present "
            "OP 1 as the conservative / worst-case bound, not the market norm — "
            "not off the table (no CBDT circular; s.194S references perpetual "
            "contracts), but the minority position on the trading P&L.",
            "The no-set-off / no-carry rule itself is confirmed for genuine VDA "
            "transfers (spot): losses cannot offset any income or carry forward. "
            "The open question is only whether the perp P&L is such a transfer.",
            "The stablecoin conversion legs (INR->USDC, USDC->INR) are the "
            "clearest VDA transfers of all — do not omit them.",
            "Surcharge on VDA income has had special treatment historically — "
            "VERIFY the current cap rather than assuming the slab surcharge.",
            "Gross-of-losses vs intra-VDA-net gap reported: "
            f"Rs {gap:,.2f}. Both readings shown; neither is asserted correct.",
        ],
        "line_items": line_items,
    }


# ===========================================================================
# Slab base — shared by OP 2 / OP 3 / OP 4 (they build the SAME base; they
# diverge on loss set-off, carry-forward, TDS surface and reporting duties).
# ===========================================================================

def _slab_base(ledger: pd.DataFrame, a: Assumptions) -> dict:
    net_pnl = _sum_inr(ledger, CAT_REALIZED)
    funding = _funding_component(ledger, a)
    fees = _fee_component(ledger, a.fee_deductible_slab)
    return {"net_pnl": net_pnl, "funding": funding, "fees": fees,
            "taxable": net_pnl + funding - fees}


def _slab_common(ledger: pd.DataFrame, a: Assumptions) -> dict:
    """Fields every slab treatment shares."""
    turnover, audit = _turnover_and_audit(ledger, a)
    tds_legs, _ = _tds_on_legs(ledger, a)
    return {
        "rate_basis": "slab",
        "turnover_inr": turnover,
        "audit_flag": audit,
        # Slab readings: no TDS on the P&L; TDS rides the stablecoin legs only.
        "tds_expected_inr": tds_legs,
        "tds_expected_on_pnl_inr": 0.0,
        "tds_expected_on_legs_inr": tds_legs,
        "tds_actually_deducted_inr": 0.0,
        "assumptions_used": asdict(a),
    }


# ===========================================================================
# OP 2 — treatment_futures_inr(): INR-margined / VDA-abstracted, slab (§5).
# ===========================================================================

def treatment_futures_inr(ledger: pd.DataFrame,
                          a: Assumptions | None = None) -> dict:
    a = a or Assumptions()
    _validate(ledger)
    b = _slab_base(ledger, a)
    tax = _finalize_slab_tax(b["taxable"], a)

    # COUNTERFACTUAL for any real HL address: HL is USDC-settled, a token moves.
    counterfactual = _has_real_stablecoin_legs(ledger)
    premise = "counterfactual" if counterfactual else "live"

    # OP 2's premise abstracts the VDA away entirely -> even the leg TDS is 0.
    common = _slab_common(ledger, a)
    common["tds_expected_on_legs_inr"] = 0.0
    common["tds_expected_inr"] = 0.0

    # Loss flavour: OP 2 defaults to the taxpayer-favourable non-speculative
    # netting (§5). A negative base is a real business loss.
    setoff = 0.0
    carry = 0.0
    if b["taxable"] < 0:
        carry = b["taxable"]

    return {
        "treatment": "futures_inr",
        "premise": premise,
        **{k: common[k] for k in ("rate_basis", "turnover_inr", "audit_flag",
                                  "tds_expected_inr", "tds_expected_on_pnl_inr",
                                  "tds_expected_on_legs_inr",
                                  "tds_actually_deducted_inr", "assumptions_used")},
        "taxable_base_inr": b["taxable"],
        **tax,
        "loss_treatment": "normal_business",
        "current_year_setoff_inr": setoff,
        "carry_forward_inr": carry,
        "carry_forward_years": a.business_carry_forward_years,
        "section_refs": SECTION_REFS["futures_inr"],
        "caveats": [
            ("COUNTERFACTUAL for a real Hyperliquid address: HL is USDC-settled, "
             "a token DOES move. Valid only under the custodial-abstraction "
             "premise that the stablecoin leg can be disregarded."
             if counterfactual else
             "The fully INR-margined, zero-VDA-anywhere benchmark."),
            "USDT-settled perps taxed at slab are captured by OP 3 / OP 4, not "
            "OP 2 — what makes OP 2 distinct is that NO VDA touches the flow, so "
            "even the stablecoin-conversion leg (which OP 3/OP 4 still carry) "
            "disappears.",
            "No CBDT circular blesses the 'derivatives aren't VDAs' reading; an "
            "exchange help page is an industry position, not law and not a CBDT "
            "position.",
        ],
        "line_items": _slab_line_items(ledger, a, a.fee_deductible_slab),
    }


# ===========================================================================
# OP 3 — treatment_speculative(): slab, losses ring-fenced (§6).
# ===========================================================================

def treatment_speculative(ledger: pd.DataFrame,
                          a: Assumptions | None = None) -> dict:
    a = a or Assumptions()
    _validate(ledger)
    b = _slab_base(ledger, a)
    tax = _finalize_slab_tax(b["taxable"], a)
    common = _slab_common(ledger, a)

    # The jail: speculative losses set off ONLY against future speculative
    # gains — never against salary, capital gains, other business, or other
    # sources. So current-year inter-head set-off is ZERO; the loss carries.
    setoff = 0.0
    carry = b["taxable"] if b["taxable"] < 0 else 0.0

    return {
        "treatment": "speculative",
        "premise": "live",
        **{k: common[k] for k in ("rate_basis", "turnover_inr", "audit_flag",
                                  "tds_expected_inr", "tds_expected_on_pnl_inr",
                                  "tds_expected_on_legs_inr",
                                  "tds_actually_deducted_inr", "assumptions_used")},
        "taxable_base_inr": b["taxable"],
        **tax,
        "loss_treatment": "ring_fenced_speculative",
        "current_year_setoff_inr": setoff,
        "carry_forward_inr": carry,
        "carry_forward_years": a.speculative_carry_forward_years,
        "section_refs": SECTION_REFS["speculative"],
        "caveats": [
            "A cash-settled perp is settled otherwise than by actual delivery = "
            "a speculative transaction; the recognised-(Indian)-exchange "
            "carve-out does NOT reach a foreign DEX.",
            "It is the LOSSES that are ring-fenced (only against speculative "
            "gains), NOT the rate: speculative income is still taxed at slab, "
            "not a penalty rate.",
            "Arguably the default slab reading for a foreign-DEX cash-settled "
            "perp once OP 1 and OP 2 are set aside. Whether equity perps sort "
            "differently from crypto perps is unresolved (asset_class tagged).",
        ],
        "line_items": _slab_line_items(ledger, a, a.fee_deductible_slab),
    }


# ===========================================================================
# OP 4 — treatment_non_speculative(): slab, normal loss set-off (§7).
# ===========================================================================

def treatment_non_speculative(ledger: pd.DataFrame,
                              a: Assumptions | None = None) -> dict:
    a = a or Assumptions()
    _validate(ledger)
    b = _slab_base(ledger, a)
    tax = _finalize_slab_tax(b["taxable"], a)
    common = _slab_common(ledger, a)

    # Normal business loss: current-year set-off against ANY head EXCEPT salary
    # (s.71/s.109). We demonstrate it against the user-supplied non-salary
    # other-head income; salary is protected and never touched. Residual carries
    # forward up to 8 AYs against business income (s.72/s.112).
    setoff = 0.0
    carry = 0.0
    other_head_after = a.other_head_income_inr
    if b["taxable"] < 0:
        loss = -b["taxable"]
        setoff = min(loss, a.other_head_income_inr)  # salary excluded by design
        other_head_after = a.other_head_income_inr - setoff
        carry = -(loss - setoff)  # negative residual, if any

    return {
        "treatment": "non_speculative",
        "premise": "live",
        **{k: common[k] for k in ("rate_basis", "turnover_inr", "audit_flag",
                                  "tds_expected_inr", "tds_expected_on_pnl_inr",
                                  "tds_expected_on_legs_inr",
                                  "tds_actually_deducted_inr", "assumptions_used")},
        "taxable_base_inr": b["taxable"],
        **tax,
        "loss_treatment": "normal_business",
        "current_year_setoff_inr": setoff,
        "salary_income_inr": a.salary_income_inr,          # asserted untouched
        "salary_income_after_setoff_inr": a.salary_income_inr,
        "other_head_income_inr": a.other_head_income_inr,
        "other_head_income_after_setoff_inr": other_head_after,
        "carry_forward_inr": carry,
        "carry_forward_years": a.business_carry_forward_years,
        "section_refs": SECTION_REFS["non_speculative"],
        "caveats": [
            "Requires defending 'business' (vs investment/hobby) on the facts — "
            "regularity, volume, infrastructure. The tool counts trips and "
            "volume to SUPPORT the argument but does not assert the conclusion.",
            "Current-year set-off reaches other heads EXCEPT salary (encoded: "
            "salary is untouched); residual carries forward against business "
            "income only.",
            "Same number as OP 2 in a net-profit year but a different risk "
            "surface — OP 4 may coexist with residual 194S / VDA-reporting risk "
            "that OP 2's INR-margined premise switches off.",
            "Under the VDA reading these losses are dead; under this reading they "
            "are the taxpayer's most valuable asset. The OP1-vs-OP4 delta on a "
            "loss-heavy address is the headline number of the whole report.",
        ],
        "line_items": _slab_line_items(ledger, a, a.fee_deductible_slab),
    }


# ===========================================================================
# Cross-cutting sections (computed here, rendered by present/) — §9/§10.
# ===========================================================================

def cross_cutting(ledger: pd.DataFrame, a: Assumptions | None = None) -> dict:
    a = a or Assumptions()
    _validate(ledger)
    has_legs = _has_real_stablecoin_legs(ledger)
    onramp_supplied = config.ON_RAMP_USDC_COST_INR is not None
    non_hl_classes = sorted(
        set(ledger["asset_class"].dropna().unique())
        - {"crypto-perp", "equity-perp", "stablecoin_leg", "other"}
    )
    return {
        "limitations_top": [
            "The law is UNSETTLED. There is no CBDT circular resolving whether a "
            "USDC-settled perp on a foreign DEX is a VDA transfer, speculative "
            "business, or ordinary business income.",
            "This tool computes ARITHMETIC under stated assumptions only. It is "
            "NOT tax advice and declares no treatment correct. Get a practising "
            "CA to co-sign before filing.",
            "Every section number here is NEEDS-VERIFICATION against the bare "
            "Act (1961 and 2025) — model memory is not a source.",
        ],
        "onramp_boundary": {
            "onramp_cost_supplied": onramp_supplied,
            "note": (
                "Without the INR on-ramp cost of the USDC (config.ON_RAMP_USDC_"
                "COST_INR), the VDA-reading numbers cover the ON-HYPERLIQUID LEG "
                "ONLY — NOT your full VDA tax liability. Supply your USDC "
                "acquisition cost from your Indian-exchange statement to complete "
                "the stablecoin-leg gain/loss (USDINR moves, so it is rarely zero)."
            ),
        },
        "ais_asymmetry_note": (
            "The department may see the Indian-exchange on-ramp (INR->USDC) and "
            "nothing after withdrawal to self-custody — a visible entry with no "
            "visible exit, the classic notice trigger (s.285BAA/2025 s.509; AIS "
            "2025 s.510). This ledger is the missing half."
        ),
        "capital_gains_excluded_note": (
            "Capital-gains treatment (1961 s.45 / 2025 s.67) is deliberately NOT "
            "shipped: it suits investors holding assets, not a daily-churn "
            "derivative trader. Business-vs-investment tests (regularity, volume, "
            "systematic intent) point away from it here."
        ),
        "disclosure_checklist": [
            "Schedule FA applicability (foreign-asset disclosure / Black Money Act)",
            "Schedule VDA granularity: per-fill vs per-episode (offer both CSVs)",
            "AIS reconciliation against the Indian-exchange ramp",
            "Ramp-CEX TDS certificate: FY 2025-26 available; from FY 2026-27 "
            "issued quarterly (Q1->30 Sep, Q2->31 Dec, Q3->31 Mar, Q4->30 Jun) — "
            "a filer may not yet have the latest quarter's certificate.",
        ],
        "has_stablecoin_legs": has_legs,
        "non_hyperliquid_asset_classes_detected": non_hl_classes,
        "assumptions_box": asdict(a),
    }


# ===========================================================================
# interpret() — the four treatments + TDS + cross-cutting, side by side.
# No key named best / recommended / correct (§0.1, §11).
# ===========================================================================

def interpret(ledger: pd.DataFrame, a: Assumptions | None = None) -> dict:
    """Compute all four treatments plus TDS and cross-cutting over one INR
    ledger. Ordered by section number, no winner declared."""
    a = a or Assumptions()
    _validate(ledger)
    return {
        "vda": treatment_vda(ledger, a),                       # OP 1
        "futures_inr": treatment_futures_inr(ledger, a),       # OP 2
        "speculative": treatment_speculative(ledger, a),       # OP 3
        "non_speculative": treatment_non_speculative(ledger, a),  # OP 4
        "tds": tds_summary(ledger, a),
        "cross_cutting": cross_cutting(ledger, a),
    }
