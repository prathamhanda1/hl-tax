"""
fx/fetch_fx.py — build/refresh the FBIL USD/INR reference-rate series, honestly.

The runtime path NEVER scrapes RBI/FBIL (their portals are anti-automation). It
pulls the SAME FBIL benchmark from Frankfurter's free, open-source FBIL provider
(verified live 2026-07-23):

    GET https://api.frankfurter.dev/v2/rates?base=USD&providers=FBIL
        &from=<A>&to=<B>            (range; a >~2yr span 422s, so we chunk by year)
    -> [{"date","base","quote":"INR","rate": <INR/USD, <=4dp>}, ...]
    Coverage 2018-07-10 -> present (daily; weekends/holidays absent). Upstream
    itself LAGS the calendar by days, so a successful fetch does not guarantee
    coverage to today — the reader's staleness model handles that.

Data at rest (config.FX_SERIES_FILE), append-mostly, manifest-gated:
    rate_date,rate,source,administrator,fetched_at_utc
`source` = retrieval path (frankfurter-fbil / fbil-primary-* / rbi-primary-seed);
`administrator` = citable authority (FBIL on/after 2018-07-10, RBI before).

Subcommands (each a distinct human action):
  --status   freshness at a glance (does one live upstream check).
  --update   pull (coverage_end, today], validate, append, rewrite manifest.
  --verify   reconcile a sample of stored rows against a fresh upstream pull.
  --reseed   rebuild the whole series from scratch (guarded; history is immutable).

A bad fetch fails LOUDLY and never poisons the series; validation runs before
anything is written, and the manifest (the completeness marker) is written LAST.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from datetime import date, datetime, timedelta, timezone

import requests

import config

_RATES_ENDPOINT = f"{config.FX_PROVIDER_URL}/v2/rates"
_HANDOVER = date.fromisoformat(config.FX_ADMIN_HANDOVER_DATE)
_CSV_HEADER = ["rate_date", "rate", "source", "administrator", "fetched_at_utc"]


class FxFetchError(Exception):
    """A fetch/validation failure. Raised before anything is written."""


# ---------------------------------------------------------------------------
# Upstream (Frankfurter FBIL provider)
# ---------------------------------------------------------------------------

def _fbil_range(frm: date, to: date) -> list[tuple[date, float]]:
    """One Frankfurter call for [frm, to]; returns sorted (date, rate) for the
    USD->INR quote only. Empty if the window has no published business days."""
    resp = requests.get(
        _RATES_ENDPOINT,
        params={"base": "USD", "providers": config.FX_PROVIDER_KEY,
                "from": frm.isoformat(), "to": to.isoformat()},
        timeout=config.REQUEST_TIMEOUT_S,
    )
    resp.raise_for_status()
    out = []
    for row in resp.json():
        if row.get("quote") == "INR":
            out.append((date.fromisoformat(row["date"]), float(row["rate"])))
    out.sort()
    return out


def fbil_fetch(frm: date, to: date) -> list[tuple[date, float]]:
    """Fetch [frm, to] from the FBIL provider, chunked by calendar year to stay
    under the API's ~2-year single-call span cap. Deduped across chunks: when a
    chunk's `from` lands on a holiday (e.g. Jan 1), Frankfurter echoes the last
    available prior date, so a boundary date can appear in two chunks — we keep
    it once and raise if the two values ever disagree."""
    if frm > to:
        return []
    merged: dict[date, float] = {}
    start = frm
    while start <= to:
        chunk_end = min(date(start.year, 12, 31), to)
        for d, rate in _fbil_range(start, chunk_end):
            if d in merged and abs(merged[d] - rate) > config.FX_RECONCILE_TOLERANCE:
                raise FxFetchError(
                    f"upstream returned conflicting rates for {d}: "
                    f"{merged[d]} vs {rate}")
            merged[d] = rate
        start = chunk_end + timedelta(days=1)
    return sorted(merged.items())


def fbil_latest_date() -> date | None:
    """FBIL's most recent published date right now (a small live probe over the
    last ~30 days). Used by --status and to inform the reader's staleness."""
    today = date.today()
    rows = _fbil_range(today - timedelta(days=30), today)
    return rows[-1][0] if rows else None


# ---------------------------------------------------------------------------
# Validation — a bad fetch must never enter the series
# ---------------------------------------------------------------------------

def validate_rows(rows: list[tuple[date, float]]) -> None:
    """Reject a fetch that is out of order, duplicated, or out of the sane rate
    band (a 0, null, or decimal-shifted value). Raises FxFetchError.

    Interior GAPS are handled separately (find_large_gaps): a full-history
    reseed legitimately contains real upstream holes and records them, whereas
    an incremental --update window must not (a gap there signals a partial
    fetch)."""
    prev: date | None = None
    for d, rate in rows:
        if prev is not None and d <= prev:
            raise FxFetchError(f"dates not strictly increasing: {prev} then {d}")
        if not (config.FX_RATE_MIN <= rate <= config.FX_RATE_MAX):
            raise FxFetchError(
                f"rate {rate} on {d} outside sane band "
                f"[{config.FX_RATE_MIN}, {config.FX_RATE_MAX}] - a zero, null, "
                f"or decimal-shifted value; refusing to poison the series."
            )
        prev = d


def find_large_gaps(rows: list[tuple[date, float]]) -> list[tuple[date, date, int]]:
    """Consecutive-date gaps larger than a normal weekend/holiday cluster
    (FX_MAX_INTERIOR_GAP_DAYS). Returns (before, after, days) triples."""
    gaps = []
    for (a, _), (b, _) in zip(rows, rows[1:]):
        g = (b - a).days
        if g > config.FX_MAX_INTERIOR_GAP_DAYS:
            gaps.append((a, b, g))
    return gaps


def _administrator_for(d: date) -> str:
    return "FBIL" if d >= _HANDOVER else "RBI"


# ---------------------------------------------------------------------------
# Series + manifest on disk
# ---------------------------------------------------------------------------

def _read_series() -> list[dict]:
    p = config.FX_SERIES_FILE
    if not p.exists():
        return []
    with p.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _write_series(records: list[dict]) -> None:
    p = config.FX_SERIES_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_CSV_HEADER)
        w.writeheader()
        w.writerows(records)
    tmp.replace(p)


def _records_from(rows: list[tuple[date, float]], source: str,
                  fetched_at: str) -> list[dict]:
    return [
        {"rate_date": d.isoformat(), "rate": f"{rate:.4f}", "source": source,
         "administrator": _administrator_for(d), "fetched_at_utc": fetched_at}
        for d, rate in rows
    ]


def _seed_sha256() -> str:
    p = config.FX_SERIES_FILE
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else ""


def _write_manifest(records: list[dict], upstream_end: date | None,
                    notes: list[str]) -> None:
    dates = [r["rate_date"] for r in records]
    sources = sorted({r["source"] for r in records})
    manifest = {
        "schema_version": 1,
        "coverage_start": dates[0] if dates else None,
        "coverage_end": dates[-1] if dates else None,
        "row_count": len(records),
        "last_fetch_utc": datetime.now(timezone.utc).isoformat(),
        "upstream_end": upstream_end.isoformat() if upstream_end else None,
        "seed_sha256": _seed_sha256(),
        "sources_present": sources,
        "administrator": "FBIL reference rate (RBI before 2018-07-10); "
                         "retrieved via Frankfurter FBIL provider",
        "gaps_expected": "weekends + Mumbai bank holidays only",
        "notes": notes,
    }
    config.FX_MANIFEST_FILE.write_text(
        json.dumps(manifest, indent=1), encoding="utf-8")


def _read_manifest() -> dict | None:
    p = config.FX_MANIFEST_FILE
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_reseed(force: bool = False) -> int:
    """Rebuild the whole series from the handover date to today. Guarded —
    history should be immutable; only for a schema change or a found error."""
    if config.FX_SERIES_FILE.exists() and not force:
        print("Refusing to reseed: series already exists. Re-run with --force "
              "to rebuild from scratch (this replaces the vendored history).",
              file=sys.stderr)
        return 1
    print(f"Reseeding FBIL series {_HANDOVER} -> today via Frankfurter "
          f"(chunked by year)...")
    rows = fbil_fetch(_HANDOVER, date.today())
    if not rows:
        print("ERROR: upstream returned no rows", file=sys.stderr)
        return 1
    validate_rows(rows)
    gaps = find_large_gaps(rows)  # real historical holes: document, don't reject
    notes = ["reseed"] + [
        f"interior gap {a} -> {b} ({g}d): upstream (FBIL/Frankfurter) has no "
        f"rate here; reads in this window walk back to {a} and are flagged "
        f"fx_stale_grace" for a, b, g in gaps
    ]
    fetched_at = datetime.now(timezone.utc).isoformat()
    records = _records_from(rows, config.FX_PROVIDER_NAME, fetched_at)
    _write_series(records)
    _write_manifest(records, upstream_end=rows[-1][0], notes=notes)
    print(f"Wrote {len(records)} rows: {records[0]['rate_date']} -> "
          f"{records[-1]['rate_date']} -> {config.FX_SERIES_FILE}")
    if gaps:
        print(f"  NOTE: {len(gaps)} interior gap(s) recorded in manifest: "
              f"{[(str(a), str(b), g) for a, b, g in gaps]}")
    return 0


def cmd_update() -> int:
    """Pull (coverage_end, today], validate, append. Never errors on 'nothing
    new' or an unreachable upstream — staleness is the reader's job."""
    manifest = _read_manifest()
    records = _read_series()
    if not manifest or not records:
        print("No series yet - run `python -m fx.fetch_fx --reseed` first.",
              file=sys.stderr)
        return 1
    coverage_end = date.fromisoformat(manifest["coverage_end"])
    frm = coverage_end + timedelta(days=1)
    today = date.today()
    if frm > today:
        print(f"Coverage already at {coverage_end}; nothing to do.")
        return 0

    try:
        new_rows = fbil_fetch(frm, today)
    except requests.RequestException as exc:
        print(f"Upstream unreachable ({exc}); coverage unchanged at "
              f"{coverage_end}. Staleness is enforced at read time.")
        return 0

    if not new_rows:
        print(f"No new data; coverage unchanged at {coverage_end}. Upstream "
              f"(FBIL) may be lagging the calendar.")
        return 0

    validate_rows(new_rows)
    gaps = find_large_gaps(new_rows)
    if gaps:
        raise FxFetchError(
            f"incremental update window has interior gap(s) {gaps} - this "
            f"signals a partial/incomplete upstream fetch, not real history. "
            f"Refusing to append; retry --update.")
    # Overlap safety: the first new date must not already be stored with a
    # different value (never silently overwrite).
    stored = {r["rate_date"]: float(r["rate"]) for r in records}
    for d, rate in new_rows:
        key = d.isoformat()
        if key in stored and abs(stored[key] - round(rate, 4)) > config.FX_RECONCILE_TOLERANCE:
            raise FxFetchError(
                f"overlap conflict on {key}: stored {stored[key]} vs fetched "
                f"{rate}; refusing to overwrite history.")

    fetched_at = datetime.now(timezone.utc).isoformat()
    fresh = [(d, r) for d, r in new_rows if d.isoformat() not in stored]
    records.extend(_records_from(fresh, config.FX_PROVIDER_NAME, fetched_at))
    records.sort(key=lambda r: r["rate_date"])
    _write_series(records)
    _write_manifest(records, upstream_end=new_rows[-1][0], notes=["update"])
    print(f"Added {len(fresh)} rows; coverage {coverage_end} -> "
          f"{records[-1]['rate_date']} ({(today - new_rows[-1][0]).days} days "
          f"behind today - upstream lag).")
    return 0


def cmd_status() -> int:
    manifest = _read_manifest()
    if not manifest:
        print("No FX series. Run `python -m fx.fetch_fx --reseed`.")
        return 1
    coverage_end = date.fromisoformat(manifest["coverage_end"])
    today = date.today()
    local_stale = (today - coverage_end).days
    try:
        up = fbil_latest_date()
    except requests.RequestException:
        up = None

    print(f"FX series (FBIL reference rate via Frankfurter):")
    print(f"  coverage:   {manifest['coverage_start']} -> {coverage_end} "
          f"({manifest['row_count']} rows)")
    print(f"  local end:  {coverage_end}  ({local_stale} days behind today)")
    print(f"  upstream:   {up if up else 'unreachable'}")
    if up is None:
        verdict = "UNKNOWN (upstream unreachable)"
    elif coverage_end >= up:
        verdict = "FRESH (local matches upstream; upstream itself lags calendar)"
    elif (up - coverage_end).days <= config.FX_MAX_STALENESS_DAYS:
        verdict = "STALE within grace (run --update to catch up)"
    else:
        verdict = "STALE (run `python -m fx.fetch_fx --update`)"
    print(f"  verdict:    {verdict}")
    return 0


def cmd_verify(sample: int = 10) -> int:
    """Reconcile a spread-out sample of stored rows against a fresh upstream
    pull — the FX analogue of the closedPnl cross-check. Deviation beyond
    tolerance is a loud failure."""
    records = _read_series()
    if not records:
        print("No series to verify.", file=sys.stderr)
        return 1
    n = len(records)
    idxs = sorted({int(i * (n - 1) / max(sample - 1, 1)) for i in range(sample)})
    max_dev = 0.0
    print(f"Verifying {len(idxs)} sampled rows against a fresh upstream pull...")
    for i in idxs:
        rec = records[i]
        d = date.fromisoformat(rec["rate_date"])
        fresh = _fbil_range(d, d)
        if not fresh:
            print(f"  {d}: upstream returned nothing (skipped)")
            continue
        dev = abs(fresh[0][1] - float(rec["rate"]))
        max_dev = max(max_dev, dev)
        flag = "" if dev <= config.FX_RECONCILE_TOLERANCE else "  <-- EXCEEDS TOL"
        print(f"  {d}: stored {rec['rate']} vs upstream {fresh[0][1]:.4f} "
              f"(dev {dev:.6f}){flag}")
    print(f"max deviation: {max_dev:.6f} (tolerance {config.FX_RECONCILE_TOLERANCE})")
    if max_dev > config.FX_RECONCILE_TOLERANCE:
        print("VERIFY FAILED: stored values disagree with upstream.",
              file=sys.stderr)
        return 1
    print("VERIFY OK.")
    return 0


def _cli() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--status", action="store_true", help="freshness at a glance")
    g.add_argument("--update", action="store_true", help="pull new tail rows")
    g.add_argument("--verify", action="store_true",
                   help="reconcile a sample vs upstream")
    g.add_argument("--reseed", action="store_true",
                   help="rebuild the whole series (guarded)")
    ap.add_argument("--force", action="store_true",
                    help="with --reseed: overwrite an existing series")
    ap.add_argument("--sample", type=int, default=10,
                    help="with --verify: how many rows to sample")
    args = ap.parse_args()
    try:
        if args.status:
            return cmd_status()
        if args.update:
            return cmd_update()
        if args.verify:
            return cmd_verify(args.sample)
        if args.reseed:
            return cmd_reseed(force=args.force)
    except (FxFetchError, requests.RequestException) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
