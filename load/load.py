"""
load/load.py — Phase 2. The ONE place raw JSON becomes typed tables.

Contract (week2_master_plan.md Phase 2):
  - raw JSON -> three typed DataFrames: fills, funding, ledger.
  - strings -> floats EXACTLY ONCE, here. Every downstream layer receives
    numbers, never number-shaped strings.
  - UTC timestamps -> a tz-aware `ts` column (datetime64[ns, UTC]). Epoch-ms
    integers are kept in `time_ms` too, for exact joins/debugging.
  - each fill's asset is tagged with its dex namespace AND an asset class
    (crypto-perp / equity-perp / hip3-perp / spot), per config. The tax
    distinction between an equity perp and a crypto perp may need this later.

Layer discipline:
  - PURE transforms. The three `load_*` functions take Python lists (exactly
    the shape fetch writes to disk / the fixtures hold) and return DataFrames.
    No network, no disk. That is what makes them testable on the paper
    fixtures. A thin `load_address` wrapper reads the cache for real runs.
  - Load CLASSIFIES but does not JUDGE economics: it computes no P&L, nets
    nothing. It does, however, WARN loudly (plan doctrine "loud failure beats
    silent degradation") when it sees flows the perp-P&L pipeline will not
    handle — spot fills, non-USDC token flows, USDC transfers that are not
    plain deposits/withdrawals, or any ledger type it does not recognise.
    A skipped event is a wrong tax number wearing a green checkmark, so
    nothing is skipped silently: out-of-scope rows are TYPED and KEPT, tagged
    so Phase 3 can decide, and their existence is announced.

SIGN CONVENTIONS carried through unchanged from the raw data (see
tests/fixtures.py for the verified-live statement of each):
  fills.fee        + = user paid, - = maker rebate.
  fills.closed_pnl venue realized P&L of the closed portion of this fill.
  funding.usdc     + = received by user, - = paid by user.
  ledger.usdc      amount of the flow in USDC (deposit/transfer-in and
                   withdraw/transfer-out both stated positive; DIRECTION is
                   carried by `type`, not by the sign — Phase 3 applies it).
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

import config


# ---------------------------------------------------------------------------
# Asset classification
# ---------------------------------------------------------------------------

def classify_asset(coin: str) -> tuple[str, str, str]:
    """Map a raw `coin` string to (dex, symbol, asset_class).

    dex          "main" | the builder-dex prefix (e.g. "xyz") | "spot"
    symbol       the coin without its namespace ("BTC", "TSLA", "@107")
    asset_class  per config: crypto-perp / equity-perp / hip3-perp / spot
    """
    if coin.startswith("@"):
        # Spot asset in index form (e.g. "@107"). Not a perp.
        return "spot", coin, config.ASSET_CLASS_SPOT
    if ":" in coin:
        dex, _, symbol = coin.partition(":")
        asset_class = config.HIP3_DEX_ASSET_CLASS.get(
            dex, config.ASSET_CLASS_HIP3_DEFAULT
        )
        return dex, symbol, asset_class
    return "main", coin, config.ASSET_CLASS_MAIN_DEX


# ---------------------------------------------------------------------------
# Small typing helpers — the string->number boundary lives ONLY here.
# ---------------------------------------------------------------------------

def _ts_utc(time_ms: pd.Series) -> pd.Series:
    """Epoch-ms integers -> tz-aware UTC timestamps."""
    return pd.to_datetime(time_ms, unit="ms", utc=True)


def _floats(df: pd.DataFrame, cols: list[str]) -> None:
    """In-place: coerce the named columns to float64. Missing keys become
    NaN (not object) so a numeric column is never object-dtype.
    """
    for c in cols:
        df[c] = pd.to_numeric(df.get(c), errors="raise").astype("float64")


# ---------------------------------------------------------------------------
# fills
# ---------------------------------------------------------------------------

_FILL_SCHEMA = {
    "time_ms": "int64", "coin": "object", "dex": "object", "symbol": "object",
    "asset_class": "object", "is_perp": "bool", "side": "object",
    "dir": "object", "px": "float64", "sz": "float64",
    "start_position": "float64", "closed_pnl": "float64", "fee": "float64",
    "builder_fee": "float64", "fee_token": "object", "crossed": "bool",
    "is_liquidation": "bool", "oid": "int64", "tid": "int64",
    "hash": "object", "cloid": "object", "twap_id": "object",
}


def _is_own_liquidation(fill: dict, account: str | None) -> bool:
    """A userFills row carries a `liquidation` object whenever the fill matched
    against a liquidation; `liquidation.liquidatedUser` names WHO was
    liquidated. Verified on live data 2026-07-22: the same field appears on
    fills where WE were merely the maker counterparty to someone else's
    liquidation (their address, our rebate). So THIS account was force-closed
    only when liquidatedUser == our address — that, and only that, is a
    forced disposal of our position. Without a known account we cannot decide,
    so default False and let the reconstruction/test inject it explicitly.
    """
    liq = fill.get("liquidation")
    if not liq or account is None:
        return False
    return str(liq.get("liquidatedUser", "")).lower() == account.lower()


def load_fills(fills: list[dict], account: str | None = None) -> pd.DataFrame:
    """Raw userFills/userFillsByTime rows -> typed fills DataFrame, ascending
    by time. One row per fill (flips are NOT split here — that is Phase 3's
    job; load stays faithful to the raw event stream). `account` (lowercase
    0x address) enables correct own-liquidation tagging; omit it and
    is_liquidation is all-False (tests inject it by tid instead).
    """
    if not fills:
        return _empty(_FILL_SCHEMA, ts=True)

    recs = []
    for f in fills:
        dex, symbol, asset_class = classify_asset(f["coin"])
        recs.append({
            "time_ms": f["time"],
            "coin": f["coin"], "dex": dex, "symbol": symbol,
            "asset_class": asset_class,
            "is_perp": asset_class != config.ASSET_CLASS_SPOT,
            "side": f["side"], "dir": f.get("dir"),
            "px": f["px"], "sz": f["sz"],
            "start_position": f["startPosition"], "closed_pnl": f["closedPnl"],
            "fee": f["fee"],
            # builderFee: an EXTRA fee charged by a HIP-3 builder dex, separate
            # from `fee` and present only when non-zero. It reduces cash just
            # like `fee` (confirmed material to the Gate 3c identity), so it is
            # loaded here; default 0.0 when the field is absent.
            "builder_fee": f.get("builderFee", "0.0"),
            "fee_token": f.get("feeToken"),
            "crossed": f["crossed"],
            "is_liquidation": _is_own_liquidation(f, account),
            "oid": f["oid"], "tid": f["tid"],
            "hash": f["hash"], "cloid": f.get("cloid"),
            "twap_id": f.get("twapId"),
        })
    df = pd.DataFrame.from_records(recs)
    _floats(df, ["px", "sz", "start_position", "closed_pnl", "fee",
                 "builder_fee"])
    df["time_ms"] = df["time_ms"].astype("int64")
    df["oid"] = df["oid"].astype("int64")
    df["tid"] = df["tid"].astype("int64")
    df["crossed"] = df["crossed"].astype("bool")
    df["is_perp"] = df["is_perp"].astype("bool")
    df["is_liquidation"] = df["is_liquidation"].astype("bool")
    df.insert(0, "ts", _ts_utc(df["time_ms"]))
    return df.sort_values("time_ms", kind="stable").reset_index(drop=True)


# ---------------------------------------------------------------------------
# funding
# ---------------------------------------------------------------------------

_FUNDING_SCHEMA = {
    "time_ms": "int64", "coin": "object", "dex": "object", "symbol": "object",
    "asset_class": "object", "usdc": "float64", "szi": "float64",
    "funding_rate": "float64", "n_samples": "Int64", "hash": "object",
}


def load_funding(funding: list[dict]) -> pd.DataFrame:
    """Raw userFunding rows -> typed funding DataFrame. Funding is a HOLDING
    cost kept as its own category — never netted into execution P&L (plan;
    errors_in_plan.md #6). Sign: usdc + = received, - = paid.
    """
    if not funding:
        return _empty(_FUNDING_SCHEMA, ts=True)

    recs = []
    for e in funding:
        d = e["delta"]
        dex, symbol, asset_class = classify_asset(d["coin"])
        recs.append({
            "time_ms": e["time"],
            "coin": d["coin"], "dex": dex, "symbol": symbol,
            "asset_class": asset_class,
            "usdc": d["usdc"], "szi": d.get("szi"),
            "funding_rate": d.get("fundingRate"),
            "n_samples": d.get("nSamples"),
            "hash": e.get("hash"),
        })
    df = pd.DataFrame.from_records(recs)
    _floats(df, ["usdc", "szi", "funding_rate"])
    df["time_ms"] = df["time_ms"].astype("int64")
    df["n_samples"] = df["n_samples"].astype("Int64")
    df.insert(0, "ts", _ts_utc(df["time_ms"]))
    return df.sort_values("time_ms", kind="stable").reset_index(drop=True)


# ---------------------------------------------------------------------------
# ledger (non-funding ledger updates)
# ---------------------------------------------------------------------------

_LEDGER_SCHEMA = {
    "time_ms": "int64", "type": "object", "in_scope": "bool",
    "is_usdc_flow": "bool", "usdc": "float64", "fee": "float64",
    "token": "object", "amount": "float64", "usdc_value": "float64",
    "hash": "object",
}


def load_ledger(ledger: list[dict]) -> pd.DataFrame:
    """Raw userNonFundingLedgerUpdates -> typed ledger DataFrame.

    Delta shapes differ by type (confirmed live 2026-07-22): deposit/withdraw
    and the transfer types carry `usdc`; token flows carry `token`+`amount`
    (+`usdcValue` on spotTransfer). All are TYPED and KEPT — never dropped —
    so Phase 3 can build the equity identity and decide scope. `usdc` is the
    magnitude of the USDC flow; its DIRECTION is carried by `type`.
    """
    if not ledger:
        return _empty(_LEDGER_SCHEMA, ts=True)

    recs = []
    for e in ledger:
        d = e["delta"]
        t = d["type"]
        recs.append({
            "time_ms": e["time"],
            "type": t,
            "in_scope": t in config.LEDGER_TYPES_IN_SCOPE,
            "is_usdc_flow": ("usdc" in d),
            "usdc": d.get("usdc"),
            "fee": d.get("fee"),
            "token": d.get("token"),
            "amount": d.get("amount"),
            "usdc_value": d.get("usdcValue"),
            "hash": e.get("hash"),
        })
    df = pd.DataFrame.from_records(recs)
    _floats(df, ["usdc", "fee", "amount", "usdc_value"])
    df["time_ms"] = df["time_ms"].astype("int64")
    df["in_scope"] = df["in_scope"].astype("bool")
    df["is_usdc_flow"] = df["is_usdc_flow"].astype("bool")
    df.insert(0, "ts", _ts_utc(df["time_ms"]))
    return df.sort_values("time_ms", kind="stable").reset_index(drop=True)


# ---------------------------------------------------------------------------
# Empty-frame construction with a fixed dtype schema
# ---------------------------------------------------------------------------

def _empty(schema: dict[str, str], ts: bool) -> pd.DataFrame:
    """Build a 0-row DataFrame with exactly the declared dtypes so an address
    with no history flows through the pipeline without dtype surprises.
    """
    df = pd.DataFrame({c: pd.Series([], dtype=dt) for c, dt in schema.items()})
    if ts:
        df.insert(0, "ts", pd.Series([], dtype="datetime64[ns, UTC]"))
    return df


# ---------------------------------------------------------------------------
# Bundle + warnings
# ---------------------------------------------------------------------------

@dataclass
class Loaded:
    fills: pd.DataFrame
    funding: pd.DataFrame
    ledger: pd.DataFrame
    warnings: list[str]


def _collect_warnings(
    fills: pd.DataFrame, ledger: pd.DataFrame
) -> list[str]:
    """Announce every flow the perp-P&L pipeline will not treat as ordinary
    perp activity. Nothing here drops data; it makes the excluded surface
    visible so no silent tax gap forms (plan correction 0.4, doctrine #4).
    """
    warns: list[str] = []

    n_spot = int((~fills["is_perp"]).sum()) if len(fills) else 0
    if n_spot:
        warns.append(
            f"fills: {n_spot} SPOT fill(s) present (coin like '@107'). Spot "
            f"is OUT OF SCOPE for perp P&L (plan 0.4) and is tagged "
            f"asset_class='spot'; it will be excluded from position "
            f"reconstruction, not silently mixed in."
        )

    hip3 = sorted(
        set(fills.loc[fills["asset_class"] == config.ASSET_CLASS_HIP3_DEFAULT,
                      "dex"])
    ) if len(fills) else []
    if hip3:
        warns.append(
            f"fills: builder-dex(es) {hip3} tagged the neutral 'hip3-perp' - "
            f"their underlying asset class (crypto vs equity vs commodity) is "
            f"unverified (config.HIP3_DEX_ASSET_CLASS). Add a mapping if the "
            f"tax distinction turns out to matter for them."
        )

    if len(ledger):
        types = set(ledger["type"])
        transfers = types & config.LEDGER_TYPES_USDC_TRANSFER
        if transfers:
            warns.append(
                f"ledger: USDC-moving transfer type(s) {sorted(transfers)} "
                f"present. These shift the perp balance and so enter the "
                f"Gate 3c equity identity (Phase 3); they are NOT plain "
                f"deposits/withdrawals and must be handled there, not assumed "
                f"away."
            )
        tokens = types & config.LEDGER_TYPES_TOKEN_FLOW
        if tokens:
            warns.append(
                f"ledger: non-USDC token flow(s) {sorted(tokens)} present "
                f"(spot/staking/etc.). Out of scope for perp P&L; tagged and "
                f"kept for audit."
            )
        unknown = types - config.LEDGER_TYPES_IN_SCOPE \
            - config.LEDGER_TYPES_USDC_TRANSFER - config.LEDGER_TYPES_TOKEN_FLOW
        if unknown:
            warns.append(
                f"ledger: UNRECOGNISED type(s) {sorted(unknown)} — not in any "
                f"known scope set (config). Typed and kept, but VERIFY what "
                f"they do to the balance before trusting the equity identity."
            )
    return warns


def load_raw(raw: dict, account: str | None = None) -> Loaded:
    """Turn one address's raw dict (the shape fetch_all_for_address returns,
    and the fixtures mirror) into three typed DataFrames plus warnings.
    Pass `account` so own-liquidation fills are tagged correctly.
    """
    fills = load_fills(raw.get("fills", []), account=account)
    funding = load_funding(raw.get("funding", []))
    ledger = load_ledger(raw.get("ledger", []))
    warnings = _collect_warnings(fills, ledger)
    return Loaded(fills=fills, funding=funding, ledger=ledger,
                  warnings=warnings)


def load_address(address: str) -> Loaded:
    """Real-data entry point: read the cached raw pulls for `address` (fetch
    must have run first) and load them. Fetch is the only writer of the
    cache; load only reads.
    """
    from fetch.fetch_user import fetch_all_for_address, normalize_address
    normalized = normalize_address(address)
    raw = fetch_all_for_address(normalized, refresh=False)
    return load_raw(raw, account=normalized)


# ---------------------------------------------------------------------------
# CLI — the Phase-2 gate check
# ---------------------------------------------------------------------------

def _print_frame(name: str, df: pd.DataFrame) -> None:
    print(f"\n[{name}] {len(df)} rows")
    print("  dtypes:")
    for col, dt in df.dtypes.items():
        print(f"    {col:<16} {dt}")
    obj_numeric = [
        c for c in df.columns
        if df[c].dtype == "object"
        and c in {"px", "sz", "start_position", "closed_pnl", "fee",
                  "usdc", "szi", "funding_rate", "amount", "usdc_value"}
    ]
    if obj_numeric:
        print(f"  !! object-dtype NUMERIC columns: {obj_numeric}  (GATE FAIL)")


def _run_fixtures() -> None:
    from tests import fixtures as fx
    print("=== GATE 2 on FIXTURES ===")
    loaded = load_raw({
        "fills": fx.FILLS, "funding": fx.FUNDING_EVENTS,
        "ledger": fx.LEDGER_EVENTS,
    })
    _print_frame("fills", loaded.fills)
    _print_frame("funding", loaded.funding)
    _print_frame("ledger", loaded.ledger)
    for w in loaded.warnings:
        print(f"  WARNING: {w}")

    # Cross-check the asset-class tags against the fixture's expectation.
    got = dict(zip(loaded.fills["coin"], loaded.fills["asset_class"]))
    for coin, want in fx.EXPECTED_ASSET_CLASSES.items():
        assert got.get(coin) == want, (
            f"asset_class[{coin}] = {got.get(coin)!r}, expected {want!r}"
        )
    print("\n  asset-class tags match fixture EXPECTED_ASSET_CLASSES.")


def _run_address(address: str) -> None:
    print(f"=== GATE 2 on REAL cached address {address} ===")
    loaded = load_address(address)
    _print_frame("fills", loaded.fills)
    _print_frame("funding", loaded.funding)
    _print_frame("ledger", loaded.ledger)
    for w in loaded.warnings:
        print(f"  WARNING: {w}")


def _cli() -> int:
    import argparse
    parser = argparse.ArgumentParser(
        description="Load cached raw pulls (or fixtures) into typed tables."
    )
    parser.add_argument(
        "address", nargs="?",
        help="Hyperliquid address whose cached pull to load. Omit with "
             "--fixtures.",
    )
    parser.add_argument(
        "--fixtures", action="store_true",
        help="load the hand-built fixtures instead of a cached address",
    )
    args = parser.parse_args()
    if args.fixtures:
        _run_fixtures()
    elif args.address:
        _run_address(args.address)
    else:
        parser.error("provide an address or --fixtures")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
