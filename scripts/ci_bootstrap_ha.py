#!/usr/bin/env python3
"""Bootstrap a fresh Home Assistant instance for conformance testing.

Stdlib only. Performs non-interactive onboarding via the REST API, creates the
i3X server config entry through the config-entries flow API, and prints a
bearer access token (valid ~30 minutes — plenty for a conformance run).

Usage: ci_bootstrap_ha.py [base_url]   (default http://127.0.0.1:8123)
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8123"
CLIENT_ID = f"{BASE}/"


def request(method: str, path: str, payload=None, token=None, form=False):
    url = f"{BASE}{path}"
    headers = {}
    data = None
    if payload is not None:
        if form:
            data = urllib.parse.urlencode(payload).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read()
        return json.loads(body) if body else {}


def wait_for_ha(timeout=300):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            request("GET", "/api/onboarding")
            return
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(2)
    raise SystemExit("Home Assistant did not come up in time")


def main() -> None:
    wait_for_ha()
    print("HA is up; onboarding…", file=sys.stderr)

    user = request(
        "POST",
        "/api/onboarding/users",
        {
            "client_id": CLIENT_ID,
            "name": "CI",
            "username": "ci",
            "password": "ci-password-1",
            "language": "en",
        },
    )
    token_resp = request(
        "POST",
        "/auth/token",
        {
            "grant_type": "authorization_code",
            "code": user["auth_code"],
            "client_id": CLIENT_ID,
        },
        form=True,
    )
    token = token_resp["access_token"]

    # Remaining onboarding steps vary across HA versions — attempt each,
    # tolerate ones that no longer exist.
    for path, payload in (
        ("/api/onboarding/core_config", {}),
        ("/api/onboarding/analytics", {}),
        (
            "/api/onboarding/integration",
            {"client_id": CLIENT_ID, "redirect_uri": f"{CLIENT_ID}?auth_callback=1"},
        ),
    ):
        try:
            request("POST", path, payload, token=token)
        except urllib.error.HTTPError as err:
            print(f"onboarding step {path}: HTTP {err.code} (skipped)", file=sys.stderr)
    print("Onboarding complete; creating i3X server entry…", file=sys.stderr)

    # The config component registers its HTTP routes asynchronously after
    # onboarding — retry until the flow API is up.
    flow = None
    for attempt in range(30):
        try:
            flow = request(
                "POST",
                "/api/config/config_entries/flow",
                {"handler": "i3x", "show_advanced_options": False},
                token=token,
            )
            break
        except urllib.error.HTTPError as err:
            if err.code not in (404, 503):
                raise
            print(
                f"config flow API not ready (HTTP {err.code}), retrying…",
                file=sys.stderr,
            )
            time.sleep(2)
    if flow is None:
        raise SystemExit("config_entries flow API never became available")
    result = request(
        "POST",
        f"/api/config/config_entries/flow/{flow['flow_id']}",
        {
            "server_name": "CI i3X Server",
            "local_only": True,
            "include_domains": [],
            "include_entity_globs": [],
            "exclude_entity_globs": [],
            "subscription_ttl": 600,
        },
        token=token,
    )
    if result.get("type") != "create_entry":
        raise SystemExit(f"Config entry creation failed: {result}")
    print("i3X server entry created", file=sys.stderr)
    print(token)


if __name__ == "__main__":
    main()
