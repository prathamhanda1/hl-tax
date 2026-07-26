# Refreshing the FX series (USD/INR FBIL reference rate)

The tool converts every USD event to INR at the **FBIL USD/INR reference rate**
— the benchmark Indian income-tax foreign-currency conversion relies on. The
series lives in `fx/data/usdinr_reference.csv` (+ `manifest.json`) and is a
snapshot that only advances when a human refreshes it. Nothing runs on a
schedule.

**Authority vs transport:** FBIL/RBI is the cited *authority* (the `administrator`
column). Frankfurter's free, open-source **FBIL provider** is only the *retrieval
mechanism* (the `source` column). We never scrape RBI/FBIL portals directly.

## Everyday commands

```
python -m fx.fetch_fx --status     # freshness at a glance (one live upstream check)
python -m fx.fetch_fx --update     # pull new tail rows, validate, append
python -m fx.fetch_fx --verify     # reconcile a sample of stored rows vs upstream
python -m fx.fetch_fx --reseed --force   # rebuild the whole series (rare)
python -m fx.fetch_fx --embed      # rebuild the embedded copy from the CSV (no network)
```

Run `--status` before a filing session. If it says `STALE`, run `--update`.

## Two files, one series (`--embed`)

A refresh writes **two** artifacts, and both must be committed:

| file | role |
|---|---|
| `fx/data/usdinr_reference.csv` | the series — source of truth, human-readable |
| `fx/_series_embedded.py` | a verbatim copy, carried as a Python module |

The second exists purely so the series survives deployment. Vercel's Python
builder bundles the `.py` files it reaches by tracing imports; it does not copy
data files. The CSV was therefore silently dropped from the lambda and every
request came back as a 422 reading *"FX series file not found:
`/var/task/fx/data/usdinr_reference.csv`"* — which looks like a staleness
problem and is not one. (`includeFiles` is the documented cure but is a
`functions` property; under the legacy `builds` config this project uses it is
accepted and ignored.) A module reached by a real `import` is bundled by any
Python packer that works at all.

`--update` and `--reseed` regenerate the module automatically, so the normal
path needs no extra step. Use `--embed` on its own only after hand-editing the
CSV (the manual fallback below) or after a merge that took one side.

At read time the CSV always wins; the module is consulted only when the file is
absent. Locally that never happens. `tests/test_fx.py` fails if the two drift,
so a refresh that commits only the CSV is caught before it can deploy yesterday's
rates while the repo shows today's.

## What `--status` verdicts mean

- **FRESH** — local series matches FBIL's latest. (FBIL itself lags the calendar
  by a few days; that's normal and not stale.)
- **STALE (run --update)** — FBIL has published dates you don't have locally.
- **UPSTREAM-LAGGING / UNKNOWN** — FBIL hasn't published recent dates yet, or the
  provider is unreachable. A refresh won't help; wait or use the manual fallback.

## The reader never guesses

At read time, an event past the local series end by more than
`FX_MAX_STALENESS_DAYS` (7) raises one of:
- `StaleSeriesError` — refresh locally (`--update`).
- `UpstreamNotYetPublishedError` — the rate isn't published anywhere yet; wait
  for the next FBIL publication (~13:30 IST business days) or add it manually.

## Manual fallback (if the provider ever changes or fails)

1. Go to **fbil.org.in → Reference Rates → USD/INR** (the primary authority).
2. Read the rates for the missing `rate_date`s (`--status` reports the range).
3. Append rows to `fx/data/usdinr_reference.csv`:
   ```
   rate_date,rate,source,administrator,fetched_at_utc
   2026-07-17,96.4123,fbil-primary-manual,FBIL,<UTC now>
   ```
4. Run `python -m fx.fetch_fx --verify` to confirm they reconcile.
5. Run `python -m fx.fetch_fx --embed` so the deployed copy carries the new rows
   too. Hand-editing the CSV is the one path that does not regenerate it for you.

## Pre-2018-07-10 dates

FBIL took over the reference rate on **2018-07-10**; before that the authority is
the **RBI** reference rate. Only needed if a trade is older than that. Obtain
those rates once, manually, from RBI's archives and tag them
`source=rbi-primary-seed`, `administrator=RBI`. Never automated.

## Known data note

Frankfurter's FBIL series has one real interior hole: **2021-01-29 → 2021-02-16**
(recorded in `manifest.json` notes). Events in that window walk back to
2021-01-29 and are flagged `fx_stale_grace=True` in the output.

> The FBIL/RBI rate being the correct one *for tax purposes* under each treatment
> (Rule 115 specified-date mechanics) is a question for your CA — see
> `docs/phase4_fx_spec.md`. This pipeline solves sourcing and freshness, not the
> legal choice of rate.
