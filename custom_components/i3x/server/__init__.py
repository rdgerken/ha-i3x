"""i3X server engine: owns the model, subscriptions, and /info payload."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entityfilter import generate_filter

from ..const import (
    CONF_LOCAL_ONLY,
    CONF_SERVER_NAME,
    CONF_SUBSCRIPTION_TTL,
    CONF_WRITE_ENABLED,
    CONF_WRITE_ENTITY_GLOBS,
    DEFAULT_LOCAL_ONLY,
    DEFAULT_SERVER_NAME,
    DEFAULT_SUBSCRIPTION_TTL,
    SPEC_VERSION,
)
from .http_util import InfoRateLimiter
from .model import I3xModel
from .subscriptions import SubscriptionManager
from .service_data import ServiceDataCache


class I3xServer:
    """Runtime engine behind the i3X HTTP views."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.model = I3xModel(hass, dict(entry.options))
        self.subscriptions = SubscriptionManager(hass, self)
        self.info_limiter = InfoRateLimiter()
        self.service_cache = ServiceDataCache(hass)
        self.server_version: str | None = None

    # ------------------------------------------------------------ options
    @property
    def local_only(self) -> bool:
        return self.entry.options.get(CONF_LOCAL_ONLY, DEFAULT_LOCAL_ONLY)

    @property
    def server_name(self) -> str:
        return self.entry.options.get(CONF_SERVER_NAME) or DEFAULT_SERVER_NAME

    @property
    def subscription_ttl(self) -> int:
        return self.entry.options.get(
            CONF_SUBSCRIPTION_TTL, DEFAULT_SUBSCRIPTION_TTL
        )

    @property
    def write_enabled(self) -> bool:
        return self.entry.options.get(CONF_WRITE_ENABLED, False)

    @property
    def write_filter(self):
        """Which entities accept value-changing writes (when writes are on).

        An empty allowlist deliberately means ALL exposed entities are
        writable: the master toggle is the gate, and the globs exist to
        narrow it. Locks are refused unconditionally in writes.py.
        """
        globs = self.entry.options.get(CONF_WRITE_ENTITY_GLOBS, [])
        if not globs:
            return lambda entity_id: True
        return generate_filter(
            include_domains=[],
            include_entities=[],
            exclude_domains=[],
            exclude_entities=[],
            include_entity_globs=globs,
            exclude_entity_globs=[],
        )

    # ---------------------------------------------------------------- info
    def capabilities(self) -> dict:
        """Capability matrix.

        update.* are always declared: idempotent echo writes (which change
        nothing) are always accepted, so the endpoints genuinely work; value-
        changing writes are additionally gated per-entity by the write
        allowlist and report per-item failures, which the bulk contract
        allows.
        """
        return {
            "query": {"history": True},
            "update": {"current": True, "history": True},
            "subscribe": {"stream": True},
        }

    def info_payload(self) -> dict:
        return {
            "specVersion": SPEC_VERSION,
            "serverVersion": self.server_version,
            "serverName": self.server_name,
            "capabilities": self.capabilities(),
        }

    # ------------------------------------------------------------ lifecycle
    async def async_start(self) -> None:
        from homeassistant.loader import async_get_integration

        integration = await async_get_integration(self.hass, self.entry.domain)
        self.server_version = str(integration.version) if integration.version else None
        self.model.async_start()
        self.subscriptions.async_start()

    async def async_stop(self) -> None:
        self.subscriptions.async_stop()
        self.model.async_stop()
