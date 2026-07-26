# Project brief: an open-source Indian tax + P&L engine for Hyperliquid perps

Context document. Paste this whole thing as the first message when planning this project.

---

## Who is building this

A 19-year-old Indian student, Python-only, comfortable with maths/stats/microeconomics.
Actively trades leveraged crypto perps and Indian equity, so understands perps, funding,
margin, liquidation from the trader's side.

Has just finished a prior project (Week 1) that collected a week of live Hyperliquid
order book and trade data over WebSocket and measured execution cost by hour of the
Indian day. That project produced: fluency with the Hyperliquid API, a tested
book-walking / VWAP / slippage module, and a working pandas analysis pipeline.

So: not a beginner with this API, but a beginner with tax code. Explain tax and
accounting concepts when they come up; don't over-explain the Hyperliquid parts.

---

## The goal, in one sentence

**Build a free, open-source, read-only tool that takes an Indian trader's Hyperliquid
wallet address and produces an INR-denominated P&L ledger plus their tax position
computed under each of the competing interpretations of Indian law — so they can take
a real document to a CA instead of guessing.**

---

## WHO THIS SERVES — read this before scoping anything

The tool requires a **public wallet address**. That single fact determines the entire
addressable audience, and it is easy to get wrong.

**It serves:** self-custody Hyperliquid traders. Anyone trading directly from their own
wallet, on the main dex or any HIP-3 builder dex. This is a real, currently unserved
population, and it is the whole user base of v1.

**It does NOT serve, without more work:** users of *custodial* Indian platforms. If a
platform takes INR over UPI, runs the USDC and the Hyperliquid position on its own
books, and returns INR to the user's bank — then the wallet on-chain belongs to the
*platform*, not the user. There is no address for that user to paste. Their trade
history lives in the platform's internal database, not on-chain.

Do not skip past this. A tool that assumes a wallet address is architecturally unable
to serve a custodial platform's users, no matter how good the tax logic is. If serving
those users ever becomes the goal, it requires a completely different input path — a
CSV export from the platform, or an API integration the platform has to build and
authorise. That is a partnership, not a scraper.

**What this means strategically:** for a custodial platform, this tool is not something
their users can run. It is (a) a demonstration of exactly the tax engine they will
eventually have to build in-house, and (b) a serious statement of the tax question
their users are going to face — see below.

---

## Why this matters (the reasoning, not the pitch)

### 1. It does not exist
Zerodha's tax P&L statement is table stakes for any Indian broker. Every Indian trader
needs a document in March. Nobody has built one for perps — not for Hyperliquid, not
for Delta, not for Pi42. Traders are currently doing this by hand in spreadsheets, or
not at all.

### 2. The tax treatment is genuinely unsettled, and the ambiguity is existential
This is the intellectual core of the project, and the reason it's interesting rather
than clerical.

A USDC-settled equity perpetual future on a foreign DEX could plausibly be treated as:

- **A VDA transfer under s.115BBH** — flat 30%, **no set-off of losses against gains,
  no deduction of expenses other than cost of acquisition**. For a leveraged trader
  doing hundreds of round trips a year, this is not a tax, it is a wall: every winning
  trade taxed at 30%, every losing trade worth nothing.
- **Business income from derivatives (F&O-style)** — losses net off, expenses deduct,
  slab rates apply, books of account and possibly audit required.
- **Speculative business income** — a third regime with its own loss set-off rules.

The difference between these is not marginal. It determines whether an active perps
trader in India can profitably exist at all.

Also live and unresolved in the same area:
- 1% TDS under s.194S and whether/how it applies to a foreign DEX with no Indian
  intermediary to deduct it.
- Whether the "VDA" definition reaches a derivative *referencing* a VDA/equity, or
  only the VDA itself.
- Schedule VDA reporting mechanics for a wallet with thousands of fills.

### 2b. The custodial abstraction question — the sharpest unresolved issue

A new class of Indian platform is emerging that presents this as ordinary leveraged
US-stock trading: the user sends INR over UPI, sees a familiar broker-style screen with
TSLA/NVDA/AAPL, and withdraws INR to their bank. The crypto layer — INR → USDC → a
USDC-settled perp on a foreign DEX → USDC → INR — happens on the platform's books. A
retail user may complete the entire round trip without ever registering that they
touched a virtual digital asset.

**Tax law looks at substance, not at the interface.** The conversion happened. The
question is *who is deemed to have done it*:

- **Reading A — the user transacted in a VDA.** The platform is an agent/intermediary;
  the user acquired and disposed of USDC and a VDA-referencing derivative. s.115BBH
  and s.194S consequences attach to the *user*, whether or not the app ever said the
  word "crypto." Note that at least one operator in this space has publicly described
  the treatment this way — 30% on VDA gains on the deposit conversion, 1% TDS, with
  trade gains treated separately — which implies this reading.
- **Reading B — the user holds an INR-denominated contractual claim against the
  platform.** The user never held a VDA; the platform did, on its own account. The
  user's position is a receivable, taxed on a different footing entirely.

Same user, same screen, same rupees in and out — two completely different tax outcomes.
This is not an academic distinction; it decides whether a retail user has an unreported
Schedule VDA obligation they have no idea exists.

Nobody has resolved this publicly. Stating the question precisely, with worked numbers
under each reading, is a genuine contribution — and it is a question the platforms
themselves will have to answer before their first tax year closes.

**Handle this carefully and neutrally.** The point is *not* to accuse any operator of
concealment — the mechanics are typically disclosed in listings and documentation, and
abstracting infrastructure away from users is normal product design, not deception. The
point is that a real, unresolved legal question sits underneath a UX that gives the user
no reason to ask it. Write it as an open question about market structure, never as an
allegation about a company. An accusatory framing would be both unfair and
self-defeating.

**IMPORTANT FRAMING:** the tool must NOT pick a winner. It computes the arithmetic
under each interpretation, side by side, and lets the user and their CA decide. The
value is in stating the question precisely with worked numbers underneath it — not in
resolving the law. Nobody building this is a tax advisor, and the output must say so.

### 3. It is a distribution asset — but be precise about for whom
A free tax calculator reaches self-custody Hyperliquid traders and, by extension, the
broader Indian perps community — a population that overlaps heavily with the target
users of any new Indian perps platform. For a company stuck in closed beta pending
regulatory approval, which cannot ship product but can ship *tools*, that overlap is
the value: it reaches exactly the people they want, before they can sell to them.

What it is **not** is something their own (custodial) users could run. Do not pitch it
that way — the wallet-address constraint above makes that claim false, and a founder
will spot it in four seconds.

### 4. It is a real credential
It is simultaneously a technical artifact (API, data engineering, financial arithmetic),
a compliance artifact (tax analysis, CA-reviewed), and a distribution artifact (a thing
with users). Very few people can produce all three at once. It gets read by every
Indian crypto exchange and every Hyperliquid-ecosystem team hiring.

---

## Scope

### What it does
1. Takes a **public wallet address** (read-only, no keys, ever).
2. Pulls the full trade history: fills, funding payments, liquidations, deposits,
   withdrawals, ledger updates.
3. Reconstructs position-level P&L: realized gains/losses per closed position, using a
   stated and documented cost-basis convention (FIFO by default; the convention chosen
   must be explicit and configurable, because it changes the answer).
4. Converts every event to INR **at the rate prevailing at the time of that event**,
   using a documented, citable FX source (RBI reference rate). This is the fiddliest
   part of the whole project — see "hard parts" below.
5. Produces, side by side:
   - the raw INR ledger (every event, every conversion)
   - tax position under s.115BBH VDA treatment
   - tax position under F&O-style business income treatment
   - tax position under speculative treatment
   - a TDS summary
6. Exports: CSV ledger, a summary PDF/HTML the user can hand to a CA, and a
   Schedule-VDA-shaped table.

### What it explicitly does NOT do
- Never accepts a private key or API secret. **Read-only, always.** The moment there is
  a field where a user can paste a key, this becomes a phishing target and the project's
  credibility is dead. This must be stated in bold at the top of the README.
- Never gives tax advice or picks an interpretation.
- Does not place trades, does not sign anything.
- Does not do backtesting, signals, or strategy.

---

## The hard parts (where the actual work is)

1. **FX conversion at event time.** Every fill needs the INR/USD rate *at that fill's
   timestamp*, not today's rate, not the year's average. This requires sourcing a
   historical FX series, deciding a documented convention for out-of-hours events
   (perps trade 24/7; RBI publishes a rate on business days), and stating that
   convention clearly. Same conceptual shape as the UTC→IST conversion from Week 1:
   an event at a timestamp needs to know what the world looked like at that timestamp.

2. **Cost-basis reconstruction.** Partial closes, cross-margin, position flips
   (long → short in one fill), and liquidations all complicate "which lot did this
   close?" The convention (FIFO/LIFO/weighted average) must be explicit, configurable,
   and documented, because it materially changes the number.

3. **Funding payments.** A perp accrues funding continuously. Is each funding payment a
   separate taxable event? Part of the cost of the position? Deductible? This is
   genuinely unclear and must be surfaced as an explicit, user-visible assumption
   rather than silently baked in. Note: funding is a **holding cost**, structurally
   different from **execution cost** (spread/slippage) and from **fees** (paid to the
   venue). Conflating them is a rookie error.

4. **Liquidations.** A forced close is still a disposal. The tax treatment doesn't care
   that you didn't choose it.

5. **Namespaced HIP-3 assets.** Coins on builder-deployed dexes are namespaced as
   `dex:COIN` (e.g. `xyz:TSLA`). The `perpDexs` and `meta` info endpoints enumerate
   these. An equity perp (`xyz:TSLA`) may or may not be the same asset class, for tax
   purposes, as a crypto perp (`BTC`) — the tool should tag them separately so the
   distinction can be applied if it turns out to matter.

---

## Technical facts (verify each against current Hyperliquid docs before relying on it)

- Info endpoint: `POST https://api.hyperliquid.xyz/info`, no auth required.
- Relevant info types to investigate: `userFills`, `userFillsByTime`, `userFunding`,
  `userNonFundingLedgerUpdates`, `clearinghouseState`, `perpDexs`, `meta`.
- The info endpoint is weight-based rate limited — historical backfill for a heavy
  trader means many paginated requests, so pace them and cache aggressively.
- Prices and sizes arrive as **strings**. Convert once, at the load layer.
- Store UTC everywhere; convert to IST/INR only at the presentation layer.
- Unlike the order book (which has no historical endpoint), user fill history IS
  queryable retroactively — so this project does NOT need a week of live collection.

---

## Architecture principle to carry over from Week 1

**Separate the layers, and let each be independently rerunnable:**

```
FETCH    (dumb: API → raw JSON on disk, cached, never recomputed)
   ↓
LOAD     (raw JSON → clean typed tables; strings → floats exactly once)
   ↓
RECONSTRUCT (fills → positions → realized P&L, with an explicit cost-basis convention)
   ↓
CONVERT  (USD events → INR at event-time FX)
   ↓
INTERPRET (the same ledger → three tax treatments, side by side)
   ↓
PRESENT  (CSV / HTML / PDF for the CA)
```

The fetch layer caches to disk so re-running the analysis never re-hits the API. Every
layer downstream is pure and rerunnable. A bug in INTERPRET must never cost you the
fetched data.

**Test the arithmetic before trusting any output.** Hand-build a small fixture — a
handful of fills with numbers checkable on paper, one partial close, one position flip,
one liquidation, one funding payment — and assert the reconstructed P&L matches the
hand calculation exactly. Bugs in this kind of code do not throw exceptions; they
produce a confident, wrong, beautifully formatted tax document. This is software people
may file returns on. The test fixture is not optional.

---

## Non-negotiables

1. **Read-only. No key fields. Ever.** Bold, at the top of the README.
2. **Calculator, not advice.** "This computes three treatments so you can discuss them
   with your CA." Never "this is your tax liability."
3. **Get a practising CA to review the analysis and put their name on it.** This is what
   turns the document from a student's opinion into something a professional can act on.
   It is the single highest-leverage sentence in the eventual writeup.
4. **Every assumption visible.** Cost-basis convention, FX source and convention,
   funding treatment, TDS handling — all surfaced in the output, not buried in code.
5. **State the limitations prominently, near the top, not buried at the bottom.** The
   law is unsettled; the tool's only claim is arithmetic under stated assumptions.
6. **On the custodial abstraction question: analyse, never accuse.** Frame it as an open
   question about a new market structure, with both readings presented fairly. Never
   as a claim that any named company is hiding something from users or regulators.
   Name no company as an example without a concrete, citable, verifiable basis — and
   even then, prefer describing the structure generically. This is the difference
   between a paper people cite and a post that gets you sued or blacklisted.

---

## Definition of done

- Public GitHub repo, MIT or Apache licensed.
- A hosted page where a trader pastes a public address and gets their ledger.
- A written piece explaining the three interpretations, with worked numbers, reviewed
  and co-signed by a named practising CA.
- An honest limitations section.
- Zero fields anywhere that accept a private key.

---

## How to work with the person building this

- Go **one file at a time.** Do not emit the whole repo in one response — a repo they
  didn't read teaches them nothing and leaves them unable to maintain it.
- Explain unfamiliar tax/accounting concepts in one or two sentences when they come up.
  Don't skip them; don't lecture.
- Ask questions when the spec is ambiguous.
- If anything in this document contradicts current Hyperliquid docs or current Indian
  tax law, the primary source wins — say what changed.
- Tax law specifics in this document reflect a general understanding and are **not
  verified legal advice**. Every section reference and rate must be independently
  checked against the current Act and recent circulars before it goes into anything
  published.
