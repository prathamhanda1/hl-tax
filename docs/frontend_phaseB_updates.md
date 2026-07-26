# Phase 8B — FastAPI server + dashboard: what changed, and what is left

**Status:** complete and verified end to end in a real browser.
**Plan reference:** `docs/phase8_plan_frontend.md` § "Phase B — FastAPI server".
**Engine files modified:** none. Phase B, like Phase A, is purely additive.

---

## Summary

The engine now has a URL. `uvicorn webapp.server:app` serves the dashboard at
`/` and answers `GET /api/report?address=0x…` with the Phase A payload. Paste
an address, get four readings, TDS, cross-cutting limitations, charts and both
tables — all of it drawn from live engine output.

Phase B also absorbed Phase C in practice: the dashboard was already designed
(`hl frontend tax/HL Tax Engine Portal.dc.html`), so the work was relocating it,
wiring it to the API, and fixing the two things that broke when it moved.

| File | Lines | Purpose |
|---|---|---|
| `webapp/server.py` | 515 | FastAPI app: routes, status codes, abuse control |
| `webapp/static/index.html` | 1652 | the dashboard, relocated and wired to live data |
| `webapp/static/support.js` | 1911 | `dc-runtime`, copied verbatim |
| `webapp/static/vendor/react*.min.js` | — | React 18.3.1 UMD, vendored (no CDN) |
| `webapp/requirements.txt` | 10 | `fastapi`, `uvicorn[standard]`, `httpx` |
| `webapp/README.md` | — | run, configure, deploy, and the two frontend traps |
| `tests/test_webapp_server.py` | 26 tests | offline gate on routing and status codes |

**Test results:** `tests/test_webapp_server.py` → 26 passed. Full suite →
**123 passed, 11 skipped, 1 failed** — the same pre-existing
`test_gate3b_venue_reconciliation[0x4e23…20c3]` data failure documented in
`docs/frontend_phaseA_updates.md`. It imports nothing from `webapp/`.

---

## 1. `webapp/server.py` (new)

A thin wrapper that owns exactly three things: **routing, status codes, and
abuse control**. No tax logic, no arithmetic, and no reshaping — the payload
`build_result` returns is the payload that goes on the wire (asserted by
`test_report_returns_payload_verbatim`).

### Routes

| Route | Behaviour |
|---|---|
| `GET /api/report` | `address`, `refresh`, `as_of`, `onramp_cost`, `salary_income_inr`, `other_head_income_inr`, `preview_rows`, `line_items` → `build_result` → JSON |
| `GET /api/ca-report/fys` | `{"fys": [], "available": false, "detail": …}` until Phase 9 |
| `GET /api/ca-report` | `501` until Phase 9 |
| `GET /api/health` | liveness |
| `GET /api/meta` | conventions, `fx_coverage_end`, limits, disclaimer |
| `/` (mounted last) | `StaticFiles(html=True)` over `webapp/static/` |

### Status-code mapping, as the plan specified

| Raised / condition | Status | Body |
|---|---|---|
| `BadAddressError` | **400** | the engine's own message, verbatim |
| unparseable `as_of` | **400** | rejected *before* the semaphore and the network call |
| malformed query value | **400** | one readable sentence (see below) |
| `FxError` and subclasses | **422** | the refusal, plus both fixes and the concrete date to bound to |
| rate limited | **429** | with `Retry-After` |
| concurrency saturated | **503** | with `Retry-After: 5` |
| anything else | **500** | class name only; traceback stays in the log |
| `empty: true` | **200** | full zeroed payload, not an error |

Three decisions worth recording:

- **FastAPI's native validation 422 is remapped to 400.** It would otherwise
  collide with the FX-refusal 422 the dashboard branches on, and its `detail`
  is a list of dicts that renders as `[object Object]`. The handler flattens it
  to `Invalid value for 'onramp_cost': …`.
- **The 422 body carries the fix, not just the complaint.** It reproduces the
  two options `main.py:100-108` prints to a CLI user and fills in the actual
  coverage end from the FX series, so the message reads
  `… or (2) bound the analysis by setting as_of=2026-07-16 under Advanced
  inputs.` A refusal that does not say how to proceed is a dead end.
- **Failing loudly survives the HTTP boundary.** Nothing degrades an engine
  refusal into a soothing partial answer. The only thing hidden is an
  unexpected traceback.

### Abuse control

This is a public endpoint that spends a third-party API's quota and runs a
pandas pipeline per request, so:

- `threading.BoundedSemaphore(HL_TAX_MAX_CONCURRENT_REPORTS, default 4)` — a
  burst gets `503` instead of exhausting the threadpool and stalling the static
  page too. Released in a `finally`, gated by `test_slot_is_released_after_a_failure`.
- A per-IP 60-second sliding window (`HL_TAX_RATE_LIMIT_PER_MIN`, default 20),
  honouring `X-Forwarded-For` behind a proxy, with a bounded-size dict so the
  limiter cannot become a slow memory leak.
- `HL_TAX_ALLOW_REFRESH=0` makes `refresh=true` a no-op **and says so** in
  `warnings[]` rather than pretending it re-fetched.
- `build_result` runs via `run_in_threadpool`: it is blocking I/O plus pandas,
  and the event loop must stay free to serve the page.

All in-process and dependency-free. One small server is the deployment target.

---

## 2. The frontend, relocated and wired

`hl frontend tax/HL Tax Engine Portal.dc.html` → `webapp/static/index.html`,
with `support.js` beside it. The originals are left untouched.

Three edits to the page, and nothing else:

1. **Live by default.**
   ```js
   API_BASE = window.HL_TAX_API_BASE || "";   // same origin
   USE_MOCK = /[?&]mock=1\b/.test(location.search);
   ```
   The design fixture is kept — it is what the layout was built against — but
   it now takes a deliberate `?mock=1` to reach. A stale mock rendering invented
   tax numbers in a real-looking dashboard is the worst failure this project can
   have, so the default had to be live.

2. **React vendored, not fetched.** Two `<script>` tags load
   `./vendor/react*.min.js` before `support.js`, which skips its unpkg fetch
   when `window.React` already exists. The page works offline, behind a strict
   CSP, and on a host that blocks third-party script origins.

3. **Deep links.** `readQuery()` reads `address`, `as_of`, `onramp_cost`,
   `salary_income_inr`, `other_head_income_inr`, `refresh` from the query
   string, echoes each into the Advanced panel (opening it when non-empty, so
   no figure is ever computed under an input the reader cannot see), and runs
   the analysis. A report is now a shareable URL — and a headless-testable one.

### The bug this phase nearly shipped

The tidy-looking way to point `dc-runtime` at local React is
`window.__resources = {…}`. It works — and it also silently emptied both data
tables.

`window.__resources` is the runtime's "this is a bundled environment" flag.
Unset, the runtime **re-fetches the page's own raw source** and re-parses the
template from text. That behaviour is not an optimisation: the HTML parser
foster-parents unknown elements out of `<table>`/`<tbody>` when the document
loads, so the `<sc-for>` loops that emit ledger rows are torn out of the table
before any JavaScript runs. The re-fetch is how the runtime recovers the true
template.

Symptom: the dashboard renders perfectly — hero number, four cards, charts, TDS
panel, the label *"showing 353 of 353 ledger events"* — under a table with
**zero rows**. Nothing errors. Caught only by dumping the rendered DOM and
counting `<tr>`: 0 with the flag set, 354 (353 + header) without it.

Both halves are now regression-tested (`test_react_is_vendored_and_resources_flag_is_not_set`)
and written up in `webapp/README.md`, because the next person to touch this file
will reach for `__resources` for exactly the same good reason.

---

## 3. `tests/test_webapp_server.py` (new, 26 tests, all passing)

Offline and deterministic: `build_result` is replaced by a spy in every test
that reaches it, so nothing touches the network, the raw cache or the FX series.
This file gates **routing and status codes** — the numbers are gated by
`tests/test_webapp_pipeline.py`, the tax logic by `tests/test_treatments.py`.

What it gates: every row of the status table above; parameter pass-through for
all six advanced inputs; `empty: true` staying a 200; `detail` being a non-empty
string on every reachable error path; the FX 422 carrying both fixes; no
traceback in a 500; the rate limiter (on, off, `Retry-After`); `503` on
saturation and no slot leak after a failure; `refresh` disclosed when ignored;
the static page and its assets serving; `USE_MOCK` off and React vendored; and
a contract test asserting every query parameter the page can send is one the
endpoint actually declares (read from `/openapi.json`, so it cannot drift).

---

## Verification performed

Beyond the test suite, the page was rendered in headless Chrome against the
running server — the check that would have caught the empty-tables bug, and the
only one that proves the frontend and the engine actually meet.

```bash
uvicorn webapp.server:app --port 8077
chrome --headless=new --virtual-time-budget=20000 --dump-dom \
  "http://127.0.0.1:8077/?address=0x4964…98ef&as_of=2026-07-16"
```

| Check | Result |
|---|---|
| payload | 192,849 bytes of strictly valid JSON |
| VDA (OP 1) | base ₹5,733.11 · tax ₹1,788.73 — matches Phase A exactly |
| OP 2 / 3 / 4 | base −₹6,435.98 · tax ₹0.00 (a loss year) |
| rendered figures | ₹1,788.73, ₹5,733.11, −₹6,435.98 all present in the DOM |
| ledger table | **354 `<tr>`** = 353 rows + header, with `fx_rate` and `fx_rate_date` per row |
| Schedule VDA | 72 episodes |
| template compilation | `sc-if` / `sc-for` count in rendered DOM: **0** (all compiled) |
| CDN requests | **0** — `unpkg.com` appears nowhere in the rendered document |
| data-source label | "data source · live /api/report" |
| console | no errors |

Error states were rendered too. Unbounded (no `as_of`), the same address
produces the designed FX panel: **"422 · FX COVERAGE — The FX series does not
yet cover your most recent events"**, the engine's own message quoted below it,
and a date picker with a **RE-RUN BOUNDED** button. The body names
`2026-07-22` (the event it refused to price), `python -m fx.fetch_fx --update`,
and `as_of=2026-07-16` (the actual coverage end). `0xdead` is rejected client
side and, if it reaches the server, as a 400.

---

## What is left

### Phase 9 — CA report bundle
`present/ca_report.py` does not exist. Both routes already delegate to it the
moment it appears: `_ca_report_module()` imports it per call and expects
`available_fys(address)` and `build_bundle(address, fy) -> bytes`. No edit to
`server.py` is required when Phase 9 lands. Until then the modal renders its
"no financial years" state from a 200 (not a red failure for a feature that
simply is not built), and the ZIP route answers 501.

### Phase F — deployment
No `Dockerfile` yet. When it is written: ship the FX CSV in the image, set
`HL_TAX_CORS_ORIGINS` to the deployed origin, set `HL_TAX_ALLOW_REFRESH=0`, and
schedule `python -m fx.fetch_fx --update` — without it the FX series goes stale
and every unbounded request becomes a permanent 422.

### Phase D — charts
Already present in the designed page (treatment comparison bar, gross-vs-net
waterfall, event-composition donut, per-episode P&L strip) and confirmed
drawing from live data in the render above. Nothing outstanding.

### Not done, deliberately
`refresh=true` is honoured by default, which means one request can trigger a
full history re-pull. That is right for local use and wrong for a public URL;
the switch exists (`HL_TAX_ALLOW_REFRESH=0`) and belongs in the Phase F deploy
checklist rather than as a local default.
