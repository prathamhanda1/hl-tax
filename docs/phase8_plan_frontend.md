# Phase 8 — Frontend Dashboard Plan (web UI for the Hyperliquid Indian-tax engine)

## Context

The engine (Phases 0–7) is **done**: a 6-layer Python pipeline (fetch → load → reconstruct → fx → interpret → present) that takes one **public Hyperliquid wallet address** and computes Indian tax under **four legal treatments side by side**, plus TDS and cross-cutting caveats. Today it is CLI-only (`python main.py <address>`) and emits a static `summary.html` + two CSVs to `data/reports/<address>/`. There is **no web server and no live input box**.

The goal: a **public web dashboard** where anyone pastes an HL address and instantly gets a super-informative, "fintech-terminal" visual readout of everything the engine computes.

**Chosen shape:** FastAPI JSON API wrapping the existing pure functions + a single-page dark dashboard that `fetch()`es it. Public deployment from the start.

### The one idea that makes this easy
`interpret.treatments.interpret(ledger, Assumptions())` already returns the **entire result as a Python dict** (keys: `vda`, `futures_inr`, `speculative`, `non_speculative`, `tds`, `cross_cutting`). The pipeline functions are pure (data in, data out, no hidden state). So the backend is a thin wrapper: **run the existing pipeline in-memory, turn the dict into JSON, send it to the browser.** No tax logic is rewritten.

### How a frontend connects to this repo (plain-English mental model)
- The **browser** cannot run Python. It can only make HTTP requests and draw HTML/JS.
- The **FastAPI server** is a small Python program that stays running. It imports the repo's functions. When the browser asks `GET /api/report?address=0x...`, the server runs the pipeline and returns JSON.
- The **dashboard page** (`index.html` + JS) takes the address the user typed, calls that URL, receives JSON, and paints cards/charts.
- Connection = **one HTTP endpoint**. Nothing more mysterious than that.

---

## Architecture (target)

```
browser (dark dashboard, index.html + app.js)
      │  fetch("/api/report?address=0x…")
      ▼
FastAPI server (webapp/server.py)   ← imports existing repo modules
      │  calls run-in-memory pipeline (a new pure helper), returns JSON
      ▼
existing engine: fetch → load → reconstruct → fx → interpret()  (UNCHANGED)
```

New code lives in a new top-level `webapp/` folder. **No existing engine file is modified** except optionally adding one thin helper.

---

## Phase-by-phase steps (each phase is independent and testable on its own)

### Phase A — In-memory result helper (the API's single dependency)
**Goal:** one function that returns the results dict for an address, computing nothing new — mirrors `main.run()` but returns data instead of writing files.

- Add `webapp/pipeline.py` with `build_result(address, *, refresh=False, as_of=None, onramp_cost=None) -> dict`.
- It reuses the exact calls from `main.py:50-133`: `normalize_address` → `fetch_all_for_address` → `load_raw` → `reconstruct_positions`/`reconstruct_funding` → `build_inr_ledger` → `interpret`. Reuse `main._apply_cutoff` logic (copy the small filter or import it).
- Return a JSON-safe dict: `{ meta, treatments: {vda, futures_inr, speculative, non_speculative}, tds, cross_cutting, ledger_preview, schedule_vda, warnings }`.
- **Serialization is the only real work:** the `interpret()` dict contains pandas DataFrames (`line_items`), numpy floats, and `Timestamp`s. Add a `_to_jsonable()` that: DataFrame → list of row dicts (`df.to_dict("records")`), numpy scalars → Python `float/int`, `Timestamp`/`NaT` → ISO string / `None`, `NaN` → `None`. Reuse the display formatters in `present/report.py` (`fmt_inr`, `fmt_usd`, `_fmt_date`) for any pre-formatted strings you want to ship alongside raw numbers.
- **Verify:** `python -c "from webapp.pipeline import build_result; import json; print(json.dumps(build_result('0x4964f89307d74519f8302c4b9695f3a80c2098ef'))[:500])"` — must print valid JSON, no `TypeError: not serializable`.

### Phase B — FastAPI server (the connection layer)
**Goal:** expose the helper over HTTP + serve the static page.

- Add `webapp/server.py` with FastAPI:
  - `GET /api/report?address=&refresh=&as_of=&onramp_cost=` → calls `build_result`, returns JSON.
  - Validate the address with the existing `config.ADDRESS_RE`; return HTTP 400 with a clean message on bad input (catch `BadAddressError`).
  - Map engine failures to clean JSON errors: `FxError` → 422 with the "refresh FX / add as-of" hint from `main.py:100-108`; empty activity → 200 with an `empty: true` flag.
  - Mount the dashboard: `app.mount("/", StaticFiles(directory="webapp/static", html=True))`.
  - Enable CORS (permissive for now; tighten at deploy).
- Add `webapp/requirements.txt`: `fastapi`, `uvicorn[standard]` (on top of repo `requirements.txt`).
- **Verify:** `uvicorn webapp.server:app --reload`, open `http://127.0.0.1:8000/docs` (FastAPI's auto UI), run the `/api/report` endpoint with the sample address, confirm JSON. This is your backend proof before any design work.

### Phase C — Static dashboard shell (dark fintech terminal)
**Goal:** the visual skeleton, wired to the API, before polishing.

- Add `webapp/static/index.html`, `app.js`, `styles.css` (self-contained; no build step, no npm — plain HTML/CSS/JS so it's easy to host and reason about).
- Layout, top → bottom:
  1. **Address bar** — big input + "Analyze" button + example-address chip. Loading state = animated terminal-style shimmer.
  2. **Header strip** — address (truncated 0x1234…abcd), event range, generated-at, FX convention (from `meta`).
  3. **Hero number** — the VDA **gross-vs-net gap** (`vda_gross_vs_net_gap_inr`), the engine's own "single most shocking number," as a large animated counter.
  4. **Four treatment cards side by side** — one per treatment, glowing panels: label, total tax (big), taxable base, rate basis, loss treatment, carry-forward, an `audit_flag` warning pill, and a `COUNTERFACTUAL` badge when `premise == "counterfactual"`. Cards expand to show `caveats[]` and `section_refs`.
  5. **TDS panel** — expected vs actually-deducted, the four per-reading expectations.
  6. **Cross-cutting / limitations panel** — `limitations_top[]`, disclosure checklist, assumptions box, on-ramp boundary. (Neutrality is mandatory: never label a treatment "best" — mirror `present/report.py:22-24`.)
  7. **Ledger + Schedule-VDA tables** — scrollable, with CSV-download buttons.
- **Verify:** type the sample address, confirm every panel fills from live JSON with no console errors.

### Phase D — Data visualization pass (make it "super informative")
**Goal:** the charts that carry the insight.

- **Treatment comparison bar** — total tax across the four treatments (counterfactual bar visually de-emphasized).
- **Gross-vs-net waterfall/bar** for VDA — shows losses that don't offset.
- **Category donut** — ledger event mix (realized_pnl / funding / fee / …) from `ledger_counts`.
- **Per-episode P&L strip** — from `schedule_vda`, green/red income per episode.
- Use lightweight inline SVG or a single small charting lib embedded locally (no external CDN if we later serve under a strict host). Theme-aware dark palette.
- **Verify:** charts match the numbers in the cards; resize doesn't break layout; empty-account case degrades gracefully.

### Phase E — Robustness & UX states
**Goal:** handle the real-world inputs the engine already guards against.

- Client + server states for: invalid address, unknown/empty address (`empty: true`), FX-coverage error (surface the as-of hint + a date picker), slow first fetch (the network layer paginates — show progress copy).
- Cache awareness: the engine caches raw pulls under `data/raw/<address>/`; expose a "refresh" toggle that sets `?refresh=true`.
- **Verify:** exercise each: `0xdead` (400), a zero-activity address, a normal address, refresh on/off.

### Phase F — Public deployment
**Goal:** a shareable URL.

- Add `webapp/Dockerfile` (python:3.11-slim, install both requirements files, `uvicorn webapp.server:app --host 0.0.0.0 --port $PORT`).
- Deploy to a free/cheap host (Render or Railway — both take a Dockerfile or a start command directly).
- **Persistent FX + cache:** the engine reads a local FX CSV (`fx/data/`) and writes caches to `data/`. On ephemeral hosts, ship the FX CSV in the image and mount a small persistent disk (or accept per-request re-fetch). Document `python -m fx.fetch_fx --update` in the deploy runbook.
- Tighten CORS to the deployed origin; add a simple per-IP rate limit (public endpoint hitting a third-party API).
- Add a visible **disclaimer banner** (not tax advice; four readings shown, none endorsed) — matches the engine's neutrality mandate and is important for a public tool.
- **Verify:** open the public URL on a phone, analyze the sample address end to end.

### Phase G — Docs & handoff
- Add `webapp/README.md`: local run (`uvicorn …`), the one-endpoint mental model, deploy steps, how to refresh FX.

---

## Files to be created (all new; engine untouched)
- `webapp/pipeline.py` — in-memory `build_result()` + JSON serialization (Phase A)
- `webapp/server.py` — FastAPI app + static mount (Phase B)
- `webapp/static/index.html`, `app.js`, `styles.css` — dashboard (Phases C–E)
- `webapp/requirements.txt`, `webapp/Dockerfile`, `webapp/README.md`

## Files reused (read, not modified)
- `main.py:50-146` — the exact pipeline order to mirror in `build_result`
- `interpret/treatments.py` — `interpret()`, `Assumptions`, the result-dict schema
- `interpret/assemble.py` — `build_inr_ledger`, INR ledger columns
- `present/report.py` — `fmt_inr`/`fmt_usd`/`schedule_vda_frame` display helpers to reuse
- `config.py` — `ADDRESS_RE`, tax constants, decimals for display
- `present/templates/summary.html.j2` — the existing presentation to mirror in content/ordering

## End-to-end verification
1. `pip install -r requirements.txt -r webapp/requirements.txt`
2. `uvicorn webapp.server:app --reload` → `/docs` returns JSON for the sample address (backend proven).
3. Open `http://127.0.0.1:8000/` → paste sample address → every panel + chart fills.
4. Error cases: `0xdead` (400), empty address (empty state), FX error (as-of hint).
5. Deploy; confirm the public URL renders the sample address on mobile.
6. Run existing tests to confirm the engine is untouched: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest` (per machine note).
