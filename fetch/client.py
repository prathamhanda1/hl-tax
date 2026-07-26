"""
fetch/client.py — transport only. The single function-level
responsibility: send a JSON body to the Hyperliquid info endpoint and get a
JSON body back, safely. It knows NOTHING about what is being fetched (no
"userFills", no addresses, no pagination) — that is fetch_user.py's job.

What "safely" means, and why each rule exists (all verified live against
https://api.hyperliquid.xyz/info on 2026-07-22):

  - Empty history is HTTP 200, not an error. A well-formed address with no
    trades returns 200 + [] (or a zeroed clearinghouseState). The transport
    returns that untouched; deciding whether "empty" is interesting is the
    caller's job, never a failure here.

  - A malformed request is HTTP 422 ("Failed to deserialize the JSON body
    into the target type"). Retrying it can never succeed, so a 4xx is
    raised IMMEDIATELY as InfoClientError — no wasted backoff on a request
    that is dead on arrival.

  - A transient failure (network error, timeout, HTTP 5xx, or 429
    rate-limit) IS retried with exponential backoff. A live 429 was
    actually observed during a heavy backfill, so this path is real, not
    theoretical. If the 429 carries a Retry-After header we honor it
    instead of guessing.

A single InfoClient instance should be reused for a whole fetch run so the
inter-request throttle is enforced globally rather than reset per call site.
"""

from __future__ import annotations

import time

import requests

import config


class InfoClientError(Exception):
    """A request that failed and will not be retried further: either a
    client-side 4xx (e.g. a malformed address) or a transient error that
    survived every retry. Always carries a human-readable message — a raw
    stack trace is never the only signal a caller gets.
    """


class InfoClient:
    def __init__(
        self,
        base_url: str = config.API_BASE_URL,
        timeout_s: float = config.REQUEST_TIMEOUT_S,
        max_retries: int = config.MAX_RETRIES,
        backoff_base_s: float = config.RETRY_BACKOFF_BASE_S,
        rate_limit_delay_s: float = config.RATE_LIMIT_DELAY_S,
    ):
        self.base_url = base_url
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.backoff_base_s = backoff_base_s
        self.rate_limit_delay_s = rate_limit_delay_s
        self._last_call_ts: float | None = None
        self._session = requests.Session()
        self.request_count = 0  # for Gate-1 "re-run hits cache = 0 calls" proof

    def _throttle(self) -> None:
        """Enforce at least rate_limit_delay_s between consecutive calls,
        measured from the START of the previous call, so a slow response
        does not stack extra delay on top of its own latency.
        """
        if self._last_call_ts is None:
            return
        remaining = self.rate_limit_delay_s - (time.monotonic() - self._last_call_ts)
        if remaining > 0:
            time.sleep(remaining)

    def post(self, body: dict) -> object:
        """POST `body`, return parsed JSON. Raise InfoClientError with a
        clear message on a 4xx (immediately) or on exhausted retries.
        """
        last_error: str | None = None
        for attempt in range(self.max_retries):
            self._throttle()
            self._last_call_ts = time.monotonic()
            self.request_count += 1
            try:
                resp = self._session.post(
                    self.base_url, json=body, timeout=self.timeout_s
                )
            except requests.exceptions.RequestException as exc:
                last_error = f"network error: {exc}"
                self._backoff(attempt)
                continue

            if 200 <= resp.status_code < 300:
                try:
                    return resp.json()
                except ValueError as exc:
                    raise InfoClientError(
                        f"info endpoint returned non-JSON body for "
                        f"{body!r}: {exc}"
                    ) from exc

            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = f"transient HTTP {resp.status_code}: {resp.text[:200]!r}"
                self._backoff(attempt, resp)
                continue

            # Any other 4xx: a client error. Retrying is pointless.
            raise InfoClientError(
                f"info endpoint rejected {body!r} with HTTP "
                f"{resp.status_code}: {resp.text[:200]!r}"
            )

        raise InfoClientError(
            f"info endpoint request {body!r} failed after "
            f"{self.max_retries} attempts: {last_error}"
        )

    def _backoff(self, attempt: int, resp: requests.Response | None = None) -> None:
        """Sleep before the next retry. Honor a Retry-After header if the
        server sent one (429s sometimes do); otherwise exponential backoff.
        """
        if resp is not None:
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    time.sleep(float(retry_after))
                    return
                except ValueError:
                    pass  # non-numeric Retry-After (an HTTP date) -> fall through
        time.sleep(self.backoff_base_s**attempt)
