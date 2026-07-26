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
```

Run `--status` before a filing session. If it says `STALE`, run `--update`.

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
