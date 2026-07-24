"""Constants for the i3X integration."""

from __future__ import annotations

DOMAIN = "i3x"

# CESMII i3X spec version this server implements.
SPEC_VERSION = "1.0"

# URL base for the server half, on Home Assistant's own HTTP server.
API_BASE = "/api/i3x/v1"

# Namespace all generated Object Types and Relationship Types live in.
NAMESPACE_URI = "https://github.com/rdgerken/ha-i3x/ns/home-assistant"
NAMESPACE_NAME = "Home Assistant"

# --- Config entry modes ---
CONF_MODE = "mode"
MODE_SERVER = "server"
MODE_CLIENT = "client"

# --- Server options ---
CONF_SERVER_NAME = "server_name"
CONF_LOCAL_ONLY = "local_only"
CONF_INCLUDE_DOMAINS = "include_domains"
CONF_INCLUDE_ENTITY_GLOBS = "include_entity_globs"
CONF_EXCLUDE_ENTITY_GLOBS = "exclude_entity_globs"
CONF_SUBSCRIPTION_TTL = "subscription_ttl"
CONF_WRITE_ENABLED = "write_enabled"
CONF_WRITE_ENTITY_GLOBS = "write_entity_globs"

# The anonymous-visible name on GET /info deliberately defaults to something
# generic; hass.config.location_name is only used on authenticated surfaces.
DEFAULT_SERVER_NAME = "Home Assistant i3X"
DEFAULT_LOCAL_ONLY = True
DEFAULT_SUBSCRIPTION_TTL = 600  # seconds without sync/stream before expiry

# --- Server limits (resource-exhaustion guards) ---
MAX_BULK_IDS = 500  # per-request cap on elementIds/updates lists
MAX_SUBSCRIPTIONS_PER_CLIENT = 20
MAX_SUBSCRIPTIONS_TOTAL = 100
MAX_MONITORED_PER_SUBSCRIPTION = 500
MAX_QUEUED_BATCHES = 1000  # per-subscription; oldest dropped beyond -> 206
MAX_HISTORY_ROWS = 100_000  # total rows per history request -> 206 if truncated
SUBSCRIPTION_JANITOR_INTERVAL = 60  # seconds

# SSE streaming
MAX_SSE_STREAMS = 10  # concurrent streams across all subscriptions -> 503 beyond
SSE_HEARTBEAT_SECONDS = 15  # keep-alive comment cadence (survives proxy idle timeouts)

# /info rate limit for non-local clients (only relevant when local_only is off).
INFO_RATE_PER_MINUTE = 30
INFO_RATE_BURST = 60
INFO_RATE_MAX_IPS = 1024

# --- i3X qualities ---
QUALITY_GOOD = "Good"
QUALITY_GOOD_NO_DATA = "GoodNoData"
QUALITY_BAD = "Bad"
QUALITY_UNCERTAIN = "Uncertain"

# --- Well-known elementIds ---
ROOT_ELEMENT_ID = "home"
AREA_PREFIX = "area:"
DEVICE_PREFIX = "device:"
FLOOR_PREFIX = "floor:"
TYPE_PREFIX = "type:"

REL_HAS_PARENT = "HasParent"
REL_HAS_CHILDREN = "HasChildren"

# HA states mapped to booleans for boolean-kind entities.
BINARY_STATE_MAP = {
    "on": True,
    "off": False,
    "true": True,
    "false": False,
    "home": True,
    "not_home": False,
    "open": True,
    "closed": False,
    "locked": True,
    "unlocked": False,
    "detected": True,
    "clear": False,
}
