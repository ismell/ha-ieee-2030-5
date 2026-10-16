"""The IEEE 2030.5 Smart Meter integration."""

from aio2030dot5 import Identity, InvalidIdentity, Meter
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError
from homeassistant.helpers import issue_registry as ir

from . import renewal
from .const import (
    CONF_DCAP_PATH,
    CONF_IDENTITY,
    CONF_METER_LFDI,
    DOMAIN,
    POLL_TIMEOUTS,
    awaiting_registration_issue_id,
    certificate_expiring_issue_id,
    renewal_pending_issue_id,
)
from .coordinator import Ieee20305ConfigEntry, MeterCoordinator, store_for

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: Ieee20305ConfigEntry) -> bool:
    """Set up an IEEE 2030.5 meter from a config entry."""
    try:
        identity = Identity.from_dict(entry.data[CONF_IDENTITY])
    except InvalidIdentity as err:
        raise ConfigEntryError(
            translation_domain=DOMAIN, translation_key="invalid_identity"
        ) from err
    # Before anything can fail: no reminder outlives what it describes.
    renewal.async_update_issues(hass, entry)
    if identity.expired() and await renewal.async_try_pending(hass, entry):
        identity = Identity.from_dict(entry.data[CONF_IDENTITY])
    if identity.expired():
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN,
            translation_key="certificate_expired",
            translation_placeholders={"expiry": identity.not_after.date().isoformat()},
        )
    meter = await Meter.create(
        entry.data[CONF_HOST],
        identity,
        entry.data[CONF_PORT],
        timeouts=POLL_TIMEOUTS,
        expected_lfdi=entry.data[CONF_METER_LFDI],
        dcap_path=entry.data[CONF_DCAP_PATH],
    )
    coordinator = MeterCoordinator(hass, entry, meter, identity)
    await coordinator.async_prepare()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.async_import_statistics()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: Ieee20305ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: Ieee20305ConfigEntry) -> None:
    """Remove what the entry kept: its repair issues and its stored profile."""
    for issue_id in (
        awaiting_registration_issue_id,
        certificate_expiring_issue_id,
        renewal_pending_issue_id,
    ):
        ir.async_delete_issue(hass, DOMAIN, issue_id(entry.entry_id))
    await store_for(hass, entry).async_remove()
