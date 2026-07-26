# Phase 8A — In-memory result helper: what changed, and what Phase B can rely on

**Status:** complete and verified.
**Plan reference:** `docs/phase8_plan_frontend.md` § "Phase A — In-memory result helper".
**Engine files modified:** none. Phase A is purely additive.

---

## Summary

Phase A adds the API's single dependency: one function that returns the engine's
entire result for a public address as a JSON-safe dict, computing nothing new.

Three new files:

| File | Lines | Purpose |
|---|---|---|
| `webapp/__init__.py` | 6 | package marker + the layer's mandate |
| `webapp/pipeline.py` | 462 | `build_result()` / `build_payload()` + JSON serialization |
| `tests/test_webapp_pipeline.py` | 22 tests | offline gate on the payload contract |

**Test results:** `tests/test_webapp_pipeline.py` → 22 passed. Full suite →
97 passed, 11 skipped, 1 failure (pre-existing and unrelated — see
[Known pre-existing failure](#known-pre-existing-failure)).

---

## 1. `webapp/__init__.py` (new)

```python
"""webapp — Phase 8: the HTTP/JSON shell around the engine.

Nothing in this package computes a tax number. `pipeline.py` runs the existing
pure pipeline in memory and serializes its output; `server.py` (Phase B) maps it
onto HTTP. Every figure originates in fetch/load/reconstruct/fx/interpret.
"""
```

---

## 2. `webapp/pipeline.py` (new, 462 lines)

```python
"""
webapp/pipeline.py — Phase 8A. The in-memory result helper: one function that
returns the engine's results for an address as a JSON-safe dict.

    from webapp.pipeline import build_result
    payload = build_result("0x…")            # dict -> json.dumps() clean

This mirrors `main.run()` step for step (fetch -> load -> reconstruct -> fx ->
assemble -> interpret) but RETURNS DATA instead of writing files. It is the
API's single dependency: `webapp/server.py` (Phase B) is a thin HTTP wrapper
over this module and contains no pipeline logic.

LAYER DISCIPLINE — this file COMPUTES NOTHING NEW. Like present/report.py it
only orders, labels, formats and serializes numbers that already exist upstream.
A bug here can never change a tax number. The two consequences:

  * Every treatment figure comes verbatim from `interpret.treatments.interpret`.
    No key is added, dropped, renamed or recomputed on the tax side.
  * Neutrality is inherited, not restated: the four treatments are emitted in
    section-number order (`TREATMENT_ORDER`), and no key here says best,
    correct, recommended or default.

WHAT THE CALLER MUST HANDLE (deliberately NOT caught here — failing loudly per
phase is doctrine #4, and the HTTP layer is where a failure becomes a status
code):

  * `fetch.fetch_user.BadAddressError`  -> Phase B maps to 400.
  * `fx.fx.FxError`                     -> Phase B maps to 422 with the
    "refresh the FX series / add --as-of" hint from main.py:100-108.

An address with no in-window activity is NOT an error: the payload comes back
with `empty: true` and the full (zeroed) structure, so the shape of the JSON is
invariant and the dashboard can render its empty state without special-casing
missing keys.

SERIALIZATION is the only real work. `interpret()` hands back pandas
DataFrames, numpy scalars, `Timestamp`/`NaT`, `pd.NA` and NaN — none of which
`json.dumps` accepts. `_to_jsonable()` converts them, and it maps every flavour
of missing (None / NaN / NaT / pd.NA / inf) to JSON `null`, NEVER to 0. A
missing number must render as an em dash downstream, not as a fabricated zero.

THREAD SAFETY. `config.ON_RAMP_USDC_COST_INR` is a mutated module global that
`interpret.cross_cutting` reads at call time — under a concurrent server two
requests could otherwise see each other's on-ramp cost (the hazard flagged in
docs/phase9_ca_report_plan.md). Every request therefore sets that global to its
own value, runs `interpret()`, and restores the previous value inside a lock.
The network fetch stays outside the lock, so requests still overlap where it
matters.
"""

from __future__ import annotations

import math
import threading
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import config
# Reused display/shape helpers from the presentation layer (Phase 6) so the API
# and the CSV artifacts cannot drift apart: same column order, same episode
# shaping. _LEDGER_CSV_COLUMNS is the audit-spine order used by ledger.csv.
from present.report import _LEDGER_CSV_COLUMNS, schedule_vda_frame

# Section-number order, never preference order (mirrors present/report.py).
TREATMENT_ORDER = ("vda", "futures_inr", "speculative", "non_speculative")

# Row cap for the table payloads. The full ledger can run to many thousands of
# rows; the browser only ever tabulates a preview, and every truncation is
# reported in the payload rather than hidden (no silent data loss).
DEFAULT_PREVIEW_ROWS = 1000

# Ledger columns shipped to the client, in audit-spine order.
LEDGER_PREVIEW_COLUMNS = list(_LEDGER_CSV_COLUMNS)

# Guards the config.ON_RAMP_USDC_COST_INR set -> interpret -> restore window.
_CONFIG_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# JSON-safety primitives.
# ---------------------------------------------------------------------------

def _is_missing(v) -> bool:
    """True for every flavour of missing: None, NaN, NaT, pd.NA.

    `pd.isna` raises or returns an array for containers; those are not missing,
    so the exception path answers False.
    """
    if v is None:
        return True
    try:
        return bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def _iso_utc(ts) -> str | None:
    """UTC instant -> '2025-01-14T08:22:10Z'. Naive input is assumed UTC (every
    timestamp in this engine is tz-aware UTC by the time it reaches here)."""
    if _is_missing(ts):
        return None
    t = pd.Timestamp(ts)
    t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_date(v) -> str | None:
    """date / Timestamp / string -> 'YYYY-MM-DD'."""
    if _is_missing(v):
        return None
    if isinstance(v, (pd.Timestamp, datetime)):
        return pd.Timestamp(v).strftime("%Y-%m-%d")
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


def _to_jsonable(obj):
    """Recursively convert engine output into json.dumps-safe Python.

    DataFrame -> list of row dicts; Series/ndarray/tuple/set -> list;
    numpy scalar -> int/float; Timestamp/datetime -> ISO-8601 Z; date ->
    'YYYY-MM-DD'; NaN/NaT/pd.NA/inf -> None. Unknown objects degrade to str()
    rather than exploding a request.
    """
    # str/bool first: bool is a subclass of int, and str is iterable.
    if obj is None or isinstance(obj, (str, bool)):
        return obj
    if isinstance(obj, np.bool_):
        return bool(obj)
    if _is_missing(obj):            # NaN, NaT, pd.NA
        return None
    if isinstance(obj, pd.DataFrame):
        return _frame_records(obj)
    if isinstance(obj, pd.Series):
        return [_to_jsonable(v) for v in obj.tolist()]
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset, np.ndarray)):
        return [_to_jsonable(v) for v in list(obj)]
    if isinstance(obj, (pd.Timestamp, datetime)):
        return _iso_utc(obj)
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (int, np.integer)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        f = float(obj)
        return None if (math.isnan(f) or math.isinf(f)) else f
    return str(obj)


def _frame_records(
    df: pd.DataFrame | None,
    *,
    columns: list[str] | None = None,
    limit: int | None = None,
) -> list[dict]:
    """DataFrame -> list of JSON-safe row dicts, optionally column-projected
    (in the given order) and row-capped."""
    if df is None or not len(df):
        return []
    view = df
    if columns:
        keep = [c for c in columns if c in view.columns]
        view = view[keep]
    if limit is not None and len(view) > limit:
        view = view.iloc[:limit]
    return [
        {str(k): _to_jsonable(v) for k, v in rec.items()}
        for rec in view.to_dict("records")
    ]


# ---------------------------------------------------------------------------
# as-of normalization (identical semantics to the CLI flag).
# ---------------------------------------------------------------------------

def normalize_as_of(as_of) -> pd.Timestamp | None:
    """Accept None / 'YYYY-MM-DD' / date / Timestamp and return the inclusive
    end of that UTC day — the exact convention `main._parse_as_of` uses, so the
    API and the CLI bound a window identically."""
    if as_of is None or as_of == "":
        return None
    if isinstance(as_of, pd.Timestamp):
        ts = as_of if as_of.tzinfo else as_of.tz_localize("UTC")
        return ts.tz_convert("UTC")
    if isinstance(as_of, (datetime, date)):
        as_of = pd.Timestamp(as_of).strftime("%Y-%m-%d")
    from main import _parse_as_of  # single source of the day-boundary rule
    return _parse_as_of(str(as_of))


# ---------------------------------------------------------------------------
# Payload assembly (network-free — the testable seam).
# ---------------------------------------------------------------------------

def build_payload(
    address: str,
    ledger: pd.DataFrame,
    *,
    warnings: list[str] | tuple = (),
    open_positions: pd.DataFrame | None = None,
    fx_convention: str | None = None,
    as_of: pd.Timestamp | None = None,
    onramp_cost: float | None = None,
    salary_income_inr: float | None = None,
    other_head_income_inr: float | None = None,
    assumptions=None,
    preview_rows: int | None = DEFAULT_PREVIEW_ROWS,
    include_line_items: bool = False,
    empty: bool | None = None,
    window_counts: dict | None = None,
    refresh: bool = False,
) -> dict:
    """Run `interpret()` over an already-assembled INR ledger and serialize the
    whole result. Pure: no network, no filesystem, no clock beyond the
    generated-at stamp. `build_result` is this function plus the fetch.

    `empty` defaults to "the ledger has no rows"; the caller passes it
    explicitly when the emptiness was decided upstream (no in-window fills and
    no in-window cashflows, as `main.run` decides it).
    """
    from interpret.treatments import Assumptions, interpret

    a = assumptions or Assumptions()
    overrides: dict = {}
    if salary_income_inr is not None:
        overrides["salary_income_inr"] = float(salary_income_inr)
    if other_head_income_inr is not None:
        overrides["other_head_income_inr"] = float(other_head_income_inr)
    if overrides:
        a = replace(a, **overrides)   # Assumptions is a frozen dataclass

    onramp = float(onramp_cost) if onramp_cost is not None else None

    # The one mutated global in the engine. Set -> interpret -> restore, locked.
    with _CONFIG_LOCK:
        previous = config.ON_RAMP_USDC_COST_INR
        config.ON_RAMP_USDC_COST_INR = onramp
        try:
            interpreted = interpret(ledger, a)
        finally:
            config.ON_RAMP_USDC_COST_INR = previous

    # --- meta (display only) ------------------------------------------------
    if len(ledger):
        lo, hi = ledger["ts_utc"].min(), ledger["ts_utc"].max()
        event_range = f"{lo:%Y-%m-%d} to {hi:%Y-%m-%d}"
    else:
        event_range = "no events"

    meta = {
        "address": address,
        "event_range": event_range,
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "fx_convention": fx_convention or config.FX_CONVENTION,
        "cost_basis_convention": config.COST_BASIS_CONVENTION,
        "as_of": _iso_date(as_of),
        "refresh": bool(refresh),
        "onramp_cost_inr": onramp,
    }
    if window_counts:
        meta["window_counts"] = {k: int(v) for k, v in window_counts.items()}

    # --- treatments ---------------------------------------------------------
    treatments: dict = {}
    for key in TREATMENT_ORDER:
        raw = dict(interpreted[key])          # shallow copy; engine dict untouched
        line_items = raw.pop("line_items", None)
        view = _to_jsonable(raw)
        n_items = int(len(line_items)) if line_items is not None else 0
        view["line_items_row_count"] = n_items
        if include_line_items:
            view["line_items"] = _frame_records(line_items, limit=preview_rows)
            view["line_items_omitted"] = False
            view["line_items_truncated"] = bool(
                preview_rows is not None and n_items > preview_rows
            )
        else:
            # Per-treatment line items are a 4x copy of the ledger and nothing
            # in the dashboard reads them. Omitted by default, and SAID so.
            view["line_items"] = []
            view["line_items_omitted"] = True
            view["line_items_truncated"] = False
        treatments[key] = view

    # --- cross-cutting ------------------------------------------------------
    cross_cutting = _to_jsonable(interpreted["cross_cutting"])
    box = cross_cutting.get("assumptions_box")
    if isinstance(box, dict):
        # Echo the manual-input slot beside the assumptions it belongs with, so
        # the output stays self-describing (README: every assumption is printed).
        box["onramp_cost_inr"] = onramp

    # --- tables -------------------------------------------------------------
    ledger_rows = int(len(ledger))
    ledger_preview = _frame_records(
        ledger, columns=LEDGER_PREVIEW_COLUMNS, limit=preview_rows)
    counts = (
        {str(k): int(v) for k, v in ledger["category"].value_counts().items()}
        if ledger_rows else {}
    )

    vda_frame = schedule_vda_frame(ledger)
    schedule_vda = _frame_records(vda_frame, limit=preview_rows)
    for row in schedule_vda:
        # Display alias for the dashboard's column key; the CSV-genre name
        # (consideration_received_inr, as ITR Schedule VDA words it) is kept.
        row["consideration_inr"] = row.get("consideration_received_inr")

    open_pos = _frame_records(open_positions, limit=preview_rows)

    return {
        "meta": meta,
        "empty": bool(ledger_rows == 0) if empty is None else bool(empty),
        "warnings": [str(w) for w in warnings],
        "treatment_order": list(TREATMENT_ORDER),
        "treatments": treatments,
        "tds": _to_jsonable(interpreted["tds"]),
        "cross_cutting": cross_cutting,
        "ledger_row_count": ledger_rows,
        "ledger_counts": counts,
        "ledger_preview": ledger_preview,
        "ledger_preview_row_count": len(ledger_preview),
        "ledger_preview_truncated": bool(len(ledger_preview) < ledger_rows),
        "schedule_vda": schedule_vda,
        "schedule_vda_row_count": int(len(vda_frame)),
        "schedule_vda_truncated": bool(len(schedule_vda) < len(vda_frame)),
        "open_positions": open_pos,
        "open_position_count": int(len(open_positions))
        if open_positions is not None else 0,
    }


# ---------------------------------------------------------------------------
# build_result — the function Phase B calls.
# ---------------------------------------------------------------------------

def build_result(
    address: str,
    *,
    refresh: bool = False,
    as_of=None,
    onramp_cost: float | None = None,
    salary_income_inr: float | None = None,
    other_head_income_inr: float | None = None,
    assumptions=None,
    preview_rows: int | None = DEFAULT_PREVIEW_ROWS,
    include_line_items: bool = False,
) -> dict:
    """The whole pipeline for one PUBLIC address, in memory, as a JSON-safe dict.

    Mirrors `main.run()` (main.py:50-133) exactly: normalize_address ->
    fetch_all_for_address -> load_raw -> _apply_cutoff -> reconstruct_positions
    / reconstruct_funding -> build_inr_ledger -> interpret. No tax logic lives
    here.

    READ-ONLY, ALWAYS. The only account input is a public address. There is no
    parameter here — and never will be — for a private key or API secret.

    Raises `BadAddressError` (bad input) and `FxError` (the engine refusing to
    price recent events rather than emit a partial tax number); Phase B turns
    those into 400 and 422.
    """
    from fetch.fetch_user import fetch_all_for_address, normalize_address
    from load.load import load_raw
    from reconstruct.funding import reconstruct_funding
    from reconstruct.positions import reconstruct_positions
    from fx.fx import default_converter
    from interpret.assemble import build_inr_ledger
    import main as _cli   # reuse the CLI's own cutoff filter, no reimplementation

    address = normalize_address(address)
    as_of_ts = normalize_as_of(as_of)

    # 1 — FETCH (the only network step; cache-first under data/raw/<address>/).
    raw = fetch_all_for_address(address, refresh=refresh)

    # 2 — LOAD, then bound the window exactly as the --as-of flag does.
    loaded = load_raw(raw, account=address)
    fills, funding_df, ledger_df = _cli._apply_cutoff(loaded, as_of_ts)
    warnings = list(loaded.warnings)
    window_counts = {
        "fills": len(fills), "funding": len(funding_df), "ledger": len(ledger_df),
    }
    # main.run's own emptiness test: no fills AND no cashflows in the window.
    empty = (not len(fills)) and (not len(ledger_df))

    # 3 — RECONSTRUCT.
    rec = reconstruct_positions(fills)
    funding_ledger = reconstruct_funding(funding_df)
    warnings += list(rec.warnings)

    # 4 + 5 — FX + ASSEMBLE (the single USD->INR multiplication, upstream).
    converter = default_converter()
    ledger = build_inr_ledger(
        rec.closing_events, funding_ledger, ledger_df, converter)

    # 6 + 7 — INTERPRET + serialize.
    return build_payload(
        address,
        ledger,
        warnings=warnings,
        open_positions=rec.open_positions,
        fx_convention=converter.convention,
        as_of=as_of_ts,
        onramp_cost=onramp_cost,
        salary_income_inr=salary_income_inr,
        other_head_income_inr=other_head_income_inr,
        assumptions=assumptions,
        preview_rows=preview_rows,
        include_line_items=include_line_items,
        empty=empty,
        window_counts=window_counts,
        refresh=refresh,
    )


# ---------------------------------------------------------------------------
# Manual check: python -m webapp.pipeline <address>
# ---------------------------------------------------------------------------

def _cli() -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(
        description="Print the JSON payload webapp/server.py will serve "
                    "(read-only; public address only).")
    ap.add_argument("address")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--as-of", default=None, metavar="YYYY-MM-DD")
    ap.add_argument("--onramp-cost", default=None, type=float, metavar="INR")
    ap.add_argument("--salary", default=None, type=float, metavar="INR")
    ap.add_argument("--other-income", default=None, type=float, metavar="INR")
    ap.add_argument("--line-items", action="store_true",
                    help="include per-treatment line items (large)")
    ap.add_argument("--full", action="store_true",
                    help="no row cap on the table payloads")
    args = ap.parse_args()

    payload = build_result(
        args.address,
        refresh=args.refresh,
        as_of=args.as_of,
        onramp_cost=args.onramp_cost,
        salary_income_inr=args.salary,
        other_head_income_inr=args.other_income,
        preview_rows=None if args.full else DEFAULT_PREVIEW_ROWS,
        include_line_items=args.line_items,
    )
    print(json.dumps(payload, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
```

---

## 3. `tests/test_webapp_pipeline.py` (new, 22 tests, all passing)

Offline and deterministic: reuses the fixed-rate `_FakeConverter` pattern from
`tests/test_present.py`, so it touches no network, no cache, and not the live FX
series. What it gates:

- `json.dumps(payload, allow_nan=False)` succeeds — no NaN / NaT / `pd.NA` /
  numpy leaks, including on a ledger containing a `cost_basis_incomplete` close.
- **Missing numbers serialize to `null`, never `0`** — asserted directly on the
  incomplete-close row (`amount_inr`, `amount_usd`, `gross_consideration_inr`).
- `_to_jsonable` unit cases: NaN, NaT, `pd.NA`, inf, `np.int64`, `np.bool_`,
  `date`, `Timestamp`, nested tuples.
- Top-level key set; `meta` fields; the four treatments in section-number order
  with every field the dashboard reads.
- **Neutrality as a test**: no key anywhere in the payload contains `best`,
  `recommended`, `correct` or `preferred`.
- Hand-computed VDA figures: gross ₹9,450 / net ₹5,850 / gap ₹3,600; OP 2's
  `premise` matches the engine invariant (`counterfactual` exactly when
  stablecoin legs are detected).
- Ledger preview column order, `ts_utc` as ISO-8601 Z, `fx_rate_date` as
  `YYYY-MM-DD`, and the row cap reported via `ledger_preview_truncated`.
- `consideration_inr` alias always equals `consideration_received_inr`.
- **No drift**: every treatment figure equals `interpret()` called directly.
- `config.ON_RAMP_USDC_COST_INR` is restored after every call, and a second
  call is unaffected by the first.
- Salary / other-head overrides reach OP 4 (salary unchanged after set-off, as
  statute requires) and the assumptions box.
- Empty ledger keeps the full payload shape at zero.
- `normalize_as_of` matches `main._parse_as_of` exactly.

### Two corrections made during testing

Both were errors in the test's own arithmetic; the engine was right in each case,
and both are now documented in the test comments:

1. Funding received counts inside the VDA gross base, because the default
   `Assumptions.funding_treatment` is `"separate_taxable_event"`. Gross is
   therefore ₹9,000 (the winning close) + ₹450 (funding) = ₹9,450, not ₹9,000.
2. OP 2 is flagged `counterfactual` only when the ledger contains real
   stablecoin legs (`_has_real_stablecoin_legs`). On-venue USDC deposits carry
   `asset_class="other"`, so a fixture without legs is legitimately `live`. The
   test now asserts the invariant rather than a hard-coded value.

---

## Verification performed

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_webapp_pipeline.py -q   # 22 passed
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q                                 # 97 passed, 11 skipped, 1 failed (pre-existing)
```

### The plan's verify command, and why it raises

`docs/phase8_plan_frontend.md` gives this as the Phase A check:

```bash
python -c "from webapp.pipeline import build_result; import json; print(json.dumps(build_result('0x4964f89307d74519f8302c4b9695f3a80c2098ef'))[:500])"
```

Unbounded, it raises `fx.fx.UpstreamNotYetPublishedError`. **This is correct
behaviour, not a defect:** the FBIL USD/INR series currently ends 2026-07-16
while the cached address has events through 2026-07-22, and the engine refuses to
emit a partial tax number rather than silently dropping unconvertible events.
It is precisely the `FxError` that Phase B must map to HTTP 422.

Bounded to a date the series covers, it succeeds:

```bash
python -m webapp.pipeline 0x4964f89307d74519f8302c4b9695f3a80c2098ef --as-of 2026-07-16
```

Observed output on the cached sample address:

| | |
|---|---|
| payload size | 206,606 bytes of strictly valid JSON (`allow_nan=False`), no `NaN` token |
| event range | 2026-04-17 to 2026-07-16 |
| window counts | 158 fills / 203 funding / 6 ledger |
| ledger rows | 353 — funding 203, realized_pnl 72, fee 72, deposit 4, withdrawal 2 |
| Schedule VDA | 72 episodes |
| open positions | 2 |
| VDA | base ₹5,733.11, tax ₹1,788.73, gross-vs-net gap ₹5,733.11 |
| OP 2 / 3 / 4 | base −₹6,435.98, tax ₹0.00 (a loss year) |
| audit flag | false on every reading |

### Known pre-existing failure

`tests/test_reconciliation.py::test_gate3b_venue_reconciliation[0x4e23…20c3]`
fails with a max per-event deviation of 0.2200 USD against
`config.TOL_VENUE_RECONCILE_USD`. It is a data-driven check against a cached
33,980-fill address whose history sits above the ~10,000-fill API wall (the load
layer warns about exactly this). It imports nothing from `webapp/` and is
unaffected by Phase A.

---

## What Phase B can rely on

### Callable surface

| Symbol | Use |
|---|---|
| `build_result(address, *, refresh, as_of, onramp_cost, salary_income_inr, other_head_income_inr, assumptions, preview_rows, include_line_items) -> dict` | the whole `/api/report` endpoint body |
| `build_payload(address, ledger, ...)` | network-free seam, for tests or a synthetic-ledger route |
| `normalize_as_of(value)` | `'YYYY-MM-DD'` → inclusive end-of-UTC-day, same as the CLI |
| `TREATMENT_ORDER`, `DEFAULT_PREVIEW_ROWS`, `LEDGER_PREVIEW_COLUMNS` | display constants |

### Exception mapping Phase B must write

Nothing is swallowed in Phase A — the HTTP layer owns the status codes.

| Raised | Origin | Status |
|---|---|---|
| `fetch.fetch_user.BadAddressError` | `normalize_address` | **400** |
| `fx.fx.FxError` (incl. `UpstreamNotYetPublishedError`, `RateUnavailableError`, `StaleSeriesError`) | `build_inr_ledger` | **422** + `str(exc)` and the "refresh the FX series / add `--as-of`" hint from `main.py:100-108` |
| — (`empty: true` in the payload) | not an exception | **200** |

Error bodies must carry a `detail` key: the dashboard reads
`body.detail || body.message`.

### Payload keys emitted

```
meta{address, event_range, generated_utc, fx_convention, cost_basis_convention,
     as_of, refresh, onramp_cost_inr, window_counts?}
empty
warnings[]
treatment_order[]
treatments{vda, futures_inr, speculative, non_speculative}
    → every interpret() key verbatim, plus line_items / line_items_row_count /
      line_items_omitted / line_items_truncated
tds{...}
cross_cutting{limitations_top[], onramp_boundary{}, ais_asymmetry_note,
              capital_gains_excluded_note, disclosure_checklist[],
              has_stablecoin_legs, non_hyperliquid_asset_classes_detected[],
              assumptions_box{... , onramp_cost_inr}}
ledger_row_count, ledger_counts{}, ledger_preview[],
    ledger_preview_row_count, ledger_preview_truncated
schedule_vda[], schedule_vda_row_count, schedule_vda_truncated
open_positions[], open_position_count
```

### Frontend contract: verified as matching

Checked field-by-field against `hl frontend tax/HL Tax Engine Portal.dc.html`:

- Every query parameter the page sends — `address`, `refresh`, `as_of`,
  `onramp_cost`, `salary_income_inr`, `other_head_income_inr` — is a
  `build_result` keyword argument.
- Every key in the page's `MOCK` object exists in the payload.
- `LEDGER_COLS` and `VDA_COLS` all resolve, including the page's
  `consideration_inr` spelling (shipped as an alias alongside the ITR-genre
  `consideration_received_inr`).
- `cross_cutting.assumptions_box.onramp_cost_inr` is populated, as the page
  expects after an on-ramp cost is supplied.

### Two things Phase B will run into

1. **`/api/ca-report` and `/api/ca-report/fys` cannot be implemented yet.** The
   dashboard's "Generate CA report" modal calls both, but `present/ca_report.py`
   and `available_fys()` belong to Phase 9 and do not exist. Phase B should
   either return 501 with a clear message or stub the FY list, and wire the real
   routes when Phase 9 lands (the contract is already fixed in
   `docs/phase9_ca_report_plan.md` § "Phase 8 (frontend) contract").
2. **The frontend needs relocating.** `hl frontend tax/HL Tax Engine Portal.dc.html`
   must become `webapp/static/index.html`, with `support.js` beside it — a space
   in the directory name and a `.dc.html` extension will not serve behind
   `StaticFiles(directory="webapp/static", html=True)`. Phase A left both files
   untouched.
