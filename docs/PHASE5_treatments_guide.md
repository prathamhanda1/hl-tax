# PHASE 5 — The Interpret Layer (`interpret/treatments.py`)
### Guide for the coding agent: four tax treatments, side by side, no winner declared

This document specifies the `interpret/` layer of the Hyperliquid perps tax engine.
It is the legal heart of the tool. Everything upstream (fetch → load → reconstruct →
fx) produces one clean **INR ledger**; this layer reads that ledger and computes the
tax position under **four named treatments**, prints them side by side, and declares
none of them correct.

**Read this whole file before writing any code.** The arithmetic is easy; the legal
reasoning is where a confident, beautifully-formatted, *wrong* tax number gets born.
Every rule below is written so it can be encoded and unit-tested.

---

## 0. NON-NEGOTIABLES (carry from the master plan — do not violate)

1. **No treatment is labeled "correct."** The tool computes; the CA advises. Any
   string, comment, variable name, or ordering that implies one reading is the right
   one is a bug. Order them by section number, not by preference.
2. **Every assumption is an explicit parameter with a default, echoed into output.**
   Funding treatment, fee deductibility, loss-scope, settlement-rail premise, FX
   convention — never a buried constant. Two runs with different assumptions must be
   visibly different documents.
3. **Every output number is traceable** to the ledger rows that produced it. A summary
   figure that cannot be drilled down to line items is not shippable.
4. **Pure functions.** DataFrames/dicts in, dicts out. No I/O, no network, no printing
   inside the treatment functions. `present/` renders; `interpret/` only computes.
5. **The law is unsettled. Say so, in the output, near the top.** The tool's only
   claim is arithmetic under stated assumptions.
6. **All section numbers below are NEEDS-VERIFICATION at build time** against the bare
   Act text. They are given in BOTH the Income-tax Act 1961 (what every CA and every
   FY 2025-26 filing still uses) and the Income-tax Act 2025 (effective for tax years
   from 1 April 2026), mapped via the navigator PDF. Model memory is not a source.

---

## 1. THE INPUT CONTRACT (what phases 3+4 hand this layer)

One tidy INR ledger — a DataFrame, one row per economic event, already FX-converted.
The treatment functions must not recompute P&L or re-fetch anything. Expected columns:

| column | meaning |
|---|---|
| `event_id` | stable unique id, traces back to a raw fill/funding/ledger row |
| `ts_utc` | tz-aware UTC timestamp of the event |
| `category` | one of: `realized_pnl`, `funding`, `fee`, `deposit`, `withdrawal` |
| `asset` | e.g. `BTC`, `xyz:TSLA` |
| `asset_class` | `crypto_perp` \| `equity_perp` \| `stablecoin_leg` \| `other` (from Phase 2 tagging) |
| `episode_id` | which open→close episode this belongs to (from Phase 3) |
| `amount_usd` | signed USD amount (P&L sign convention documented in Phase 3) |
| `fx_rate` | INR per USD used for this event |
| `fx_rate_date` | the business day whose rate was used (audit trail) |
| `amount_inr` | `amount_usd * fx_rate`, computed once, upstream |
| `is_liquidation` | bool, for the closing event |

**Two things this ledger does NOT contain and the tool must handle explicitly:**

- **The on-ramp cost of the USDC/USDT itself.** Hyperliquid cannot see what INR was
  paid to acquire the stablecoin on an Indian exchange. This is a **manual-input
  slot** (`onramp_cost_inr`, `onramp_units`). Without it, the stablecoin-leg gain/loss
  cannot be computed and the VDA treatment covers only the on-Hyperliquid leg. State
  this boundary loudly in output.
- **Non-Hyperliquid activity** (spot, vaults, other venues). Out of scope; warn if the
  address shows it.

**Decompose P&L from stablecoin-conversion impact (confirmed by the incumbent).**
CoinDCX's own Futures/Global-Futures report separates **Gross P&L (INR)** from a
distinct **"USDT Settlement Amount (INR) — the INR impact of USDC/USDT↔INR conversion"**
line, then nets to **Net P&L (INR)**. Mirror this exactly. The ledger (or a derived
view) must expose, per episode:
- `gross_pnl_inr` — the trading result measured in the settlement token, converted at
  the trade-date rate;
- `conversion_impact_inr` — the additional INR gain/loss from the stablecoin moving
  against INR between acquisition and disposal of the *stablecoin itself*;
- `net_pnl_inr = gross_pnl_inr + conversion_impact_inr − fees`.
This split is not cosmetic: under the slab readings the `conversion_impact` is the
*only* part that is even arguably a VDA event, and under OP 1 it is the cleanest VDA
event. Keeping them separate is what lets one number feed two different legal buckets
without double-counting (the exact trap from the worked ₹2L→₹4L example).

---

## 2. THE TWO ORTHOGONAL QUESTIONS (why there are exactly four operations)

The four treatments are **not four rival descriptions of one universe.** They are the
answers to two independent questions. Encode this taxonomy explicitly — it is the
"airtight reasoning" the CA will check.

```
Q1 — RATE REGIME: Is a perp close a "transfer of a VDA"?
        YES ──────────────► Special regime: flat 30% (s.115BBH / 2025 s.194)      → OP 1 (VDA)
        NO  ──────────────► Ordinary income at slab rates ─┐
                                                            │
Q2 — (only if ordinary) SPECULATIVE OR NOT?                 │
        Settlement premise = INR-margined / VDA abstracted  ├─► OP 2 (FUTURES-IN-INR)
        Cash-settled, foreign DEX, ring-fenced losses       ├─► OP 3 (SPECULATIVE)
        Business on the merits, normal loss set-off         └─► OP 4 (NON-SPECULATIVE)
```

**Consequence you must internalize:** OP 2, OP 3, and OP 4 are all *slab-rate* readings
and will sometimes produce the **same headline number** in a single net-profit year.
They diverge on **loss treatment, carry-forward, TDS exposure, and audit/reporting
duties** — and those divergences are the whole point. The report shows both where the
numbers coincide and where the *premises and risks* differ. Do not collapse them into
one just because a given address's arithmetic happens to match this year.

**Settlement rail — what it does and does NOT do (read carefully; corrected).**
Earlier drafts of this project treated settlement currency as a switch that flips the
perp **P&L** into the 30% VDA regime. That is wrong, and the incumbent evidence says so.
A FIU-registered Indian exchange (CoinDCX) treats **both** its INR-margined futures
**and** its USDT-settled *Global Futures* as **slab-rate, with no TDS on the P&L**, on
the stated reasoning that a derivative does not involve ownership or transfer of the
underlying crypto (see the confirmed-positions appendix, §14). So:

- **The perp P&L is not a VDA transfer under the prevailing reading — regardless of
  whether it settles in INR or USDC.** OP 1 (30% on the P&L) is therefore the
  *minority / maximum-caution* reading that the domestic market actively rejects for its
  own USDT-settled product. Still computed (no CBDT circular blesses either side, and
  s.194S's text references perpetual contracts), but presented as the conservative
  outer bound, not the default.
- **What settlement rail actually governs is the STABLECOIN LEG, not the P&L.** Under
  USDC settlement a VDA *is* disposed of when you convert USDC→INR (a spot sale) — that
  leg carries the 30%/TDS exposure. Under a truly INR-margined product no token is ever
  touched, so even that leg disappears. This is the real, encodable effect of the rail:
  it toggles the **stablecoin-leg** tax and reporting, not the rate on the trading P&L.
- **For a real HL address**, the live contenders on the P&L are OP 3 and OP 4 (slab,
  differing on loss set-off), with OP 1 as the conservative bound; OP 2 (fully
  INR-margined) is **counterfactual** — no HL flow is INR-margined — and exists only as
  the "zero-VDA-anywhere" benchmark. Flag OP 2 counterfactual whenever `asset_class`
  shows real stablecoin legs.

---

## 3. SHARED PARAMETERS (defaults; every one echoed into output)

```python
@dataclass
class Assumptions:
    # --- rate / regime knobs ---
    vda_flat_rate: float = 0.30            # s.115BBH / 2025 s.194  [VERIFY]
    cess_rate: float = 0.04               # health & education cess on tax
    surcharge_schedule: dict = ...         # slab-based; [VERIFY current Finance Act]
    slab_regime: str = "new_115BAC"        # "new_115BAC"(2025 s.202) | "old"  [VERIFY rates]

    # --- VDA loss scope: compute BOTH, show the gap (see §4) ---
    vda_loss_scope: str = "no_set_off_at_all"   # | "intra_vda_allowed"

    # --- cross-cutting event treatment (apply identically across all 4 ops) ---
    funding_treatment: str = "separate_taxable_event"
        # | "position_cost" | "ignore"  — the Act is SILENT; this is a live CA question
    fee_deductible_slab: bool = True       # fees deduct under business/spec readings
    fee_deductible_vda: bool = False       # under 115BBH only cost of acquisition
    include_stablecoin_legs: bool = True   # the INR→USDC→INR VDA micro gain/loss

    # --- settlement premise for OP 2 ---
    treat_settlement_as_inr: bool = False  # True only models the INR-margined counterfactual

    # --- audit / turnover ---
    audit_turnover_threshold_inr: float = 1e7   # s.44AB / 2025 s.63  [VERIFY threshold + cash%]
```

Rule: if a parameter changes the number, it is a parameter, not a literal. If the CA
would want to argue it, it is a parameter.

---

## 4. OPERATION 1 — `treatment_vda()`  (the special 30% regime)

**Legal reading.** The perp references a VDA and settles in a stablecoin (itself a
VDA), so gains on closing are treated as **income from transfer of a VDA**.

**Sections.**
- VDA definition: **1961 s.2(47A)** → **2025 s.2** (definitions). Object of the charge
  must be a "token… generated through cryptographic means… that can be transferred,
  stored or traded electronically." A *contract/position* is arguably not that token —
  this is the hinge weakness; state it.
- Charge: **1961 s.115BBH** → **2025 s.194** (Tax on certain incomes, the 115B family).
- "Transfer": **1961 s.2(47)** (sale/exchange/relinquishment/**extinguishment of
  rights**) — borrowed from the capital-asset world; whether *closing* a perp is
  "extinguishment of rights in a VDA" is an open sub-question.
- TDS: **1961 s.194S** → **2025 s.393** (see §8).

**Computation.**
```
base_gain = Σ amount_inr WHERE category=realized_pnl AND amount_inr > 0
# plus, if include_stablecoin_legs: gain on the USDC/USDT off-ramp vs on-ramp cost
# plus funding, IF funding_treatment == "separate_taxable_event" AND funding > 0
if vda_loss_scope == "no_set_off_at_all":
    taxable_base = base_gain                      # losing episodes contribute ZERO
elif vda_loss_scope == "intra_vda_allowed":
    taxable_base = max(0, Σ all VDA amount_inr)    # net, but never below zero
tax = taxable_base * vda_flat_rate
tax = tax * (1 + cess_rate) + surcharge(...)
```

**The rules that make this brutal (unit-test each):**
- **No expense deduction except cost of acquisition.** `fee_deductible_vda=False`.
  Trading fees, funding paid, gas — none reduce the base.
- **No loss set-off.** Under the strict reading a loss on one VDA transfer cannot
  reduce the gain on another (widely read this way — VERIFY the words), and cannot
  touch any other income.
- **No carry-forward.** A net loss year produces a taxable base of **zero**, not a
  negative number, and nothing carries to next year.
- **Compute BOTH `vda_loss_scope` readings and report the gap.** Gross-of-losses vs
  intra-VDA-net is the single most shocking number in the report for a
  high-round-trip trader. That gap falls straight out of one section's wording.

**Caveats to print with this treatment.**
- Rests on the weakest premise of the four: that a cash-settled derivative *is* a
  transfer of the underlying VDA, when no underlying coin is acquired or disposed.
- **This reading is actively rejected by the domestic market for the P&L.** CoinDCX's
  current help center states the 30% VDA rate does **not** apply to Futures/Options and
  that these are taxed at slab, because they do not involve ownership or transfer of
  crypto (§14). Present OP 1 honestly as the **conservative / worst-case** bound a
  cautious filer or an aggressive assessing officer might assert — not as the market
  norm. It is not off the table (no CBDT circular; s.194S references perpetual
  contracts), but it is the minority position on the trading P&L.
- **The no-set-off / no-carry rule itself is confirmed** for VDA (spot) by CoinDCX:
  VDA losses cannot be set off against any income or carried forward (§14). So the
  brutal loss rules above are solid *for anything that is genuinely a VDA transfer* —
  the live question is only *whether the perp P&L is one*, not what happens if it is.
- Even under this reading, the stablecoin *conversion* legs (INR→USDC, USDC→INR) are
  the clearest VDA transfers of all — often the only unambiguous VDA events in the
  whole timeline. Do not omit them (`include_stablecoin_legs`).
- Surcharge on VDA income has had special treatment historically — VERIFY the current
  cap/schedule rather than assuming the ordinary slab surcharge.

---

## 5. OPERATION 2 — `treatment_futures_inr()`  (INR-margined / VDA-abstracted, slab)

**Legal reading.** The cleanest slab case: an **INR-margined** product where margin,
settlement and P&L are all in rupees, so *no VDA is transferred at any point* —
**s.115BBH never engages**, **s.194S TDS does not apply**, and profit is ordinary
income at **slab rates**. This is the strongest form of the "no token touched" argument.

**Important scope correction (per §14).** CoinDCX applies the *slab, no-TDS-on-P&L*
treatment to **all** its futures — the INR-margined product **and** the USDT-settled
*Global Futures* — on the reasoning that a derivative involves no ownership/transfer of
crypto. So the slab reading is **not** exclusive to INR settlement; USDT-settled perps
taxed at slab are captured by **OP 3 / OP 4**, not OP 2. What makes OP 2 distinct is the
*fully rupee* fact pattern where **zero VDA touches the flow at any point** — so even
the stablecoin-conversion leg (which OP 3/OP 4 still carry) disappears. OP 2 is the
"no-VDA-anywhere" benchmark; it is **counterfactual for any real HL address**.

**Sections.**
- Charge as business income: **1961 s.28** → **2025 s.26** (Income under head PGBP).
- Deductions: **1961 s.37** general → **2025 s.34** (general conditions for allowable
  deductions). Slab rate for individuals: **1961 s.115BAC** → **2025 s.202**.
- Why 115BBH is switched off: the object transferred is an INR contract, not a
  s.2(47A) token. Why 194S is switched off: no "consideration for transfer of a VDA."

**Computation.**
```
net_pnl  = Σ amount_inr WHERE category=realized_pnl            # gains AND losses net
funding  = Σ amount_inr WHERE category=funding  (per funding_treatment)
fees     = Σ amount_inr WHERE category=fee       (deductible: fee_deductible_slab)
taxable  = net_pnl + funding - fees               # can be negative → a real loss
tax      = slab_tax(taxable, slab_regime) if taxable > 0 else 0
# plus: this op ALSO carries a set-off/carry-forward flavor — default here is the
# taxpayer-favorable NON-SPECULATIVE netting (see §7 for why, and OP 3/OP 4 for the poles)
```

**What distinguishes OP 2 from OP 4 (they can share arithmetic):**
- OP 2's premise is **INR settlement / VDA abstracted away** — so it *also* concludes
  **no 194S TDS** and **no Schedule VDA reporting obligation**. OP 4 reaches slab via
  "it's business income on a VDA-referencing derivative," which may **coexist with
  residual 194S / VDA-reporting risk.** Same number, different risk surface. Emit both.

**Caveats to print (critical — do not soft-pedal).**
- **For a real Hyperliquid address this treatment is COUNTERFACTUAL.** HL is
  USDC-settled; a token *does* move. OP 2 is valid only if one accepts the
  custodial-abstraction premise (§7) that the stablecoin leg can be disregarded. When
  the ledger shows genuine `stablecoin_leg` events, tag OP 2's output
  `premise=counterfactual` in bold.
- No CBDT circular blesses the "derivatives aren't VDAs" reading; it is interpretive.
- An exchange help page is **not law and not a CBDT position**, and venues have a
  commercial incentive toward the slab reading (derivatives volume). Cite it as a
  stated industry position, never as authority.

---

## 6. OPERATION 3 — `treatment_speculative()`  (slab, but losses ring-fenced)

**Legal reading.** A contract **settled otherwise than by actual delivery** is a
*speculative transaction*. A cash-settled crypto perp is exactly that. The 1961 Act
carved *out* of "speculative" the exchange-traded derivatives on a **recognised
(Indian) stock exchange** — a **foreign DEX does not qualify** for that carve-out. So
the default for a foreign cash-settled perp is **speculative business income**.

**Sections.**
- Speculative-transaction definition: **1961 s.43(5)** → **2025** within the PGBP
  interpretation provisions **ss.26–66** (VERIFY exact clause + the recognised-exchange
  carve-out wording in the 2025 Act).
- Speculation-loss set-off jail: **1961 s.73** → **2025 s.113** (set off & carry
  forward of losses of a speculation business).

**Computation.**
```
spec_pnl = net_pnl + funding - fees      # same base construction as OP 2/4
if spec_pnl > 0:
    tax = slab_tax(spec_pnl, slab_regime)
else:
    tax = 0
    carry_forward_speculative_loss = spec_pnl   # usable ONLY against future speculative gains
```

**The jail rules (unit-test each):**
- Speculative **losses set off only against speculative gains** — not against salary,
  capital gains, other business, or other-source income.
- Carry-forward limited to **4 assessment years** (VERIFY under 2025 Act) and again
  only against speculative income.
- Speculative *income* is still taxed at **slab** like other business income — it is
  the **losses**, not the rate, that are ring-fenced. (Common misconception: encode
  the rate as slab, not as a penalty rate.)

**Caveats to print.**
- This is arguably the **default slab reading** for a foreign-DEX cash-settled perp if
  the VDA regime does not capture it — i.e. the residual once OP 1 and OP 2 are set
  aside. Say so neutrally.
- Whether *equity* perps (`xyz:TSLA`) vs *crypto* perps (`BTC`) sort differently under
  the speculative definition is unresolved — that is why Phase 2 tagged `asset_class`.
  Compute the split so the CA can apply a distinction if one turns out to matter.

---

## 7. OPERATION 4 — `treatment_non_speculative()`  (slab, normal loss set-off)

**Legal reading.** The trading is a **business** (regularity, volume, systematic
intent — a hundreds-of-round-trips-a-year trader plausibly qualifies), and the
contracts are argued into **non-speculative** derivative treatment (as exchange-traded
F&O is), so **normal business-income rules** apply: expenses deduct, losses set off
broadly, slab rates.

**Sections.**
- Business charge / deductions: **1961 s.28 / s.37** → **2025 s.26 / s.34**.
- Current-year set-off across heads: **1961 s.71** → **2025 s.109** (business loss may
  offset other heads **except salary** — encode the salary exception).
- Carry-forward of business loss: **1961 s.72** → **2025 s.112** (up to **8** AYs,
  against business income only; VERIFY).
- Books & audit: **1961 s.44AA / s.44AB** → **2025 s.62 / s.63**.

**Computation.**
```
biz_pnl = net_pnl + funding - fees
if biz_pnl > 0:
    tax = slab_tax(biz_pnl, slab_regime)
else:
    tax = 0
    current_year_setoff = biz_pnl   # against any head EXCEPT salary this year (s.71/109)
    carry_forward_biz_loss = residual  # 8 yrs, business income only (s.72/112)
```

**Audit / turnover warning (an engine output, all slab ops).**
- Compute **derivative "turnover"** the ICAI-guidance way: **sum of absolute values of
  favourable and unfavourable differences** (i.e. Σ|realized_pnl per episode|), NOT net
  P&L, NOT notional. VERIFY the convention survives under the 2025 Act.
- If turnover crosses `audit_turnover_threshold_inr`, emit a **tax-audit-likely
  warning** (s.44AB / 2025 s.63) and a **books-of-account** flag (s.44AA / 2025 s.62).
- A leveraged trader crosses these thresholds shockingly fast — this warning is one of
  the most practically useful things the tool prints.

**Caveats to print.**
- Requires defending "business" (vs investment/hobby) on the facts — regularity,
  volume, infrastructure. The tool can *count* trips and volume to support the
  argument but must not *assert* the conclusion.
- Under the VDA reading these same losses are dead; under this reading they are the
  taxpayer's most valuable asset. The delta between OP 1 and OP 4 on a loss-heavy
  address is the headline number of the whole report.

---

## 8. `tds_summary()`  — 1% TDS, computed and flagged, never assumed resolved

**Sections.** **1961 s.194S** → **2025 s.393** (Tax to be deducted at source).

**The confirmed industry rule (from §14) — TDS attaches to the STABLECOIN LEG, not the
perp P&L.** CoinDCX states plainly that **TDS is not applicable on Futures, Options, or
Global Futures**, because these do not involve ownership/transfer of crypto — and this
holds even for USDT-settled Global Futures. TDS instead attaches to **spot VDA
disposals**: selling USDT/USDC for INR, or any crypto-to-crypto trade. Encode exactly
that split:

- **The perp trading P&L → `tds_expected = 0` under every reading EXCEPT OP 1.** This is
  now the mainstream position, not a guess. Only OP 1 (the minority VDA-on-P&L reading)
  notionally puts 1% on each perp close, and even then no Indian intermediary exists to
  deduct it on a foreign DEX.
- **The stablecoin conversion legs → this is where 194S actually lives.** Compute 1% of
  the **gross consideration** on each USDC→INR off-ramp (and each C2C hop, both sides —
  see below). This exposure exists under *all four* readings, because converting the
  stablecoin to INR is a spot VDA sale regardless of how the trading P&L was classified.
- **TDS is on gross value, independent of profit or loss** (confirmed §14: TDS is
  deducted on eligible sells "whether the trade resulted in a profit or loss"). So a
  losing trader who still off-ramps ₹4L of USDC has a 1%-of-₹4L expected-TDS figure.
  Do NOT gate `tds_expected` on P&L sign.
- **Crypto-to-crypto = TDS on BOTH sides** (confirmed §14). If the ledger shows the
  trader routed through another token (e.g. BTC→USDT before withdrawal) rather than a
  clean INR→USDC→INR path, count both legs. Detect and flag C2C hops.
- **Foreign-DEX reality:** on Hyperliquid itself there is **no Indian intermediary** to
  deduct, so on-venue `tds_actually_deducted ≈ 0`. TDS that *was* withheld happens at
  the **Indian-exchange ramp** (the FIU-registered CEX where USDC is bought/sold), and
  that amount is **creditable / refundable** against final liability — never an extra
  tax. Reconcile the ramp CEX's TDS certificate against the ledger (see §10).
- **Thresholds:** ₹50,000 / ₹10,000 per year by taxpayer category (VERIFY current
  figures + 2025-Act equivalents).
- **Counter-signal to surface honestly:** s.194S's own language references *perpetual
  contracts*, which cuts against the "derivatives never involve a VDA" premise. Note it;
  do not resolve it. The tool reports both the industry position and this tension.

Output an explicit three-line gap: **TDS expected on P&L (≈0 except OP 1) · TDS expected
on stablecoin legs (1% of gross off-ramp, all readings) · TDS actually deducted (at the
ramp CEX, from certificate)**. That honest decomposition is worth more than a single
number.

---

## 9. WHAT THE TOOL DELIBERATELY DOES NOT SHIP (state it, one paragraph)

- **Capital-gains treatment (1961 s.45 → 2025 s.67).** Excluded for an active perps
  trader: capital-gains treatment suits investors holding assets, not daily-churn
  derivative traders. One paragraph in the report explains *why not*, citing the
  business-vs-investment tests — huge credibility for one paragraph. Do not compute it.
- **Foreign-asset disclosure / Black Money Act** and **Schedule VDA row mechanics** are
  **checklist items, not computations** (see §10). The engine flags; it does not decide.

---

## 10. CROSS-CUTTING REPORT SECTIONS (feed these to `present/`, computed here)

- **On-ramp/off-ramp boundary box** — states that without `onramp_cost_inr`, the VDA
  numbers cover the HL leg only; computes the stablecoin-leg micro gain/loss when the
  manual input is supplied (USDINR moves, so this is rarely exactly zero).
- **AIS-asymmetry note** (**1961 s.285BAA → 2025 s.509** crypto-transaction reporting;
  **2025 s.510** AIS): the department may see the Indian-exchange on-ramp and nothing
  after withdrawal to self-custody — a visible entry with no visible exit, the classic
  notice trigger. The engine's ledger is the missing half.
- **Disclosure checklist** (not computed): Schedule FA applicability, Schedule VDA
  granularity (per-fill vs per-episode — offer both CSVs), AIS reconciliation.
- **Ramp-CEX TDS reconciliation box.** The only TDS the trader actually paid sits with
  the Indian exchange used to buy/sell USDC. Tell the user to pull that TDS certificate
  and reconcile it against the tool's expected-TDS-on-legs figure. Include the
  availability timeline (per §14): certificates exist for FY 2025-26, and from FY 2026-27
  become **quarterly** — Q1→30 Sep, Q2→31 Dec, Q3→31 Mar, Q4→30 Jun — so a filer in, say,
  July may not yet have the latest quarter's certificate. Flag this timing so users don't
  file against an incomplete TDS credit.
- **Incumbent-context note (for the user, not the report):** CEXes punt final tax
  computation to third parties — CoinDCX explicitly directs users to KoinX for the tax
  report and says it does not compute total liability itself (§14). Your tool is the
  DEX-side equivalent of that KoinX step; the CEX report is a *trade* report, not a tax
  report, so users genuinely lack this for Hyperliquid today.
- **Assumptions box**: every value from §3, verbatim, so the document is reproducible.
- **Limitations box, AT THE TOP**: law unsettled; no CBDT circular; tool computes
  arithmetic under stated assumptions only; not advice; get a CA to co-sign.

---

## 11. OUTPUT SCHEMA (each treatment returns this shape)

```python
{
  "treatment": "vda" | "futures_inr" | "speculative" | "non_speculative",
  "premise": "live" | "counterfactual",          # counterfactual when settlement rail contradicts it
  "rate_basis": "flat_30" | "slab",
  "taxable_base_inr": float,
  "tax_before_cess_inr": float,
  "cess_inr": float,
  "surcharge_inr": float,
  "total_tax_inr": float,
  "loss_treatment": "none" | "ring_fenced_speculative" | "normal_business",
  "current_year_setoff_inr": float,              # 0 for vda/speculative
  "carry_forward_inr": float,                     # 0 for vda
  "carry_forward_years": int | None,
  "tds_expected_inr": float,
  "tds_actually_deducted_inr": float,             # ≈0 on a foreign DEX
  "audit_flag": bool,
  "turnover_inr": float,                          # abs-sum convention
  "assumptions_used": {...},                       # the full Assumptions dataclass
  "section_refs": {"1961": [...], "2025": [...]},
  "caveats": [ "...", "..." ],                     # the printed caveats for this reading
  "line_items": DataFrame,                         # every event → its contribution, traceable
}
```

`interpret()` returns `{"vda": {...}, "futures_inr": {...}, "speculative": {...},
"non_speculative": {...}, "tds": {...}, "cross_cutting": {...}}`. No key named `best`,
`recommended`, or `correct`.

---

## 12. GATE 5 — the tests that must pass before Phase 6

Hand-compute every expected number BY HAND in the fixture comments first (paper before
code), exactly like Week 1's test book.

1. **VDA no-set-off** — a fixture with one +₹250k episode and one −₹50k episode: the
   VDA taxable base is **₹250k, not ₹200k**. The loss must NOT reduce it.
2. **VDA all-losses** — an all-losing fixture: VDA taxable base is **exactly 0**, never
   negative, carry-forward = 0.
3. **VDA gross-vs-net gap** — assert `no_set_off_at_all` ≥ `intra_vda_allowed`, and that
   the gap is reported.
4. **Speculative jail** — a speculative net loss does NOT reduce a non-speculative or
   other-head income in the same fixture; it only carries against future speculative
   gains.
5. **Non-speculative set-off** — a business loss offsets other-head income **except
   salary** in the current year (encode a salary line and assert it is untouched).
6. **Slab ops agree in a net-profit year** — OP 2 / OP 3 / OP 4 headline tax match on a
   single all-profit fixture; then a loss fixture makes them **diverge**, and the test
   asserts the divergence (proves they are genuinely different operations).
7. **Counterfactual flag** — a fixture with real `stablecoin_leg` rows makes OP 2 come
   back `premise="counterfactual"`.
8. **Turnover** — abs-sum turnover on a mixed win/loss fixture equals the hand figure,
   not net P&L.
9. **TDS split** — on a fixture with a losing perp P&L but a real USDC→INR off-ramp:
   `tds_expected_on_pnl = 0` under the slab ops (and >0 only under OP 1), while
   `tds_expected_on_legs = 1% × gross off-ramp > 0` under ALL ops (independent of the
   loss), and `tds_actually_deducted_on_venue = 0`. Asserts TDS rides the stablecoin
   leg, not the P&L, and doesn't gate on profit.
10. **C2C both-sides** — a fixture routing BTC→USDT before withdrawal produces a TDS-leg
    count on both sides of the hop, not one.
11. **Assumptions echoed** — changing `funding_treatment` changes at least one number
    AND is reflected in `assumptions_used`.

A phase is not done until its gate is green. If a slab op and a VDA op ever produce the
same number on the §12.1 fixture, a set-off rule is wired wrong — stop and fix it.

---

## 13. ONE-SCREEN SUMMARY FOR THE AGENT

- Four pure functions over one INR ledger. No I/O. No winner.
- Two questions decide the four: **is it a VDA transfer? if not, is it speculative?**
- **Settlement rail toggles the STABLECOIN LEG, not the P&L rate.** The prevailing
  industry reading (incl. CoinDCX Global Futures) taxes even USDT-settled perp *P&L* at
  slab with no TDS; TDS/30% ride the USDC↔INR conversion. HL is USDC → OP 2 (zero-VDA
  INR-margined) is a counterfactual benchmark, flag it; OP 3/OP 4 are the live P&L
  readings, OP 1 the conservative bound.
- VDA = 30% flat, **losses die, nothing carries** (confirmed for spot VDA). The other
  three = slab, differing on **loss set-off / carry / TDS / audit**, sometimes sharing a
  number but never a risk profile.
- Every section number is dual-cited (1961 + 2025) and **NEEDS-VERIFICATION**.
- Every assumption is a parameter, echoed in output. Every number traces to a row.
- Tests are hand-computed first, and the no-set-off wall is the one that must never
  silently break.

---

## 14. APPENDIX — CoinDCX confirmed positions (evidence base, scraped help center)

These are the incumbent's *stated* positions, taken from CoinDCX's Tax & Reports help
articles (dated ~8 days before this scrape). They are **industry positions, not law and
not CBDT circulars** — a FIU-registered exchange's operating stance, which carries a
commercial incentive toward the derivative-friendly reading. Use them to calibrate what
is mainstream vs conservative, never as authority. Paraphrased:

**On the 30% rate / classification**
- The 30% VDA rate applies to *eligible spot* crypto transfers. Futures, Options and
  other derivatives are **not** taxed at 30%; they follow ordinary Income-tax Act
  provisions and the taxpayer's **slab**. → validates OP 3 / OP 4 as mainstream; marks
  OP 1 (30%-on-P&L) as the minority/conservative reading.
- There is **no fixed rate** for F&O gains; it depends on the Act and the individual's
  slab and circumstances. → the tool must take slab + taxpayer context as input, not
  hardcode a rate for the slab ops.

**On VDA losses (spot)**
- VDA losses **cannot be set off** against any other income and **cannot be carried
  forward**. → confirms the OP 1 loss rules for anything that is genuinely a VDA
  transfer. The open question is only whether the perp P&L *is* one.

**On TDS**
- **No TDS on Futures, Options, or Global Futures**, because these "do not involve direct
  ownership or transfer of crypto assets." Global Futures is USDT-settled — so even a
  USDT-settled perp attracts **no TDS on its P&L** per the incumbent. → `tds_expected` on
  perp P&L = 0 under the slab ops; only OP 1 puts TDS on the P&L.
- TDS **is** deducted on eligible **sell** transactions, **regardless of profit or
  loss**. → TDS on the stablecoin off-ramp is on gross value, not gated on P&L sign.
- **Crypto-to-crypto trades attract TDS on BOTH sides** (a sale occurs on each leg). →
  count both legs when the trader routes through an intermediary token.
- TDS is deducted by the (Indian) exchange, reflected against PAN, and is
  **adjustable/refundable** at ITR. → creditable, not an extra tax; reconcile via
  certificate.
- A delisted token auto-converted to USDT may attract TDS on the forced conversion.
  → edge case; flag if such an event appears.

**On TDS certificates / reconciliation**
- Certificates exist for FY 2025-26; from FY 2026-27 they are issued **quarterly**
  (Q1→30 Sep, Q2→31 Dec, Q3→31 Mar, Q4→30 Jun), subject to government filing timelines.
  → warn users filing before their latest quarter's certificate is out.

**On reports / who computes tax**
- The exchange **Trade Report is not a tax report** and is **not sufficient to file** an
  ITR by itself. CoinDCX does **not** compute final tax liability; it directs users to a
  third party (KoinX) for a tax report. → positions this tool as the DEX-side equivalent
  of the missing tax-report step, which genuinely does not exist for Hyperliquid today.
- The Futures/Global-Futures report separates **Gross P&L (INR)** from **USDT Settlement
  Amount (INR)** (the USDC/USDT↔INR conversion impact) and **Fees (INR)**, netting to
  **Net P&L (INR)**. → mirror this three-way decomposition (see §1); it is what keeps the
  P&L layer and the stablecoin-VDA layer from double-counting.

**What CoinDCX does NOT resolve (still open, still the tool's job to surface):**
- Whether, absent the exchange's self-interested stance and with no CBDT circular, an
  assessing officer could still assert the VDA reading on the P&L (OP 1).
- The speculative vs non-speculative split (OP 3 vs OP 4) — CoinDCX says "slab" but not
  which loss-set-off regime applies; that is the whole point of keeping both.
- s.194S's reference to *perpetual contracts*, which sits in tension with the blanket
  "derivatives aren't VDAs" line.
