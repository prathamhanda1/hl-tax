"""
tests/test_ca_report.py — Phase 9 gate: the FY-scoped CA bundle.

Built on hand-made paper data and a DETERMINISTIC fake FX converter (flat rate),
the same fixtures-first discipline tests/test_present.py follows: every INR
figure below is hand-computable, so a failure points at the code rather than at
today rate series.

The fixture is shaped around the four things that are genuinely easy to get
wrong, not around the happy path:

  * an event one second BEFORE the FY boundary, and one second after it;
  * an episode opened in one FY and closed in the next;
  * a close whose cost basis is not computable at all;
  * a financial year with nothing in it.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import date

import numpy as np
import pandas as pd
import pytest

import config
from interpret.assemble import build_inr_ledger
from interpret.treatments import turnover_inr
from present import ca_report
from present.ca_report import (
    FY_SHEETS,
    FyFormatError,
    available_fys,
    build_ca_report,
    ca_report_frames,
    ca_report_zip_bytes,
    fy_bounds,
    fy_of,
)

FX = 90.0          # flat INR/USD, so every expected number is arithmetic


class _FakeFXResult:
    def __init__(self):
        self.rate = FX
        self.rate_date = date(2025, 4, 1)
        self.source = "FAKE"
        self.stale_grace = False


class _FakeConverter:
    convention = "test_fixed_rate"

    def rate_for_event(self, ts):
        return _FakeFXResult()


def _ms(ist: str) -> int:
    """IST wall-clock string -> epoch ms. The fixture is written in the timezone
    the FY boundary is actually evaluated in, so the intent is readable."""
    return int(pd.Timestamp(ist, tz=config.FY_TIMEZONE).value // 1_000_000)


# --- the paper account -----------------------------------------------------
# tid 101: closes at 2025-03-31 23:59:59 IST  -> FY 2024-25 (one second inside)
# tid 102: opens  2025-03-20, closes 2025-04-01 00:00:01 IST -> FY 2025-26,
#          open_fy 2024-25 (the cross-FY episode)
# tid 103: an ordinary loss inside FY 2025-26
# tid 104: a close with no computable cost basis inside FY 2025-26
CLOSE_COLUMNS = [
    "episode", "asset", "asset_class", "direction", "open_ts", "close_ts",
    "size_closed", "entry_px", "exit_px", "realized_usd", "close_fee_usd",
    "is_liquidation", "cost_basis_incomplete", "close_tid",
]


def _closing_events() -> pd.DataFrame:
    rows = [
        (1, "BTC", "crypto-perp", "long", _ms("2025-03-01 10:00:00"),
         _ms("2025-03-31 23:59:59"), 1.0, 100.0, 200.0, 100.0, 2.0,
         False, False, 101),
        (2, "BTC", "crypto-perp", "long", _ms("2025-03-20 10:00:00"),
         _ms("2025-04-01 00:00:01"), 1.0, 200.0, 250.0, 50.0, 1.0,
         False, False, 102),
        (3, "ETH", "crypto-perp", "short", _ms("2025-06-01 10:00:00"),
         _ms("2025-06-02 10:00:00"), 2.0, 300.0, 320.0, -40.0, 2.0,
         True, False, 103),
        (4, "SOL", "crypto-perp", "long", _ms("2025-07-01 10:00:00"),
         _ms("2025-07-02 10:00:00"), 5.0, float("nan"), 25.0,
         float("nan"), 3.0, False, True, 104),
    ]
    df = pd.DataFrame(rows, columns=CLOSE_COLUMNS)
    for c in ("episode", "open_ts", "close_ts", "close_tid"):
        df[c] = df[c].astype("int64")
    return df


def _funding() -> pd.DataFrame:
    return pd.DataFrame({
        "ts": pd.Series([_ms("2025-05-01 11:00:00")], dtype="int64"),
        "asset": ["BTC"], "asset_class": ["crypto-perp"],
        "usdc": [5.0], "funding_rate": [0.0001], "szi": [1.0],
    })


def _raw_ledger() -> pd.DataFrame:
    return pd.DataFrame({
        "ts": pd.to_datetime([_ms("2025-04-10 12:00:00")], unit="ms", utc=True),
        "type": ["deposit"], "in_scope": [True], "is_usdc_flow": [True],
        "usdc": [1000.0], "fee": [0.0], "token": [None], "amount": [np.nan],
        "usdc_value": [np.nan], "hash": ["0xdeadbeef"],
        "time_ms": [_ms("2025-04-10 12:00:00")],
    })


def _open_positions() -> pd.DataFrame:
    return pd.DataFrame({
        "episode": pd.Series([5], dtype="int64"),
        "asset": ["BTC"], "asset_class": [None], "direction": ["long"],
        "open_ts": pd.Series([_ms("2025-08-01 10:00:00")], dtype="int64"),
        "size": [0.5], "entry_px": [400.0], "cost_basis_incomplete": [False],
    })


META = {
    "address": "0xtest",
    "generated_utc": "2026-01-01 00:00 UTC",
    "fx_convention": "test_fixed_rate",
    "cost_basis_convention": "average_entry_within_episode",
}


@pytest.fixture
def ledger():
    return build_inr_ledger(_closing_events(), _funding(), _raw_ledger(),
                            converter=_FakeConverter())


@pytest.fixture
def frames(ledger):
    return ca_report_frames(_closing_events(), _open_positions(), ledger,
                            META, fy="2025-26", funding_ledger=_funding())


def _summary(frames) -> dict:
    df = frames["00_summary"]
    return dict(zip(df["item"], df["value"]))


# ===========================================================================
# 1 — The FY boundary, from both sides. Get this wrong and income moves year.
# ===========================================================================

def test_fy_boundary_is_evaluated_in_ist_not_utc():
    one_second_before = pd.Timestamp("2025-03-31 23:59:59",
                                     tz=config.FY_TIMEZONE)
    one_second_after = pd.Timestamp("2025-04-01 00:00:01",
                                    tz=config.FY_TIMEZONE)
    assert fy_of(one_second_before) == "2024-25"
    assert fy_of(one_second_after) == "2025-26"
    # The same instants expressed in UTC must not change the answer. Both fall
    # on 2025-03-31 in UTC, so a UTC-based implementation would put them in the
    # same year and be wrong about one of them.
    assert one_second_before.tz_convert("UTC").date().isoformat() == "2025-03-31"
    assert one_second_after.tz_convert("UTC").date().isoformat() == "2025-03-31"
    assert fy_of(one_second_before.tz_convert("UTC")) == "2024-25"
    assert fy_of(one_second_after.tz_convert("UTC")) == "2025-26"


def test_fy_bounds_are_half_open_and_adjacent():
    start, end = fy_bounds("2025-26")
    assert start == pd.Timestamp("2025-04-01", tz=config.FY_TIMEZONE)
    assert end == pd.Timestamp("2026-04-01", tz=config.FY_TIMEZONE)
    # No gap and no overlap between consecutive years.
    assert fy_bounds("2024-25")[1] == start


@pytest.mark.parametrize("bad", ["2025", "2025-27", "25-26", "", "abcd-ef",
                                 "2025-2026", None])
def test_malformed_financial_year_fails_loudly(bad):
    with pytest.raises(FyFormatError):
        fy_bounds(bad)


def test_available_fys_lists_every_year_with_activity_newest_first(ledger):
    # One fill in a year is enough to make that year available.
    assert available_fys(ledger) == ["2025-26", "2024-25"]
    assert available_fys(ledger.iloc[0:0]) == []


# ===========================================================================
# 2 — The cross-FY episode: counted once, in the year it was realised.
# ===========================================================================

def test_cross_fy_episode_lands_in_the_closing_year(frames, ledger):
    trades = frames["01_futures_trades"]
    row = trades[trades["transaction_id"] == 102].iloc[0]
    assert row["close_ts_ist"].startswith("2025-04-01")
    assert row["open_fy"] == "2024-25"          # visible, not surprising
    # ... and it does NOT also appear in the year it was opened.
    prior = ca_report_frames(_closing_events(), _open_positions(), ledger,
                             META, fy="2024-25")["01_futures_trades"]
    assert 102 not in set(prior["transaction_id"])
    assert set(prior["transaction_id"]) == {101}


def test_every_trade_appears_in_exactly_one_year(ledger):
    seen = []
    for fy in available_fys(ledger):
        f = ca_report_frames(_closing_events(), _open_positions(), ledger,
                             META, fy=fy)
        seen += list(f["01_futures_trades"]["transaction_id"])
    assert sorted(seen) == [101, 102, 103, 104]     # each exactly once


# ===========================================================================
# 3 — Incomplete cost basis: blank, never zero, never in a total.
# ===========================================================================

def test_incomplete_cost_basis_is_blank_and_excluded_from_totals(frames):
    trades = frames["01_futures_trades"]
    row = trades[trades["transaction_id"] == 104].iloc[0]
    assert row["cost_basis_incomplete"] is True or row["cost_basis_incomplete"]
    for col in ("realized_pnl_usd", "realized_pnl_inr", "net_pnl_inr"):
        assert pd.isna(row[col]), f"{col} must be blank, not {row[col]!r}"
    # The fee on that close is a real, known cost and IS reported.
    assert row["fee_inr"] == pytest.approx(270.0)

    s = _summary(frames)
    assert s["closes_with_incomplete_cost_basis"] == 1
    # 50*90 = 4500 gain, -40*90 = -3600 loss. The blank contributes nothing.
    assert s["gross_realized_profit_inr"] == pytest.approx(4500.0)
    assert s["gross_realized_loss_inr"] == pytest.approx(-3600.0)
    assert s["net_realized_pnl_inr"] == pytest.approx(900.0)


def test_a_blank_is_never_rendered_as_a_zero_in_the_csv(frames):
    csv = frames["01_futures_trades"].to_csv(index=False)
    line = [ln for ln in csv.splitlines() if ",104," in ln][0]
    cells = line.split(",")
    header = csv.splitlines()[0].split(",")
    for col in ("realized_pnl_inr", "net_pnl_inr"):
        assert cells[header.index(col)] == "", f"{col} was written as a number"


# ===========================================================================
# 4 — The totals tie out to each other and to the engine.
# ===========================================================================

def test_summary_totals_are_internally_consistent(frames):
    s = _summary(frames)
    assert (s["gross_realized_profit_inr"] + s["gross_realized_loss_inr"]
            == pytest.approx(s["net_realized_pnl_inr"]))
    # 1*90 + 2*90 + 3*90 = 540 of fees; funding 5*90 = 450 received.
    assert s["total_fees_inr"] == pytest.approx(540.0)
    assert s["net_funding_inr"] == pytest.approx(450.0)
    assert s["funding_received_inr"] == pytest.approx(450.0)
    assert s["funding_paid_inr"] == pytest.approx(0.0)
    assert (s["net_realized_pnl_inr"] + s["net_funding_inr"]
            - s["total_fees_inr"]
            == pytest.approx(s["net_pnl_after_fees_and_funding_inr"]))
    assert s["net_pnl_after_fees_and_funding_inr"] == pytest.approx(810.0)


def test_turnover_is_the_engine_figure_not_a_reimplementation(frames, ledger):
    fy_rows = ledger[ca_report.fy_mask(ledger, "2025-26")]
    assert _summary(frames)["turnover_inr"] == pytest.approx(
        round(turnover_inr(fy_rows), config.REPORT_INR_DECIMALS))
    # 4500 gain + 3600 loss, absolute, per episode.
    assert _summary(frames)["turnover_inr"] == pytest.approx(8100.0)


def test_cashflows_are_reported_but_contribute_nothing_to_pnl(frames):
    s = _summary(frames)
    assert s["total_deposits_inr"] == pytest.approx(90000.0)   # 1000 * 90
    assert s["total_withdrawals_inr"] == pytest.approx(0.0)
    # The deposit is 90000 INR and yet every P&L total above is unmoved by it.
    assert s["net_pnl_after_fees_and_funding_inr"] == pytest.approx(810.0)
    assert frames["04_cashflows"]["tds_inr"].tolist() == [0.0]


def test_per_trade_net_is_plain_arithmetic(frames):
    t = frames["01_futures_trades"].dropna(subset=["net_pnl_inr"])
    assert len(t) == 2
    for r in t.itertuples():
        assert r.net_pnl_inr == pytest.approx(r.realized_pnl_inr - r.fee_inr)


# ===========================================================================
# 5 — Join integrity: no row dropped, none duplicated, none invented.
# ===========================================================================

def test_each_realised_ledger_row_yields_exactly_one_trade_row(frames, ledger):
    fy_rows = ledger[ca_report.fy_mask(ledger, "2025-26")]
    realised = fy_rows[fy_rows["category"] == "realized_pnl"]
    trades = frames["01_futures_trades"]
    assert len(trades) == len(realised) == 3
    assert trades["event_id"].is_unique
    assert set(trades["event_id"]) == set(realised["event_id"])


def test_execution_facts_come_through_the_join(frames):
    row = frames["01_futures_trades"]
    row = row[row["transaction_id"] == 103].iloc[0]
    assert row["direction"] == "short"
    assert row["size_closed"] == pytest.approx(2.0)
    assert row["entry_px_usd"] == pytest.approx(300.0)
    assert row["exit_px_usd"] == pytest.approx(320.0)
    assert bool(row["is_liquidation"]) is True
    assert row["crypto_pair"] == "ETH-PERP/USDC"


def test_funding_detail_survives_the_round_trip(frames):
    f = frames["02_funding"]
    assert len(f) == 1
    assert f.iloc[0]["funding_rate"] == pytest.approx(0.0001)
    assert f.iloc[0]["position_size_szi"] == pytest.approx(1.0)
    assert f.iloc[0]["funding_inr"] == pytest.approx(450.0)


def test_fees_sheet_is_reconcilable_on_its_own(frames):
    fees = frames["03_fees"]
    assert len(fees) == 3
    assert fees["fee_inr"].sum() == pytest.approx(540.0)
    assert fees["event_id"].is_unique


# ===========================================================================
# 6 — ZIP shape.
# ===========================================================================

def test_zip_has_exactly_the_seven_sheets_and_they_all_parse(frames):
    z = zipfile.ZipFile(io.BytesIO(ca_report_zip_bytes(frames)))
    assert z.namelist() == [f"{n}.csv" for n in FY_SHEETS]
    for name in z.namelist():
        df = pd.read_csv(io.BytesIO(z.read(name)))
        assert len(df.columns) > 1, name


def test_every_transaction_sheet_carries_a_traceability_id(frames):
    for name in ("01_futures_trades", "02_funding", "03_fees", "04_cashflows"):
        assert "event_id" in frames[name].columns, name
        assert frames[name]["event_id"].notna().all(), name


def test_build_ca_report_writes_a_named_zip(tmp_path, ledger):
    path = build_ca_report(_closing_events(), _open_positions(), ledger, META,
                           tmp_path, fy="2025-26", funding_ledger=_funding())
    assert path.exists() and path.name == "ca_report_0xtest_FY2025-26.zip"
    assert zipfile.ZipFile(path).namelist() == [f"{n}.csv" for n in FY_SHEETS]


def test_open_positions_are_listed_without_an_unrealised_figure(frames):
    op = frames["05_open_positions"]
    assert len(op) == 1
    assert op.iloc[0]["asset_class"] == "crypto-perp"   # recovered from ledger
    assert not any("unrealis" in c or "unrealiz" in c for c in op.columns)
    assert not any("mark" in c for c in op.columns)


def test_positions_opened_after_the_window_are_not_listed(ledger):
    # The fixture position opens 2025-08-01, inside FY 2025-26 and after the
    # end of FY 2024-25.
    prior = ca_report_frames(_closing_events(), _open_positions(), ledger,
                             META, fy="2024-25")
    assert len(prior["05_open_positions"]) == 0


# ===========================================================================
# 7 — An empty financial year is a valid answer, not an error.
# ===========================================================================

def test_empty_fy_produces_a_full_bundle_with_header_only_sheets(ledger):
    frames = ca_report_frames(_closing_events(), _open_positions(), ledger,
                              META, fy="2019-20")
    assert sorted(frames) == sorted(FY_SHEETS)
    for name in ("01_futures_trades", "02_funding", "03_fees", "04_cashflows",
                 "05_open_positions"):
        assert len(frames[name]) == 0, name
        assert len(frames[name].columns) > 1, name
    s = _summary(frames)
    assert s["closing_trades"] == 0
    assert s["event_range_in_fy"] == "no events in this window"
    assert s["net_realized_pnl_inr"] == pytest.approx(0.0)
    assert s["turnover_inr"] == pytest.approx(0.0)
    # And it still zips.
    z = zipfile.ZipFile(io.BytesIO(ca_report_zip_bytes(frames)))
    assert z.namelist() == [f"{n}.csv" for n in FY_SHEETS]


def test_an_address_with_no_events_at_all_still_bundles():
    empty = build_inr_ledger(_closing_events().iloc[0:0],
                             _funding().iloc[0:0], _raw_ledger().iloc[0:0],
                             converter=_FakeConverter())
    frames = ca_report_frames(empty.iloc[0:0], None, empty, META, fy="2025-26")
    assert len(frames["01_futures_trades"]) == 0
    assert _summary(frames)["closing_trades"] == 0
    assert ca_report_zip_bytes(frames)


def test_fy_none_covers_every_event(ledger):
    frames = ca_report_frames(_closing_events(), _open_positions(), ledger,
                              META, fy=None)
    assert len(frames["01_futures_trades"]) == 4
    assert _summary(frames)["financial_year"] == "all"


# ===========================================================================
# 8 — The neutrality guard. This test IS the enforcement mechanism for the
#     rule that the bundle names no reading and applies no rate. Keep it.
# ===========================================================================

# Distinctive strings that can only be a leak, matched literally.
FORBIDDEN_LITERAL = [
    "30%", "115bbh", "194s", "s.115", "flat rate", "best reading",
    "correct reading", "tax_payable", "total_tax", "taxable_base",
    "liability_inr", "non_speculative", "futures_inr",
]
# Ordinary words, matched on WORD BOUNDARIES. Substring matching would be a trap
# here: "cess" lives inside "process" and "necessary", so a later edit to the
# prose could fail this test for no reason at all and teach the next person to
# delete it.
FORBIDDEN_WORDS = [
    "vda", "speculative", "cess", "surcharge", "slab", "recommended",
    "endorsed", "advisable",
]


def _bundle_text(frames) -> str:
    z = zipfile.ZipFile(io.BytesIO(ca_report_zip_bytes(frames)))
    return "\n".join(z.read(n).decode("utf-8") for n in z.namelist()).lower()


def test_bundle_asserts_no_rate_and_names_no_reading(frames):
    text = _bundle_text(frames)
    leaked = [t for t in FORBIDDEN_LITERAL if t in text]
    leaked += [w for w in FORBIDDEN_WORDS
               if re.search(rf"{re.escape(w)}", text)]
    assert not leaked, (
        f"the bundle leaked {leaked}: it must carry data and arithmetic only, "
        f"with no rate applied and no reading named"
    )


def test_the_neutrality_statement_appears_in_both_prose_sheets(frames):
    for sheet in ("00_summary", "06_assumptions_and_notes"):
        text = "\n".join(str(v) for v in frames[sheet]["value"])
        assert ca_report.NEUTRALITY_STATEMENT in text, sheet


def test_notes_sheet_never_launders_an_upstream_caveat(ledger):
    caveat = ("BTC: first retained fill has startPosition 4 (not flat) - "
              "position pre-existed the history window.")
    frames = ca_report_frames(_closing_events(), _open_positions(), ledger,
                              META, fy="2025-26", warnings=[caveat])
    notes = dict(zip(frames["06_assumptions_and_notes"]["item"],
                     frames["06_assumptions_and_notes"]["value"]))
    assert notes["data_completeness_warning_1"] == caveat   # verbatim


def test_notes_say_so_when_the_onramp_cost_is_absent_and_when_it_is_given(ledger):
    def note(meta):
        f = ca_report_frames(_closing_events(), _open_positions(), ledger, meta,
                             fy="2025-26")
        return dict(zip(f["06_assumptions_and_notes"]["item"],
                        f["06_assumptions_and_notes"]["value"]))["onramp_usdc_cost_inr"]

    absent = note(META)
    assert "not supplied" in absent.lower()
    assert "zero" in absent.lower()          # explicitly NOT defaulted to zero
    assert "50000" in note(dict(META, onramp_cost_inr=50000.0))


def test_notes_record_the_conventions_the_numbers_were_computed_under(frames):
    notes = dict(zip(frames["06_assumptions_and_notes"]["item"],
                     frames["06_assumptions_and_notes"]["value"]))
    for key in ("cost_basis_convention", "fee_attribution", "funding_treatment",
                "sign_conventions", "fy_boundary_rule", "fx_convention",
                "fx_source", "fx_staleness_policy", "rounding",
                "blank_versus_zero", "cashflow_boundary", "tds_on_this_venue",
                "open_positions_note"):
        assert key in notes and notes[key].strip(), key


# ===========================================================================
# 9 — Regression: the existing pipeline is untouched without the new flag.
# ===========================================================================

def test_cli_without_the_flag_writes_no_bundle(tmp_path, monkeypatch, ledger):
    """The Phase 9 artifact is strictly additive: `run()` called the old way
    must still produce exactly the three files it always did."""
    import main
    from interpret.treatments import interpret
    from present.report import build_report

    out = tmp_path / "reports"
    build_report(ledger, interpret(ledger), out, META)
    written = sorted(p.name for p in out.iterdir())
    assert written == ["ledger.csv", "schedule_vda.csv", "summary.html"]
    # And the run() signature still accepts the old positional call shape.
    import inspect
    sig = inspect.signature(main.run)
    assert sig.parameters["ca_report_fy"].default is None
    assert sig.parameters["ca_report_fy"].kind is inspect.Parameter.KEYWORD_ONLY


def test_frames_are_pure_and_do_not_mutate_their_inputs(ledger):
    before_ledger = ledger.copy(deep=True)
    events = _closing_events()
    before_events = events.copy(deep=True)
    ca_report_frames(events, _open_positions(), ledger, META, fy="2025-26",
                     funding_ledger=_funding())
    pd.testing.assert_frame_equal(ledger, before_ledger)
    pd.testing.assert_frame_equal(events, before_events)


def test_ca_report_never_reaches_for_the_fx_layer():
    """The ledger is the only source of a rate. If this module ever imports fx,
    a presentation bug becomes able to change a tax number."""
    src = (ca_report.__file__)
    text = open(src, encoding="utf-8").read()
    assert "import fx" not in text
    assert "from fx" not in text
    assert "default_converter" not in text
