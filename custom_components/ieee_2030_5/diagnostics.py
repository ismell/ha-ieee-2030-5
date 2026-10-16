"""Diagnostics for the IEEE 2030.5 Smart Meter integration."""

from typing import Any

from aio2030dot5 import REDACT_KEYS
from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant

from .const import CONF_IDENTITY, CONF_PENDING_IDENTITY
from .coordinator import Ieee20305ConfigEntry

TO_REDACT = REDACT_KEYS | {CONF_HOST, CONF_IDENTITY, CONF_PENDING_IDENTITY}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: Ieee20305ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    identity = coordinator.identity
    snapshot = coordinator.data.snapshot if coordinator.data else None
    return async_redact_data(
        {
            "entry": dict(entry.data),
            "client_certificate": {
                "lfdi": identity.lfdi,
                "not_after": identity.not_after.isoformat(),
                "problems": list(identity.problems),
            },
            "profile": coordinator.profile.to_dict(),
            "present": sorted(coordinator.present),
            "tou_tiers": coordinator.tou_tiers,
            "snapshot": snapshot.to_dict() if snapshot else None,
            "transport": coordinator.meter.stats.to_dict(),
            "guard": {
                "rejected": coordinator.guard.rejected,
                "pending": sorted(coordinator.guard.pending),
            },
        },
        TO_REDACT,
    )
