"""
fetch/fetch_user.py — knows WHAT to fetch. client.py knows only how to POST
safely; this module knows the request types, each one's pagination rules,
and the cache policy. It computes nothing about the DATA (no P&L, no
classification) — the load layer (Phase 2) is the first consumer that turns
these raw files into typed DataFrames.

CACHE MODEL — manifest-gated, not file-existence-gated
------------------------------------------------------
Every completed fetch of an address writes data/raw/<address>/manifest.json
LAST, after all data files are on disk. Cache-trust keys on that manifest,
which buys two things a bare "does fills.json exist?" check cannot:

  1. Interrupted fetches don't get trusted. If a run dies mid-pagination,
     the partial data file exists but no manifest does, so the next run
     refetches instead of silently building a tax report on truncated
     history.

  2. Provenance survives a cache hit. The manifest records row/page counts,
     the clearinghouse snapshot time, and any warnings (e.g. "history may
     be incomplete — hit the ~10k-fills wall"). Those warnings are RE-EMITTED
     on every cache hit, so a re-run never launders away a caveat that was
     true when the data was pulled.

perpDexs/meta are address-independent and cached once under
data/raw/_global/ with their own manifest.

Layer discipline: this file inspects only fetch-COMPLETENESS (row counts,
the fills wall) — never data SEMANTICS. Whether the ledger contains spot or
vault activity that must be excluded (plan correction 0.4) is a
classification concern and belongs to the load layer, not here; fetch stays
dumb about meaning on purpose.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import config
from fetch.client import InfoClient, InfoClientError

MANIFEST_SCHEMA_VERSION = 1

# Safety valve against a pathological infinite loop. At 2000 rows/page this
# permits 20M rows before tripping — far beyond any real account.
_MAX_PAGES = 10_000


class BadAddressError(Exception):
    """A locally-detected malformed address. Raised before any API call so
    the message is specific instead of the endpoint's generic 422 text.
    """


# ---------------------------------------------------------------------------
# Address + small IO helpers
# ---------------------------------------------------------------------------

def normalize_address(address: str) -> str:
    candidate = address if address.startswith("0x") else f"0x{address}"
    if not re.match(config.ADDRESS_RE, candidate):
        raise BadAddressError(
            f"'{address}' is not a valid Hyperliquid address: expected "
            f"'0x' followed by exactly 40 hex characters "
            f"(got {len(address)} chars). No API call was made."
        )
    return candidate.lower()


def _address_dir(address: str) -> Path:
    return config.RAW_CACHE_DIR / address


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)  # atomic on POSIX and Windows


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Pagination — shared by the three time-cursor endpoints
# ---------------------------------------------------------------------------

def _paginate_by_time(
    client: InfoClient, req_type: str, address: str
) -> tuple[list[dict], int]:
    """Walk userFillsByTime / userFunding / userNonFundingLedgerUpdates from
    the beginning of time to now. All three take (type, user, startTime) and
    return a time-ASCENDING list capped per page.

    Cursor policy: advance to the last row's OWN timestamp (not +1) and
    dedupe on full row content. Multiple rows can share one millisecond at a
    page boundary (confirmed on busy accounts), so advancing past the
    timestamp would drop them; re-reading the boundary millisecond and
    discarding the dupes is the safe trade. Returns (rows, n_pages).
    """
    seen: set[str] = set()
    rows: list[dict] = []
    cursor = 0
    pages = 0
    while True:
        pages += 1
        if pages > _MAX_PAGES:
            raise RuntimeError(
                f"{req_type} for {address}: exceeded {_MAX_PAGES} pages — "
                f"this indicates a pagination bug, not real data. Stopping."
            )
        page = client.post(
            {"type": req_type, "user": address, "startTime": cursor}
        )
        if not page:
            break
        fresh = 0
        for row in page:
            key = json.dumps(row, sort_keys=True)
            if key not in seen:
                seen.add(key)
                rows.append(row)
                fresh += 1
        if fresh == 0:
            break  # page repeated the previous one exactly -> exhausted
        cursor = page[-1]["time"]
    rows.sort(key=lambda r: r["time"])
    return rows, pages


# ---------------------------------------------------------------------------
# Fresh fetch of one address (no cache consulted)
# ---------------------------------------------------------------------------

def _fetch_address_fresh(client: InfoClient, address: str) -> tuple[dict, dict]:
    """Pull everything for one address from the API. Returns (data,
    manifest). Writes nothing — the caller persists atomically so a manifest
    only ever lands next to a complete data set.
    """
    warnings: list[str] = []

    fills, fills_pages = _paginate_by_time(client, "userFillsByTime", address)
    if len(fills) >= config.USERFILLS_BY_TIME_MAX_RECENT:
        warnings.append(
            f"fills: {len(fills)} rows, at or above HL's observed "
            f"~{config.USERFILLS_BY_TIME_MAX_RECENT}-most-recent-fills wall. "
            f"Earlier fills may exist but be UNRETRIEVABLE via this endpoint "
            f"-- this history may be INCOMPLETE, not merely large."
        )

    funding, funding_pages = _paginate_by_time(client, "userFunding", address)
    ledger, ledger_pages = _paginate_by_time(
        client, "userNonFundingLedgerUpdates", address
    )
    chs = client.post({"type": "clearinghouseState", "user": address})
    # spotClearinghouseState: the account's SPOT USDC (and token) balances.
    # Needed for the Gate 3c equity identity — USDC is fungible across the
    # perp and spot sides of one account, so the "current equity" the ledger
    # must reconcile to is perp accountValue + spot USDC, not perp alone
    # (verified 2026-07-23: on a clean deposits-only account the entire
    # residual was exactly the spot USDC balance).
    spot_chs = client.post({"type": "spotClearinghouseState", "user": address})

    data = {
        "fills": fills,
        "funding": funding,
        "ledger": ledger,
        "clearinghouse_state": chs,
        "spot_clearinghouse_state": spot_chs,
    }
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "address": address,
        "api_base": client.base_url,
        "fetched_at_utc": _now_utc_iso(),
        "rate_limit_delay_s": client.rate_limit_delay_s,
        "types": {
            "fills": {
                "source": "userFillsByTime",
                "rows": len(fills),
                "pages": fills_pages,
            },
            "funding": {
                "source": "userFunding",
                "rows": len(funding),
                "pages": funding_pages,
            },
            "ledger": {
                "source": "userNonFundingLedgerUpdates",
                "rows": len(ledger),
                "pages": ledger_pages,
            },
            "clearinghouse_state": {
                "source": "clearinghouseState",
                "snapshot_time_ms": chs.get("time") if isinstance(chs, dict) else None,
            },
            "spot_clearinghouse_state": {
                "source": "spotClearinghouseState",
                "balances": (
                    len(spot_chs.get("balances", []))
                    if isinstance(spot_chs, dict) else None
                ),
            },
        },
        "warnings": warnings,
    }
    return data, manifest


def _persist_address(address: str, data: dict, manifest: dict) -> None:
    """Write all data files, THEN the manifest. Order matters: the manifest
    is the completeness marker, so it must land last.
    """
    d = _address_dir(address)
    _write_json(d / "fills.json", data["fills"])
    _write_json(d / "funding.json", data["funding"])
    _write_json(d / "ledger.json", data["ledger"])
    _write_json(d / "clearinghouse_state.json", data["clearinghouse_state"])
    _write_json(d / "spot_clearinghouse_state.json",
                data["spot_clearinghouse_state"])
    _write_json(d / "manifest.json", manifest)


def _load_cached_address(address: str, manifest: dict) -> dict:
    d = _address_dir(address)
    spot_path = d / "spot_clearinghouse_state.json"
    return {
        "fills": _read_json(d / "fills.json"),
        "funding": _read_json(d / "funding.json"),
        "ledger": _read_json(d / "ledger.json"),
        "clearinghouse_state": _read_json(d / "clearinghouse_state.json"),
        # Tolerate caches written before spot state was fetched (older schema).
        "spot_clearinghouse_state": (
            _read_json(spot_path) if spot_path.exists() else None
        ),
    }


def _print_address_summary(manifest: dict, from_cache: bool) -> None:
    tag = "cache" if from_cache else "fetched"
    t = manifest["types"]
    print(f"  [{tag}] fills:              {t['fills']['rows']} rows")
    print(f"  [{tag}] funding:            {t['funding']['rows']} rows")
    print(f"  [{tag}] ledger:             {t['ledger']['rows']} rows")
    print(f"  [{tag}] clearinghouseState: snapshot @ "
          f"{t['clearinghouse_state']['snapshot_time_ms']}")
    for w in manifest.get("warnings", []):
        print(f"  WARNING: {w}")


# ---------------------------------------------------------------------------
# Global (address-independent) cache: perpDexs, meta
# ---------------------------------------------------------------------------

def _fetch_global(client: InfoClient, refresh: bool) -> dict:
    manifest_path = config.GLOBAL_CACHE_DIR / "manifest.json"
    if manifest_path.exists() and not refresh:
        return {
            "perp_dexs": _read_json(config.GLOBAL_CACHE_DIR / "perp_dexs.json"),
            "meta": _read_json(config.GLOBAL_CACHE_DIR / "meta.json"),
        }
    perp_dexs = client.post({"type": "perpDexs"})
    meta = client.post({"type": "meta", "dex": ""})
    _write_json(config.GLOBAL_CACHE_DIR / "perp_dexs.json", perp_dexs)
    _write_json(config.GLOBAL_CACHE_DIR / "meta.json", meta)
    _write_json(manifest_path, {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "fetched_at_utc": _now_utc_iso(),
        "perp_dexs_count": len(perp_dexs) if isinstance(perp_dexs, list) else None,
        "meta_universe_size": (
            len(meta.get("universe", [])) if isinstance(meta, dict) else None
        ),
    })
    return {"perp_dexs": perp_dexs, "meta": meta}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def fetch_all_for_address(address: str, refresh: bool = False) -> dict:
    """Fetch (or load from cache) everything for one address plus the global
    metadata. Returns a dict of raw data; the durable contract is the
    on-disk cache under data/raw/. Prints a per-type summary and re-emits any
    persisted completeness warnings — on cache hits too.
    """
    normalized = normalize_address(address)
    client = InfoClient()
    manifest_path = _address_dir(normalized) / "manifest.json"

    print(f"Fetching Hyperliquid data for {normalized}"
          f"{' (--refresh)' if refresh else ''}:")

    if manifest_path.exists() and not refresh:
        manifest = _read_json(manifest_path)
        data = _load_cached_address(normalized, manifest)
        _print_address_summary(manifest, from_cache=True)
    else:
        data, manifest = _fetch_address_fresh(client, normalized)
        _persist_address(normalized, data, manifest)
        _print_address_summary(manifest, from_cache=False)

    global_data = _fetch_global(client, refresh)
    data.update(global_data)

    print(f"  ({client.request_count} API call(s) this run)")
    return data


def _cli() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch (and cache) one address's Hyperliquid history."
    )
    parser.add_argument("address", help="Hyperliquid wallet address (0x...)")
    parser.add_argument(
        "--refresh", action="store_true",
        help="bypass the cache and re-fetch everything from the API",
    )
    args = parser.parse_args()
    try:
        fetch_all_for_address(args.address, refresh=args.refresh)
    except BadAddressError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except InfoClientError as exc:
        print(f"ERROR: Hyperliquid API request failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
