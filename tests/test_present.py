"""
tests/test_present.py — Phase 6 gate: the assembler contract and the three
artifacts. Uses a DETERMINISTIC fake FX converter (fixed rate) so the test is
pure arithmetic on paper, independent of the live rate series — the same
fixtures-first discipline the rest of the project follows.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from interpret.assemble import build_inr_ledger
from interpret.treatments import interpret
from present.report import build_report, schedule_vda_frame

FX = 90.0  # flat INR/USD for hand-checkable numbers.


class _FakeFXResult:
    def __init__(self):
        self.rate = FX
        self.rate_date = date(2026, 4, 17)
        self.source = "FAKE"
        self.stale_grace = False


class _FakeConverter:
    convention = "test_fixed_rate"

    def rate_for_event(self, ts):
        return _FakeFXResult()


def _ms(y, mo, d):
    return int(pd.Timestamp(y, mo, d, tz="UTC").value // 1_000_000)


def _closing_events():
    """Two closes: one winning long (+100 USD), one losing (-40 USD), each with
    a $2 fee and an exit notional. Hand-computed INR: win +9000, loss -3600."""
    cols = [
        "episode", "asset", "asset_class", "direction", "open_ts", "close_ts",
        "size_closed", "entry_px", "exit_px", "realized_usd", "close_fee_usd",
        "is_liquidation", "cost_basis_incomplete", "close_tid",
    ]
    rows = [
        (1, "BTC", "crypto-perp", "long", _ms(2026, 4, 17), _ms(2026, 5, 1),
         1.0, 100.0, 200.0, 100.0, 2.0, False, False, 111),
        (2, "BTC", "crypto-perp", "long", _ms(2026, 5, 2), _ms(2026, 5, 3),
         1.0, 200.0, 160.0, -40.0, 2.0, False, False, 222),
    ]
    df = pd.DataFrame(rows, columns=cols)
    for c in ("episode", "open_ts", "close_ts", "close_tid"):
        df[c] = df[c].astype("int64")
    return df


def _funding():
    return pd.DataFrame({
        "ts": pd.Series([_ms(2026, 4, 20)], dtype="int64"),
        "asset": ["BTC"], "asset_class": ["crypto-perp"],
        "usdc": [5.0], "funding_rate": [0.0001], "szi": [1.0],
    })


def _ledger():
    return pd.DataFrame({
        "ts": pd.to_datetime([_ms(2026, 4, 16)], unit="ms", utc=True),
        "type": ["deposit"], "in_scope": [True], "is_usdc_flow": [True],
        "usdc": [1000.0], "fee": [0.0], "token": [None], "amount": [np.nan],
        "usdc_value": [np.nan], "hash": ["0xabc"], "time_ms": [_ms(2026, 4, 16)],
    })


@pytest.fixture
def ledger_inr():
    return build_inr_ledger(_closing_events(), _funding(), _ledger(),
                            converter=_FakeConverter())


def test_assembler_shape_and_signs(ledger_inr):
    # 2 realized + 2 fee + 1 funding + 1 deposit = 6 rows.
    assert len(ledger_inr) == 6
    counts = ledger_inr["category"].value_counts().to_dict()
    assert counts == {"realized_pnl": 2, "fee": 2, "funding": 1, "deposit": 1}

    rp = ledger_inr[ledger_inr["category"] == "realized_pnl"].sort_values("amount_inr")
    # Loss -40*90 = -3600 ; win +100*90 = +9000.
    assert rp["amount_inr"].tolist() == pytest.approx([-3600.0, 9000.0])
    # Gross consideration = exit_px*size*FX : loss 160*90=14400, win 200*90=18000.
    assert sorted(rp["gross_consideration_inr"].tolist()) == pytest.approx(
        [14400.0, 18000.0])

    fee = ledger_inr[ledger_inr["category"] == "fee"]
    assert fee["amount_inr"].tolist() == pytest.approx([180.0, 180.0])  # +2*90 paid
    dep = ledger_inr[ledger_inr["category"] == "deposit"]
    assert dep["amount_inr"].iloc[0] == pytest.approx(90000.0)  # +1000*90


def test_vda_no_setoff_uses_gross(ledger_inr):
    out = interpret(ledger_inr)
    # VDA base = positive buckets only: win 9000 + positive funding 450 = 9450
    # (funding_treatment defaults to separate_taxable_event). The -3600 loss and
    # the -180 fees are NOT deductible under 115BBH, so they never reduce it.
    assert out["vda"]["taxable_base_inr"] == pytest.approx(9450.0)
    # Gross 9450 vs net max(0, 9000-3600+450)=5850 -> gap 3600 (the loss).
    assert out["vda"]["vda_gross_vs_net_gap_inr"] == pytest.approx(3600.0)
    # Slab readings net the loss: 9000 - 3600 + funding 450 - fees 360 = 5490.
    assert out["non_speculative"]["taxable_base_inr"] == pytest.approx(5490.0)


def test_schedule_vda_cost_ties_out(ledger_inr):
    sv = schedule_vda_frame(ledger_inr)
    assert len(sv) == 2
    # cost = consideration - income, exactly (ties to the ledger).
    for r in sv.itertuples():
        assert r.cost_of_acquisition_inr == pytest.approx(
            r.consideration_received_inr - r.income_inr)
    assert set(sv["date_of_acquisition"]) == {"2026-04-17", "2026-05-02"}


def test_build_report_writes_three_artifacts(tmp_path, ledger_inr):
    out = interpret(ledger_inr)
    meta = {"address": "0xtest", "event_range": "2026-04-16 to 2026-05-03",
            "generated_utc": "now", "fx_convention": "test_fixed_rate",
            "cost_basis_convention": "average_entry_within_episode"}
    paths = build_report(ledger_inr, out, tmp_path, meta)
    assert paths.ledger_csv.exists()
    assert paths.schedule_vda_csv.exists()
    assert paths.summary_html.exists()
    html = paths.summary_html.read_text(encoding="utf-8")
    # Neutrality + the four ops present; limitations at the top.
    assert "declares none of them correct" in html
    assert html.count("OP ") >= 4
    # Neutrality is asserted, not a winner: the disclaimer must be present.
    assert "not by preference" in html
    # A number that must trace to the ledger: the VDA taxable base (9450).
    assert "9,450" in html
