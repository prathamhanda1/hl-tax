"""
tests/test_webapp_server.py — Phase 8B gate on the HTTP layer.

OFFLINE AND DETERMINISTIC. `webapp.pipeline.build_result` is replaced with a
stub in every test that reaches it, so nothing here touches the network, the
raw cache or the FX series. That is the point: this file tests ROUTING AND
STATUS CODES, which is all `webapp/server.py` owns. The numbers themselves are
already gated by tests/test_webapp_pipeline.py, and the tax logic by
tests/test_treatments.py.

The contract under test is the one the dashboard branches on
(webapp/static/index.html: 400 -> "invalid", 422 -> "fx", 500 -> "server"):
a wrong status code here is a wrong error message in front of a user, and a
swallowed FxError would be a partial tax number wearing a green checkmark.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="webapp/requirements.txt")
from fastapi.testclient import TestClient          # noqa: E402

from webapp import server                          # noqa: E402

ADDRESS = "0x4964f89307d74519f8302c4b9695f3a80c2098ef"
STATIC = Path(server.__file__).resolve().parent / "static"


@pytest.fixture(autouse=True)
def _clean_limiter():
    """The limiter and the concurrency semaphore are process globals; reset them
    so test order cannot change a result."""
    with server._rate_lock:
        server._rate_hits.clear()
    yield
    with server._rate_lock:
        server._rate_hits.clear()


@pytest.fixture()
def client():
    return TestClient(server.app)


def _payload(**over) -> dict:
    """A minimal payload of the shape Phase A emits — enough for routing tests."""
    base = {
        "meta": {"address": ADDRESS, "event_range": "2026-04-17 to 2026-07-16",
                 "generated_utc": "2026-07-26 09:14 UTC", "as_of": None,
                 "refresh": False, "onramp_cost_inr": None},
        "empty": False, "warnings": [],
        "treatment_order": list(server.TREATMENT_ORDER),
        "treatments": {k: {"total_tax_inr": 0.0} for k in server.TREATMENT_ORDER},
        "tds": {}, "cross_cutting": {}, "ledger_row_count": 0,
        "ledger_counts": {}, "ledger_preview": [], "schedule_vda": [],
    }
    base.update(over)
    return base


class _Spy:
    """Stands in for build_result and records how it was called."""

    def __init__(self, result=None, raises=None):
        self.result, self.raises, self.calls = result, raises, []

    def __call__(self, address, **kw):
        self.calls.append((address, kw))
        if self.raises is not None:
            raise self.raises
        return self.result if self.result is not None else _payload()


def _install(monkeypatch, spy):
    monkeypatch.setattr(server, "build_result", spy)
    return spy


# ---------------------------------------------------------------------------
# Introspection routes.
# ---------------------------------------------------------------------------

def test_health_ok(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_meta_reports_conventions_and_limits(client):
    body = client.get("/api/meta").json()
    assert body["treatment_order"] == list(server.TREATMENT_ORDER)
    assert body["ledger_columns"] == list(server.LEDGER_PREVIEW_COLUMNS)
    assert body["limits"]["max_preview_rows"] == server.MAX_PREVIEW_ROWS
    # Neutrality is inherited by the API's own description too.
    assert "not tax advice" in body["disclaimer"].lower()


# ---------------------------------------------------------------------------
# /api/report — the happy path and the parameter pass-through.
# ---------------------------------------------------------------------------

def test_report_returns_payload_verbatim(client, monkeypatch):
    spy = _install(monkeypatch, _Spy(_payload()))
    r = client.get("/api/report", params={"address": ADDRESS})
    assert r.status_code == 200
    assert r.json() == _payload()          # not reshaped by the HTTP layer
    assert spy.calls[0][0] == ADDRESS


def test_report_forwards_every_advanced_input(client, monkeypatch):
    spy = _install(monkeypatch, _Spy())
    client.get("/api/report", params={
        "address": ADDRESS, "as_of": "2026-07-16", "onramp_cost": "250000",
        "salary_income_inr": "1200000", "other_head_income_inr": "50000",
        "refresh": "true", "line_items": "true", "preview_rows": "50",
    })
    _, kw = spy.calls[0]
    assert kw["as_of"] == "2026-07-16"
    assert kw["onramp_cost"] == 250000.0
    assert kw["salary_income_inr"] == 1200000.0
    assert kw["other_head_income_inr"] == 50000.0
    assert kw["refresh"] is True
    assert kw["include_line_items"] is True
    assert kw["preview_rows"] == 50


def test_empty_activity_is_200_not_an_error(client, monkeypatch):
    """A valid address with no in-window activity is a fact, not a failure."""
    _install(monkeypatch, _Spy(_payload(empty=True)))
    r = client.get("/api/report", params={"address": ADDRESS})
    assert r.status_code == 200
    assert r.json()["empty"] is True


# ---------------------------------------------------------------------------
# Error mapping — the contract the dashboard branches on.
# ---------------------------------------------------------------------------

def test_bad_address_is_400_with_the_engines_own_message(client, monkeypatch):
    from fetch.fetch_user import BadAddressError
    _install(monkeypatch, _Spy(raises=BadAddressError("'0xdead' is not valid")))
    r = client.get("/api/report", params={"address": "0xdead"})
    assert r.status_code == 400
    assert "not valid" in r.json()["detail"]


def test_fx_error_is_422_and_carries_both_fixes(client, monkeypatch):
    from fx.fx import UpstreamNotYetPublishedError
    _install(monkeypatch, _Spy(
        raises=UpstreamNotYetPublishedError("rate for 2026-07-22 not published.")))
    r = client.get("/api/report", params={"address": ADDRESS})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "2026-07-22" in detail                       # what it refused, and why
    assert "fx.fetch_fx --update" in detail             # fix 1
    assert "as_of" in detail                            # fix 2
    # A refusal must never be dressed up as a number.
    assert "0" != detail.strip()


def test_unexpected_failure_is_500_without_a_traceback(client, monkeypatch):
    _install(monkeypatch, _Spy(raises=RuntimeError("boom at line 42")))
    r = client.get("/api/report", params={"address": ADDRESS})
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert "RuntimeError" in detail
    assert "line 42" not in detail and "Traceback" not in detail


def test_unparseable_as_of_is_400_before_any_work(client, monkeypatch):
    spy = _install(monkeypatch, _Spy())
    r = client.get("/api/report",
                   params={"address": ADDRESS, "as_of": "not-a-date"})
    assert r.status_code == 400
    assert "as_of" in r.json()["detail"]
    assert spy.calls == []          # rejected before the network step


def test_malformed_number_is_400_with_a_readable_string(client, monkeypatch):
    """FastAPI's native 422-with-a-list-of-dicts would both collide with the FX
    status and render as '[object Object]' in the page."""
    _install(monkeypatch, _Spy())
    r = client.get("/api/report",
                   params={"address": ADDRESS, "onramp_cost": "abc"})
    assert r.status_code == 400
    assert isinstance(r.json()["detail"], str)
    assert "onramp_cost" in r.json()["detail"]


def test_missing_address_is_400(client, monkeypatch):
    _install(monkeypatch, _Spy())
    assert client.get("/api/report").status_code == 400


def test_preview_rows_over_the_ceiling_is_400(client, monkeypatch):
    _install(monkeypatch, _Spy())
    r = client.get("/api/report", params={
        "address": ADDRESS, "preview_rows": server.MAX_PREVIEW_ROWS + 1})
    assert r.status_code == 400


def test_every_error_body_has_a_string_detail(client, monkeypatch):
    """The page reads `body.detail || body.message` and nothing else."""
    from fetch.fetch_user import BadAddressError
    from fx.fx import FxError
    for exc in (BadAddressError("bad"), FxError("no rate"), RuntimeError("x")):
        _install(monkeypatch, _Spy(raises=exc))
        body = client.get("/api/report", params={"address": ADDRESS}).json()
        assert isinstance(body.get("detail"), str) and body["detail"]


# ---------------------------------------------------------------------------
# Abuse control.
# ---------------------------------------------------------------------------

def test_rate_limit_returns_429_with_retry_after(client, monkeypatch):
    _install(monkeypatch, _Spy())
    monkeypatch.setattr(server, "RATE_LIMIT_PER_MIN", 3)
    for _ in range(3):
        assert client.get("/api/report",
                          params={"address": ADDRESS}).status_code == 200
    r = client.get("/api/report", params={"address": ADDRESS})
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) >= 1
    assert "per minute" in r.json()["detail"]


def test_rate_limit_can_be_disabled(client, monkeypatch):
    _install(monkeypatch, _Spy())
    monkeypatch.setattr(server, "RATE_LIMIT_PER_MIN", 0)
    for _ in range(25):
        assert client.get("/api/report",
                          params={"address": ADDRESS}).status_code == 200


def test_saturated_concurrency_is_503_not_a_queue(client, monkeypatch):
    _install(monkeypatch, _Spy())
    held = [server._REPORT_SLOTS.acquire(blocking=False)
            for _ in range(server.MAX_CONCURRENT_REPORTS)]
    try:
        assert all(held)
        r = client.get("/api/report", params={"address": ADDRESS})
        assert r.status_code == 503
        assert r.headers["retry-after"] == "5"
    finally:
        for _ in held:
            server._REPORT_SLOTS.release()


def test_slot_is_released_after_a_failure(client, monkeypatch):
    """A raising pipeline must not leak a semaphore slot, or the server bleeds
    capacity until it answers 503 forever."""
    _install(monkeypatch, _Spy(raises=RuntimeError("boom")))
    for _ in range(server.MAX_CONCURRENT_REPORTS + 2):
        assert client.get("/api/report",
                          params={"address": ADDRESS}).status_code == 500
    _install(monkeypatch, _Spy())
    assert client.get("/api/report",
                      params={"address": ADDRESS}).status_code == 200


def test_refresh_is_ignored_and_disclosed_when_disabled(client, monkeypatch):
    spy = _install(monkeypatch, _Spy())
    monkeypatch.setattr(server, "ALLOW_REFRESH", False)
    body = client.get("/api/report",
                      params={"address": ADDRESS, "refresh": "true"}).json()
    assert spy.calls[0][1]["refresh"] is False        # not honoured
    assert any("refresh=true was ignored" in w for w in body["warnings"])


# ---------------------------------------------------------------------------
# CA report (Phase 9 pending).
# ---------------------------------------------------------------------------

def test_fys_degrades_to_an_empty_list_not_an_error(client):
    """The modal renders its 'no financial years' state from a 200; an error
    here would show a red failure for a feature that simply is not built."""
    r = client.get("/api/ca-report/fys", params={"address": ADDRESS})
    assert r.status_code == 200
    body = r.json()
    assert body["fys"] == [] and body["available"] is False
    assert "Phase 9" in body["detail"]


def test_ca_report_bundle_is_501_while_pending(client):
    r = client.get("/api/ca-report", params={"address": ADDRESS, "fy": "2025-26"})
    assert r.status_code == 501
    assert isinstance(r.json()["detail"], str)


# ---------------------------------------------------------------------------
# The static dashboard, and the two ways it is easy to break.
# ---------------------------------------------------------------------------

def test_dashboard_and_its_assets_are_served(client):
    assert "<x-dc>" in client.get("/").text
    assert client.get("/support.js").status_code == 200
    assert client.get("/vendor/react.production.min.js").status_code == 200
    assert client.get("/vendor/react-dom.production.min.js").status_code == 200


def test_page_defaults_to_live_data_not_the_mock_fixture(client):
    """USE_MOCK = true would render invented tax numbers in a real-looking
    dashboard. It must default off, with ?mock=1 as the only way in."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    # The literal assignment, not the words: "(USE_MOCK = true)" also appears
    # inside the data-source label the page prints when the mock IS in use.
    assert not re.search(r"USE_MOCK\s*=\s*true\s*;", html)
    assert re.search(r"USE_MOCK\s*=\s*/\[\?&\]mock=1", html)


def test_react_is_vendored_and_resources_flag_is_not_set(client):
    """Two regressions in one assertion.

    1. A CDN <script> would break the page offline and under a strict CSP.
    2. window.__resources tells dc-runtime the in-DOM template is authoritative
       — but the HTML parser foster-parents <sc-for> out of <table>, so setting
       it silently empties the ledger and Schedule-VDA tables while every other
       panel keeps working. Verified by rendering: 353 rows with the flag
       unset, 0 rows with it set.
    """
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "./vendor/react.production.min.js" in html
    assert "unpkg.com" not in html
    assert not re.search(r"window\.__resources\s*=", html)


def test_frontend_query_parameters_all_exist_on_the_endpoint(client):
    """Every parameter the page can send must be one the endpoint declares —
    the field-by-field check from Phase A, kept honest as code."""
    spec = client.get("/openapi.json").json()
    declared = {p["name"] for p in
                spec["paths"]["/api/report"]["get"]["parameters"]}
    sent = {"address", "refresh", "as_of", "onramp_cost",
            "salary_income_inr", "other_head_income_inr"}
    assert sent <= declared


def test_payload_is_rendered_as_strict_json(client, monkeypatch):
    """Starlette renders with allow_nan=False. A NaN leaking out of Phase A
    must raise here rather than emit `NaN`, which JSON.parse rejects and the
    dashboard would report as a network failure."""
    _install(monkeypatch, _Spy(_payload(warnings=["ok"])))
    text = client.get("/api/report", params={"address": ADDRESS}).text
    assert "NaN" not in text
    json.loads(text)


def test_cors_headers_are_present(client):
    r = client.get("/api/health", headers={"Origin": "http://localhost:5173"})
    assert r.headers.get("access-control-allow-origin")
