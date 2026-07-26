"""
tests/test_webapp_pipeline.py — Phase 8A gate: the JSON payload contract.

Offline and deterministic. Reuses the fixed-rate fake FX converter pattern from
tests/test_present.py, so every number below is hand-checkable paper arithmetic
and nothing here touches the network, the cache, or the live rate series.

What is gated:
  * json.dumps(payload, allow_nan=False) — no NaN/NaT/pd.NA/numpy leaks.
  * the key set the dashboard consumes, including the four treatments in
    section-number order.
  * missing numbers serialize to null, NEVER to 0.
  * the payload's tax figures are byte-identical to interpret()'s own (the
    serializer computes nothing).
  * config.ON_RAMP_USDC_COST_INR is restored after every call.
"""

from __future__ import annotations

import json
from datetime import date

import numpy as np
import pandas as pd
import pytest

import config
from interpret.assemble import build_inr_ledger
from interpret.treatments import Assumptions, interpret
from webapp.pipeline import (
    TREATMENT_ORDER,
    build_payload,
    normalize_as_of,
    _to_jsonable,
)

FX = 90.0
ADDR = "0x" + "ab" * 20

_CLOSING_COLUMNS = [
    "episode", "asset", "asset_class", "direction", "open_ts", "close_ts",
    "size_closed", "entry_px", "exit_px", "realized_usd", "close_fee_usd",
    "is_liquidation", "cost_basis_incomplete", "close_tid",
]


class _FakeFXResult:
    rate = FX
    rate_date = date(2026, 4, 17)
    source = "FAKE"
    stale_grace = False


class _FakeConverter:
    convention = "test_fixed_rate"

    def rate_for_event(self, ts):
        return _FakeFXResult()


def _ms(y, mo, d):
    return int(pd.Timestamp(y, mo, d, tz="UTC").value // 1_000_000)


def _closing_events(with_incomplete: bool = False) -> pd.DataFrame:
    """One winning close (+100 USD -> +9000 INR), one losing (-40 -> -3600),
    each with a $2 fee. Optionally a third close whose entry was never seen:
    realized NaN, cost_basis_incomplete True — the null-not-zero case."""
    rows = [
        (1, "BTC", "crypto-perp", "long", _ms(2026, 4, 17), _ms(2026, 5, 1),
         1.0, 100.0, 200.0, 100.0, 2.0, False, False, 111),
        (2, "BTC", "crypto-perp", "long", _ms(2026, 5, 2), _ms(2026, 5, 3),
         1.0, 200.0, 160.0, -40.0, 2.0, False, False, 222),
    ]
    if with_incomplete:
        rows.append(
            (3, "ETH", "crypto-perp", "short", _ms(2026, 5, 4), _ms(2026, 5, 5),
             2.0, float("nan"), 50.0, float("nan"), 1.0, True, True, 333))
    df = pd.DataFrame(rows, columns=_CLOSING_COLUMNS)
    for c in ("episode", "open_ts", "close_ts", "close_tid"):
        df[c] = df[c].astype("int64")
    return df


def _funding() -> pd.DataFrame:
    return pd.DataFrame({
        "ts": pd.Series([_ms(2026, 4, 20)], dtype="int64"),
        "asset": ["BTC"], "asset_class": ["crypto-perp"],
        "usdc": [5.0], "funding_rate": [0.0001], "szi": [1.0],
    })


def _cashflows() -> pd.DataFrame:
    return pd.DataFrame({
        "ts": pd.to_datetime([_ms(2026, 4, 16)], unit="ms", utc=True),
        "type": ["deposit"], "in_scope": [True], "is_usdc_flow": [True],
        "usdc": [1000.0], "fee": [0.0], "token": [None], "amount": [np.nan],
        "usdc_value": [np.nan], "hash": ["0xabc"], "time_ms": [_ms(2026, 4, 16)],
    })


def _ledger(with_incomplete: bool = False) -> pd.DataFrame:
    return build_inr_ledger(_closing_events(with_incomplete), _funding(),
                            _cashflows(), converter=_FakeConverter())


@pytest.fixture
def ledger_inr():
    return _ledger()


@pytest.fixture
def payload(ledger_inr):
    return build_payload(ADDR, ledger_inr, fx_convention="test_fixed_rate",
                         warnings=["spot fills excluded"])


# ---------------------------------------------------------------------------
# 1 — JSON safety.
# ---------------------------------------------------------------------------

def test_payload_is_strictly_json_serializable(payload):
    """allow_nan=False is the real gate: it raises on NaN/Infinity, which are
    invalid JSON and would arrive in the browser as unparseable tokens."""
    text = json.dumps(payload, allow_nan=False)
    assert "NaN" not in text
    assert "NaT" not in text
    assert "Infinity" not in text


def test_json_safety_holds_with_incomplete_cost_basis():
    p = build_payload(ADDR, _ledger(with_incomplete=True))
    json.dumps(p, allow_nan=False)          # must not raise
    assert p["schedule_vda_row_count"] == 3


def test_missing_numbers_become_null_never_zero():
    p = build_payload(ADDR, _ledger(with_incomplete=True))
    incomplete = [r for r in p["ledger_preview"]
                  if r["category"] == "realized_pnl" and r["cost_basis_incomplete"]]
    assert len(incomplete) == 1
    row = incomplete[0]
    assert row["amount_inr"] is None            # not 0.0
    assert row["amount_usd"] is None
    assert row["gross_consideration_inr"] is None


def test_to_jsonable_missing_and_scalar_conversions():
    assert _to_jsonable(np.float64("nan")) is None
    assert _to_jsonable(pd.NaT) is None
    assert _to_jsonable(pd.NA) is None
    assert _to_jsonable(float("inf")) is None
    assert _to_jsonable(np.int64(7)) == 7 and isinstance(_to_jsonable(np.int64(7)), int)
    assert _to_jsonable(np.bool_(True)) is True
    assert _to_jsonable(date(2026, 4, 17)) == "2026-04-17"
    assert _to_jsonable(pd.Timestamp("2026-04-17T05:06:07Z")) == "2026-04-17T05:06:07Z"
    assert _to_jsonable((1, (2.0, np.int64(3)))) == [1, [2.0, 3]]


# ---------------------------------------------------------------------------
# 2 — The contract the dashboard reads.
# ---------------------------------------------------------------------------

def test_top_level_keys(payload):
    for key in ("meta", "empty", "warnings", "treatment_order", "treatments",
                "tds", "cross_cutting", "ledger_row_count", "ledger_counts",
                "ledger_preview", "schedule_vda", "open_positions"):
        assert key in payload, key
    assert payload["empty"] is False
    assert payload["warnings"] == ["spot fills excluded"]


def test_meta_fields(payload):
    m = payload["meta"]
    assert m["address"] == ADDR
    assert m["event_range"] == "2026-04-16 to 2026-05-03"
    assert m["generated_utc"].endswith("UTC")
    assert m["fx_convention"] == "test_fixed_rate"
    assert m["cost_basis_convention"] == config.COST_BASIS_CONVENTION
    assert m["as_of"] is None and m["onramp_cost_inr"] is None


def test_four_treatments_in_section_number_order(payload):
    assert payload["treatment_order"] == list(TREATMENT_ORDER)
    assert list(payload["treatments"].keys()) == list(TREATMENT_ORDER)
    for key, t in payload["treatments"].items():
        assert t["treatment"] == key
        assert t["premise"] in ("live", "counterfactual")
        for field in ("taxable_base_inr", "total_tax_inr", "rate_basis",
                      "loss_treatment", "section_refs", "caveats",
                      "audit_flag", "turnover_inr", "tds_expected_inr"):
            assert field in t, (key, field)


def test_no_key_declares_a_winner(payload):
    """Neutrality is a product requirement, not a style choice."""
    text = json.dumps(payload).lower()
    for banned in ('"best', '"recommended', '"correct', '"preferred'):
        assert banned not in text


def test_vda_gap_and_counterfactual_premise(payload):
    vda = payload["treatments"]["vda"]
    # Funding is a separate taxable event by default, so the charged buckets are
    # the realized closes plus funding: gross = +9000 (win) + 450 (funding
    # received) = 9450; net = 9450 - 3600 (the loss) = 5850; gap = 3600 — the
    # loss that the no-set-off reading refuses to recognise.
    assert vda["vda_gross_base_inr"] == pytest.approx(9450.0)
    assert vda["vda_net_base_inr"] == pytest.approx(5850.0)
    assert vda["vda_gross_vs_net_gap_inr"] == pytest.approx(3600.0)
    assert vda["rate_basis"] == "flat_30"
    # OP 2's INR-margined premise is contradicted by a real USDC<->INR leg, and
    # only by that. The payload must carry the engine's own invariant through:
    # premise == counterfactual exactly when stablecoin legs were detected.
    expected = ("counterfactual"
                if payload["cross_cutting"]["has_stablecoin_legs"] else "live")
    assert payload["treatments"]["futures_inr"]["premise"] == expected


def test_ledger_preview_shape_and_formats(payload):
    assert payload["ledger_row_count"] == 6
    assert payload["ledger_counts"] == {
        "realized_pnl": 2, "fee": 2, "funding": 1, "deposit": 1}
    assert payload["ledger_preview_truncated"] is False
    row = payload["ledger_preview"][0]
    assert list(row.keys())[:6] == [
        "event_id", "ts_utc", "category", "asset", "asset_class", "episode_id"]
    assert row["ts_utc"].endswith("Z") and "T" in row["ts_utc"]
    assert row["fx_rate_date"] == "2026-04-17"
    assert row["fx_rate"] == pytest.approx(FX)
    assert isinstance(row["is_liquidation"], bool)


def test_ledger_preview_row_cap_is_reported(ledger_inr):
    p = build_payload(ADDR, ledger_inr, preview_rows=2)
    assert len(p["ledger_preview"]) == 2
    assert p["ledger_row_count"] == 6
    assert p["ledger_preview_truncated"] is True


def test_schedule_vda_alias_and_values(payload):
    rows = payload["schedule_vda"]
    assert payload["schedule_vda_row_count"] == 2
    for r in rows:
        # Dashboard key and ITR-genre key must agree, always.
        assert r["consideration_inr"] == r["consideration_received_inr"]
        assert r["date_of_acquisition"] and r["date_of_transfer"]
    incomes = sorted(r["income_inr"] for r in rows)
    assert incomes == pytest.approx([-3600.0, 9000.0])
    # Consideration = exit notional in INR: 160*1*90 and 200*1*90.
    assert sorted(r["consideration_inr"] for r in rows) == pytest.approx(
        [14400.0, 18000.0])


def test_line_items_omitted_by_default_and_flagged(ledger_inr, payload):
    for t in payload["treatments"].values():
        assert t["line_items"] == []
        assert t["line_items_omitted"] is True
        assert t["line_items_row_count"] == 6      # said, not hidden
    p = build_payload(ADDR, ledger_inr, include_line_items=True)
    spec = p["treatments"]["speculative"]
    assert len(spec["line_items"]) == 6
    assert spec["line_items_omitted"] is False
    assert "contribution_inr" in spec["line_items"][0]


def test_open_positions_serialize():
    open_pos = pd.DataFrame({
        "episode": pd.Series([4], dtype="int64"),
        "asset": ["SOL"], "asset_class": ["crypto-perp"], "direction": ["long"],
        "open_ts": pd.Series([_ms(2026, 5, 6)], dtype="int64"),
        "size": [3.0], "entry_px": [np.nan], "cost_basis_incomplete": [True],
    })
    p = build_payload(ADDR, _ledger(), open_positions=open_pos)
    assert p["open_position_count"] == 1
    assert p["open_positions"][0]["asset"] == "SOL"
    assert p["open_positions"][0]["entry_px"] is None


# ---------------------------------------------------------------------------
# 3 — The serializer computes nothing (no drift from interpret()).
# ---------------------------------------------------------------------------

def test_figures_match_interpret_exactly(ledger_inr, payload):
    direct = interpret(ledger_inr, Assumptions())
    for key in TREATMENT_ORDER:
        for field in ("taxable_base_inr", "tax_before_cess_inr", "surcharge_inr",
                      "cess_inr", "total_tax_inr", "turnover_inr",
                      "current_year_setoff_inr", "carry_forward_inr",
                      "tds_expected_inr"):
            assert payload["treatments"][key][field] == pytest.approx(
                direct[key][field]), (key, field)
    assert payload["tds"]["tds_rate"] == direct["tds"]["tds_rate"]
    assert (payload["tds"]["tds_expected_on_pnl_by_reading"]["vda"]
            == pytest.approx(direct["tds"]["tds_expected_on_pnl_by_reading"]["vda"]))


def test_cross_cutting_carries_limitations_and_assumptions(payload):
    cc = payload["cross_cutting"]
    assert len(cc["limitations_top"]) >= 3
    assert cc["onramp_boundary"]["onramp_cost_supplied"] is False
    assert cc["disclosure_checklist"]
    box = cc["assumptions_box"]
    assert box["vda_flat_rate"] == pytest.approx(config.VDA_FLAT_RATE)
    assert box["onramp_cost_inr"] is None
    # Tuple-of-tuple schedules must have become lists of lists.
    assert isinstance(box["slab_schedule"], list)
    assert isinstance(box["slab_schedule"][0], list)


# ---------------------------------------------------------------------------
# 4 — The mutated-global hazard and the Assumptions overrides.
# ---------------------------------------------------------------------------

def test_onramp_cost_is_applied_then_restored(ledger_inr):
    before = config.ON_RAMP_USDC_COST_INR
    p = build_payload(ADDR, ledger_inr, onramp_cost=250000.0)
    assert config.ON_RAMP_USDC_COST_INR is before      # restored, no leak
    assert p["cross_cutting"]["onramp_boundary"]["onramp_cost_supplied"] is True
    assert p["cross_cutting"]["assumptions_box"]["onramp_cost_inr"] == 250000.0
    assert p["meta"]["onramp_cost_inr"] == 250000.0

    q = build_payload(ADDR, ledger_inr)                # next request unaffected
    assert q["cross_cutting"]["onramp_boundary"]["onramp_cost_supplied"] is False


def test_other_head_income_override_reaches_op4(ledger_inr):
    p = build_payload(ADDR, ledger_inr, salary_income_inr=1200000.0,
                      other_head_income_inr=300000.0)
    op4 = p["treatments"]["non_speculative"]
    assert op4["salary_income_inr"] == pytest.approx(1200000.0)
    # Salary is protected by statute: a business loss may not touch it.
    assert op4["salary_income_after_setoff_inr"] == pytest.approx(1200000.0)
    assert op4["other_head_income_inr"] == pytest.approx(300000.0)
    box = p["cross_cutting"]["assumptions_box"]
    assert box["salary_income_inr"] == pytest.approx(1200000.0)
    assert box["other_head_income_inr"] == pytest.approx(300000.0)


def test_explicit_assumptions_object_is_honoured(ledger_inr):
    a = Assumptions(funding_treatment="ignore")
    p = build_payload(ADDR, ledger_inr, assumptions=a)
    assert p["cross_cutting"]["assumptions_box"]["funding_treatment"] == "ignore"


# ---------------------------------------------------------------------------
# 5 — The empty account: a state, not an error.
# ---------------------------------------------------------------------------

def test_empty_ledger_keeps_the_payload_shape():
    blank = build_inr_ledger(pd.DataFrame(columns=_CLOSING_COLUMNS),
                             pd.DataFrame(), pd.DataFrame(),
                             converter=_FakeConverter())
    p = build_payload(ADDR, blank, empty=True)
    json.dumps(p, allow_nan=False)
    assert p["empty"] is True
    assert p["meta"]["event_range"] == "no events"
    assert p["ledger_row_count"] == 0 and p["ledger_preview"] == []
    assert p["ledger_counts"] == {} and p["schedule_vda"] == []
    # Every panel the dashboard renders still has its keys, at zero.
    assert list(p["treatments"].keys()) == list(TREATMENT_ORDER)
    assert p["treatments"]["vda"]["total_tax_inr"] == pytest.approx(0.0)
    assert p["cross_cutting"]["limitations_top"]


# ---------------------------------------------------------------------------
# 6 — as-of window semantics match the CLI flag.
# ---------------------------------------------------------------------------

def test_normalize_as_of_matches_cli_day_boundary():
    from main import _parse_as_of
    assert normalize_as_of(None) is None
    assert normalize_as_of("") is None
    ts = normalize_as_of("2026-03-31")
    assert ts == _parse_as_of("2026-03-31")
    assert str(ts) == "2026-03-31 23:59:59+00:00"
    assert normalize_as_of(date(2026, 3, 31)) == ts


def test_as_of_is_echoed_in_meta(ledger_inr):
    p = build_payload(ADDR, ledger_inr, as_of=normalize_as_of("2026-05-02"),
                      window_counts={"fills": 4, "funding": 1, "ledger": 1})
    assert p["meta"]["as_of"] == "2026-05-02"
    assert p["meta"]["window_counts"] == {"fills": 4, "funding": 1, "ledger": 1}
