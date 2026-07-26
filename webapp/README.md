# `webapp/` — the dashboard and the API in front of the engine

The engine (Phases 0–7) computes the Indian income-tax position of a
Hyperliquid account under four legal readings and ranks none of them. This
folder is the shell that puts it in a browser. **It computes nothing.** Every
number on the page comes from `interpret.treatments.interpret`, unchanged.

**Read-only, always.** A public wallet address is the only account input. No
route, field or parameter here accepts a private key, seed phrase or exchange
API secret, and none ever will.

---

## Run it

```bash
pip install -r requirements.txt -r webapp/requirements.txt
uvicorn webapp.server:app --reload
```

| URL | What it is |
|---|---|
| <http://127.0.0.1:8000/> | the dashboard |
| <http://127.0.0.1:8000/docs> | FastAPI's auto-generated API console |
| <http://127.0.0.1:8000/api/meta> | this server's conventions and limits |

`python -m webapp.server --port 8000 --reload` does the same thing and prints
both URLs.

## The mental model, in four lines

1. The browser cannot run Python. It can only make HTTP requests and draw HTML.
2. `uvicorn` keeps this Python process running; it imports the repo's functions.
3. The page asks `GET /api/report?address=0x…`; the server runs the pipeline
   in memory and answers with JSON.
4. The page paints that JSON.

The connection between the frontend and this repo is **that one endpoint**.
There is nothing else to it.

## The endpoints

| Route | Purpose |
|---|---|
| `GET /api/report` | the whole thing. `address`, plus optional `refresh`, `as_of`, `onramp_cost`, `salary_income_inr`, `other_head_income_inr`, `preview_rows`, `line_items` |
| `GET /api/ca-report/fys` | financial years available for the CA bundle — `{"fys": [], "available": false}` until Phase 9 |
| `GET /api/ca-report` | the CA ZIP — `501` until Phase 9 |
| `GET /api/health` | liveness |
| `GET /api/meta` | conventions, FX coverage end, limits |

### Status codes are the contract

The dashboard branches on these, so they are load-bearing:

| Code | Meaning |
|---|---|
| `200` | a report. `{"empty": true}` means a valid address with no in-window activity — a fact, not an error |
| `400` | bad address, unreadable `as_of`, malformed query value |
| `422` | **FX coverage** — the engine refusing to price recent events rather than emit a partial tax number. The body names the missing date and both fixes |
| `429` | rate limited |
| `503` | too many concurrent analyses |
| `500` | a bug. Class name only; the traceback stays in the server log |

Every error body carries a string `detail`, because the page reads
`body.detail || body.message` and nothing else.

### The 422 is not a malfunction

The FBIL USD/INR series publishes on a lag. When an account has events newer
than the series, the engine stops instead of guessing a rate. The fix is one of:

```bash
python -m fx.fetch_fx --update      # pull the newer rates, then re-run
```

or bound the analysis: set **as_of** under Advanced inputs to the last date the
series covers (`/api/meta` → `fx_coverage_end`, and the error body names it).

## Deep links

A report is a URL:

```
/?address=0x4964…98ef&as_of=2026-07-16&onramp_cost=250000
```

`address`, `as_of`, `onramp_cost`, `salary_income_inr`, `other_head_income_inr`
and `refresh=1` are read on load, echoed into the Advanced panel so the reader
can see every input the figures were computed under, and analysed immediately.

`?mock=1` renders the built-in design fixture instead of calling the API —
useful for offline design work, and clearly labelled as MOCK on the page.

## Configuration

All environment variables; all optional.

| Variable | Default | Why you would change it |
|---|---|---|
| `HL_TAX_CORS_ORIGINS` | `*` | tighten to your origin at deploy |
| `HL_TAX_RATE_LIMIT_PER_MIN` | `20` | per-IP limit on `/api/report`; `0` disables |
| `HL_TAX_MAX_CONCURRENT_REPORTS` | `4` | beyond this, callers get `503` rather than a stalled server |
| `HL_TAX_ALLOW_REFRESH` | `1` | set `0` in public deployments: `refresh=true` re-pulls full history from the venue API |
| `HL_TAX_MAX_PREVIEW_ROWS` | `5000` | ceiling on caller-requested table rows |
| `PORT` | `8000` | honoured by `python -m webapp.server` |

The rate limiter and the concurrency cap are in-process and dependency-free.
One small server is the deployment target; a Redis here would be a bigger lie
about this thing's scale than the limiter is worth.

## The frontend

`static/index.html` is a single self-contained page — no npm, no build step.
It is a `dc-runtime` component: `support.js` compiles the `<x-dc>` template and
the class at the bottom of the file, and React 18.3.1 is **vendored** under
`static/vendor/` so nothing is fetched from a CDN.

Two things will bite whoever edits it next:

1. **Do not set `window.__resources`.** It looks like the tidy way to point the
   runtime at the local React copies, and it is also the flag that tells the
   runtime the in-DOM template is authoritative. It is not: the HTML parser
   foster-parents `<sc-for>` out of `<table>`/`<tbody>` at load, so the ledger
   and Schedule-VDA tables silently render **zero rows** while every other panel
   looks perfect. Loading React with plain `<script>` tags avoids both problems
   — `support.js` skips its CDN fetch when `window.React` already exists.
2. **`USE_MOCK` must stay off.** A stale mock rendering invented tax numbers in
   a real-looking dashboard is the worst failure this project can have.
   `tests/test_webapp_server.py` asserts both of these.

## Tests

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_webapp_server.py tests/test_webapp_pipeline.py -q
```

Offline and deterministic: `build_result` is stubbed, so the HTTP tests gate
routing and status codes only. Payload shape is gated by
`tests/test_webapp_pipeline.py`; tax arithmetic by `tests/test_treatments.py`.

## Not built yet

- **Phase 9 (CA report ZIP).** `present/ca_report.py` does not exist. Both
  routes already delegate to it the moment it appears — `_ca_report_module()`
  imports it per call and expects `available_fys(address)` and
  `build_bundle(address, fy)`. Until then the modal renders its "no financial
  years" state from a 200, and the ZIP route answers 501.
- **Phase F (deployment).** No `Dockerfile` yet. When it lands: ship the FX CSV
  in the image, set `HL_TAX_CORS_ORIGINS` and `HL_TAX_ALLOW_REFRESH=0`, and run
  `python -m fx.fetch_fx --update` on a schedule or the 422 becomes permanent.
