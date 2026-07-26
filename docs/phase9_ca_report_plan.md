# Phase 9 — CA Tax Report (FY-scoped CSV bundle)

**Status:** planned, not built.
**Depends on:** Phases 1–6 (complete). **Blocks nothing** — Phase 8 (frontend) can
proceed in parallel; this phase defines the API contract Phase 8 wires a button to.

---

## Context

The engine today produces three artifacts (`ledger.csv`, `schedule_vda.csv`,
`summary.html`) aimed at *arguing the tax question* — four treatments side by
side, section references, unsettled-law caveats. That is the right artifact for a
user deciding how to file.

It is the wrong artifact to hand a Chartered Accountant. A CA does not want our
reading of s.115BBH; they want **the numbers, structured, for one financial year**,
in the genre they already process from Indian exchanges. CoinDCX's Trade Report
(`docs/phase5data_dcx.md`) is that genre: one sheet per product, one row per
transaction, every column a fact (pair, side, quantity, price, gross, fees, net,
TDS) and **no tax computation anywhere** — CoinDCX explicitly refuses to compute
liability and tells the user to take the report to a CA.

This phase adds the equivalent for a Hyperliquid address: a **financial-year-scoped
ZIP of CSVs, containing structured data and neutral arithmetic totals only — no
tax rate applied, no treatment named, no liability asserted.** Because the
platform is perps-heavy, the trades sheet is modelled on CoinDCX's *Futures Orders*
sheet (realized P&L per transaction), not their Spot sheet.

**Hard constraint: the existing pipeline must be untouched in behaviour.** Every
change is additive. Running `python main.py <addr>` without the new flag must
produce byte-identical output to today.

### Design decisions (confirmed with the user)

| Question | Decision |
|---|---|
| Packaging | **ZIP of CSVs**, one CSV per "sheet" (stdlib `zipfile`, no new dependency) |
| Time scope | **Indian FY selector**, boundaries in IST (1 Apr 00:00:00 → 31 Mar 23:59:59.999 IST) |
| Trade granularity | **One row per closing episode** (matches the engine's cost-basis unit and CoinDCX Futures) |
| Tax numbers | **Structured data + neutral totals only.** No rate, no treatment, no liability |

---

## Layer placement

This is a **presentation-layer** artifact and obeys the Week-1 layer discipline:
*it computes nothing new.* Every number already exists in the INR ledger or the
reconstruction. The module only **joins, filters by FY, orders, labels, aggregates,
and writes.** A bug here can never change a tax number.

```
reconstruct.closing_events ─┐
reconstruct.open_positions ─┼─> present/ca_report.py ─> ca_report_<addr>_FY<yy-yy>.zip
interpret.assemble.ledger  ─┘        (join + FY filter + write)
```

The ledger is the **only** source of `fx_rate` / `amount_inr` — `ca_report.py`
must never import `fx` and never multiply USD by a rate. The join to
`closing_events` exists solely to recover the execution facts the ledger flattens
away (`entry_px`, `exit_px`, `size_closed`, `direction`, `open_ts`).

---

## New files

### 1. `present/ca_report.py` (the whole feature)

Public surface:

```python
FY_SHEETS = ["00_summary", "01_futures_trades", "02_funding", "03_fees",
             "04_cashflows", "05_open_positions", "06_assumptions_and_notes"]

def fy_bounds(fy: str) -> tuple[pd.Timestamp, pd.Timestamp]      # UTC, IST-derived
def fy_of(ts_utc) -> str                                          # "2025-26"
def available_fys(ledger: pd.DataFrame) -> list[str]              # for the FY dropdown

def ca_report_frames(closing_events, open_positions, ledger, meta,
                     fy: str | None = None,
                     warnings: list[str] = ()) -> dict[str, pd.DataFrame]
    """Pure. Returns {sheet_name: DataFrame}. No I/O. This is what the
       webapp calls for an on-screen preview or a JSON response."""

def write_ca_report_zip(frames: dict[str, pd.DataFrame], path: Path) -> Path
    """Writes one CSV per frame into a single ZIP. No computation."""

def build_ca_report(closing_events, open_positions, ledger, meta, out_dir,
                    fy: str | None = None, warnings=()) -> Path
    """frames -> zip. Returns the ZIP path."""
```

Splitting `ca_report_frames` (pure) from `write_ca_report_zip` (I/O) is what lets
Phase 8 serve a preview table and a download from the same code path.

### 2. `tests/test_ca_report.py`

See "Verification" below.

---

## Existing files touched (additive only)

### `config.py` — append a small block, change nothing existing

```python
# --- Phase 9: CA report -----------------------------------------------------
FY_TIMEZONE = DISPLAY_TIMEZONE          # "Asia/Kolkata" — FY boundaries are IST
FY_START_MONTH = 4                      # Indian FY: 1 Apr -> 31 Mar
CA_REPORT_ROUND_INR = True              # round INR columns for CA readability
```

### `main.py` — one argparse flag and one guarded call

```python
ap.add_argument("--ca-report", default=None, metavar="FY",
                help="also emit the CA CSV bundle for an Indian FY "
                     "(e.g. 2025-26), or 'all' for every FY present")
```

and, after `paths = build_report(...)`:

```python
if ca_report_fy:
    from present.ca_report import available_fys, build_ca_report
    fys = available_fys(ledger) if ca_report_fy == "all" else [ca_report_fy]
    for fy in fys:
        zp = build_ca_report(rec.closing_events, rec.open_positions, ledger,
                             meta, out_dir, fy=fy,
                             warnings=loaded.warnings + rec.warnings)
        print(f"    CA report:    {zp}")
```

Without the flag, `run()` behaves exactly as before. `run()`'s signature gains one
keyword-only parameter with a default of `None`, so existing callers and tests are
unaffected.

---

## Sheet specifications

Every sheet carries **both** `ts_utc` (ISO-8601) and `ts_ist` (`YYYY-MM-DD HH:MM:SS`)
— internal truth plus the Indian date a CA files against. Every row carries
`event_id` so any figure traces back to `ledger.csv` and from there to a raw fill.

### `01_futures_trades.csv` — one row per closing episode

Built by joining `closing_events` to the ledger's `realized_pnl` rows on
`event_id == f"rp:{close_tid}"`, and to the `fee` rows on `f"fee:{close_tid}"`.

| Column | Source |
|---|---|
| `sr_no` | 1-based, after FY filter + time sort |
| `asset`, `asset_class` | `closing_events` (`asset_class` = crypto-perp / equity-perp) |
| `direction` | `closing_events.direction` (long/short) |
| `open_ts_utc`, `open_ts_ist`, `open_fy` | `closing_events.open_ts` — flags a cross-FY episode |
| `close_ts_utc`, `close_ts_ist` | `closing_events.close_ts` — **the realization date; this is what the FY filter uses** |
| `size_closed`, `entry_px_usd`, `exit_px_usd` | `closing_events` |
| `gross_notional_usd`, `gross_notional_inr` | ledger `gross_consideration_usd/_inr` (exit notional) |
| `realized_pnl_usd`, `realized_pnl_inr` | ledger realized row |
| `fee_usd`, `fee_inr` | ledger fee row (**+ = paid, − = maker rebate**) |
| `net_pnl_inr` | `realized_pnl_inr − fee_inr` — plain arithmetic, no treatment choice |
| `fx_rate`, `fx_rate_date`, `fx_stale_grace` | ledger |
| `is_liquidation` | `closing_events` |
| `cost_basis_incomplete` | `closing_events` — when True, P&L columns are **blank, not 0** |
| `episode_id`, `event_id` | traceability |

### `02_funding.csv`
`sr_no, ts_utc, ts_ist, asset, asset_class, funding_usd, fx_rate, fx_rate_date,
funding_inr, funding_rate, position_size_szi, event_id`
Sign: **+ = received, − = paid.** Funding is a holding cost, never netted into
execution P&L (`errors_in_plan.md` #6) — hence its own sheet.
`funding_rate` / `position_size_szi` come from the funding ledger, joined on `event_id`.

### `03_fees.csv`
`sr_no, ts_utc, ts_ist, asset, asset_class, episode_id, fee_usd, fx_rate,
fx_rate_date, fee_inr, is_liquidation, event_id`
Every `category == "fee"` row. Duplicates the per-trade fee column in sheet 01 by
design: a CA wants the total fee line independently.

### `04_cashflows.csv`
`sr_no, ts_utc, ts_ist, type (deposit|withdrawal), asset (USDC), amount_usd,
fx_rate, fx_rate_date, amount_inr, tx_reference, event_id`
`tx_reference` = the on-chain hash embedded in `event_id` where present.
Header note row in sheet 06: these are **USDC movements on the venue, not the
INR↔USDC conversion** — the real on/off-ramp happened at an Indian exchange and is
invisible to this tool. They contribute **zero** to every P&L total.

### `05_open_positions.csv`
`asset, asset_class, direction, open_ts_utc, open_ts_ist, size, entry_px_usd,
cost_basis_incomplete`
From `rec.open_positions`, filtered to episodes open at the FY end. **No unrealized
P&L is computed** (would need a mark price the engine does not fetch, and it is
income only under a mark-to-market reading nobody has chosen). Sheet 06 states this
explicitly.

### `00_summary.csv` — key/value rows (`item, value, unit, note`)

Identity block: `address`, `financial_year`, `fy_start_ist`, `fy_end_ist`,
`generated_utc`, `event_range_in_fy`, `cost_basis_convention`, `fx_convention`.

Counts: `closing_trades`, `funding_events`, `fee_events`, `cashflow_events`,
`open_positions_at_fy_end`, `liquidation_closes`, `closes_with_incomplete_cost_basis`.

Neutral totals (INR, and USD alongside):

| Item | Definition |
|---|---|
| `gross_realized_profit_inr` | Σ of **positive** `realized_pnl_inr` |
| `gross_realized_loss_inr` | Σ of **negative** `realized_pnl_inr` (kept negative) |
| `net_realized_pnl_inr` | their sum (`np.nansum`, never `sum` — see below) |
| `total_fees_inr` | Σ `fee_inr` (positive = net cost after rebates) |
| `funding_received_inr` / `funding_paid_inr` / `net_funding_inr` | signed split |
| `net_pnl_after_fees_and_funding_inr` | `net_realized + net_funding − total_fees` — arithmetic only |
| `turnover_inr` | **reuse `interpret.treatments.turnover_inr(ledger)`** — ICAI abs-sum of per-episode realized. Do not reimplement |
| `total_deposits_inr` / `total_withdrawals_inr` | contribute 0 to P&L; shown for reconciliation |
| `tds_deducted_on_venue_inr` | **0.00**, with note: foreign DEX, no Indian intermediary; any TDS was withheld at the Indian-exchange ramp and is creditable |

Then three verbatim disclaimer rows (see Neutrality below).

### `06_assumptions_and_notes.csv` — key/value (`item, value`)
- Conventions: cost basis (`average_entry_within_episode`), fee attribution (full fee
  of a flip lands on the close leg), funding kept separate, sign conventions for
  every signed column.
- FX: `convention`, `source` (FBIL via Frankfurter), `provider`, publication-cutoff
  rule, staleness policy, and the count of rows flagged `fx_stale_grace`.
- Rounding: INR columns rounded to `config.REPORT_INR_DECIMALS`; USD and price
  columns kept at full precision.
- Data-completeness: every warning from `load` and `reconstruct`, verbatim
  (truncation at the ~10k-fill wall, unmapped HIP-3 dexes, spot fills excluded,
  `startPosition` desyncs). **Never launder a caveat.**
- Boundary notes: the on-ramp INR cost of USDC (`config.ON_RAMP_USDC_COST_INR`,
  supplied or not); non-Hyperliquid activity out of scope; open positions carry no
  unrealized figure.
- The neutrality statement.

---

## Rules the implementation must not break

1. **`cost_basis_incomplete` closes are blank, never zero.** `realized_usd` is
   `NaN` for a close whose entry predates the fill window. Export them as rows with
   empty P&L cells, exclude them from every total (`np.nansum`, mirroring
   `reconstruct.positions.total_realized_usd`), and report their **count** in sheet 00
   and their cause in sheet 06. A silent zero is a wrong tax number wearing a
   green checkmark.
2. **FY assignment is by realization date** = `close_ts`, converted to IST. An
   episode opened in FY 2024-25 and closed in FY 2025-26 belongs to FY 2025-26;
   `open_fy` on the row makes that visible rather than surprising.
3. **Filter after assembly, never before.** The FY filter is a mask over the
   already-built INR ledger. Filtering upstream would change what the FX layer is
   asked to price and could turn a coverage error into a silent omission.
4. **No new FX work.** Rates arrive via the join. `ca_report.py` imports neither
   `fx.fx` nor `default_converter`.
5. **Empty FY is a valid answer.** Emit the full ZIP with header-only CSVs and a
   summary stating zero activity in the window. Never raise, never skip the file.
6. **Neutrality (brief non-negotiable #2).** No tax rate, no treatment name, no
   liability figure, no "recommended" anything anywhere in the bundle. Sheet 00 and
   06 both carry:
   > This report contains transaction data and arithmetic totals only. No tax rate
   > has been applied and no tax treatment has been selected. The tax treatment of
   > perpetual-futures P&L on a foreign venue is unsettled in Indian law; determining
   > it is the responsibility of a qualified Chartered Accountant.

   The four-treatment analysis stays in `summary.html`, which is a separate document
   with a separate purpose.
7. **Naming:** `ca_report_<address>_FY<yyyy-yy>.zip` in the same `out_dir` as the
   existing artifacts, so the frontend can serve it from a known path.

---

## Phase 8 (frontend) contract — define now, wire later

So the button is a one-liner when the webapp is built:

- `GET /api/ca-report/fys?address=0x…` → `{"fys": ["2025-26", "2024-25"]}`
  (backed by `available_fys(ledger)`) — populates the FY dropdown.
- `GET /api/ca-report?address=0x…&fy=2025-26` → the ZIP as a
  `FileResponse`/`StreamingResponse` with
  `Content-Disposition: attachment; filename="ca_report_0x…_FY2025-26.zip"`.
- Errors reuse the Phase 8 mapping: `BadAddressError` → 400, `FxError` → 422,
  unknown FY → 404.

Both routes call the same `run`-equivalent pipeline the report endpoint already
needs, then `build_ca_report(...)`. **No pipeline logic lives in the route.**

Known hazard to carry into Phase 8: `config.ON_RAMP_USDC_COST_INR` is a mutated
module global and will race under a concurrent server. It does not affect this
bundle's numbers (the CA report applies no rate), but sheet 06 reads it, so the
value must be read once at frame-build time, not at write time.

---

## Verification

`tests/test_ca_report.py`, built on the existing `tests/fixtures.py` paper data and
the `_FakeConverter` pattern from `tests/test_present.py` (no network, no real FX):

1. **FY boundary, both sides.** An event at `2025-03-31 23:59:59 IST` lands in
   FY 2024-25; `2025-04-01 00:00:01 IST` lands in FY 2025-26. Same instant expressed
   in UTC must not flip the answer.
2. **Cross-FY episode.** Opened before 1 Apr, closed after → appears once, in the
   close-date FY, with the correct `open_fy`.
3. **Incomplete cost basis.** A `cost_basis_incomplete` close appears as a row with
   blank P&L, is absent from every total, and is counted in sheet 00.
4. **Totals tie out.** `gross_profit + gross_loss == net_realized`;
   `net_realized + net_funding − total_fees == net_pnl_after_fees_and_funding`;
   `turnover_inr` equals `interpret.treatments.turnover_inr` on the same filtered ledger.
5. **Join integrity.** Every FY-filtered closing event finds exactly one ledger
   realized row and at most one fee row; no row is dropped or duplicated.
6. **ZIP shape.** Exactly the seven expected members; each parses with
   `pd.read_csv`; every sheet has `event_id` where applicable.
7. **Empty FY.** An FY with no activity produces a valid ZIP with header-only CSVs
   and a "no activity" summary — no exception.
8. **Neutrality guard.** Assert the concatenated bundle text contains no `30%`, no
   `115BBH`, no treatment key (`vda`, `speculative`, `non_speculative`,
   `futures_inr`), and no `tax_payable`. This test is the enforcement mechanism for
   rule 6 — keep it.
9. **Regression.** `tests/test_present.py` and the full suite pass unchanged;
   `python main.py <cached-addr>` without `--ca-report` produces the same three
   files, byte-identical.

End-to-end smoke, against an already-cached address (zero API calls):

```
python -m pytest -q
python main.py 0x<cached-address> --ca-report 2025-26
python main.py 0x<cached-address> --ca-report all
```
Then unzip and read `00_summary.csv` and `01_futures_trades.csv` as a CA would.

---

## Implementation order

1. `fy_bounds` / `fy_of` / `available_fys` + their boundary tests (get IST right first).
2. The join + `01_futures_trades` frame.
3. Sheets 02–05.
4. Sheets 00 and 06 (aggregates and prose last — they depend on all the above).
5. `write_ca_report_zip` + `build_ca_report`.
6. `main.py` flag.
7. Full test suite + the regression check.
