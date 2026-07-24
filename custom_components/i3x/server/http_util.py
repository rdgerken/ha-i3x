"""Envelope helpers and security guards for the i3X HTTP layer.

Every response — success or error — must be produced through these helpers so
the i3X response envelope, RFC 9457 problem details, and gzip negotiation
(HomeAssistantView.json → enable_compression) are applied consistently.
"""

from __future__ import annotations

import ipaddress
import time
from collections import OrderedDict
from typing import Any

from aiohttp import web

from ..const import (
    INFO_RATE_BURST,
    INFO_RATE_MAX_IPS,
    INFO_RATE_PER_MINUTE,
    MAX_BULK_IDS,
)


class ProblemError(Exception):
    """Raised by handlers to short-circuit into a problem response."""

    def __init__(self, status: int, title: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.title = title
        self.detail = detail


def problem_body(status: int, title: str, detail: str) -> dict:
    return {
        "success": False,
        "responseDetail": {"title": title, "status": status, "detail": detail},
    }


def item_ok(key_field: str, key: str, result: Any) -> dict:
    return {"success": True, key_field: key, "result": result}


def item_problem(key_field: str, key: str, status: int, title: str, detail: str) -> dict:
    return {
        "success": False,
        key_field: key,
        "responseDetail": {"title": title, "status": status, "detail": detail},
    }


def item_not_found(key_field: str, key: str, what: str = "Element") -> dict:
    return item_problem(key_field, key, 404, "Not Found", f"{what} not found: {key}")


def bulk_body(results: list[dict]) -> dict:
    """Bulk envelope: same order/length as the request, per-item failures."""
    return {
        "success": all(item.get("success") for item in results),
        "results": results,
    }


def require_id_list(body: Any, field: str = "elementIds") -> list[str]:
    """Validate a bulk id list: present, a list of strings, within the cap."""
    if not isinstance(body, dict) or not isinstance(body.get(field), list):
        raise ProblemError(400, "Bad Request", f"{field} array is required")
    ids = body[field]
    if len(ids) > MAX_BULK_IDS:
        raise ProblemError(
            400,
            "Bad Request",
            f"{field} exceeds the server limit of {MAX_BULK_IDS} ids per request",
        )
    if not all(isinstance(i, str) for i in ids):
        raise ProblemError(400, "Bad Request", f"{field} must contain only strings")
    return ids


async def parse_json_body(request: web.Request) -> dict:
    """Parse the request body as a JSON object (empty body → {})."""
    if request.content_length in (None, 0):
        return {}
    try:
        body = await request.json()
    except ValueError as err:
        raise ProblemError(400, "Bad Request", "Request body is not valid JSON") from err
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise ProblemError(400, "Bad Request", "Request body must be a JSON object")
    return body


# --------------------------------------------------------------------- guards


def is_local_address(remote: str | None) -> bool:
    """True when the effective client IP is private/loopback/link-local."""
    if not remote:
        return False
    try:
        ip = ipaddress.ip_address(remote)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


def check_local_only(request: web.Request, local_only: bool) -> None:
    """Enforce the local_only option for every i3X endpoint."""
    if local_only and not is_local_address(request.remote):
        raise ProblemError(
            403,
            "Forbidden",
            "This i3X server only accepts requests from the local network",
        )


class InfoRateLimiter:
    """Per-IP token bucket for the unauthenticated /info endpoint.

    Only applied to non-local addresses (local traffic and the conformance
    suite are exempt). The IP map is LRU-bounded so the limiter itself cannot
    be used to exhaust memory.
    """

    def __init__(
        self,
        rate_per_minute: int = INFO_RATE_PER_MINUTE,
        burst: int = INFO_RATE_BURST,
        max_ips: int = INFO_RATE_MAX_IPS,
    ) -> None:
        self._rate = rate_per_minute / 60.0
        self._burst = float(burst)
        self._max_ips = max_ips
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()

    def check(self, remote: str | None, now: float | None = None) -> None:
        """Consume one token for this IP or raise a 429 problem."""
        if remote is None or is_local_address(remote):
            return
        now = time.monotonic() if now is None else now
        tokens, last = self._buckets.pop(remote, (self._burst, now))
        tokens = min(self._burst, tokens + (now - last) * self._rate)
        if tokens < 1.0:
            self._buckets[remote] = (tokens, now)
            self._trim()
            raise ProblemError(
                429, "Too Many Requests", "Rate limit exceeded for this endpoint"
            )
        self._buckets[remote] = (tokens - 1.0, now)
        self._trim()

    def _trim(self) -> None:
        while len(self._buckets) > self._max_ips:
            self._buckets.popitem(last=False)
