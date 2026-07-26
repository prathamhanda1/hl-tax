"""
webapp/server.py — Phase 8B. The connection layer: the engine over HTTP.

    uvicorn webapp.server:app --reload        # http://127.0.0.1:8000/

This module is a THIN WRAPPER. It owns exactly three things — routing, status
codes, and abuse control — and nothing else. Every number it serves comes from
`webapp.pipeline.build_result`, which in turn comes from the unchanged engine
(fetch -> load -> reconstruct -> fx -> assemble -> interpret). There is no tax
logic here, no arithmetic, and no reshaping of the payload: whatever Phase A
produced is what goes on the wire.

READ-ONLY, ALWAYS. The only account input any route accepts is a PUBLIC wallet
address. There is no parameter here — and never will be — for a private key,
seed phrase or exchange API secret.

THE MENTAL MODEL, in one paragraph. The browser cannot run Python. It can only
make HTTP requests and paint HTML. This process stays running, imports the
repo's functions, and answers `GET /api/report?address=0x…` with JSON. The
dashboard at `/` fetches that URL and draws it. The connection between the
frontend and this repo is that one endpoint; there is nothing else to it.

STATUS CODES are the whole contract with the dashboard, which branches on them
(index.html: 400 -> "invalid", 422 -> "fx", 500 -> "server", else "network"):

    400  bad address, unparseable as-of, or a malformed query value
    422  FxError — the engine refusing to price recent events rather than
         emit a partial tax number. Carries the as-of hint from main.py:100-108.
    429  rate limited (public endpoint fronting a third-party API)
    503  too many concurrent pipeline runs
    500  anything unforeseen, with the class name and no stack trace
    200 + {"empty": true}  a valid address with no in-window activity. NOT an
         error: the payload keeps its full zeroed shape so the dashboard's
         empty state needs no special-casing.

Every error body carries a `detail` string, because the page reads
`body.detail || body.message` and nothing else.

FAILING LOUDLY (doctrine #4) SURVIVES THE HTTP BOUNDARY. Nothing here degrades
an engine refusal into a soothing partial answer: an FxError becomes a 422 that
says which dates are missing and how to fix it, never a zero. The one thing
this layer is allowed to hide is an unexpected exception's traceback, which is
logged server-side and reported as a class name.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

import config
from webapp.pipeline import (
    DEFAULT_PREVIEW_ROWS,
    LEDGER_PREVIEW_COLUMNS,
    TREATMENT_ORDER,
    build_result,
    ca_report_bundle,
    ca_report_fys,
    normalize_as_of,
)

log = logging.getLogger("webapp.server")

STATIC_DIR = Path(__file__).resolve().parent / "static"

# The address the dashboard offers as its example chip. Public, and already the
# sample address used throughout the docs and the cached fixtures.
EXAMPLE_ADDRESS = "0x4964f89307d74519f8302c4b9695f3a80c2098ef"


# ---------------------------------------------------------------------------
# Configuration — every knob is an environment variable with a sane local
# default, so `uvicorn webapp.server:app` works with nothing set.
# ---------------------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        log.warning("%s=%r is not an integer; using %d", name, raw, default)
        return default


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


# Comma-separated origins, or "*" while developing. Tighten at deploy: this
# endpoint spends someone else's API quota on every miss.
CORS_ORIGINS = [
    o.strip() for o in os.environ.get("HL_TAX_CORS_ORIGINS", "*").split(",")
    if o.strip()
] or ["*"]

# A report is a network fetch plus a pandas pipeline: expensive, and blocking.
# Cap how many run at once so a burst queues (503) instead of exhausting the
# threadpool and stalling every other route, including the static page.
MAX_CONCURRENT_REPORTS = _env_int("HL_TAX_MAX_CONCURRENT_REPORTS", 4)

# Per-IP sliding window on /api/report. 0 disables it entirely.
RATE_LIMIT_PER_MIN = _env_int("HL_TAX_RATE_LIMIT_PER_MIN", 20)

# refresh=true bypasses the cache and re-pulls the full history from the venue.
# Fine locally; a free gift to an abuser on a public URL. Off -> the parameter
# is accepted and ignored, and the payload says so via meta.refresh.
ALLOW_REFRESH = _env_flag("HL_TAX_ALLOW_REFRESH", True)

# Ceiling on the caller-requested row cap for the table payloads.
MAX_PREVIEW_ROWS = _env_int("HL_TAX_MAX_PREVIEW_ROWS", 5000)


# ---------------------------------------------------------------------------
# Abuse control. Deliberately in-process and dependency-free: one small server
# is the deployment target, and a Redis for a rate limiter would be a heavier
# lie about this thing's scale than the limiter is worth.
# ---------------------------------------------------------------------------

_REPORT_SLOTS = threading.BoundedSemaphore(max(1, MAX_CONCURRENT_REPORTS))

_rate_lock = threading.Lock()
_rate_hits: dict[str, deque] = {}


def _client_ip(request: Request) -> str:
    """Best-effort client identity. Behind a proxy (Render, Railway, nginx) the
    socket peer is the proxy, so X-Forwarded-For's first hop is used when
    present. Spoofable — this is a courtesy limiter, not a security control."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _rate_limit(request: Request) -> None:
    """Sliding 60-second window per IP. Raises 429 with a Retry-After."""
    if RATE_LIMIT_PER_MIN <= 0:
        return
    ip, now = _client_ip(request), time.monotonic()
    with _rate_lock:
        hits = _rate_hits.setdefault(ip, deque())
        while hits and now - hits[0] > 60.0:
            hits.popleft()
        if len(hits) >= RATE_LIMIT_PER_MIN:
            retry = int(60.0 - (now - hits[0])) + 1
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit: {RATE_LIMIT_PER_MIN} analyses per minute "
                       f"per address of origin. Try again in {retry}s.",
                headers={"Retry-After": str(retry)},
            )
        hits.append(now)
        if len(_rate_hits) > 4096:      # unbounded dict = a slow memory leak
            for k in [k for k, v in _rate_hits.items() if not v or
                      now - v[-1] > 300.0]:
                _rate_hits.pop(k, None)


# ---------------------------------------------------------------------------
# App.
# ---------------------------------------------------------------------------

app = FastAPI(
    title="hl-tax-engine API",
    version="8B",
    description=(
        "Indian income-tax position of Hyperliquid perpetuals, computed under "
        "four legal readings side by side and ranked under none. Read-only: "
        "a public wallet address is the only account input. Not tax advice."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,     # no cookies, no auth: nothing to credential
    allow_methods=["GET"],       # every route is a read
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError):
    """FastAPI's own 422 would collide with the FX-refusal 422 the dashboard
    branches on, and its detail is a list of dicts the page would render as
    '[object Object]'. Malformed input is a 400 with one readable sentence."""
    first = (exc.errors() or [{}])[0]
    loc = ".".join(str(p) for p in first.get("loc", ()) if p != "query")
    msg = first.get("msg", "invalid request")
    return JSONResponse(
        status_code=400,
        content={"detail": f"Invalid value for '{loc or 'query'}': {msg}."},
    )


@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException):
    """Same shape as FastAPI's default, kept explicit so `detail` is guaranteed
    to be a string on every error path the dashboard can reach."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": str(exc.detail)},
        headers=getattr(exc, "headers", None),
    )


# ---------------------------------------------------------------------------
# /api/report — the one endpoint that matters.
# ---------------------------------------------------------------------------

@app.get("/api/report", response_model=None, summary="Full four-reading report")
async def api_report(
    request: Request,
    address: str = Query(..., description="public Hyperliquid address (0x…)"),
    refresh: bool = Query(False, description="bypass the raw cache and re-fetch"),
    as_of: str | None = Query(
        None, description="bound analysis to events on/before this UTC date "
                          "(YYYY-MM-DD)"),
    onramp_cost: float | None = Query(
        None, description="your INR cost of acquiring the USDC (manual-input "
                          "slot; extends the VDA chain back past the venue)"),
    salary_income_inr: float | None = Query(
        None, description="salary income, for the OP 4 set-off display"),
    other_head_income_inr: float | None = Query(
        None, description="other-head income, for the OP 4 set-off display"),
    preview_rows: int | None = Query(
        None, ge=1, le=MAX_PREVIEW_ROWS,
        description=f"row cap on the table payloads (default "
                    f"{DEFAULT_PREVIEW_ROWS}); truncation is always reported"),
    line_items: bool = Query(
        False, description="include per-treatment line items (4x the ledger)"),
) -> JSONResponse:
    """Run the whole pipeline for one public address and return the payload
    documented in docs/frontend_phaseA_updates.md § 'Payload keys emitted'.

    Nothing is computed here. This function validates, delegates to
    `build_result`, and translates the two exceptions the engine raises by
    design into the two status codes the dashboard understands.
    """
    from fetch.fetch_user import BadAddressError
    from fx.fx import FxError

    _rate_limit(request)

    # Parse before spending a semaphore slot or a network call on it.
    try:
        normalize_as_of(as_of)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Could not read as_of={as_of!r}: expected YYYY-MM-DD "
                   f"({exc}).",
        ) from exc

    if refresh and not ALLOW_REFRESH:
        # Say so rather than pretending: meta.refresh in the payload will read
        # false, and the caller is told why here.
        log.info("refresh requested but disabled by HL_TAX_ALLOW_REFRESH")
    effective_refresh = bool(refresh) and ALLOW_REFRESH

    if not _REPORT_SLOTS.acquire(blocking=False):
        raise HTTPException(
            status_code=503,
            detail=f"{MAX_CONCURRENT_REPORTS} analyses are already running. "
                   f"This is one small server talking to a public API — please "
                   f"retry in a few seconds.",
            headers={"Retry-After": "5"},
        )
    try:
        payload = await run_in_threadpool(
            build_result,
            address,
            refresh=effective_refresh,
            as_of=as_of,
            onramp_cost=onramp_cost,
            salary_income_inr=salary_income_inr,
            other_head_income_inr=other_head_income_inr,
            preview_rows=preview_rows or DEFAULT_PREVIEW_ROWS,
            include_line_items=line_items,
        )
    except BadAddressError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FxError as exc:
        # The engine refused to price events it has no rate for. Hand back the
        # same two fixes main.py prints, with the exact date to bound to.
        raise HTTPException(status_code=422, detail=_fx_detail(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:                      # noqa: BLE001 — last resort
        log.exception("unhandled failure building report for %r", address)
        raise HTTPException(
            status_code=500,
            detail=f"The engine failed while building this report "
                   f"({type(exc).__name__}). This is a bug, not a tax "
                   f"position — nothing partial has been reported.",
        ) from exc
    finally:
        _REPORT_SLOTS.release()

    if refresh and not ALLOW_REFRESH:
        payload.setdefault("warnings", []).append(
            "refresh=true was ignored: live re-fetching is disabled on this "
            "deployment (HL_TAX_ALLOW_REFRESH=0). Figures come from the "
            "server's cached raw history.")

    # Starlette's JSONResponse renders with allow_nan=False, so a NaN leaking
    # out of Phase A would raise here rather than emit invalid JSON that
    # JSON.parse then rejects in the browser. That is the behaviour we want.
    return JSONResponse(content=payload)


def _fx_coverage_end() -> str | None:
    """Last date the default FX series covers — the date to suggest as `as_of`.
    Best effort: never let a hint-builder break an error path."""
    try:
        from fx.fx import default_converter
        return str(default_converter().series.max_date())
    except Exception:                             # noqa: BLE001
        return None


def _fx_detail(exc: Exception) -> str:
    """The 422 body: what the engine refused to do, and the two ways out —
    the same pair main.py:100-108 prints to a CLI user."""
    end = _fx_coverage_end()
    bound = f"as_of={end}" if end else "as_of=<a date the series covers>"
    return (
        f"FX coverage error — refusing to produce a partial tax number. {exc} "
        f"Fix one of: (1) refresh the FX series on the server "
        f"(python -m fx.fetch_fx --update); or (2) bound the analysis by "
        f"setting {bound} under Advanced inputs."
    )


# ---------------------------------------------------------------------------
# CA report (Phase 9) — the financial-year CSV bundle.
#
# Both routes run the same engine prefix as /api/report and then hand off to
# present.ca_report. No report logic lives here: this layer picks status codes
# and nothing else.
# ---------------------------------------------------------------------------

CA_SHEET_COUNT = 7


@app.get("/api/ca-report/fys", response_model=None,
         summary="Financial years available for the CA bundle")
async def api_ca_report_fys(
    request: Request,
    address: str = Query(..., description="public Hyperliquid address (0x…)"),
) -> JSONResponse:
    """The years this address has any activity in, most recent first.

    An empty list is a TRUE answer (this address traded in no financial year),
    not a failure, and it is deliberately distinguishable from a failure: the
    modal branches on `available`, so it can say "no activity" and "could not
    look this up" as the different things they are.
    """
    from fetch.fetch_user import BadAddressError
    from fx.fx import FxError

    _rate_limit(request)
    if not _REPORT_SLOTS.acquire(blocking=False):
        raise HTTPException(status_code=503, detail="Server busy; retry.",
                            headers={"Retry-After": "5"})
    try:
        fys = await run_in_threadpool(ca_report_fys, address)
    except BadAddressError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FxError as exc:
        raise HTTPException(status_code=422, detail=_fx_detail(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:                      # noqa: BLE001
        log.exception("ca-report/fys failed for %r", address)
        raise HTTPException(
            status_code=500,
            detail=f"Could not list financial years for this address "
                   f"({type(exc).__name__}).",
        ) from exc
    finally:
        _REPORT_SLOTS.release()

    return JSONResponse(content={
        "fys": list(fys), "available": True, "detail": None})


@app.get("/api/ca-report", response_model=None,
         summary="CA report bundle (ZIP of CSVs) for one financial year")
async def api_ca_report(
    request: Request,
    address: str = Query(..., description="public Hyperliquid address (0x…)"),
    fy: str = Query(..., description="financial year, e.g. 2025-26"),
    onramp_cost: float | None = Query(
        None, description="your INR cost of acquiring the USDC. Recorded in the "
                          "notes sheet; used in none of the arithmetic"),
):
    """The ZIP, streamed from memory.

    A well-formed year with no activity is NOT a 404: it returns a valid bundle
    whose sheets are headers-only and whose summary says the window is empty. A
    missing file reads as a broken tool; an empty one reads as the true
    statement it is. Only an unparseable year label is a 400.
    """
    from fetch.fetch_user import BadAddressError
    from fx.fx import FxError
    from fastapi.responses import Response
    from present.ca_report import FyFormatError

    _rate_limit(request)
    if not _REPORT_SLOTS.acquire(blocking=False):
        raise HTTPException(status_code=503, detail="Server busy; retry.",
                            headers={"Retry-After": "5"})
    try:
        blob, name = await run_in_threadpool(
            ca_report_bundle, address, fy, onramp_cost=onramp_cost)
    except FyFormatError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except BadAddressError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FxError as exc:
        raise HTTPException(status_code=422, detail=_fx_detail(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:                      # noqa: BLE001
        log.exception("ca-report failed for %r fy=%r", address, fy)
        raise HTTPException(
            status_code=500,
            detail=f"The engine failed while building the {fy} bundle "
                   f"({type(exc).__name__}). Nothing partial has been "
                   f"reported.",
        ) from exc
    finally:
        _REPORT_SLOTS.release()

    return Response(
        content=blob, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---------------------------------------------------------------------------
# Introspection routes. Cheap, no network, safe to poll.
# ---------------------------------------------------------------------------

@app.get("/api/health", summary="Liveness probe")
async def api_health() -> dict:
    return {"status": "ok", "service": "hl-tax-engine", "version": app.version}


@app.get("/api/meta", summary="Server conventions and limits")
async def api_meta() -> dict:
    """What this deployment will and will not do — the conventions the numbers
    are computed under, and the operational limits, in one place. Useful in a
    browser tab when a figure looks surprising."""
    return {
        "service": "hl-tax-engine",
        "version": app.version,
        "example_address": EXAMPLE_ADDRESS,
        "treatment_order": list(TREATMENT_ORDER),
        "ledger_columns": list(LEDGER_PREVIEW_COLUMNS),
        "fx_convention": config.FX_CONVENTION,
        "fx_coverage_end": _fx_coverage_end(),
        "cost_basis_convention": config.COST_BASIS_CONVENTION,
        "ca_report_available": True,
        "ca_report_sheets": CA_SHEET_COUNT,
        "limits": {
            "default_preview_rows": DEFAULT_PREVIEW_ROWS,
            "max_preview_rows": MAX_PREVIEW_ROWS,
            "max_concurrent_reports": MAX_CONCURRENT_REPORTS,
            "rate_limit_per_min": RATE_LIMIT_PER_MIN,
            "refresh_allowed": ALLOW_REFRESH,
        },
        "disclaimer": (
            "Not tax advice. Four readings are computed and none is endorsed. "
            "Read-only: a public address is the only account input."
        ),
    }


# ---------------------------------------------------------------------------
# The dashboard. Mounted LAST: StaticFiles at "/" swallows every unclaimed
# path, so any route declared after it would be unreachable.
# ---------------------------------------------------------------------------

if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True),
              name="dashboard")
else:                                             # pragma: no cover
    log.warning("no static dir at %s — API only, no dashboard", STATIC_DIR)


# ---------------------------------------------------------------------------
# python -m webapp.server  — same as uvicorn, one less thing to remember.
# ---------------------------------------------------------------------------

def _main() -> int:                               # pragma: no cover
    import argparse

    import uvicorn

    ap = argparse.ArgumentParser(description="Serve the hl-tax-engine dashboard.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    ap.add_argument("--reload", action="store_true")
    args = ap.parse_args()

    print(f"dashboard  http://{args.host}:{args.port}/")
    print(f"API docs   http://{args.host}:{args.port}/docs")
    uvicorn.run("webapp.server:app", host=args.host, port=args.port,
                reload=args.reload)
    return 0


if __name__ == "__main__":                        # pragma: no cover
    raise SystemExit(_main())
