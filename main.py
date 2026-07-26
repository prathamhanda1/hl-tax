"""
main.py — Phase 6 orchestrator. One command, the whole pipeline.

    python main.py <address> [--refresh] [--out DIR] [--as-of YYYY-MM-DD]
                             [--onramp-cost INR]

Wires the layers in order and prints one progress line per phase, failing
LOUDLY per phase rather than degrading silently (troubleshooting doctrine #4):

    fetch -> load -> reconstruct -> fx -> assemble -> interpret -> present

READ-ONLY, ALWAYS. This tool takes a PUBLIC wallet address and nothing else.
There is no field, flag, or prompt for a private key or API secret, and there
never will be.

--as-of bounds the analysis to events on or before a date. It is both a genuine
feature (compute a position as of a financial-year end) and the honest response
to an FX series that does not yet cover the most recent, still-unpublished
business days: rather than silently dropping unconvertible recent events (a
wrong tax number wearing a green checkmark), the tool converts only what it can
price and tells you exactly where it stopped.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

import pandas as pd

import config


def _log(step: int, total: int, msg: str) -> None:
    print(f"[{step}/{total}] {msg}", flush=True)


def _apply_cutoff(loaded, as_of: pd.Timestamp | None):
    """Keep only events at or before `as_of` (UTC). Pure filter over the three
    loaded frames; returns a new Loaded-like tuple of frames."""
    if as_of is None:
        return loaded.fills, loaded.funding, loaded.ledger
    f = loaded.fills[loaded.fills["ts"] <= as_of]
    fn = loaded.funding[loaded.funding["ts"] <= as_of] if len(loaded.funding) else loaded.funding
    lg = loaded.ledger[loaded.ledger["ts"] <= as_of] if len(loaded.ledger) else loaded.ledger
    return f, fn, lg


def run(address: str, out_dir: str | None, refresh: bool,
        as_of: pd.Timestamp | None, onramp_cost: float | None) -> int:
    from fetch.fetch_user import fetch_all_for_address, normalize_address
    from load.load import load_raw
    from reconstruct.positions import reconstruct_positions
    from reconstruct.funding import reconstruct_funding
    from fx.fx import FxError, default_converter
    from interpret.assemble import build_inr_ledger
    from interpret.treatments import Assumptions, interpret
    from present.report import build_report

    TOTAL = 7
    address = normalize_address(address)
    if onramp_cost is not None:
        # Runtime override of the manual-input slot (echoed in the report).
        config.ON_RAMP_USDC_COST_INR = onramp_cost

    # 1 — FETCH (the only network step; cache-first).
    _log(1, TOTAL, f"Fetch — {address}")
    raw = fetch_all_for_address(address, refresh=refresh)

    # 2 — LOAD.
    _log(2, TOTAL, "Load — raw JSON -> typed frames")
    loaded = load_raw(raw, account=address)
    for w in loaded.warnings:
        print(f"    WARNING: {w}")
    fills, funding_df, ledger_df = _apply_cutoff(loaded, as_of)
    if as_of is not None:
        print(f"    --as-of {as_of.date()}: {len(fills)} fills / "
              f"{len(funding_df)} funding / {len(ledger_df)} ledger in window")

    if not len(fills) and not len(ledger_df):
        print("    No in-window activity for this address. Nothing to report.")
        return 0

    # 3 — RECONSTRUCT.
    _log(3, TOTAL, "Reconstruct — fills -> closing episodes + funding")
    rec = reconstruct_positions(fills)
    funding_ledger = reconstruct_funding(funding_df)
    for w in rec.warnings:
        print(f"    WARNING: {w}")
    print(f"    {len(rec.closing_events)} closing events, "
          f"{len(rec.open_positions)} still-open positions")

    # 4 + 5 — FX + ASSEMBLE (the single USD->INR step lives in the assembler).
    _log(4, TOTAL, "FX + assemble — stamp event-time INR rates, build ledger")
    converter = default_converter()
    try:
        ledger = build_inr_ledger(rec.closing_events, funding_ledger,
                                  ledger_df, converter)
    except FxError as e:
        print("\nFX COVERAGE ERROR — refusing to produce a partial tax number.",
              file=sys.stderr)
        print(f"  {e}", file=sys.stderr)
        print("\n  Fix one of:", file=sys.stderr)
        print("   - refresh the FX series:  python -m fx.fetch_fx --update",
              file=sys.stderr)
        print("   - bound the analysis:     add --as-of <a date the series "
              "covers>", file=sys.stderr)
        return 2
    print(f"    {len(ledger)} INR ledger events assembled")

    # 6 — INTERPRET.
    _log(5, TOTAL, "Interpret — four treatments + TDS + cross-cutting")
    assumptions = Assumptions()
    interpreted = interpret(ledger, assumptions)

    # 7 — PRESENT.
    _log(6, TOTAL, "Present — CSV ledger, Schedule-VDA CSV, CA summary HTML")
    out_dir = out_dir or str(config.DATA_DIR / "reports" / address)
    if len(ledger):
        lo = ledger["ts_utc"].min()
        hi = ledger["ts_utc"].max()
        event_range = f"{lo:%Y-%m-%d} to {hi:%Y-%m-%d}"
    else:
        event_range = "no events"
    meta = {
        "address": address,
        "event_range": event_range,
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "fx_convention": converter.convention,
        "cost_basis_convention": config.COST_BASIS_CONVENTION,
    }
    paths = build_report(ledger, interpreted, out_dir, meta)

    _log(7, TOTAL, "Done.")
    print(f"    ledger:       {paths.ledger_csv}")
    print(f"    schedule VDA: {paths.schedule_vda_csv}")
    print(f"    CA summary:   {paths.summary_html}")
    return 0


def _parse_as_of(s: str | None) -> pd.Timestamp | None:
    if s is None:
        return None
    # Inclusive end of the given UTC day.
    return pd.Timestamp(s, tz="UTC") + pd.Timedelta(hours=23, minutes=59, seconds=59)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Indian tax + P&L engine for Hyperliquid perps "
                    "(read-only; public address only; never a private key).")
    ap.add_argument("address", help="public Hyperliquid wallet address (0x...)")
    ap.add_argument("--refresh", action="store_true",
                    help="bypass the cache and re-fetch from the API")
    ap.add_argument("--out", default=None,
                    help="output directory (default: data/reports/<address>/)")
    ap.add_argument("--as-of", default=None, metavar="YYYY-MM-DD",
                    help="bound analysis to events on/before this UTC date")
    ap.add_argument("--onramp-cost", default=None, type=float, metavar="INR",
                    help="your INR cost of acquiring the USDC (manual-input slot)")
    args = ap.parse_args(argv)

    try:
        return run(args.address, args.out, args.refresh,
                   _parse_as_of(args.as_of), args.onramp_cost)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
