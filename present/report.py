"""
present/report.py — Phase 6, the presentation layer.

Renders three artifacts from the INR ledger (interpret.assemble.build_inr_ledger)
and the interpret() output. It COMPUTES NOTHING NEW — every number here already
exists upstream; this layer only formats, orders, and labels. A change in
presentation can never change a tax number (Week-1 layer discipline).

Artifacts:
  (a) ledger CSV            — every event: USD, INR, fx_rate, fx_rate_date,
                              category, episode, traceability id. The audit
                              spine: every summary number traces back to rows here.
  (b) CA summary (HTML)     — the four treatments side by side, the assumptions
                              box, the on-ramp/off-ramp boundary with its
                              manual-input slot, and the LIMITATIONS AT THE TOP.
                              A non-technical reader (the CA) follows it without
                              seeing code.
  (c) Schedule-VDA CSV      — one row per closing episode in the shape Schedule
                              VDA of the ITR expects (date of acquisition, date
                              of transfer, consideration, cost, income).

Neutrality is mandated (brief non-negotiable #2, master-plan §0.1/§11): no
artifact labels any treatment correct, best, or recommended. They are ordered
by section number and shown side by side.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

import config

# Display order + human labels. Ordered by section number, NEVER by preference.
TREATMENT_ORDER = ["vda", "futures_inr", "speculative", "non_speculative"]
TREATMENT_LABELS = {
    "vda": "OP 1 — VDA transfer / s.115BBH (flat 30%)",
    "futures_inr": "OP 2 — Futures, INR-margined premise (slab)",
    "speculative": "OP 3 — Speculative business (slab, losses ring-fenced)",
    "non_speculative": "OP 4 — Non-speculative business (slab, normal set-off)",
}
PREMISE_NOTE = {
    "live": "live contender",
    "counterfactual": "COUNTERFACTUAL for this address",
}

# Human-readable ledger column order for the CSV audit spine.
_LEDGER_CSV_COLUMNS = [
    "event_id", "ts_utc", "category", "asset", "asset_class", "episode_id",
    "amount_usd", "fx_rate", "fx_rate_date", "amount_inr",
    "gross_consideration_inr", "is_liquidation", "cost_basis_incomplete",
    "fx_source", "fx_convention", "fx_stale_grace",
]


@dataclass
class ReportPaths:
    ledger_csv: Path
    schedule_vda_csv: Path
    summary_html: Path


# ---------------------------------------------------------------------------
# Formatting helpers (pure display; used by the template too).
# ---------------------------------------------------------------------------

def fmt_inr(x) -> str:
    """Indian-rupee display with the configured decimals. NaN -> em dash."""
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return "—"
    return f"₹{float(x):,.{config.REPORT_INR_DECIMALS}f}"


def fmt_usd(x) -> str:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return "—"
    return f"${float(x):,.{config.REPORT_USD_DECIMALS}f}"


def _fmt_ts(ts) -> str:
    if ts is None or pd.isna(ts):
        return ""
    return pd.Timestamp(ts).strftime("%Y-%m-%d %H:%M:%S UTC")


def _fmt_date(ts) -> str:
    if ts is None or pd.isna(ts):
        return ""
    return pd.Timestamp(ts).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# (a) Ledger CSV — the audit spine.
# ---------------------------------------------------------------------------

def write_ledger_csv(ledger: pd.DataFrame, path: Path) -> Path:
    """Full INR ledger to CSV, every event traceable. No rounding of the stored
    numbers (rounding is a display choice, never a stored one)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = [c for c in _LEDGER_CSV_COLUMNS if c in ledger.columns]
    ledger[cols].to_csv(path, index=False)
    return path


# ---------------------------------------------------------------------------
# (c) Schedule-VDA CSV — one row per closing episode.
# ---------------------------------------------------------------------------

def schedule_vda_frame(ledger: pd.DataFrame) -> pd.DataFrame:
    """Shape the realized closes into the Schedule-VDA reporting genre.

    Columns mirror the ITR Schedule VDA: date of acquisition, date of transfer,
    consideration received, cost of acquisition, income. For a perp we back out
    the implied cost as (consideration - income) so the arithmetic ties out to
    the ledger exactly. Income here is the realized P&L in INR; a losing episode
    shows negative income, surfaced honestly (Schedule VDA itself then applies
    the no-set-off rule — that is the interpret layer's VDA base, not ours)."""
    rp = ledger[ledger["category"] == "realized_pnl"].copy()
    rows = []
    for i, r in enumerate(rp.itertuples(index=False), start=1):
        income = float(r.amount_inr) if not pd.isna(r.amount_inr) else float("nan")
        consideration = (
            float(r.gross_consideration_inr)
            if not pd.isna(r.gross_consideration_inr) else float("nan")
        )
        cost = consideration - income if consideration == consideration else float("nan")
        acq = getattr(r, "acq_ts", pd.NaT)
        rows.append({
            "sr_no": i,
            "asset": r.asset,
            "asset_class": r.asset_class,
            "episode_id": r.episode_id,
            "date_of_acquisition": _fmt_date(acq),
            "date_of_transfer": _fmt_date(r.ts_utc),
            "consideration_received_inr": consideration,
            "cost_of_acquisition_inr": cost,
            "income_inr": income,
            "is_liquidation": bool(r.is_liquidation),
            "cost_basis_incomplete": bool(getattr(r, "cost_basis_incomplete", False)),
        })
    return pd.DataFrame(rows)


def write_schedule_vda_csv(ledger: pd.DataFrame, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    schedule_vda_frame(ledger).to_csv(path, index=False)
    return path


# ---------------------------------------------------------------------------
# (b) CA summary HTML.
# ---------------------------------------------------------------------------

def _treatment_view(t: dict) -> dict:
    """Flatten one treatment dict into template-ready display strings, keeping
    every raw number reachable too (the template shows formatted; the CSV keeps
    raw)."""
    v = {
        "label": TREATMENT_LABELS.get(t["treatment"], t["treatment"]),
        "premise": t.get("premise", "live"),
        "premise_note": PREMISE_NOTE.get(t.get("premise", "live"), ""),
        "rate_basis": t.get("rate_basis", ""),
        "taxable_base_inr": fmt_inr(t.get("taxable_base_inr")),
        "tax_before_cess_inr": fmt_inr(t.get("tax_before_cess_inr")),
        "surcharge_inr": fmt_inr(t.get("surcharge_inr")),
        "cess_inr": fmt_inr(t.get("cess_inr")),
        "total_tax_inr": fmt_inr(t.get("total_tax_inr")),
        "loss_treatment": t.get("loss_treatment", ""),
        "current_year_setoff_inr": fmt_inr(t.get("current_year_setoff_inr")),
        "carry_forward_inr": fmt_inr(t.get("carry_forward_inr")),
        "carry_forward_years": t.get("carry_forward_years"),
        "tds_expected_inr": fmt_inr(t.get("tds_expected_inr")),
        "audit_flag": t.get("audit_flag", False),
        "turnover_inr": fmt_inr(t.get("turnover_inr")),
        "section_refs": t.get("section_refs", {}),
        "caveats": t.get("caveats", []),
        "is_counterfactual": t.get("premise") == "counterfactual",
    }
    # VDA-only extras: the gross-vs-net gap is the headline shock number.
    if t["treatment"] == "vda":
        v["vda_gross_base_inr"] = fmt_inr(t.get("vda_gross_base_inr"))
        v["vda_net_base_inr"] = fmt_inr(t.get("vda_net_base_inr"))
        v["vda_gross_vs_net_gap_inr"] = fmt_inr(t.get("vda_gross_vs_net_gap_inr"))
    return v


def _jinja_env():
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    templates_dir = Path(__file__).parent / "templates"
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(["html"]),
    )
    env.filters["inr"] = fmt_inr
    return env


def render_summary_html(
    ledger: pd.DataFrame,
    interpreted: dict,
    meta: dict,
    path: Path,
) -> Path:
    """Render the CA-facing HTML. `meta` carries run context (address, date
    range, assumptions, on-ramp cost) — display only."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    treatments = [_treatment_view(interpreted[k]) for k in TREATMENT_ORDER]
    cc = interpreted["cross_cutting"]
    tds = interpreted["tds"]

    # A tiny ledger digest so the summary is self-standing (counts by category).
    counts = (
        ledger["category"].value_counts().to_dict() if len(ledger) else {}
    )

    ctx = {
        "meta": meta,
        "treatments": treatments,
        "cross_cutting": cc,
        "tds": tds,
        "ledger_counts": counts,
        "ledger_row_count": int(len(ledger)),
        "onramp_cost": config.ON_RAMP_USDC_COST_INR,
        "fmt": {"inr": fmt_inr},
    }
    html = _jinja_env().get_template("summary.html.j2").render(**ctx)
    path.write_text(html, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Orchestrator: write all three artifacts.
# ---------------------------------------------------------------------------

def build_report(
    ledger: pd.DataFrame,
    interpreted: dict,
    out_dir: Path,
    meta: dict,
) -> ReportPaths:
    """Write ledger CSV, Schedule-VDA CSV, and the CA summary HTML into out_dir.
    Returns the three paths. Computes nothing new."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = ReportPaths(
        ledger_csv=write_ledger_csv(ledger, out_dir / "ledger.csv"),
        schedule_vda_csv=write_schedule_vda_csv(
            ledger, out_dir / "schedule_vda.csv"),
        summary_html=render_summary_html(
            ledger, interpreted, meta, out_dir / "summary.html"),
    )
    return paths
