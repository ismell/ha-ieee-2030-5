"""Renewing the client certificate while the current one still works.

A utility takes days to register a certificate, and the meter refuses it
until then. A renewed certificate therefore waits in the entry as *pending*
while the current one keeps the readings coming, and takes over as soon as
the meter accepts it: checked on every slow poll, on request from its repair
issue, and whenever the meter refuses the current certificate.

Asking the meter can wait for its only connection slot, so whatever the
entry held before that may have changed by the answer (another check, a
discard, a newer renewal, removal): only the answer's own certificate is
ever switched to, and only while it is still the pending one.
"""

import asyncio
from contextlib import suppress
from datetime import timedelta
from typing import Any

from aio2030dot5 import (
    Identity,
    InvalidIdentity,
    Meter,
    MeterError,
    MeterIdentityMismatch,
    PeerInfo,
    format_lfdi,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .const import (
    CONF_CERTIFICATE,
    CONF_DCAP_PATH,
    CONF_IDENTITY,
    CONF_METER_LFDI,
    CONF_PENDING_IDENTITY,
    CONF_PRIVATE_KEY,
    CONF_REGISTERED,
    DOMAIN,
    FLOW_STEP_TIMEOUT,
    FLOW_TIMEOUTS,
    LOGGER,
    UTILITY_PLACEHOLDERS,
    certificate_expiring_issue_id,
    renewal_pending_issue_id,
)

# Registration takes days: start well before the certificate expires.
RENEWAL_REMINDER = timedelta(days=60)

_MULTILINE = TextSelector(TextSelectorConfig(multiline=True))
IMPORT_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CERTIFICATE): _MULTILINE,
        vol.Required(CONF_PRIVATE_KEY): _MULTILINE,
    }
)


async def async_probe(
    host: str, port: int, dcap_path: str, identity: Identity
) -> PeerInfo:
    """One TLS handshake, no request: who is there, and do they take us?

    Waits for the meter's single connection slot (a poll may hold it),
    within a bound; raises MeterError subclasses.
    """
    meter = await Meter.create(
        host, identity, port, timeouts=FLOW_TIMEOUTS, dcap_path=dcap_path
    )
    try:
        async with asyncio.timeout(FLOW_STEP_TIMEOUT):
            return await meter.probe_tls()
    except TimeoutError as err:
        raise MeterError(f"{host}:{port} did not answer in time") from err


def pending_identity(entry: ConfigEntry) -> Identity | None:
    """The renewed certificate waiting for registration, if usable.

    A broken or expired one is no renewal: the reminder for the current
    certificate stays, and Reconfigure can discard it.
    """
    if (data := entry.data.get(CONF_PENDING_IDENTITY)) is None:
        return None
    try:
        identity = Identity.from_dict(data)
    except InvalidIdentity as err:
        LOGGER.debug("The pending client certificate cannot be used: %s", err)
        return None
    return None if identity.expired() else identity


async def async_meter_accepts(entry: ConfigEntry, identity: Identity) -> bool:
    """Whether the configured meter accepts a certificate now."""
    host: str = entry.data[CONF_HOST]
    peer = await async_probe(
        host, entry.data[CONF_PORT], entry.data[CONF_DCAP_PATH], identity
    )
    if peer.lfdi != entry.data[CONF_METER_LFDI]:
        raise MeterIdentityMismatch(host, entry.data[CONF_METER_LFDI], peer.lfdi)
    return peer.client_accepted


async def async_stage(
    hass: HomeAssistant, entry: ConfigEntry, identity: Identity
) -> bool:
    """Keep a renewed certificate as pending; True if it took over at once.

    It is stored before the meter is asked, so the private key survives
    whatever happens next. The caller reloads the entry if it took over.
    """
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_PENDING_IDENTITY: identity.to_dict()}
    )
    try:
        return await async_try_pending(hass, entry)
    finally:
        if hass.config_entries.async_get_entry(entry.entry_id) is not None:
            async_update_issues(hass, entry)


async def async_try_pending(
    hass: HomeAssistant, entry: ConfigEntry, *, raise_errors: bool = False
) -> bool:
    """Switch to the pending certificate if the meter accepts it now.

    True if it did, or another check already did; the caller reloads the
    entry. With ``raise_errors``, a meter that cannot answer raises its
    MeterError instead of counting as a refusal.
    """
    if (identity := pending_identity(entry)) is None:
        return False
    try:
        accepted = await async_meter_accepts(entry, identity)
    except MeterError as err:
        if raise_errors:
            raise
        LOGGER.debug("Cannot check the pending client certificate: %s", err)
        return False
    if hass.config_entries.async_get_entry(entry.entry_id) is None:
        return False  # removed meanwhile
    proposed = identity.to_dict()
    if entry.data[CONF_IDENTITY] == proposed:
        return True
    if not accepted or entry.data.get(CONF_PENDING_IDENTITY) != proposed:
        return False  # refused, or discarded or replaced meanwhile
    LOGGER.info(
        "The meter accepts the renewed client certificate %s; switching to it",
        format_lfdi(identity.lfdi),
    )
    data = {**entry.data, CONF_IDENTITY: proposed, CONF_REGISTERED: True}
    data.pop(CONF_PENDING_IDENTITY)
    hass.config_entries.async_update_entry(entry, data=data)
    async_update_issues(hass, entry)
    return True


@callback
def async_discard(
    hass: HomeAssistant, entry: ConfigEntry, confirmed: dict[str, Any] | None
) -> bool:
    """Forget the pending certificate the user confirmed to discard.

    False, and nothing changes, if another one was staged meanwhile.
    """
    if confirmed is None or entry.data.get(CONF_PENDING_IDENTITY) != confirmed:
        return False
    data = dict(entry.data)
    del data[CONF_PENDING_IDENTITY]
    hass.config_entries.async_update_entry(entry, data=data)
    async_update_issues(hass, entry)
    return True


def placeholders(entry: ConfigEntry) -> dict[str, str]:
    """What renewal messages show: the meter, the expiry, the pending LFDI."""
    try:
        expiry = Identity.from_dict(entry.data[CONF_IDENTITY]).not_after
    except InvalidIdentity:
        expiry = None
    lfdi = ""
    if (stored := entry.data.get(CONF_PENDING_IDENTITY)) is not None:
        # Also an expired one: discarding it shows what it was.
        with suppress(InvalidIdentity):
            lfdi = format_lfdi(Identity.from_dict(stored).lfdi)
    return {
        "sfdi": entry.unique_id or "",
        "expiry": expiry.date().isoformat() if expiry else "",
        "lfdi": lfdi,
        **UTILITY_PLACEHOLDERS,
    }


@callback
def async_update_issues(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Show what a renewal needs from the user, if anything.

    A pending certificate shows its LFDI until the meter accepts it; without
    one, a certificate that expires within RENEWAL_REMINDER asks for renewal.
    An expired one is left to reauthentication.
    """
    expiring_id = certificate_expiring_issue_id(entry.entry_id)
    pending_id = renewal_pending_issue_id(entry.entry_id)
    if pending_identity(entry) is not None:
        ir.async_delete_issue(hass, DOMAIN, expiring_id)
        ir.async_create_issue(
            hass,
            DOMAIN,
            pending_id,
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="renewal_pending",
            translation_placeholders=placeholders(entry),
            data={"entry_id": entry.entry_id},
        )
        return
    ir.async_delete_issue(hass, DOMAIN, pending_id)
    try:
        current = Identity.from_dict(entry.data[CONF_IDENTITY])
    except InvalidIdentity:
        current = None
    if (
        current is None
        or not entry.data[CONF_REGISTERED]
        or current.expired()
        or current.not_after - dt_util.utcnow() > RENEWAL_REMINDER
    ):
        ir.async_delete_issue(hass, DOMAIN, expiring_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        expiring_id,
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="certificate_expiring",
        translation_placeholders=placeholders(entry),
        data={"entry_id": entry.entry_id},
    )
