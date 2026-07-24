"""i3X server engine: owns the model, subscriptions, and /info payload."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from ..const import (
    CONF_LOCAL_ONLY,
    CONF_SERVER_NAME,
    CONF_SUBSCRIPTION_TTL,
    DEFAULT_LOCAL_ONLY,
    DEFAULT_SERVER_NAME,
    DEFAULT_SUBSCRIPTION_TTL,
    SPEC_VERSION,
)
from .http_util import InfoRateLimiter
from .model import I3xModel
from .subscriptions import SubscriptionManager


class I3xServer:
    """Runtime engine behind the i3X HTTP views."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.model = I3xModel(hass, dict(entry.options))
        self.subscriptions = SubscriptionManager(hass, self)
        self.info_limiter = InfoRateLimiter()
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

    # ---------------------------------------------------------------- info
    def capabilities(self) -> dict:
        """Honest capability matrix — computed from what actually works."""
        return {
            "query": {"history": True},
            "update": {"current": False, "history": False},
            "subscribe": {"stream": False},
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
