# Errors, Gaps, and Delusions in the Week 2 Plan

A skeptical audit of `week2_master_plan.md`, `week2_tax_engine_brief.md`, and
`tax_act_reading_guide.md` — conducted *before* any code is written. The brief
itself instructs: "model memory of Indian tax law is not a source" and "if the docs
disagree with this plan, the docs win." This document applies that instruction to the
plan itself.

The plan is unusually thoughtful for a project brief. Its optimism, however,
concentrates at exactly the points where the failure modes are *silent*: the equity
identity, the FX layer, the Act straddle, and the on-ramp caveat. Fixing the prose at
those four points (not the code) is the highest-leverage work before Phase 0.

---

## 1. The equity identity (Gate 3c) is wrong as stated

`master_plan.md:161`:
> deposits − withdrawals + Σrealized P&L + Σfunding − Σfees ≈ current account equity

This is **not an identity that will hold**, and chasing it will waste days. Two
distinct problems:

### 1a. Unrealized P&L is missing from the LHS

`clearinghouseState` returns equity = margin + **unrealized** P&L. The LHS above has
**no unrealized term**. If the trader has any open position at the snapshot moment,
this identity *cannot* reconcile by construction — it is off by exactly the open
position's floating P&L. The plan says "≈ within tolerance" as if the gap is rounding;
it isn't, it's a missing term.

The correct identity is:

```
deposits − withdrawals + Σrealized + Σfunding(signed) − Σfees + unrealized_now ≈ equity_now
```

Either:
- the snapshot must be taken at a moment with **zero open positions across all assets**
  (rare for an active trader; requires fetching `clearinghouseState` and waiting until
  the account is flat before snapshotting, which is operationally awkward), **or**
- unrealized must be reconstructed too — which needs a **price snapshot at the
  snapshot time** for every asset the account has ever touched. That is much harder
  than the plan implies.

### 1b. Funding sign is silently assumed

Funding paid *reduces* equity; funding received *increases* it. The formula just says
"+Σfunding" as if funding is always additive. It is additive only under the
convention "received positive, paid negative" — a convention, not a fact. The plan
elsewhere (correction 0.7, line 73-79) explicitly flags sign conventions as the **#1
silent-failure risk**. The identity should be written `Σfunding (signed: + received,
− paid)`, stated, not implied.

This is the plan's single biggest reasoning error, and it sits inside the gate it
calls "the whole reason the tool can be trusted."

---

## 2. The 1961 → 2025 Act straddle is treated as a labeling problem; it is a logic problem

`tax_act_reading_guide.md:17` says "Every section number I gave you earlier is a 1961
number" and provides a mapping table (e.g. 115BBH → "inside s.194" of the 2025 Act).

But:

- The guide itself marks the effective date as "tax year beginning **1 April 2026**"
  (line 14) and labels transition dates **NEEDS-VERIFICATION** (line 24).
- The brief and master_plan repeatedly cite **s.115BBH** (a 1961 number) as if it is
  the live answer — `tax_engine_brief.md:75-77`, `master_plan.md:92`.
- For **FY 2025-26 returns** (which is what most users filing in 2026 will be doing),
  the 1961 Act is still the governing statute. The plan acknowledges this at
  `tax_act_reading_guide.md:21` but never resolves how the engine is supposed to
  handle the straddle.

"Parameterize by year" is asserted as the fix — that is a one-line answer to a
multi-month design problem. The three treatments may have **different section numbers,
different definitions, and possibly different rates** between the two Acts. The plan
treats this as a labeling problem; it is a logic problem. Whichever Act a given tax
year falls under, the **definitions** of "VDA", "speculative transaction", and
"transfer" in that Act govern — and they may have moved. The engine cannot treat
section numbers as stable strings to look up; it has to treat the year as selecting
a different *namespace* of rules.

---

## 3. Gate 3b's per-fill reconciliation target may be structurally unreachable

`master_plan.md:21-29` (correction 0.1) gives three reasons to recompute realized
P&L independently: lot-level attribution, episode accounting, distrust. Then Gate 3b
asserts our recomputed per-fill P&L matches HL's `closedPnl` "within tolerance."

But `closedPnl` is computed by HL using **its own** cost-basis convention (average
entry, per correction 0.3). Our reconstruction also uses average-entry (per 0.3). So
they should match — *unless* we disagree on **episode boundaries** (what counts as one
position). A flip, partial close, or a position that goes flat and reopens could
easily be one episode in HL's view and two in ours, producing per-fill `closedPnl`
values that **do not sum** to our episode P&L.

The gate would then "fail" and the plan offers no diagnostic path other than "stop and
find it." The reconciliation target may be structurally unreachable at per-fill
granularity. The correct target is **per-episode sum vs HL's `closedPnl` summed over
the same fills**, not per-fill equality. Otherwise Gate 3b becomes a debugging black
hole.

---

## 4. The on-ramp caveat is honest in prose but produces a structurally misleading numeric answer

`master_plan.md:36-40` (correction 0.2) correctly states the INR→USDC step is itself
a taxable VDA event the tool cannot see, and adds a "manual-input slot" in the report.

But:

- It appears nowhere in any gate. Gate 5 checks the three treatments match
  hand-computed numbers — but those numbers are *computed without* the on-ramp cost,
  by design. So the VDA tax computation is **structurally incomplete**, and the plan
  calls this "honest" while letting the headline number be wrong by an unbounded
  amount (the USDC cost basis could be anything).

- The Schedule-VDA CSV (Phase 6) will report VDA disposals whose "cost of acquisition"
  is unknown. Section 115BBH allows deducting **only** cost of acquisition. If the
  tool doesn't have it, the VDA treatment **cannot be computed**, only gestured at.
  The plan's framing — "covered number is Hyperliquid leg only" — is honest in prose
  but the **numeric output** will look like an answer. A CA looking at the report
  will read it as the answer.

This is the plan's biggest *ethical* gap: it identifies the problem, asserts that
stating it loudly is the fix, and then produces a number that does not include it.
Stating a caveat does not undo producing a misleading number — particularly when the
output goes to a CA who may not read the fine print.

**Fix:** either gate the VDA treatment behind the manual input (refuse to print a
number if on-ramp cost is absent), or label the number **in the same cell** as
"Hyperliquid leg only — *not* your VDA tax liability." A footnote does not suffice.

---

## 5. Phase 4 (FX) is not one day — realistically 2-3, possibly more

`master_plan.md:166` allocates 1 day to the FX layer and describes it as
`date → (rate, rate_date_used)`. It handwaves the actual hard part:

- **No cited source.** The plan says "FBIL publishes once per business day around
  noon IST" and marks the source URL **NEEDS-VERIFICATION** (0.6, line 71). A 1-day
  estimate is only realistic if the source is already known and documented. If the
  builder has to find the FBIL historical series, get past any rate-limit / ToS /
  scraping issues, format it, and build the refresh script, **Phase 4 is realistically
  2-3 days**, and may turn out to require manual CSV maintenance (worse: it usually
  does).

- **Multi-year coverage.** A user who has been trading on HL since 2023 needs INR/USD
  rates back to 2023. The FBIL archive goes back further but the plan doesn't say
  where to get a *clean, complete* series. Gap handling for missing business days is a
  real source of bugs.

- **Intra-day timing problem.** "Most recent prior business day" is one choice; "same
  -day if available else prior" is another. The FBIL rate published around noon is
  arguably not the right rate for a fill at 09:30 IST the same day. The plan picks a
  convention but does not acknowledge the intra-day timing problem. A fill at 10:00 IST
  on a business day is *prior* to the noon publication — strictly applying the plan's
  convention means using *yesterday's* rate for a same-morning fill, which is
  defensible but should be a conscious decision, not an accident.

The 8-12 day total is optimistic. Allocate 2-3 days to Phase 4.

---

## 6. Phase 0 fixture locks in one funding treatment; Phase 5 pretends all three are open

`brief.md:199-204` and `master_plan.md:155` both stress funding ≠ execution P&L. Good.
But the plan then punts the actual question: is each funding payment a **separate
taxable event** under the VDA regime (a transfer? income?), a **cost of the position**
(so it nets into realized P&L on close), or **deductible** under the business
treatment?

Phase 5 says "assumptions are explicit parameters" (`master_plan.md:177`) — which is
a way of *not* deciding. But the **arithmetic differs** depending on the choice, and
the fixture (Phase 0) has to commit to one to compute expected numbers.

There is a quiet contradiction:
- "Make it a parameter" (Phase 5) → user picks at runtime.
- "Hand-compute the expected value" (Phase 0) → fixture has to pick ONE.

The fixture therefore biases the test surface toward one reading of funding, and the
others go untested. Either the fixture must exercise **all** values of the funding
parameter (3× the expected-value work), or the plan must own up to a default choice
that the tests lock in and the docs acknowledge as the tested path.

---

## 7. Average-entry = HL's display convention is unverified

`master_plan.md:48` says "v1 uses average-entry-price within an episode (matches how
HL itself displays entry price)." This is stated as fact. It is **NEEDS-VERIFICATION**
against current HL docs but is not flagged as such.

If HL's display rounds, uses a different averaging window, or excludes fees from the
average, our reconstruction will silently diverge from HL's `closedPnl` — and Gate 3b
"fails" with no diagnostic path, because we never verified the convention matches, we
only assumed it does. The plan's own doctrine (loud failure, every assumption visible)
is violated here at the foundation.

**Fix:** verify against HL docs before Phase 3. If it cannot be verified, treat Gate
3b as a **diagnostic** (report max deviation, do not fail on it), and rely on Gate 3c
only.

---

## 8. "Read-only, no key fields" is under-specified for the hosted page

The brief and plan repeat the no-private-key rule like a mantra
(`brief.md:176-179, 264`, `master_plan.md:332`). Nowhere does either document specify
**what** the read-only guarantee actually requires technically:

- The hosted page (`brief.md:287`, "a hosted page where a trader pastes a public
  address") — who hosts it? If it is a static client-side page (CORS allowing direct
  calls to the HL info endpoint), fine. If it is a server, the server logs the address
  (and possibly an attacker injects one that triggers heavy backfill — a DoS vector).
  The plan says nothing about the hosted path's architecture, but lists it in
  "definition of done."

- A wallet **address** is not a secret, but its full trade history is sensitive data
  (front-running, de-anonymization from fill patterns). Saying "no keys" is true but
  insufficient; "read-only" does not mean "harmless." The plan treats wallet address
  as a fully benign input. It is *legally* benign but not *operationally* benign at
  scale.

This is not a "don't build it" objection — it is a "the non-negotiable is
under-specified and a reviewer will catch it" observation. **Fix:** scope v1 to CLI
only, or specify the hosted architecture in the README before building it.

---

## 9. Leaderboard addresses as test data needs an explicit don't-publish rule

`master_plan.md:138` says to grab active addresses from the public leaderboard for
Gate 1 testing. Two problems the plan does not acknowledge:

- **HL's leaderboard addresses are real traders' wallets.** Running the tool against
  them and publishing a tax report (even locally cached) using their on-chain
  activity as the test corpus raises a real privacy question the plan does not touch.
  Using them for *fetch / cache / reconciliation* debugging (summary statistics) is
  fine; using them to print **a worked tax number** for a named real address is
  different.

- The "definition of done" includes "a hosted page where a trader pastes a public
  address" — built and tested against real third-party wallet data. At minimum the
  README needs to say "test runs against leaderboard addresses should not be
  published or shared." The plan never says this.

**Fix:** add to the README, in the limitations section, an explicit rule that
leaderboard-derived test outputs are for the builder's local verification only and
must not be committed, screenshotted with the address visible, or shared.

---

## 10. "No magic numbers anywhere else" is immediately violated by the plan itself

`master_plan.md:217` says all constants live in `config.py`. But:

- Gate 3c (line 161) hardcodes the equity identity formula in prose.
- Phase 5 (line 178) says "fee deductibility" is a parameter — meaning the fee
  treatment has at least one constant somewhere.
- **Tax rates** (30% for VDA, slab rates for business, etc.) are numbers that **must**
  be parameters if they are to be verifiable, but the plan never says where they live.
  If they live in `treatments.py` as defaults, that violates the "no magic numbers"
  rule. If they live in `config.py`, then `config.py` contains **tax law**, which
  means a single edit can change the law and the README must warn.

The plan does not address this. **Fix:** all rates and section numbers live in
`config.py`, each with **both 1961 and 2025 Act references** where applicable, and
each with a `NEEDS-VERIFICATION` flag. A single-rate edit changes the output; the
output must echo the version of `config.py` it was computed under.

---

## 11. "Pure functions, no I/O" is contradicted by the eager-FX design

`master_plan.md:287-290` says reconstruct and interpret are PURE (no I/O).
`treatments.py` takes "the INR ledger from phases 3+4" — but Phase 4 (FX) reads a CSV
from `fx/data/`. So the INR ledger that Phase 5 consumes is downstream of a disk
read.

That is fine as long as we are clear, but the plan says `fx/fx.py` returns
`(rate, rate_date)` per date — meaning the FX conversion is **eager** (happens in
Phase 4, before interpret). That means **every downstream test of treatments must
reconvert**, or the treatments are coupled to a specific FX assumption baked in at
conversion time.

The cleaner architecture (stated nowhere in the plan) is:

```
Phase 3 (USD events)  →  DataFrame of USD events
Phase 4 (rate lookup) →  a pure function: date -> (rate, rate_date)
Phase 5 (treatments)  →  calls the rate function per event, then computes
```

That way the rate module is **pure given the CSV** and treatments are **pure given the
rate function**. The plan's eager-conversion design couples Phase 5 to Phase 4 in a
way that breaks the claimed purity — you cannot test treatments on paper fixtures
without also stubbing the FX CSV.

**Fix:** adopt the rate-function design. Phase 4 returns a function, not a converted
ledger. Treatments tests inject a stub rate function; production injects the
CSV-backed one.

---

## 12. The CA critical path is decoupled late, not early

`master_plan.md:60-63` (correction 0.5) correctly says decouple the CA from the tool
ship. But it says "send them the three-treatments framing in week 1 of the build."

Week 1 of an 8-12 day build is **day 2-3**. The three-treatments framing requires the
builder to have already read the Act (Part 1 of the reading guide is ~15-20 hours).
Calendar-wise, sending a CA a framing on day 2 means sending it **before** the
builder has done the reading to produce a defensible framing.

So either:
- the CA gets a half-baked framing (and probably a 1961-Act one, given the gap in
  §2), or
- the CA contact slips to day 7-8, which makes it no longer "early."

**Fix:** send the CA the *question* (three treatments = unsettled law, will you
review?) in week 1 on the strength of the brief alone. Send the *framing document*
after the reading guide's sessions 1-5 are done (~day 5-7). Two-step contact, not
one.

---

## Summary — what to fix before Phase 0

Ranked by leverage (highest silent-failure risk first):

1. **Gate 3c's formula is missing an unrealized P&L term and silently assumes a
   funding sign convention.** Fix the formula in the plan before any code.
2. **The 1961 → 2025 Act straddle is treated as a labeling issue when it is a logic
   issue.** Two regimes may have different rates/definitions; "parameterize by year"
   needs a real design, not a one-liner.
3. **Gate 3b's per-fill reconciliation target may be structurally unreachable** if
   HL's episode definition differs from ours. Reconcile per-episode-sum, not
   per-fill.
4. **The on-ramp caveat is honest in prose but produces a structurally misleading
   numeric answer.** Gate the VDA treatment behind manual input, or label the number
   in-cell — not as a footnote.
5. **Phase 4 is not 1 day.** It is 2-3, possibly more if the FBIL source has to be
   reverse-engineered. The 8-12 day total is optimistic.
6. **Phase 0 fixture locks in one funding treatment but Phase 5 pretends all are
   open.** Decide whether funding is a parameter that Phase 0 tests **all** values
   of, or a fixed choice the docs own up to.
7. **Average-entry = HL's display convention is unverified.** Flag it
   NEEDS-VERIFICATION before Phase 3, or Gate 3b becomes a debugging black hole.
8. **"Read-only, no keys" is under-specified for the hosted page.** State the hosted
   architecture, or scope v1 to CLI-only.
9. **Leaderboard addresses as test data needs an explicit "don't publish results"
   rule** in the README, not just a vibe.
10. **"No magic numbers" is contradicted by tax rates living somewhere unspecified.**
    Put all rates and section numbers in `config.py` with both 1961 and 2025
    references and a `NEEDS-VERIFICATION` flag on each, or the rule is theater.
11. **Eager-FX coupling breaks the claimed purity of the interpret layer.** Make
    Phase 4 return a rate function; treatments inject it as a dependency.
12. **CA contact slips if done honestly.** Two-step contact: the *question* in week 1,
    the *framing document* after reading-guide sessions 1-5.

The plan is strong. These twelve fixes are all prose-level — no code decisions need
to be revisited yet, only the reasoning around them. Doing this now costs hours;
doing it later, after gates start "failing" for reasons the plan does not
anticipate, costs days each.
