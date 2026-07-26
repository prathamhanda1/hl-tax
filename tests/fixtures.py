"""
tests/fixtures.py — the paper order book of this project.

Hand-built fixture data in the EXACT shape returned by the live Hyperliquid
info endpoint (field names, string-typed numbers, optional fields), with
every expected number computed BY HAND in the comments below. Written in
Phase 0, BEFORE the code it tests. If the code and this file disagree, the
code is wrong until proven otherwise.

Field formats verified against live api.hyperliquid.xyz responses on
2026-07-22, and against:
https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint

=====================================================================
SIGN CONVENTIONS (each verified against live data, none assumed)
=====================================================================
FILLS (userFills / userFillsByTime):
  side           "B" = buy, "A" = sell.
  startPosition  signed string: net position BEFORE this fill. + = long,
                 - = short.
  dir            "Open Long" | "Close Long" | "Open Short" | "Close Short"
                 | "Long > Short" | "Short > Long"   (all observed live)
  closedPnl      venue-computed realized P&L (USD) of the CLOSED portion of
                 this fill only. "0.0" on pure opens. On a flip fill it
                 covers only the closed leg (verified live).
  fee            USD string. Positive = user paid. NEGATIVE = maker rebate
                 (observed on live fills, e.g. fee "-0.621468").
  px, sz         strings. Numbers arrive as strings everywhere; the load
                 layer converts strings -> floats exactly once.
  cloid, twapId  optional fields observed live; fixtures omit cloid and
                 set twapId to None, matching API-placed vs app-placed rows.

FUNDING (userFunding): one event per funding settlement.
  delta.usdc     signed: + = received by user, - = paid by user. Verified
                 live: a SHORT with positive fundingRate received +usdc;
                 a LONG with positive fundingRate paid (-usdc).
                 Rule of thumb: usdc ~= -szi * px * fundingRate.
  delta.szi      signed position size the funding was charged on.
  hash           all-zero for funding events (verified live).
  Tax note: funding is a HOLDING cost (paid for keeping a perp open),
  structurally different from execution fees (paid to the venue per
  trade). They are kept as separate ledger rows, never netted into
  execution P&L. Their TAX treatment is an explicit parameter in Phase 5
  (errors_in_plan.md #6): these fixtures pin only the reconstruction-level
  rows, which are treatment-agnostic. Phase 5 fixtures must exercise every
  value of the funding-treatment parameter.

LEDGER (userNonFundingLedgerUpdates):
  deposit        delta.usdc = amount IN (positive).
  withdraw       delta.usdc = amount OUT (stated positive; money leaves the
                 account). delta.fee: real withdrawals carry a fee ("1.0"
                 observed live); whether that fee is inside `usdc` or
                 charged on top is NEEDS VERIFICATION. This fixture uses
                 fee "0.0" so the equity identity stays hand-checkable.
  Other live-observed types NOT exercised here (loader must handle or
  loudly reject): send, subAccountTransfer, spotTransfer,
  cStakingTransfer, spotGenesis, gossipPriorityGasAuction.

LIQUIDATION: a forced close arrives in userFills shaped like an ordinary
closing fill; no marking field was found in docs or live samples as of
2026-07-22 (NEEDS VERIFICATION). This fixture injects liquidation
knowledge via LIQUIDATION_TIDS below; the production detection source is
verified in Phase 1/3. Tax note: a liquidation is still a disposal — the
tax law does not care that the close was forced.
"""

# =====================================================================
# THE SCENARIO — one coherent timeline, all timestamps fabricated
# (epoch ms, UTC, starting 2025-01-02T00:00:00Z = 1735776000000).
# Fee convention for clean hand arithmetic: every fill is a taker fill
# at 0.004% of notional  ->  fee = px * sz * 0.00004.
# =====================================================================
#
#  t0  1735776000000  deposit 10,000.0 USDC
#
#  --- Episode 1: BTC long, simple open+close, PROFIT ---
#  t1  1735779600000  F1  BUY  0.5 BTC @ 100,000   open long
#                        notional 50,000 -> fee 2.00
#  t2  1735783200000  FUND1 BTC funding: long pays, rate +0.00025
#                        usdc = -(0.5 * 100,000 * 0.00025) = -12.50  (PAID)
#  t3  1735786800000  F2  SELL 0.5 BTC @ 101,000   close long
#                        notional 50,500 -> fee 2.02
#                        realized = (101,000 - 100,000) * 0.5 = +500.00
#                        closedPnl "500.0"
#     Episode 1: realized +500.00 | fees 2.00 + 2.02 = 4.02
#
#  --- Episode 2: ETH short, open+close, PROFIT (the sign test:
#      a short profits when price FALLS) ---
#  t4  1735790400000  F3  SELL 2.0 ETH @ 3,000     open short
#                        notional 6,000 -> fee 0.24
#  t5  1735794000000  FUND2 ETH funding: short receives, rate +0.000725
#                        usdc = +(2.0 * 3,000 * 0.000725) = +4.35  (RECEIVED)
#  t6  1735797600000  F4  BUY  2.0 ETH @ 2,900     close short
#                        notional 5,800 -> fee 0.232
#                        realized = (3,000 - 2,900) * 2.0 = +200.00
#                        closedPnl "200.0"
#     Episode 2: realized +200.00 | fees 0.24 + 0.232 = 0.472
#
#  --- Episode 3: BTC long, two entries, PARTIAL CLOSE, average entry ---
#  t7  1735801200000  F5  BUY  1.0 BTC @ 100,000   open long, fee 4.00
#  t8  1735804800000  F6  BUY  1.0 BTC @ 102,000   add long (startPosition
#                        "1.0"), fee 4.08
#                        avg entry = (100,000*1 + 102,000*1) / 2 = 101,000
#  t9  1735808400000  F7  SELL 1.0 BTC @ 103,000   close HALF (startPosition
#                        "2.0"), fee 4.12
#                        realized = (103,000 - 101,000) * 1.0 = +2,000.00
#                        closedPnl "2000.0"; remaining 1.0 BTC @ 101,000
#  t10 1735812000000  F8  SELL 1.0 BTC @ 99,000    close rest, fee 3.96
#                        realized = (99,000 - 101,000) * 1.0 = -2,000.00
#                        closedPnl "-2000.0"
#     Episode 3: realized 2,000 - 2,000 = 0.00 | fees 4.00+4.08+4.12+3.96
#                = 16.16   (a break-even episode that still COST money —
#                and under the VDA reading its +2,000 leg is taxable while
#                its -2,000 leg sets off nothing)
#
#  --- Episode 4+5: ETH FLIP — one fill closes a long AND opens a short ---
#  t11 1735815600000  F9  BUY  1.0 ETH @ 3,000     open long, fee 0.12
#  t12 1735819200000  F10 SELL 3.0 ETH @ 3,100     dir "Long > Short",
#                        startPosition "1.0", notional 9,300 -> fee 0.372
#                        SPLIT into two economic events:
#                          (a) close 1.0 long: realized
#                              (3,100 - 3,000) * 1.0 = +100.00
#                              closedPnl "100.0"  (covers closed leg only)
#                          (b) open 2.0 short @ 3,100 (new episode)
#  t13 1735822800000  F11 BUY  2.0 ETH @ 3,050     close short, fee 0.244
#                        realized = (3,100 - 3,050) * 2.0 = +100.00
#                        closedPnl "100.0"
#     Episode 4 (the long):  realized +100.00 | fees 0.12 + 0.372 = 0.492
#       (the flip fill's full fee lands on the CLOSING event it causes —
#        per-fill fee convention, see config.py)
#     Episode 5 (the short): realized +100.00 | fees 0.244
#
#  --- Episode 6: SOL long, LIQUIDATED (forced close, deep loss) ---
#  t14 1735826400000  F12 BUY  10 SOL @ 200        open long, fee 0.80
#  t15 1735830000000  F13 SELL 10 SOL @ 180        close long, fee 0.072
#                        realized = (180 - 200) * 10 = -200.00
#                        closedPnl "-200.0"   [LIQUIDATION: tid in
#                        LIQUIDATION_TIDS]
#     Episode 6: realized -200.00 | fees 0.80 + 0.072 = 0.872
#     [CORRECTED 2026-07-23: was -2,000.00 in the original hand build — a
#      factor-of-10 slip (-20 * 10 = -200, not -2000). Caught by the Phase-3
#      reconstruction disagreeing with the fixture; the arithmetic proves the
#      fixture wrong. Fixed here and in F13.closedPnl, EXPECTED_CLOSING_EVENTS
#      episode 6, and EXPECTED_TOTALS below.]
#
#  --- Episode 7: xyz:TSLA — HIP-3 namespaced equity perp (dex tagging) ---
#  t16 1735833600000  F14 BUY  1.0 xyz:TSLA @ 450  open long, fee 0.018
#  t17 1735837200000  F15 SELL 1.0 xyz:TSLA @ 460  close long, fee 0.0184
#                        realized = (460 - 450) * 1.0 = +10.00
#                        closedPnl "10.0"
#     Episode 7: realized +10.00 | fees 0.018 + 0.0184 = 0.0364
#
#  t18 1735840800000  withdraw 5,000.0 USDC (fee "0.0")
#
# =====================================================================
# HAND-COMPUTED TOTALS (the numbers Gate 3 must reproduce)
# =====================================================================
#   Realized P&L:   +500 +200 +0 +100 +100 -200 +10 = 710.00 USD
#     (cross-check vs venue closedPnl sum:
#      500+200+2000-2000+100+100-200+10 = 710.00  MATCH)
#   Fees:           4.02 + 0.472 + 16.16 + 0.492 + 0.244 + 0.872 + 0.0364
#                 = 22.2964 USD
#   Funding (signed +received/-paid):  -12.50 + 4.35 = -8.15 USD
#   Deposits 10,000 ; Withdrawals 5,000
#
#   EQUITY IDENTITY (corrected form, errors_in_plan.md #1; account is FLAT
#   at the end so unrealized = 0):
#     10,000 - 5,000 + 710 + (-8.15) - 22.2964 + 0
#       = 5,679.5536 USD   <- expected final account equity
# =====================================================================

FILLS = [
    # --- Episode 1 ---
    {
        "coin": "BTC", "px": "100000.0", "sz": "0.5", "side": "B",
        "time": 1735779600000, "startPosition": "0.0", "dir": "Open Long",
        "closedPnl": "0.0",
        "hash": "0xf100000000000000000000000000000000000000000000000000000000000001",
        "oid": 100000000001, "crossed": True, "fee": "2.0",
        "tid": 1000000000000001, "feeToken": "USDC", "twapId": None,
    },
    {
        "coin": "BTC", "px": "101000.0", "sz": "0.5", "side": "A",
        "time": 1735786800000, "startPosition": "0.5", "dir": "Close Long",
        "closedPnl": "500.0",
        "hash": "0xf100000000000000000000000000000000000000000000000000000000000002",
        "oid": 100000000002, "crossed": True, "fee": "2.02",
        "tid": 1000000000000002, "feeToken": "USDC", "twapId": None,
    },
    # --- Episode 2 ---
    {
        "coin": "ETH", "px": "3000.0", "sz": "2.0", "side": "A",
        "time": 1735790400000, "startPosition": "0.0", "dir": "Open Short",
        "closedPnl": "0.0",
        "hash": "0xf100000000000000000000000000000000000000000000000000000000000003",
        "oid": 100000000003, "crossed": True, "fee": "0.24",
        "tid": 1000000000000003, "feeToken": "USDC", "twapId": None,
    },
    {
        "coin": "ETH", "px": "2900.0", "sz": "2.0", "side": "B",
        "time": 1735797600000, "startPosition": "-2.0", "dir": "Close Short",
        "closedPnl": "200.0",
        "hash": "0xf100000000000000000000000000000000000000000000000000000000000004",
        "oid": 100000000004, "crossed": True, "fee": "0.232",
        "tid": 1000000000000004, "feeToken": "USDC", "twapId": None,
    },
    # --- Episode 3 ---
    {
        "coin": "BTC", "px": "100000.0", "sz": "1.0", "side": "B",
        "time": 1735801200000, "startPosition": "0.0", "dir": "Open Long",
        "closedPnl": "0.0",
        "hash": "0xf100000000000000000000000000000000000000000000000000000000000005",
        "oid": 100000000005, "crossed": True, "fee": "4.0",
        "tid": 1000000000000005, "feeToken": "USDC", "twapId": None,
    },
    {
        "coin": "BTC", "px": "102000.0", "sz": "1.0", "side": "B",
        "time": 1735804800000, "startPosition": "1.0", "dir": "Open Long",
        "closedPnl": "0.0",
        "hash": "0xf100000000000000000000000000000000000000000000000000000000000006",
        "oid": 100000000006, "crossed": True, "fee": "4.08",
        "tid": 1000000000000006, "feeToken": "USDC", "twapId": None,
    },
    {
        "coin": "BTC", "px": "103000.0", "sz": "1.0", "side": "A",
        "time": 1735808400000, "startPosition": "2.0", "dir": "Close Long",
        "closedPnl": "2000.0",
        "hash": "0xf100000000000000000000000000000000000000000000000000000000000007",
        "oid": 100000000007, "crossed": True, "fee": "4.12",
        "tid": 1000000000000007, "feeToken": "USDC", "twapId": None,
    },
    {
        "coin": "BTC", "px": "99000.0", "sz": "1.0", "side": "A",
        "time": 1735812000000, "startPosition": "1.0", "dir": "Close Long",
        "closedPnl": "-2000.0",
        "hash": "0xf100000000000000000000000000000000000000000000000000000000000008",
        "oid": 100000000008, "crossed": True, "fee": "3.96",
        "tid": 1000000000000008, "feeToken": "USDC", "twapId": None,
    },
    # --- Episodes 4+5 (the flip) ---
    {
        "coin": "ETH", "px": "3000.0", "sz": "1.0", "side": "B",
        "time": 1735815600000, "startPosition": "0.0", "dir": "Open Long",
        "closedPnl": "0.0",
        "hash": "0xf100000000000000000000000000000000000000000000000000000000000009",
        "oid": 100000000009, "crossed": True, "fee": "0.12",
        "tid": 1000000000000009, "feeToken": "USDC", "twapId": None,
    },
    {
        "coin": "ETH", "px": "3100.0", "sz": "3.0", "side": "A",
        "time": 1735819200000, "startPosition": "1.0", "dir": "Long > Short",
        "closedPnl": "100.0",
        "hash": "0xf100000000000000000000000000000000000000000000000000000000000010",
        "oid": 100000000010, "crossed": True, "fee": "0.372",
        "tid": 1000000000000010, "feeToken": "USDC", "twapId": None,
    },
    {
        "coin": "ETH", "px": "3050.0", "sz": "2.0", "side": "B",
        "time": 1735822800000, "startPosition": "-2.0", "dir": "Close Short",
        "closedPnl": "100.0",
        "hash": "0xf100000000000000000000000000000000000000000000000000000000000011",
        "oid": 100000000011, "crossed": True, "fee": "0.244",
        "tid": 1000000000000011, "feeToken": "USDC", "twapId": None,
    },
    # --- Episode 6 (liquidation) ---
    {
        "coin": "SOL", "px": "200.0", "sz": "10.0", "side": "B",
        "time": 1735826400000, "startPosition": "0.0", "dir": "Open Long",
        "closedPnl": "0.0",
        "hash": "0xf100000000000000000000000000000000000000000000000000000000000012",
        "oid": 100000000012, "crossed": True, "fee": "0.8",
        "tid": 1000000000000012, "feeToken": "USDC", "twapId": None,
    },
    {
        "coin": "SOL", "px": "180.0", "sz": "10.0", "side": "A",
        "time": 1735830000000, "startPosition": "10.0", "dir": "Close Long",
        "closedPnl": "-200.0",  # CORRECTED: (180-200)*10 = -200, not -2000
        "hash": "0xf100000000000000000000000000000000000000000000000000000000000013",
        "oid": 100000000013, "crossed": True, "fee": "0.072",
        "tid": 1000000000000013, "feeToken": "USDC", "twapId": None,
    },
    # --- Episode 7 (HIP-3 namespaced asset) ---
    {
        "coin": "xyz:TSLA", "px": "450.0", "sz": "1.0", "side": "B",
        "time": 1735833600000, "startPosition": "0.0", "dir": "Open Long",
        "closedPnl": "0.0",
        "hash": "0xf100000000000000000000000000000000000000000000000000000000000014",
        "oid": 100000000014, "crossed": True, "fee": "0.018",
        "tid": 1000000000000014, "feeToken": "USDC", "twapId": None,
    },
    {
        "coin": "xyz:TSLA", "px": "460.0", "sz": "1.0", "side": "A",
        "time": 1735837200000, "startPosition": "1.0", "dir": "Close Long",
        "closedPnl": "10.0",
        "hash": "0xf100000000000000000000000000000000000000000000000000000000000015",
        "oid": 100000000015, "crossed": True, "fee": "0.0184",
        "tid": 1000000000000015, "feeToken": "USDC", "twapId": None,
    },
]

FUNDING_EVENTS = [
    {
        "time": 1735783200000,
        "hash": "0x0000000000000000000000000000000000000000000000000000000000000000",
        "delta": {
            "type": "funding", "coin": "BTC", "usdc": "-12.5",
            "szi": "0.5", "fundingRate": "0.00025", "nSamples": None,
        },
    },
    {
        "time": 1735794000000,
        "hash": "0x0000000000000000000000000000000000000000000000000000000000000000",
        "delta": {
            "type": "funding", "coin": "ETH", "usdc": "4.35",
            "szi": "-2.0", "fundingRate": "0.000725", "nSamples": None,
        },
    },
]

LEDGER_EVENTS = [
    {
        "time": 1735776000000,
        "hash": "0xf200000000000000000000000000000000000000000000000000000000000001",
        "delta": {"type": "deposit", "usdc": "10000.0"},
    },
    {
        "time": 1735840800000,
        "hash": "0xf200000000000000000000000000000000000000000000000000000000000002",
        "delta": {
            "type": "withdraw", "usdc": "5000.0",
            "nonce": 1735840800000000, "fee": "0.0",
        },
    },
]

# Liquidation knowledge injected by the test harness (real-data detection
# source: NEEDS VERIFICATION — see module docstring).
LIQUIDATION_TIDS = {1000000000000013}

# ---------------------------------------------------------------------
# EXPECTED RECONSTRUCTION OUTPUT — one row per CLOSING event.
# Fields mirror reconstruct/positions.py Phase 3 spec: episode id, asset,
# direction, open/close timestamps, size closed, entry px, exit px,
# realized P&L (USD), closing-fill fee (USD), liquidation flag.
# Episode open timestamp convention: timestamp of the FIRST open of the
# episode. Entry px convention: size-weighted average within the episode.
# ---------------------------------------------------------------------
EXPECTED_CLOSING_EVENTS = [
    {  # Episode 1: BTC long
        "episode": 1, "asset": "BTC", "direction": "long",
        "open_ts": 1735779600000, "close_ts": 1735786800000,
        "size_closed": 0.5, "entry_px": 100000.0, "exit_px": 101000.0,
        "realized_usd": 500.0, "close_fee_usd": 2.02, "is_liquidation": False,
    },
    {  # Episode 2: ETH short
        "episode": 2, "asset": "ETH", "direction": "short",
        "open_ts": 1735790400000, "close_ts": 1735797600000,
        "size_closed": 2.0, "entry_px": 3000.0, "exit_px": 2900.0,
        "realized_usd": 200.0, "close_fee_usd": 0.232, "is_liquidation": False,
    },
    {  # Episode 3, first close (partial): BTC long, avg entry 101,000
        "episode": 3, "asset": "BTC", "direction": "long",
        "open_ts": 1735801200000, "close_ts": 1735808400000,
        "size_closed": 1.0, "entry_px": 101000.0, "exit_px": 103000.0,
        "realized_usd": 2000.0, "close_fee_usd": 4.12, "is_liquidation": False,
    },
    {  # Episode 3, second close (remainder, a LOSS)
        "episode": 3, "asset": "BTC", "direction": "long",
        "open_ts": 1735801200000, "close_ts": 1735812000000,
        "size_closed": 1.0, "entry_px": 101000.0, "exit_px": 99000.0,
        "realized_usd": -2000.0, "close_fee_usd": 3.96, "is_liquidation": False,
    },
    {  # Episode 4: ETH long closed by the FLIP fill (closes 1.0 of the
       # 3.0 sold; remaining 2.0 opens episode 5 short). The flip fill's
       # full fee (0.372) lands on this closing event.
        "episode": 4, "asset": "ETH", "direction": "long",
        "open_ts": 1735815600000, "close_ts": 1735819200000,
        "size_closed": 1.0, "entry_px": 3000.0, "exit_px": 3100.0,
        "realized_usd": 100.0, "close_fee_usd": 0.372, "is_liquidation": False,
    },
    {  # Episode 5: ETH short opened by the flip fill @ 3,100
        "episode": 5, "asset": "ETH", "direction": "short",
        "open_ts": 1735819200000, "close_ts": 1735822800000,
        "size_closed": 2.0, "entry_px": 3100.0, "exit_px": 3050.0,
        "realized_usd": 100.0, "close_fee_usd": 0.244, "is_liquidation": False,
    },
    {  # Episode 6: SOL long, LIQUIDATION
        "episode": 6, "asset": "SOL", "direction": "long",
        "open_ts": 1735826400000, "close_ts": 1735830000000,
        "size_closed": 10.0, "entry_px": 200.0, "exit_px": 180.0,
        "realized_usd": -200.0, "close_fee_usd": 0.072, "is_liquidation": True,
    },
    {  # Episode 7: xyz:TSLA long (HIP-3 namespaced equity perp)
        "episode": 7, "asset": "xyz:TSLA", "direction": "long",
        "open_ts": 1735833600000, "close_ts": 1735837200000,
        "size_closed": 1.0, "entry_px": 450.0, "exit_px": 460.0,
        "realized_usd": 10.0, "close_fee_usd": 0.0184, "is_liquidation": False,
    },
]

# Expected funding ledger rows (signed: + received, - paid). Kept as their
# own category, separate from execution P&L.
EXPECTED_FUNDING_ROWS = [
    {"ts": 1735783200000, "asset": "BTC", "usdc": -12.5},   # paid (long, rate > 0)
    {"ts": 1735794000000, "asset": "ETH", "usdc": 4.35},    # received (short, rate > 0)
]

EXPECTED_TOTALS = {
    "total_realized_usd": 710.0,       # 500+200+0+100+100-200+10 (SOL corrected)
    "total_fees_usd": 22.2964,         # sum of all fill fees
    "total_funding_usd": -8.15,        # -12.50 + 4.35
    "total_deposits_usd": 10000.0,
    "total_withdrawals_usd": 5000.0,
    # Equity identity: deposits - withdrawals + realized + funding(signed)
    # - fees + unrealized(=0, account flat) = final equity
    "final_equity_usd": 5679.5536,     # 10000-5000+710-8.15-22.2964
    "n_episodes": 7,
    "n_closing_events": 8,
}

# Asset-class tags Phase 2 must derive from the dex namespace:
#   "BTC"/"ETH"/"SOL" -> crypto-perp ; "xyz:TSLA" -> equity-perp (HIP-3)
EXPECTED_ASSET_CLASSES = {
    "BTC": "crypto-perp", "ETH": "crypto-perp", "SOL": "crypto-perp",
    "xyz:TSLA": "equity-perp",
}


def _self_check():
    """GATE 0: fixtures load and are internally consistent.

    Verifies the hand arithmetic above by re-deriving the totals three
    independent ways. If any assertion fails, the fixture itself is wrong.
    """
    tol = 1e-6

    # 1. Every fixture fill's hand-stated closedPnl sums to total realized.
    venue_sum = sum(float(f["closedPnl"]) for f in FILLS)
    assert abs(venue_sum - EXPECTED_TOTALS["total_realized_usd"]) < tol, (
        f"venue closedPnl sum {venue_sum} != expected realized "
        f"{EXPECTED_TOTALS['total_realized_usd']}"
    )

    # 2. Expected closing events reproduce realized total, fee total per
    #    episode, and the liquidation flag.
    our_sum = sum(e["realized_usd"] for e in EXPECTED_CLOSING_EVENTS)
    assert abs(our_sum - EXPECTED_TOTALS["total_realized_usd"]) < tol, (
        f"closing-event realized sum {our_sum} != expected"
    )
    liq = [e for e in EXPECTED_CLOSING_EVENTS if e["is_liquidation"]]
    assert len(liq) == 1 and liq[0]["asset"] == "SOL"

    # 3. Fee total: every fill fee summed = total fees.
    fee_sum = sum(float(f["fee"]) for f in FILLS)
    assert abs(fee_sum - EXPECTED_TOTALS["total_fees_usd"]) < tol, (
        f"fill fee sum {fee_sum} != expected {EXPECTED_TOTALS['total_fees_usd']}"
    )

    # 4. Funding rows signed total.
    fund_sum = sum(r["usdc"] for r in EXPECTED_FUNDING_ROWS)
    assert abs(fund_sum - EXPECTED_TOTALS["total_funding_usd"]) < tol

    # 5. The equity identity (corrected form; unrealized = 0, flat account).
    identity = (
        EXPECTED_TOTALS["total_deposits_usd"]
        - EXPECTED_TOTALS["total_withdrawals_usd"]
        + EXPECTED_TOTALS["total_realized_usd"]
        + EXPECTED_TOTALS["total_funding_usd"]
        - EXPECTED_TOTALS["total_fees_usd"]
    )
    assert abs(identity - EXPECTED_TOTALS["final_equity_usd"]) < tol, (
        f"equity identity gives {identity}, "
        f"expected {EXPECTED_TOTALS['final_equity_usd']}"
    )

    # 6. Coverage claimed by the plan: open+close long, open+close short,
    #    partial close, flip, liquidation, funding both directions,
    #    deposit + withdrawal, HIP-3 namespaced asset.
    dirs = {f["dir"] for f in FILLS}
    assert {"Open Long", "Close Long", "Open Short", "Close Short",
            "Long > Short"} <= dirs
    assert any(":" in f["coin"] for f in FILLS), "need a HIP-3 asset"
    assert any(e["usdc"] < 0 for e in EXPECTED_FUNDING_ROWS)
    assert any(e["usdc"] > 0 for e in EXPECTED_FUNDING_ROWS)
    types = {e["delta"]["type"] for e in LEDGER_EVENTS}
    assert {"deposit", "withdraw"} <= types

    print("GATE 0 PASSED")
    print(f"  fills:            {len(FILLS)}")
    print(f"  funding events:   {len(FUNDING_EVENTS)}")
    print(f"  ledger events:    {len(LEDGER_EVENTS)}")
    print(f"  closing events:   {len(EXPECTED_CLOSING_EVENTS)} "
          f"across {EXPECTED_TOTALS['n_episodes']} episodes")
    print(f"  realized total:   {EXPECTED_TOTALS['total_realized_usd']} USD "
          f"(matches venue closedPnl sum)")
    print(f"  fees total:       {EXPECTED_TOTALS['total_fees_usd']} USD")
    print(f"  funding total:    {EXPECTED_TOTALS['total_funding_usd']} USD "
          f"(signed)")
    print(f"  equity identity:  {EXPECTED_TOTALS['final_equity_usd']} USD "
          f"(flat account, unrealized = 0)")


if __name__ == "__main__":
    _self_check()
