# hl-tax-engine

> **READ-ONLY. This tool NEVER asks for a private key, API secret, or seed
> phrase. Ever.** It reads public on-chain data for a public wallet address.
> There is no field, flag, or prompt anywhere in this software that accepts a
> key. If any copy of this software ever asks you for one, it is compromised —
> do not use it.

An open-source Indian tax + P&L engine for Hyperliquid perpetuals. Give it a
public wallet address; it produces an INR-denominated P&L ledger and the tax
position computed under each of the competing interpretations of Indian law,
side by side — so you can take a real document to a CA instead of guessing.

## This is a calculator, not tax advice — read this first

- **It computes arithmetic under stated assumptions. It does not pick a
  "correct" treatment.** The tax treatment of a USDC-settled foreign-DEX
  perpetual is genuinely unsettled in India. This tool computes the numbers
  under each reading and lets you and a practising CA decide. Nobody who built
  this is a tax advisor.
- **The on-ramp/off-ramp is invisible to it.** The tool sees on-Hyperliquid
  activity only. It **cannot** see what you paid in INR to acquire your USDC
  on some Indian exchange. Under the VDA reading that INR→USDC purchase is
  itself a taxable event this tool cannot compute — so VDA-treatment numbers
  cover the Hyperliquid leg only, unless you supply your acquisition cost with
  `--onramp-cost`.
- **Every assumption is printed in the output**, not buried in code:
  cost-basis convention, FX source and convention, funding treatment, fee
  deductibility per treatment.
- **The law is unsettled; the tool's only claim is arithmetic under stated
  assumptions.** Verify every section number and rate against the current
  Income-tax Act and a practising CA before relying on any output.

## Scope (v1)

- **Perpetuals only.** Spot, vaults, subaccounts, and staking are excluded —
  detected and warned about, never silently mixed in.
- Deposits and withdrawals **are** tracked (needed for the reconciliation
  identity and the on-ramp boundary).
- Cost basis uses **average-entry within an episode** (matches how Hyperliquid
  displays entry price); the convention is stated in every report.
- FX uses the **FBIL/RBI USD/INR reference rate at event time**, most-recent-
  prior-business-day convention, with the exact rate-date recorded per event.

## Quickstart

Requires Python 3.10+.

```bash
pip install -r requirements.txt
python main.py <public-wallet-address>
```

That runs the whole pipeline — fetch → load → reconstruct → FX → interpret →
present — and writes three artifacts to `data/reports/<address>/`:

- a full **CSV ledger** (every event, in USD and INR, with the FX rate and
  rate-date used),
- a **Schedule-VDA-shaped CSV**, and
- a **CA-facing HTML summary** — the treatments side by side, an assumptions
  box, the on-ramp boundary section, and the limitations at the top.

Useful flags:

| flag | meaning |
|------|---------|
| `--refresh` | bypass the on-disk cache and re-fetch from the API |
| `--out DIR` | write the report somewhere other than the default |
| `--as-of YYYY-MM-DD` | bound the analysis to events on/before a date (e.g. a financial-year end, or to stop before the FX series ends) |
| `--onramp-cost INR` | supply your INR cost of acquiring the USDC (the manual-input slot) |

The fetch layer caches raw pulls to `data/raw/<address>/`; re-running never
re-hits the API unless you pass `--refresh`.

## Architecture

Each layer is independently rerunnable, and a bug downstream can never cost you
the fetched data:

```
FETCH → LOAD → RECONSTRUCT → FX → INTERPRET → PRESENT
```

Fetch is the only thing that touches the network; load is the only place
strings become numbers; reconstruct and interpret are pure (unit-tested on
hand-computed fixtures); present computes nothing new. See
`docs/week2_master_plan.md` for the full per-file specification.

## Tests

```bash
python -m pytest -q
```

The suite includes hand-computed fixtures (longs, shorts, partial closes, a
flip, a liquidation, funding), the venue-reconciliation and equity-identity
checks, the FX-convention cases, the four tax treatments, and a Phase-7
edge-case sweep (empty / single-fill / spot-only / malformed address /
pre-FX-series event). Reconciliation tests against real cached data skip
cleanly on a fresh clone and enforce once a real address has been fetched.

## A note on privacy

Test runs against public leaderboard addresses are for local verification
only. Cached pulls under `data/raw/` are git-ignored. Do not commit, publish,
or share output computed from a real third party's address.

## License

[MIT](LICENSE).
