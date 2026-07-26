# Reading the Income-tax Act for the Tax Engine
### A guided path through the Act for crypto, US-stock perps, and the Week 2 artifact

You are not reading this Act like a law student. You are reading it like an engineer
hunting for every rule your engine must encode, and like a taxpayer hunting for every
trap. Both mindsets, at once. This guide gives you the path, what to extract at each
stop, and the engine feature each stop implies.

---

## PART 0 — THE BIGGEST THING FIRST: you uploaded the NEW Act

The document you have is the **Income-tax Act, 2025** — the full replacement of the
1961 Act, effective from the tax year beginning **1 April 2026**. This has three
consequences you must internalize before reading a single section:

1. **Every section number I gave you earlier is a 1961 number.** s.115BBH (VDA 30%),
   s.194S (1% TDS), s.43(5) (speculative transaction) — all 1961. The 2025 Act
   renumbers everything. Your navigator PDF is exactly the map between the two:
   column A = new number, column E = old number. Keep it open beside you always.
2. **Your engine's users straddle both regimes.** Returns for FY 2025-26 are filed
   under the OLD Act. Activity from April 2026 onward falls under the NEW Act. A tax
   engine built in 2026 must either target the new Act explicitly or parameterize by
   year. This is a design decision — make it consciously, and print which regime every
   report was computed under. [NEEDS VERIFICATION: exact transition/effective dates.]
3. **"Tax year" replaces "previous year/assessment year"** (see s.3 of the new Act).
   The old PY/AY dance is gone. Your output labels should use the new vocabulary for
   new-regime years.

Known mappings you will use constantly (verify each against the navigator):
| Concept | 1961 Act | 2025 Act |
|---|---|---|
| VDA flat 30% regime | 115BBH | inside s.194 (Tax on certain incomes — the 115B* family) |
| TDS on VDA transfer 1% | 194S | inside s.393 (Tax to be deducted at source) |
| Speculative transaction definition | 43(5) | within ss.26–66 (PGBP interpretation provisions) |
| Speculation loss set-off | 73 | s.113 |
| VDA definition | 2(47A) | inside s.2 (Definitions) |
| Crypto transaction reporting by entities | 285BAA | s.509 (visible in your navigator!) |
| Set-off chapter | 70–80 | ss.108–121 |
| Capital gains computation | 45–55A | ss.67–91 |
| Other sources (incl. VDA gifts) | 56 | ss.92–95 |

---

## PART 1 — How to read a tax act (technique, 30 minutes to learn, saves weeks)

- **Charge → computation → machinery.** Every tax regime has three layers: a charging
  provision ("this income shall be taxed at X"), computation rules ("compute it like
  this, deduct only this"), and machinery (TDS, filing, penalties). Identify which
  layer a section belongs to before parsing its words. Your engine encodes the first
  two; the third generates warnings and checklists.
- **Definitions are the law.** Half of every dispute is whether something falls inside
  a defined term. Read s.2 definitions slowly; everything else is built on them.
- **Provisos and "notwithstanding" are where the action is.** A proviso carves an
  exception; "notwithstanding anything contained in..." means this section BEATS
  whatever it names. The VDA regime is a giant "notwithstanding" that overrides normal
  computation — that override IS the no-loss-set-off wall.
- **Keep two running lists as you read:** (a) RULES — anything computable, destined
  for `interpret/treatments.py`; (b) QUESTIONS FOR THE CA — anything ambiguous,
  destined for the writeup. Every reading session should grow both lists.
- **Read with a specific taxpayer in your head.** Yours: an Indian resident individual
  who bought USDC on an Indian exchange, moved it to a self-custody wallet, traded
  US-stock and crypto perps on a foreign DEX all year (hundreds of round trips,
  funding paid and received, one liquidation), and withdrew back to INR. Every section
  you read, ask: what does this do to HIM?

---

## PART 2 — The reading path, in order

### Stop 1: Definitions — s.2 of the 2025 Act (old 2(47A) for VDA)
**Read for:** the exact definition of "virtual digital asset". Parse it clause by
clause. The critical questions for your engine:
- Does USDC fall inside it? (Almost certainly yes — any crypto token does.)
- Does a **perpetual futures contract referencing a stock**, settled in USDC, fall
  inside it? The definition speaks of information/code/token generated through
  cryptographic means — does a contractual position on a DEX qualify as the asset
  itself, or is it a derivative OF an asset? This single definitional question is the
  hinge of your entire three-treatments design. Nobody has a settled answer. Write
  down the exact words that create the ambiguity — you will quote them in the writeup.
- Also find: "specified person" (drives TDS thresholds), "tax year" (s.3).
**Engine implication:** the asset-classification tags from Phase 2 of the master plan
(equity-perp vs crypto-perp vs stablecoin) map directly onto readings of this
definition. Each treatment in `treatments.py` is one answer to "what falls in 2(x)?"

### Stop 2: The VDA charging section — inside s.194 (old 115BBH)
**Read for:** the flat 30%, and — more important — the computation restriction: no
deduction other than cost of acquisition, **no set-off of any loss**, no carry
forward. Read the exact words three times. Note:
- "Transfer" — is closing a perp position a "transfer" of a VDA? Is a funding payment?
  Is USDC→INR? Each event type in your ledger needs a yes/no under this section.
- The no-set-off rule's exact scope: loss from one VDA against gain from another VDA?
  (Widely read as blocked even that — verify the words.) This is the difference
  between a trader owing 30% of NET gains vs 30% of GROSS gains. For a
  hundred-round-trip perps trader those numbers are wildly different — your engine
  should compute BOTH and show the gap. That gap is the single most shocking number
  in your eventual report, and it falls straight out of this one section's wording.
**Engine implication:** `treatment_vda()` gets a parameter: `loss_scope` ∈
{no_set_off_at_all, intra_vda_allowed} — compute under both readings, print both.

### Stop 3: PGBP — ss.26–66, especially the speculative-transaction definition
**Read for:** what makes trading income "business income" (regularity, volume, intent
— your taxpayer with hundreds of trades is plausibly a business), what expenses
deduct (s.34 general deductions — internet, hardware, subscriptions?), and the
speculative-transaction definition (old 43(5)): a transaction settled **otherwise
than by actual delivery**. A cash-settled perp is literally that — EXCEPT the old Act
carved out exchange-traded derivatives on recognised Indian exchanges from
"speculative". A foreign DEX is NOT a recognised exchange. Find the new Act's version
of this carve-out and read exactly who qualifies.
**The chain to trace:** cash-settled + not on a recognised exchange → speculative
business income → s.113 set-off jail (speculative losses only offset speculative
gains, limited carry-forward). This is your third treatment, and it is not
hypothetical — it is arguably the DEFAULT reading for foreign-DEX derivatives if the
VDA regime doesn't capture them first.
**Also read:** s.62–63 (books of account, tax audit thresholds — old 44AA/44AB).
Your report should warn when turnover crosses the audit threshold. Compute "turnover"
for F&O the way the ICAI guidance does (sum of absolute profits and losses) —
[NEEDS VERIFICATION for the new Act]. That turnover number is an engine output.

### Stop 4: Capital gains — ss.67–91
**Read for:** why this head probably does NOT fit an active perps trader (capital
gains suit investors holding assets; derivatives with daily churn read as business),
but note the computation mechanics (s.72, old 48) anyway: full value of consideration
minus cost of acquisition — in which currency, converted when? The FX-at-event-time
problem you already designed for lives here. Also note which section governs
non-equity short-term gains rates for the residual case.
**Engine implication:** you likely do NOT ship a capital-gains treatment for perps in
v1 — but the writeup must say WHY, citing the business-vs-investment tests. One
paragraph, huge credibility.

### Stop 5: Set-off chapter — ss.108–121
**Read for:** the general rules (same head first, then inter-head), then the three
jails: s.113 speculation losses, the specified-business rules, and the VDA
no-set-off override from Stop 2 sitting on top of all of it. Draw the flowchart:
for each treatment, which losses net against which gains, what carries forward, for
how many years. That flowchart IS `treatments.py` — literally its control flow.

### Stop 6: TDS — inside s.393 (old 194S)
**Read for:** who must deduct 1% on VDA transfers, the thresholds for "specified
persons", and the mechanics when there is NO Indian intermediary — a foreign DEX
deducts nothing. Does the liability shift to the buyer/user? [The old-Act guidance on
P2P said the buyer must deduct — verify the new Act's words.] This is a live
compliance gap for every self-custody trader.
**Engine implication:** `tds_summary()` reports what SHOULD have been deducted per
event, notes nothing was, and flags the open question. That honest gap-surfacing is
worth more than pretending the answer is known.

### Stop 7: The surveillance layer — s.509 and s.510
Your navigator shows these plainly: s.509 "Obligation to furnish information on
transaction of crypto-asset" (old 285BAA) and s.510 (Annual Information Statement).
**Read for:** who reports crypto transactions to the department, and what lands in
the taxpayer's AIS. Then internalize the taxpayer's-mind consequence: **the
department may already see the on-ramp** (Indian exchange reports the INR→USDC buy)
**and sees nothing after the withdrawal to self-custody** — a visible entry with no
visible exit. That asymmetry is precisely what triggers notices. Your engine's ledger
is, functionally, the missing half the taxpayer needs when that notice arrives.
**Engine implication:** frame the report as "reconciles what the AIS shows with what
actually happened on-chain." That one sentence is your best marketing and it comes
straight from s.509.

### Stop 8: Foreign-asset disclosure (the trap almost every DEX user misses)
Search the Act and the return schedules for the foreign-asset disclosure regime
(old-regime Schedule FA in the ITR; residents must disclose foreign assets —
and the Black Money Act, a SEPARATE statute, attaches severe penalties for
non-disclosure). The unresolved question: is a self-custody wallet or a balance on a
foreign DEX a "foreign asset"? Custodial foreign exchanges: strongly argued yes.
Self-custody: genuinely contested (where IS the asset?).
**Engine implication:** the report includes a "disclosure checklist" box — not
computing anything, just listing what a CA will ask: Schedule FA applicability,
Schedule VDA rows, AIS mismatches. [NEEDS VERIFICATION: how the 2025 Act and current
ITR forms handle this — check the year's actual return forms, not just the Act.]

### Stop 9: Returns and Schedule VDA mechanics — ss.263+ (old 139 family)
**Read for:** filing obligations, and then go OUTSIDE the Act to the actual ITR form
(ITR-2/ITR-3) and look at Schedule VDA's columns: date of acquisition, date of
transfer, cost, consideration — PER TRANSFER. Now confront the practical absurdity:
your taxpayer has thousands of fills. Does each fill get a row? Aggregation
conventions are unsettled.
**Engine implication:** your Schedule-VDA-shaped CSV output (Phase 6 of the master
plan) should offer both granularities: per-fill and per-episode aggregate, with a
note that the correct granularity is a CA question.

---

## PART 3 — The taxpayer's mind: hacks, traps, and grey zones to collect as you read

Collect these into the writeup as you encounter their statutory basis. Never present
them as advice — present them as "questions the law leaves open," each traceable to
the exact words that create the opening.

1. **Gross vs net under the VDA regime** (Stop 2) — the no-set-off wall. The most
   consequential unknown for any active trader. Your engine quantifies it per user.
2. **Which regime even applies to an equity perp** (Stops 1+3) — VDA vs speculative
   business vs non-speculative F&O-style. Three readings, three wildly different
   liabilities. The core of the whole artifact.
3. **Funding payments** — income when received? cost when paid? part of the position?
   Nothing in the Act speaks to them. Every choice is defensible; your engine makes
   the choice a visible parameter.
4. **The on-ramp/off-ramp boundary** — INR→USDC is itself a VDA acquisition; the later
   USDC→INR is a disposal with its own gain/loss (USDC is not exactly ₹-stable —
   USDINR moves!). Micro gains/losses on the stablecoin leg are real and almost
   universally unreported. Your engine computes them from the manual-input on-ramp
   cost. Nobody else does this.
5. **The AIS asymmetry** (Stop 7) — visible entry, invisible exit. The notice trigger.
6. **Foreign-asset disclosure** (Stop 8) — the Black Money Act shadow over every
   self-custody wallet. Disclosure checklist, not computation.
7. **Turnover and audit** (Stop 3) — a leveraged trader's F&O-convention "turnover"
   can cross audit thresholds shockingly fast. An engine-computed warning.
8. **TDS orphan liability** (Stop 6) — 1% that nobody deducted; whose problem is it?
9. **Regime straddle** (Part 0) — FY 2025-26 under the old Act, later years under the
   new one. Same trades, two rulebooks. Parameterize.

---

## PART 4 — Reading schedule (realistic, alongside everything else)

| Session | Material | Output |
|---|---|---|
| 1 (2h) | Part 1 technique + s.2 definitions + s.3 | VDA-definition clause map; first CA questions |
| 2 (2h) | s.194 (VDA regime) + navigator cross-check to 115BBH | the no-set-off flowchart; gross-vs-net spec |
| 3 (3h) | ss.26–66 skim, deep on speculative definition + deductions + audit | speculative-chain memo; expense list; turnover rule |
| 4 (1h) | ss.67–91 skim | one-paragraph "why not capital gains" |
| 5 (2h) | ss.108–121 | the set-off flowchart = treatments.py control flow |
| 6 (2h) | s.393 (TDS) + s.509–510 | TDS spec; AIS-asymmetry paragraph |
| 7 (2h) | Foreign-asset regime + actual ITR Schedule VDA form | disclosure checklist; CSV column spec |

~14 focused hours. After session 7 you will know this corner of the Act better than
most practising professionals, because almost none of them have traced a DEX perps
trader through it end to end. That is the entire point.

## PART 5 — Rules for yourself while reading

- The Act's words beat every explainer, every blog, every model (including me). When
  a secondary source disagrees with the section in front of you, the section wins;
  note the discrepancy for the CA.
- Every rule you extract gets written as a testable statement ("a loss on a VDA
  transfer cannot reduce the gain on another VDA transfer") — those statements become
  fixtures in `test_treatments.py` almost verbatim.
- Every ambiguity gets written as a neutral question, never a conclusion. You are
  building the best-stated open questions in this space, co-signed by a CA — not
  legal opinions from a 19-year-old.
- Log the exact section number (BOTH acts, via the navigator) next to everything.
  Traceability is what separates your document from every crypto-tax blog post.
