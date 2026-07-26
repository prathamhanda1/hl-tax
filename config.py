"""
config.py — every constant in the project lives here.

Rules (week2_master_plan.md Part 3):
- No magic numbers anywhere else in the codebase.
- Every value not yet verified against a primary source (Hyperliquid docs,
  the Income-tax Act, FBIL) is flagged NEEDS VERIFICATION.
- Tax rates and section references live here too (errors_in_plan.md #10),
  each with its IT Act 1961 reference AND its IT Act 2025 status. Editing a
  rate changes the output, so every report must echo the config values it
  was computed under.
"""

import os
from pathlib import Path

# =============================================================================
# Paths (defined first so later sections can build file paths from them)
# =============================================================================

# Anchored to this file, not to the current working directory. A serverless
# host invokes the app from a directory of its choosing, and a relative "data"
# would then resolve somewhere unintended — or not at all.
REPO_ROOT = Path(__file__).resolve().parent

# The only WRITTEN directory in the project. Overridable because serverless
# filesystems are read-only apart from /tmp: set HL_TAX_DATA_DIR=/tmp/hl-tax
# there. Local and container runs need no override and behave exactly as before.
DATA_DIR = Path(os.environ.get("HL_TAX_DATA_DIR") or (REPO_ROOT / "data"))
RAW_CACHE_DIR = DATA_DIR / "raw"      # fetch layer is the only writer
GLOBAL_CACHE_DIR = RAW_CACHE_DIR / "_global"  # address-independent: perpDexs, meta

# READ-ONLY, and always shipped with the code: the FX series must be found
# whatever the working directory, so it is anchored to the repo, never to $PWD.
FX_DATA_DIR = REPO_ROOT / "fx" / "data"   # cached USD/INR rate series (Phase 4)

# =============================================================================
# API (Hyperliquid info endpoint)
# =============================================================================

# Verified against live endpoint 2026-07-22.
API_BASE_URL = "https://api.hyperliquid.xyz/info"

REQUEST_TIMEOUT_S = 30
MAX_RETRIES = 5
RETRY_BACKOFF_BASE_S = 2.0  # exponential backoff: base ** attempt

# Delay between consecutive API calls. The info endpoint is weight-based
# rate limited; the exact weights per request type are still NEEDS
# VERIFICATION against current docs, but 0.25s was confirmed TOO FAST live
# on 2026-07-22: a full backfill of a very heavy address (14k+ fills, 15k+
# funding rows, 8.5k+ ledger rows -> dozens of consecutive paginated calls)
# tripped a live HTTP 429 after several dozen requests. Widened
# conservatively; client.py's retry/backoff also absorbs a transient 429,
# but the delay should keep bursts from tripping it in the first place.
RATE_LIMIT_DELAY_S = 0.6  # was 0.25 (too fast); confirmed via live 429

# Row caps per response. CONFIRMED LIVE 2026-07-22 (not just docs) by
# paginating real leaderboard addresses through fetch/client.py's request
# path:
# - userFillsByTime: page cap of 2000 confirmed (8 consecutive full pages
#   observed on one very high-frequency address, each exactly 2000 rows,
#   strictly ascending, no duplicate tids across pages).
# - The "only the 10000 most recent fills are available" wall is REAL, not
#   just a doc claim: on that same high-frequency address, requesting any
#   endTime before the earliest fill reachable via startTime=0 returns an
#   EMPTY list -- the fill data before that point is gone, not merely
#   unpaginated. Confirmed absent on a low-frequency address (119 total
#   fills, startTime=0 correctly returns its true first-ever fill from
#   2025-06, well under 10000, so no truncation there).
#   CAVEAT (undecidable from the API alone): if an account's full-history
#   fetch returns a count at or near this wall, the tool CANNOT tell "the
#   account genuinely has no earlier fills" apart from "earlier fills exist
#   but the API no longer exposes them." fetch_user.py must warn explicitly
#   rather than silently presenting a truncated history as complete.
# - userNonFundingLedgerUpdates: page cap of 2000 confirmed (one busy
#   address hit exactly 2000; a quieter address returned 842, well under
#   any cap, i.e. genuinely all of its history). The docs page's "500
#   elements" wording is stale/wrong for this endpoint.
# - userFunding: no round-number cap was observed live (two addresses
#   returned 500 and 235 respectively, neither suggesting a hard wall) --
#   still paginate defensively as if a cap exists, since a null result
#   either way is cheap and the alternative (assuming no cap) risks silent
#   truncation for a busier account than we happened to test.
PAGE_CAP_USERFILLS = 2000
PAGE_CAP_USERFILLS_BY_TIME = 2000
USERFILLS_BY_TIME_MAX_RECENT = 10000  # CONFIRMED LIVE, see note above
PAGE_CAP_LEDGER_UPDATES = 2000  # CONFIRMED LIVE 2026-07-22
PAGE_CAP_USERFUNDING = 2000  # assumed defensively; no cap observed live yet

# Address format: CONFIRMED LIVE 2026-07-22. A well-formed "0x" + 40 hex
# chars address with NO trading history returns HTTP 200 with an empty
# list / zeroed clearinghouseState -- this is a legitimate response, not an
# error. A malformed "user" field (wrong length, non-hex, or missing)
# returns HTTP 422 with body "Failed to deserialize the JSON body into the
# target type" -- this is the loud-failure path Gate 1 requires, and
# fetch_user.py validates the format locally first so the error message is
# specific rather than relaying the endpoint's generic 422 text. The
# endpoint also tolerates a "user" value with no "0x" prefix (verified
# live), but fetch_user.py always normalizes to a 0x-prefixed lowercase
# form for stable cache paths.
ADDRESS_RE = r"^0x[0-9a-fA-F]{40}$"

# =============================================================================
# Verification gate tolerances
# =============================================================================

# Gate 3b — venue reconciliation. Target (errors_in_plan.md #3): compare our
# realized P&L against the venue's closedPnl over the SAME closing fills, on
# episodes we fully captured (cost_basis_incomplete == False), NOT per-fill
# equality on truncated history.
# CALIBRATED on real data 2026-07-23: on 6,410 fully-captured closing events
# from a live heavy account, our average-entry + gross-of-fees reconstruction
# matched HL's closedPnl with a max per-event deviation of 0.041 USD and a
# whole-account total deviation of 0.37 USD. The residual is HL carrying
# entry-price precision differently across many small fills, not a sign or
# convention error (which would be dollars, or a factor of two). This EMPIRICALLY
# confirms the average-entry convention (errors_in_plan.md #7) — Gate 3b is a
# real pass here, not merely a diagnostic. Tolerance set above the observed
# noise floor but well below any structural error.
TOL_VENUE_RECONCILE_USD = 0.10  # per fully-captured closing event; observed max 0.041

# Gate 3c — the equity identity (CORRECTED per errors_in_plan.md #1; the
# plan's original formula was missing the unrealized term and left the
# funding sign implicit):
#
#   deposits - withdrawals
#       + SUM(realized P&L)
#       + SUM(funding, signed: + received, - paid)
#       - SUM(fees, signed: + paid, - rebate)
#       + unrealized P&L at snapshot time
#   ~= account equity (clearinghouseState.marginSummary.accountValue)
#
# With zero open positions at snapshot time the unrealized term is 0.
#
# CORRECTED AGAIN on real data 2026-07-23. The plan's formula (errors_in_plan.md
# #1 already fixed the missing unrealized term and the funding sign) is STILL
# incomplete against reality — two more terms were found empirically:
#   * builderFee: HIP-3 builder dexes charge an extra per-fill fee, separate
#     from `fee`, that also reduces cash.
#   * spot USDC: USDC is fungible across the perp and spot sides of ONE
#     account, so "current equity" is perp accountValue + spot USDC, not perp
#     alone. On a clean deposits-only account the ENTIRE residual was exactly
#     the spot USDC balance (3.2108 predicted vs 3.210798 held).
# Full identity actually verified to hold:
#   deposits - withdrawals - withdraw_fees
#       + SUM(realized, fully-captured episodes)
#       + SUM(funding, signed)
#       - SUM(fees) - SUM(builder_fees)
#       + unrealized_now
#   ~= perp accountValue_now + spot USDC_now
# Observed residuals on real, fully-reconcilable accounts 2026-07-23:
#   * deposits-only, no withdrawals:      0.0001 USD  (identity is EXACT)
#   * with withdrawals (each a $1 fee):   0.85   USD  (a sub-dollar residual
#     tied to the withdraw-fee flows — whether HL's withdraw `usdc` is debited
#     gross or net of the fee is genuinely ambiguous from the data, and a
#     small reward/rebate credit may also sit here).
# A backwards SIGN or a MISSING FLOW would throw this off by dollars-to-
# thousands (e.g. a flipped funding sign on the second account is ~14 USD, a
# flipped realized sign ~118 USD), so a sub-dollar tolerance still firmly
# catches every structural error the gate exists to catch. The identity holds
# only when history is NOT truncated (no incomplete episodes) and the ledger
# has no USDC-moving transfers/sends we don't model; such addresses are
# skipped by the Gate-3c check rather than force-fit.
TOL_EQUITY_IDENTITY_USD = 1.0  # sub-dollar; tightest observed 0.0001 (no-withdrawal acct)

# =============================================================================
# Reconstruction conventions
# =============================================================================

# Cost basis: average entry price within an episode. Claim that this matches
# HL's own displayed entryPx is NEEDS VERIFICATION (errors_in_plan.md #7).
# If it cannot be verified, Gate 3b becomes a diagnostic (report max
# deviation) and only Gate 3c is enforced.
COST_BASIS_CONVENTION = "average_entry_within_episode"  # NEEDS VERIFICATION

# Fee attribution: fees are tracked per FILL and never split within a fill.
# A flip fill's full fee lands on the closing event of the episode it
# closes; the newly opened episode inherits none of it. (Convention, stated
# so two runs agree; not a claim about tax law.)

# Liquidation detection on real data: NEEDS VERIFICATION. Liquidated
# positions arrive in userFills shaped like ordinary closing fills; no flag
# was found in the docs or live samples as of 2026-07-22. The fixture
# injects liquidation knowledge via a tid set; the production detection
# source must be verified in Phase 1/3 (candidate: cross-referencing
# historicalOrders statuses).

# =============================================================================
# Asset-class tagging (Phase 2 load layer)
# =============================================================================
#
# A coin string carries two separable facts:
#   1. Its DEX NAMESPACE — verifiable from the string and perpDexs:
#        "BTC"        -> main dex (no prefix)
#        "xyz:TSLA"   -> HIP-3 builder dex "xyz"
#        "@107"       -> a SPOT asset (index form), NOT a perp
#   2. Its underlying ASSET CLASS — equity vs crypto vs commodity. This is
#      tax-relevant (brief: a USDC-settled EQUITY perp may be a different
#      animal from a crypto perp) but only PARTIALLY derivable: the API does
#      not label an underlying's asset class.
#
# v1 tags by dex namespace, which is honest and verifiable, and keeps the
# mapping HERE (visible, per errors_in_plan.md #10) rather than buried in
# code:
#   - main dex            -> "crypto-perp"
#   - a mapped builder dex-> its mapped class (see HIP3_DEX_ASSET_CLASS)
#   - any other builder dex-> the neutral "hip3-perp" (character unverified)
#   - spot ("@N")         -> "spot" (OUT OF SCOPE for perp P&L; loader warns)
#
# The "xyz" dex is predominantly tokenized equities/RWAs but its universe
# ALSO lists commodities (xyz:GOLD, xyz:CORN, xyz:BRENTOIL) and FX
# (xyz:JPY) — so labeling the whole dex "equity-perp" is a v1 approximation;
# per-symbol refinement is NEEDS VERIFICATION.
ASSET_CLASS_MAIN_DEX = "crypto-perp"
ASSET_CLASS_SPOT = "spot"
ASSET_CLASS_HIP3_DEFAULT = "hip3-perp"
HIP3_DEX_ASSET_CLASS = {
    "xyz": "equity-perp",  # NEEDS VERIFICATION: also lists commodities/FX
}

# Ledger delta types (userNonFundingLedgerUpdates). Confirmed live 2026-07-22.
# Only deposit/withdraw are in scope for v1 (plan correction 0.4). The other
# USDC-moving types shift the perp account balance and therefore matter to
# the Gate 3c equity identity (Phase 3), so the loader must NOT drop them —
# it types them faithfully and warns; Phase 3 decides how they enter the
# identity. Token-only flows (non-USDC) are spot/staking activity, out of
# scope for perp P&L.
LEDGER_TYPES_IN_SCOPE = {"deposit", "withdraw"}
LEDGER_TYPES_USDC_TRANSFER = {  # carry a `usdc` field; affect perp balance
    "subAccountTransfer", "accountClassTransfer", "internalTransfer",
}
LEDGER_TYPES_TOKEN_FLOW = {  # carry `token`+`amount`, not `usdc`; out of scope
    "spotTransfer", "spotGenesis", "cStakingTransfer", "send",
    "gossipPriorityGasAuction",
}

# =============================================================================
# FX layer
# =============================================================================

# The FX CONVENTION is a first-class, configurable parameter — not a hardcode
# (phase4_fx_spec.md §0; the single most important FX instruction). It has two
# separable parts, both echoed in every report so two runs under different
# conventions are visibly different documents:
#
#   1. The SPECIFIED-DATE RULE — which calendar date's rate an event uses.
#      Rule 115 of the Income-tax Rules, 1962 prescribes different dates by
#      head of income (capital gains -> date of transfer; business / other
#      sources -> last day of the previous year). v1 implements the
#      transfer/event-date rule ("event_time_prior_business_day"); the
#      last-day-of-year variant is a documented FUTURE convention the same
#      interface can express, so Phase 5 never reaches back into Phase 4.
#
#   2. The RATE SOURCE — which published series supplies the number.
FX_CONVENTION = "event_time_prior_business_day"

# Publication-time nuance (phase4_fx_spec.md §2b): a reference rate only exists
# in the world from ~13:30 IST on its own publication day, so an event EARLIER
# that same business day has no published rate yet and must use the PREVIOUS
# business day. Cutoff is IST wall-clock "HH:MM".
FX_PUBLICATION_CUTOFF_IST = "13:30"

# Staleness ceiling (found 2026-07-23, not in the original spec docs): the
# cached series is a SNAPSHOT, refreshed only when a human reruns
# `python -m fx.fetch_fx` -- nothing in this project runs on a schedule. The
# lookup already walks backward, unbounded, past any gap in the series
# (that's how weekends/holidays are handled, correctly). Without a ceiling,
# that same mechanism silently reuses the last cached rate for ANY event past
# the series' end -- a trade from 6 months or 3 years after the last refresh
# would get today's rate with no warning, which is exactly the "confident,
# wrong, beautifully formatted" failure mode Part 4 of the master plan warns
# about. A short grace window (covers a normal multi-day festival cluster
# without a refresh) is allowed silently -- rate_date in the output already
# differs visibly from the event date, which is self-documenting for a CA.
# Beyond the grace window, the lookup refuses and demands a refresh instead of
# guessing. 5 calendar days is a reasoned operational default, not a verified
# holiday-calendar fact.
FX_MAX_STALENESS_DAYS = 5

# --- Rate source: the FBIL reference rate, via Frankfurter -------------------
# v1 uses the FBIL USD/INR REFERENCE RATE — the benchmark Indian income-tax
# foreign-currency conversion relies on (phase4_fx_spec.md). We do NOT scrape
# RBI/FBIL (their portals are anti-automation); we pull the SAME FBIL benchmark
# from Frankfurter's free, open-source, no-auth FBIL provider, verified live
# 2026-07-23:
#   GET https://api.frankfurter.dev/v2/rates
#       ?base=USD&providers=FBIL&from=<A>&to=<B>   (range; use date=<D> for one)
#   -> [{"date","base","quote":"INR","rate": <INR per USD, up to 4dp>}, ...]
#   Coverage 2018-07-10 (68.7942) -> present, daily, weekends/holidays absent.
#   A single call spanning >~2 years 422s, so bulk seeding chunks by year.
# Frankfurter is the RETRIEVAL MECHANISM; FBIL/RBI is the cited AUTHORITY.
FX_PROVIDER_URL = "https://api.frankfurter.dev"
FX_PROVIDER_KEY = "FBIL"          # Frankfurter provider key (Financial Benchmarks India)
FX_PROVIDER_NAME = "frankfurter-fbil"  # `source` (retrieval path) written per row

# Administrator split (the citable AUTHORITY, independent of retrieval path):
# RBI reference rate BEFORE this date, FBIL on/after. Frankfurter's FBIL
# provider starts exactly here. Pre-2018-07-10 rates (only if a trade is ever
# that old) must be seeded manually from RBI archives, tagged administrator=RBI.
FX_ADMIN_HANDOVER_DATE = "2018-07-10"  # RBI -> FBIL

# DO NOT substitute the US Fed H.10 "Indian rupees per US dollar" series — it is
# the Fed's New York noon rate, not the RBI/FBIL reference rate, and cannot be
# cited as such to an Indian CA (phase4_fx_spec.md §1).

# Data at rest: one append-mostly CSV + a sidecar manifest (manifest-gated like
# the fetch-layer cache, so provenance survives). Columns:
#   rate_date,rate,source,administrator,fetched_at_utc
FX_SERIES_FILE = FX_DATA_DIR / "usdinr_reference.csv"
FX_MANIFEST_FILE = FX_DATA_DIR / "manifest.json"
FX_DEFAULT_SERIES_FILE = FX_SERIES_FILE  # the pipeline's default series

# Validation band for a fetched USD/INR row: reject a 0, a null, or a
# decimal-shifted value before it can poison the series. NEEDS-VERIFICATION as
# a band (widen if the rupee ever moves outside it); the rest is confirmed.
FX_RATE_MIN, FX_RATE_MAX = 50.0, 200.0
# `--verify`: max tolerated deviation of a stored row vs a fresh upstream pull.
FX_RECONCILE_TOLERANCE = 1e-4
# A run of missing business days larger than this inside the series is treated
# as a suspicious interior GAP (not a normal weekend/festival cluster) and
# flagged during validation, rather than silently walked over at read time.
FX_MAX_INTERIOR_GAP_DAYS = 6

# Human-readable source note echoed into reports (also flags the CA-review point).
FX_SOURCE_NAME = (
    "FBIL USD/INR reference rate (the benchmark Indian tax conversion relies "
    "on), retrieved via the Frankfurter FBIL provider; FBIL/RBI is the cited "
    "authority. Which rate/date applies under each treatment (Rule 115) is NOT "
    "settled tax law — CA to confirm."
)

# =============================================================================
# Tax parameters — ALL NEEDS VERIFICATION against the current Act and at
# least two reputable current sources before Phase 5 ships. Model memory is
# not a source for tax law.
# =============================================================================

# Which statute governs a given tax year (errors_in_plan.md #2): the IT Act
# 2025 is expected effective for the tax year beginning 2026-04-01. FY
# 2025-26 returns are filed under the IT Act 1961. The year selects the
# namespace of rules; section numbers below are 1961 numbers unless noted.
IT_ACT_2025_EFFECTIVE_DATE = "2026-04-01"  # NEEDS VERIFICATION

# VDA treatment ("115BBH-style"): flat rate on gains, NO set-off of losses
# against gains, NO deduction of expenses other than cost of acquisition.
VDA_FLAT_RATE = 0.30  # IT Act 1961 s.115BBH; IT Act 2025 mapping NEEDS VERIFICATION

# TDS on VDA transfers at concept level.
TDS_RATE_VDA = 0.01  # IT Act 1961 s.194S; IT Act 2025 mapping NEEDS VERIFICATION

# Health & education cess, levied on the tax (after surcharge) under both the
# VDA and slab readings.
CESS_RATE = 0.04  # NEEDS VERIFICATION (current Finance Act)

# --- Slab rates (the ordinary-income readings: OP 2 / OP 3 / OP 4) -----------
# New personal regime (IT Act 1961 s.115BAC; IT Act 2025 s.202). Marginal
# bands as (upper_bound_inr, rate); the last band's bound is None = infinity.
# These are the FY 2025-26 / AY 2026-27 new-regime bands. ALL NEEDS
# VERIFICATION against the current Finance Act and two reputable sources.
SLAB_REGIME_DEFAULT = "new_115BAC"
SLAB_SCHEDULE_NEW_115BAC = [
    (400000, 0.00),
    (800000, 0.05),
    (1200000, 0.10),
    (1600000, 0.15),
    (2000000, 0.20),
    (2400000, 0.25),
    (None, 0.30),
]  # NEEDS VERIFICATION
# s.87A rebate: under the new regime, total income up to this ceiling pays zero
# tax (the rebate wipes the computed tax). Applied to the slab readings only,
# never to the VDA flat rate (115BBH income is excluded from 87A). NEEDS
# VERIFICATION (limit + whether it reaches derivative business income).
REBATE_87A_LIMIT_NEW_INR = 1200000  # NEEDS VERIFICATION

# Surcharge on income-tax, by total-income band, as (upper_bound_inr, rate).
# The new regime caps the top surcharge at 25%. Applied to both slab and VDA
# tax here as a v1 approximation; the VDA-specific surcharge cap has had special
# treatment historically and is flagged as a caveat in treatment_vda(). NEEDS
# VERIFICATION.
SURCHARGE_SCHEDULE = [
    (5000000, 0.00),
    (10000000, 0.10),
    (20000000, 0.15),
    (50000000, 0.25),
    (None, 0.25),
]  # NEEDS VERIFICATION

# --- Loss carry-forward horizons (assessment years) --------------------------
# Speculative business loss: only against future speculative gains (IT Act 1961
# s.73; IT Act 2025 s.113). Non-speculative business loss: against business
# income (IT Act 1961 s.72; IT Act 2025 s.112).
SPECULATIVE_CARRY_FORWARD_YEARS = 4  # NEEDS VERIFICATION under 2025 Act
BUSINESS_CARRY_FORWARD_YEARS = 8     # NEEDS VERIFICATION under 2025 Act

# --- Tax-audit / turnover (IT Act 1961 s.44AA/s.44AB; 2025 s.62/s.63) --------
# Derivative "turnover" is the ICAI-guidance abs-sum, NOT net P&L, NOT notional.
AUDIT_TURNOVER_THRESHOLD_INR = 1e7  # NEEDS VERIFICATION (threshold + cash %)

# --- TDS on the stablecoin leg (s.194S / 2025 s.393) -------------------------
# Same 1% as the VDA rate; annual per-payer thresholds by taxpayer category.
TDS_RATE_LEG = 0.01  # NEEDS VERIFICATION
TDS_THRESHOLD_SPECIFIED_PERSON_INR = 50000  # NEEDS VERIFICATION
TDS_THRESHOLD_OTHER_INR = 10000             # NEEDS VERIFICATION

# Funding treatment default (errors_in_plan.md #6): funding is a HOLDING
# cost, structurally different from execution cost and from venue fees. The
# interpret layer takes this as an explicit parameter; the default is to
# keep funding as its own signed ledger lines, never netted into execution
# P&L. Phase 5 tests must exercise every value of this parameter.
FUNDING_TREATMENT_DEFAULT = "separate_signed_lines"

# On-ramp boundary (errors_in_plan.md #4): the tool cannot see the INR cost
# of acquiring USDC. Unless the user supplies it here, any VDA-treatment
# number must be labeled IN THE SAME CELL as "Hyperliquid leg only — NOT
# your VDA tax liability", never merely footnoted.
ON_RAMP_USDC_COST_INR = None  # manual input slot; user supplies from exchange statement

# =============================================================================
# Presentation
# =============================================================================

REPORT_INR_DECIMALS = 2
REPORT_USD_DECIMALS = 6

# All internal timestamps are tz-aware UTC. IST is a presentation-layer
# concern only.
INTERNAL_TIMEZONE = "UTC"
DISPLAY_TIMEZONE = "Asia/Kolkata"

# (Path constants are defined at the top of this file.)
