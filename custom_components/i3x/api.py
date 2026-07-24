"""Async client for CESMII i3X 1.0 servers.

Hand-rolled on aiohttp (Home Assistant's bundled HTTP stack) rather than the
official sync `i3x-client` package, so requests share HA's event loop and the
integration keeps zero pip requirements. Supports the auth conventions the
i3X ecosystem uses: Bearer token, HTTP Basic, or a custom header.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import aiohttp

from .const import (
    AUTH_BASIC,
    AUTH_BEARER,
    AUTH_HEADER,
    AUTH_NONE,
)

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)
# Streams idle between heartbeats; allow several missed beats before giving up.
STREAM_TIMEOUT = aiohttp.ClientTimeout(total=None, connect=30, sock_read=90)


class I3xError(Exception):
    """Base error for i3X client failures."""


class I3xConnectionError(I3xError):
    """The server could not be reached."""


class I3xApiError(I3xError):
    """The server answered with an error envelope."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.detail = message


class I3xAuthError(I3xApiError):
    """Authentication was rejected (401/403)."""


class I3xApiClient:
    """Minimal-but-complete async client for the i3X 1.0 REST API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        *,
        auth_type: str = AUTH_NONE,
        token: str | None = None,
        username: str | None = None,
        password: str | None = None,
        header_name: str | None = None,
        header_value: str | None = None,
        verify_ssl: bool = True,
    ) -> None:
        self._session = session
        self.base_url = base_url.rstrip("/")
        self._ssl = None if verify_ssl else False
        self._headers = {"Accept": "application/json"}
        if auth_type == AUTH_BEARER and token:
            self._headers["Authorization"] = f"Bearer {token}"
        elif auth_type == AUTH_BASIC and username is not None:
            cred = base64.b64encode(
                f"{username}:{password or ''}".encode()
            ).decode()
            self._headers["Authorization"] = f"Basic {cred}"
        elif auth_type == AUTH_HEADER and header_name:
            self._headers[header_name] = header_value or ""

    # ----------------------------------------------------------------- core
    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        url = f"{self.base_url}{path}"
        try:
            async with self._session.request(
                method,
                url,
                json=json_body,
                params=params,
                headers=self._headers,
                timeout=REQUEST_TIMEOUT,
                ssl=self._ssl,
            ) as resp:
                try:
                    body = await resp.json(content_type=None)
                except (ValueError, aiohttp.ClientError):
                    body = None
                if resp.status >= 400:
                    detail = ""
                    if isinstance(body, dict):
                        rd = body.get("responseDetail") or {}
                        detail = rd.get("detail") or rd.get("title") or ""
                    cls = I3xAuthError if resp.status in (401, 403) else I3xApiError
                    raise cls(resp.status, detail or resp.reason or "request failed")
                if not isinstance(body, dict):
                    raise I3xApiError(resp.status, "response body is not JSON")
                return body
        except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError) as err:
            raise I3xConnectionError(f"Cannot reach {url}: {err}") from err

    # ------------------------------------------------------------ discovery
    async def async_get_info(self) -> dict:
        return (await self._request("GET", "/info"))["result"]

    async def async_get_namespaces(self) -> list[dict]:
        return (await self._request("GET", "/namespaces"))["result"]

    async def async_get_objects(
        self, *, root: bool | None = None, include_metadata: bool = False
    ) -> list[dict]:
        params: dict[str, str] = {}
        if root is not None:
            params["root"] = "true" if root else "false"
        if include_metadata:
            params["includeMetadata"] = "true"
        return (await self._request("GET", "/objects", params=params))["result"]

    async def async_list_objects(
        self, element_ids: list[str], *, include_metadata: bool = False
    ) -> list[dict]:
        body = {"elementIds": element_ids, "includeMetadata": include_metadata}
        return (await self._request("POST", "/objects/list", json_body=body))[
            "results"
        ]

    async def async_query_object_types(self, element_ids: list[str]) -> list[dict]:
        return (
            await self._request(
                "POST", "/objecttypes/query", json_body={"elementIds": element_ids}
            )
        )["results"]

    # ---------------------------------------------------------------- values
    async def async_get_values(self, element_ids: list[str]) -> list[dict]:
        return (
            await self._request(
                "POST", "/objects/value", json_body={"elementIds": element_ids}
            )
        )["results"]

    async def async_get_history(
        self, element_ids: list[str], start_time: str, end_time: str
    ) -> list[dict]:
        body = {
            "elementIds": element_ids,
            "startTime": start_time,
            "endTime": end_time,
        }
        return (await self._request("POST", "/objects/history", json_body=body))[
            "results"
        ]

    async def async_put_values(self, updates: list[dict]) -> list[dict]:
        return (
            await self._request(
                "PUT", "/objects/value", json_body={"updates": updates}
            )
        )["results"]

    # --------------------------------------------------------- subscriptions
    async def async_create_subscription(
        self, client_id: str, display_name: str | None = None
    ) -> str:
        body: dict[str, Any] = {"clientId": client_id}
        if display_name:
            body["displayName"] = display_name
        result = (await self._request("POST", "/subscriptions", json_body=body))[
            "result"
        ]
        return result["subscriptionId"]

    async def async_register(
        self, client_id: str, subscription_id: str, element_ids: list[str]
    ) -> list[dict]:
        body = {
            "clientId": client_id,
            "subscriptionId": subscription_id,
            "elementIds": element_ids,
        }
        return (
            await self._request("POST", "/subscriptions/register", json_body=body)
        )["results"]

    async def async_sync(
        self,
        client_id: str,
        subscription_id: str,
        last_sequence_number: int | None = None,
    ) -> list[dict]:
        body: dict[str, Any] = {
            "clientId": client_id,
            "subscriptionId": subscription_id,
        }
        if last_sequence_number is not None:
            body["lastSequenceNumber"] = last_sequence_number
        return (await self._request("POST", "/subscriptions/sync", json_body=body))[
            "result"
        ]

    async def async_delete_subscription(
        self, client_id: str, subscription_id: str
    ) -> None:
        await self._request(
            "POST",
            "/subscriptions/delete",
            json_body={"clientId": client_id, "subscriptionIds": [subscription_id]},
        )

    async def stream(
        self, client_id: str, subscription_id: str
    ) -> AsyncIterator[list[dict]]:
        """Open the SSE stream and yield each event's update array."""
        url = f"{self.base_url}/subscriptions/stream"
        body = {"clientId": client_id, "subscriptionId": subscription_id}
        try:
            async with self._session.post(
                url,
                json=body,
                headers=self._headers,
                timeout=STREAM_TIMEOUT,
                ssl=self._ssl,
            ) as resp:
                if resp.status >= 400:
                    raise I3xApiError(resp.status, "stream request refused")
                ctype = resp.headers.get("Content-Type", "")
                if "text/event-stream" not in ctype:
                    raise I3xApiError(resp.status, f"not an SSE stream: {ctype}")
                data_lines: list[bytes] = []
                async for raw in resp.content:
                    line = raw.rstrip(b"\r\n")
                    if not line:
                        if data_lines:
                            try:
                                yield json.loads(b"\n".join(data_lines))
                            except ValueError:
                                _LOGGER.debug("Discarding malformed SSE frame")
                            data_lines = []
                        continue
                    if line.startswith(b"data:"):
                        data_lines.append(line[5:].lstrip())
                    # Comment lines (heartbeats) and other fields are ignored.
        except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError) as err:
            raise I3xConnectionError(f"Stream to {url} failed: {err}") from err
