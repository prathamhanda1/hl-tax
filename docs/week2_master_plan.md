# MASTER PLAN — Indian Tax + P&L Engine for Hyperliquid Perps

This is the complete execution plan. It is written to be handed to a coding model as
the first message of a session. Read the companion document
`week2_tax_engine_brief.md` first if present — it holds the strategic context (why
this exists, who it serves, the custodial-abstraction question, the non-negotiables).
This document holds the HOW: prerequisites, phases, architecture, per-file specs,
and the verification gates that make failure visible early instead of at the end.

The builder: 19-year-old Indian student, Python-only, has already shipped a
Hyperliquid WebSocket collector + pandas analysis pipeline (a slippage study), so is
fluent with the HL info endpoint, JSONL, DataFrames, and pytest. Beginner at tax and
accounting. Explain tax/accounting concepts in 1-2 sentences as they arise.

---

## PART 0 — Design corrections made to the original idea (read first)

These change the original brief. They are reasoned decisions, not oversights.

**0.1 — Hyperliquid already reports `closedPnl` per fill. We recompute it anyway.**
The `userFills` response includes a `closedPnl` field on closing fills. A lazy build
would just sum it. We instead reconstruct realized P&L independently from raw fills,
then RECONCILE against the venue's own numbers. Reasoning: (a) if our reconstruction
matches the venue's within tolerance, every layer beneath it is proven correct — this
is the single strongest self-test available and the heart of the troubleshooting
strategy; (b) tax may require lot-level (episode-level) attribution and timing that a
single per-fill number cannot give; (c) blind trust in one field is exactly how a
confident wrong tax document gets produced.

**0.2 — The on-ramp is invisible to this tool. Say so, loudly.**
The tool sees on-Hyperliquid activity (deposits of USDC in, trading, withdrawals out).
It CANNOT see what the user paid in INR to acquire that USDC on some Indian exchange,
because that happened off-chain or on another venue. Under the VDA reading, the
INR→USDC purchase and the final USDC→INR sale are themselves taxable VDA events —
and this tool cannot compute them. Resolution: the report includes an explicit
"on-ramp/off-ramp boundary" section with a manual-input slot (user supplies their
USDC acquisition cost from their exchange statement) and a clear statement that
without it, the VDA-reading numbers cover the Hyperliquid leg only. Hiding this
boundary would make the tool quietly wrong; stating it makes the tool honest.

**0.3 — Hyperliquid nets one position per asset. This simplifies cost basis — partly.**
Unlike spot, where you can hold many lots, HL maintains a single net perp position
per asset per account. So "which lot did this close?" reduces to episode accounting:
a position opens, grows/shrinks, and closes (or flips). The convention question
survives in two places only: partial closes (average-entry vs FIFO-within-episode)
and flips (a single fill that closes long AND opens short must be split into two
economic events). v1 uses average-entry-price within an episode (matches how HL
itself displays entry price), with the convention stated in output. FIFO-within-
episode can be a later option.

**0.4 — Scope: perps only, spot excluded, vaults/subaccounts/staking excluded.**
HL also has spot trading, vaults, and staking. All out of scope for v1 — but
deposits/withdrawals ARE tracked (they're needed for the reconciliation identity and
the on-ramp boundary). If the address has spot/vault activity, the tool detects it
and prints a warning that those flows are excluded, rather than silently producing
numbers that don't reconcile.

**0.5 — The CA is a critical-path risk. Decouple.**
Original plan gates the writeup on a CA co-signing. If the CA flakes, everything
stalls. Fix: the TOOL ships independently ("computes three stated treatments; not
advice"). The CA review gates only the WRITTEN TAX ANALYSIS. Sequence the CA early:
send them the three-treatments framing in week 1 of the build, not at the end.

**0.6 — FX source precision: the "RBI rate" is published by FBIL.**
The commonly cited INR/USD reference rate is published by FBIL (Financial Benchmarks
India), once per business day around noon IST; RBI republishes it. No rate exists
for weekends/holidays — and perps trade 24/7. Convention (stated in every output):
each event uses the most recent PRIOR business day's reference rate. The FX module
must also record WHICH date's rate each event used, so a CA can audit line by line.
The exact current source URL/format is a NEEDS-VERIFICATION item for the builder.

**0.7 — Sign and field conventions are the #1 silent-failure risk.**
Week 1's one real bug was a backwards side-sign, caught because it produced
physically impossible numbers. The same class of bug here (fee sign, funding
direction, short-position P&L sign) produces a wrong tax number that looks fine.
Defense, mandated throughout: every module states its sign convention in its
docstring; every phase ends with an invariant check against reality (see gates).

---

## PART 1 — Prerequisites to study (before or alongside the build)

Ordered. Total ~15-20 focused hours. Skip nothing in tier 1.

**Tier 1 — cannot build without:**
1. *Perp P&L mechanics* (~3h): entry/exit/realized vs unrealized P&L, why shorts
   profit when price falls, funding payments (who pays whom, when), liquidation as a
   forced close. Source: Hyperliquid docs + any major exchange's perp guide. The
   builder trades perps, so this is formalisation, not first contact.
2. *The three Indian tax treatments, at framework level* (~4h): s.115BBH (30% flat,
   no loss set-off, no expense deduction — the VDA regime), F&O-style non-speculative
   business income (s.43(5) proviso — losses set off, expenses deductible, slab
   rates), speculative business income (losses only against speculative gains).
   Plus s.194S TDS (1%) at concept level. Sources: the Act's sections themselves +
   two reputable CA-firm explainers, cross-checked. EVERY section number in this
   plan is NEEDS-VERIFICATION against the current Act — model memory of Indian tax
   law is not a source.
3. *Double-entry thinking, minimal* (~2h): a ledger where every event has a
   timestamp, a category, an amount, and a running balance; the idea that the sum of
   all events must reconcile to the change in account value. This single idea powers
   the master verification gate.

**Tier 2 — makes the output professional:**
4. *FBIL/RBI reference rate mechanics* (~1h): what it is, when it publishes, what to
   do on holidays.
5. *Cost-basis conventions* (~1h): FIFO / weighted-average, and why the choice
   changes the number.
6. *Schedule VDA of the ITR* (~1h): the actual reporting shape the numbers land in.

**Tier 3 — context, skim:** how Zerodha's tax P&L report is structured (the genre
the output imitates); one read of a good crypto-tax explainer for prior art.

---

## PART 2 — The build, in phases, with verification gates

Rule carried from Week 1: ONE FILE AT A TIME. The coding model must not emit the
whole repo in one response. Each phase ends with a GATE — a concrete, runnable check.
A phase is not done until its gate passes. Gates exist so that failure surfaces in
the phase that caused it, never three phases later.

### Phase 0 — Skeleton + fixtures-first (half a day)
Create the repo structure (Part 3), empty `__init__.py`s, `config.py` with every
constant flagged, and `tests/fixtures.py`: a HAND-BUILT set of ~12 fake fills
covering: simple open+close long (profit), open+close short (profit — sign test!),
partial close, a flip (long→short in one fill), a liquidation fill, funding payments
in both directions, a deposit and a withdrawal. Every expected number computed BY
HAND in comments, exactly like Week 1's test book.
**GATE 0:** fixtures load; hand-computed expected values documented in the file.

### Phase 1 — Fetch layer (1 day)
`fetch/client.py` + `fetch/fetch_user.py`. Paginated pulls of `userFills` (or
`userFillsByTime`), `userFunding`, `userNonFundingLedgerUpdates` for one address,
cached to `data/raw/<address>/`. Respect rate limits (delay between calls, honor the
documented per-request row caps by paginating on timestamps). Never refetch what is
cached unless `--refresh`.
**GATE 1:** run against 2-3 REAL public addresses (take active ones from HL's public
leaderboard); row counts printed; re-running hits cache (zero API calls); a
deliberately bad address fails loudly with a clear message, not a stack trace.

### Phase 2 — Load layer (half a day)
`load/load.py`: raw JSON → three typed DataFrames (fills, funding, ledger). Strings
→ floats exactly once, here. UTC timestamps → tz-aware `ts` column. Tag each fill's
asset with its dex namespace (`xyz:TSLA` → equity-perp, `BTC` → crypto-perp) — the
tax distinction may need it later.
**GATE 2:** loader runs on both the fixtures and the real cached pulls; row counts
match Phase 1; dtypes printed and correct; no object-dtype numeric columns.

### Phase 3 — Position reconstruction (2-3 days, the hard core)
`reconstruct/positions.py`: replay fills per asset in time order, maintain net
position + average entry, split flips into close+open, emit one row per CLOSING
event with: episode id, asset, open/close timestamps, size closed, entry px, exit
px, realized P&L (USD), fees (USD), liquidation flag. `reconstruct/funding.py`:
funding events as their own ledger rows (they are NOT execution P&L — different
column, different possible tax treatment).
**GATE 3 (the master gate):** three parts, all must pass —
 (a) unit tests: reconstruction matches every hand-computed fixture number exactly;
 (b) venue reconciliation: on real addresses, our per-fill realized P&L matches HL's
     own `closedPnl` field within a stated tolerance (report the max deviation);
 (c) the equity identity: deposits − withdrawals + Σrealized P&L + Σfunding − Σfees
     ≈ current account equity (from `clearinghouseState`), within tolerance.
If (b) or (c) fails, a sign or a missed flow is wrong SOMEWHERE — stop and find it
before proceeding. This gate is the whole reason the tool can be trusted.

### Phase 4 — FX layer (1 day)
`fx/fx.py`: date → INR rate from a locally cached FBIL/RBI series (`fx/data/`),
most-recent-prior-business-day convention, returns BOTH the rate and the rate-date
used. Loud failure if an event predates the cached series.
**GATE 4:** unit tests for a weekend event, a holiday event, a normal day; every
converted event in output carries its rate AND rate-date columns.

### Phase 5 — Interpret layer (1-2 days)
`interpret/treatments.py`: ONE input (the INR ledger from phases 3+4), THREE pure
functions — `treatment_vda()`, `treatment_business()`, `treatment_speculative()` —
each returning a summary dict + line-level detail. Every assumption (funding
treatment, fee deductibility, loss set-off) is an explicit, printed parameter, never
a buried constant. Plus `tds_summary()`. NO treatment is labeled "correct."
**GATE 5:** hand-computed fixture tax numbers match for all three treatments; the
VDA no-loss-set-off rule is specifically unit-tested (a losing episode must NOT
reduce the VDA tax base); an all-losses fixture produces zero VDA tax base, not a
negative one.

### Phase 6 — Present layer (1-2 days)
`present/report.py`: (a) full CSV ledger (every event, USD, INR, rate, rate-date,
category); (b) an HTML/PDF summary for a CA — the three treatments side by side, the
assumptions box, the on-ramp boundary section with the manual-input slot, the
limitations section AT THE TOP; (c) a Schedule-VDA-shaped table. Also `main.py`:
`python main.py <address>` runs fetch→report end to end with progress printed.
**GATE 6:** end-to-end run on a real address produces all three artifacts; a
non-technical reader (the CA) can follow the summary without seeing code; every
number in the summary traces to a ledger row.

### Phase 7 — Hardening + publication (1-2 days)
Edge-case sweep: empty address, address with one fill, address with ONLY spot
activity (must warn, not crash), enormous address (pagination + memory), address
with activity predating the FX series. README with the read-only guarantee in bold
at top. License. Publish repo. THEN the writeup (CA-gated part) as its own track.
**GATE 7:** the full pytest suite green; a stranger can clone, run one command on a
public address, and get a report without asking the builder anything.

Realistic total: 8-12 working days alongside study. The phases are strictly ordered;
gates make each phase's completion objective rather than felt.

---

## PART 3 — Repository architecture, file by file

```
hl-tax-engine/
  README.md              # read-only guarantee in bold at top; what/why/how; limitations
  LICENSE                # MIT or Apache-2.0
  requirements.txt       # requests, pandas, pytest, jinja2 (for HTML), nothing exotic
  config.py              # ALL constants: API base, rate-limit delay, page sizes,
                         # tolerance values for gates, FX convention name, clip of
                         # decimals in reports. Every unverified value flagged
                         # "NEEDS VERIFICATION". No magic numbers anywhere else.
  main.py                # CLI: python main.py <address> [--refresh] [--out DIR]
                         # orchestrates fetch->load->reconstruct->fx->interpret->present
                         # prints a progress line per phase; fails loudly per phase.

  fetch/
    client.py            # ONE function-level responsibility: a rate-limited,
                         # retrying POST to the info endpoint. Knows nothing about
                         # what is being fetched. Timeout, backoff, clear errors.
    fetch_user.py        # knows WHAT to fetch: paginated userFills/ userFunding/
                         # ledger updates for an address; writes raw JSON to
                         # data/raw/<address>/<type>.json; cache-first; --refresh
                         # bypasses. Also fetches clearinghouseState (for Gate 3c)
                         # and perpDexs/meta once (for asset tagging).

  data/
    raw/<address>/       # cached raw pulls. append-only in spirit: the fetch layer
                         # is the only writer. NEVER computed against directly.

  load/
    load.py              # raw JSON -> typed DataFrames: fills, funding, ledger.
                         # strings->floats ONCE. tz-aware UTC ts. dex/asset-class
                         # tagging (equity-perp vs crypto-perp vs other).

  fx/
    fx.py                # get_rate(date) -> (rate, rate_date_used). Convention:
                         # most recent prior business day. Loud failure outside the
                         # cached series range. Docstring states the convention and
                         # the source.
    data/                # the cached FBIL/RBI series as CSV, with a fetch/update
                         # script or documented manual refresh procedure.

  reconstruct/
    positions.py         # the hard core. Replays fills per asset chronologically:
                         # net position, average entry, episode ids; splits flips;
                         # emits realized-P&L rows on every closing fill with fees
                         # and liquidation flags. Sign conventions in docstring.
                         # Pure: DataFrames in, DataFrames out, no I/O.
    funding.py           # funding events -> ledger rows, separate category from
                         # execution P&L. Sign convention (who paid whom) verified
                         # against a real account and stated in docstring.

  interpret/
    treatments.py        # three pure functions over the same INR ledger:
                         # treatment_vda / treatment_business / treatment_speculative,
                         # + tds_summary. Assumptions are parameters with defaults,
                         # echoed into every output. No winner declared.

  present/
    report.py            # renders: full CSV ledger; CA-facing HTML summary
                         # (three treatments side by side, assumptions box, on-ramp
                         # boundary + manual-input slot, limitations AT TOP);
                         # Schedule-VDA-shaped CSV. Computes NOTHING new.
    templates/           # jinja2 HTML template(s) for the summary.

  tests/
    fixtures.py          # the hand-built fill/funding/ledger set with hand-computed
                         # expected values in comments. Written in Phase 0, BEFORE
                         # the code it tests. The paper order book of this project.
    test_positions.py    # reconstruction vs fixtures: longs, shorts, partial closes,
                         # the flip split, the liquidation, fee handling.
    test_fx.py           # weekend/holiday/normal-day convention; rate-date echoed.
    test_treatments.py   # three treatments vs hand-computed fixture tax numbers;
                         # VDA no-loss-set-off specifically; all-losses edge case.
    test_reconciliation.py  # Gates 3b and 3c against cached REAL data: closedPnl
                         # cross-check and the equity identity. Skips cleanly if no
                         # cached real pulls exist (fresh clone), enforces once they do.
  conftest.py            # empty; makes the project root importable for pytest.
```

Layer discipline (identical to Week 1): fetch is dumb and is the only thing that
touches the network; load is the only place strings become numbers; reconstruct and
interpret are PURE (no I/O, no network) so they are unit-testable on paper fixtures;
present computes nothing. A bug in interpret can never cost fetched data; a change in
presentation can never change a number.

---

## PART 4 — The troubleshooting doctrine (why nothing fails at the end)

1. **Fixtures before code.** The hand-computed fixture set (Phase 0) is written
   before the logic it tests, exactly like Week 1's paper order book. If the code and
   the hand arithmetic disagree, the code is wrong until proven otherwise.
2. **Reconcile against reality at every level.** Our P&L vs the venue's `closedPnl`
   (per fill); our ledger vs the venue's account equity (the identity in Gate 3c);
   our FX vs the published series. Reconciliation converts "looks right" into
   "matches an independent source."
3. **Gates are ordered so failure surfaces where it was caused.** A sign error dies
   in Gate 3, not in the tax table. An FX gap dies in Gate 4, not in the CA meeting.
4. **Loud failure beats silent degradation.** Unknown event types, spot activity,
   out-of-range dates, unfetchable addresses: warn or halt with a message. Never
   skip silently — a skipped event is a wrong tax number wearing a green checkmark.
5. **Every number in the report is traceable.** Summary → treatment detail → INR
   ledger row → USD event → raw JSON on disk. A CA (or a skeptic) can audit any
   line to its source. This is also the debugging path when something looks off.
6. **Assumptions are output, not code.** FX convention, cost-basis convention,
   funding treatment, fee deductibility per treatment — all echoed in every report,
   so two runs with different assumptions are visibly different documents.

---

## PART 5 — Instructions to the coding model receiving this plan

- Work ONE FILE AT A TIME, in phase order. Show the file, wait for the builder to
  run the gate, then proceed. Do not scaffold ahead.
- Phase 0 (fixtures) comes before any logic. Do not let the builder skip it.
- Explain any tax/accounting concept in 1-2 sentences at first use. Do not assume
  the builder knows what "set-off" or "cost basis" means; do not lecture either.
- All Indian tax specifics (section numbers, rates, TDS mechanics) in this plan are
  NEEDS-VERIFICATION. Before Phase 5, instruct the builder to verify each against
  the current Income-tax Act and at least two reputable current sources. Model
  memory is not a source for tax law.
- All Hyperliquid API details (endpoint names, pagination limits, response fields
  like closedPnl) are NEEDS-VERIFICATION against current docs at build time. If the
  docs disagree with this plan, the docs win — say what changed.
- Never create any input path for a private key or API secret. If the builder asks
  for one, refuse and restate the read-only rule.
- Keep the three-treatments framing strictly neutral. If asked to make the tool
  "recommend" a treatment, refuse: it computes, the CA advises.
- Respect the analyse-never-accuse rule from the companion brief in all written
  output: the custodial-abstraction question is discussed as market structure,
  never as an allegation about a named company.
