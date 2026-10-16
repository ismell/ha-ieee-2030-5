"""Repairs for the IEEE 2030.5 Smart Meter integration."""

from typing import Any

from aio2030dot5 import (
    Identity,
    InvalidIdentity,
    MeterError,
    MeterIdentityMismatch,
    generate_identity,
    load_identity,
)
from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from . import renewal
from .const import (
    CONF_CERTIFICATE,
    CONF_IDENTITY,
    CONF_PRIVATE_KEY,
    UTILITY_PLACEHOLDERS,
)


class AwaitingRegistrationFlow(RepairsFlow):
    """Check again whether the utility has registered our certificate."""

    def __init__(self, entry_id: str, placeholders: dict[str, str]) -> None:
        """Initialize the flow."""
        self._entry_id = entry_id
        self._placeholders = placeholders

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Start the flow."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Set the entry up again; done only if the meter now accepts us."""
        if user_input is not None:
            entry = self.hass.config_entries.async_get_entry(self._entry_id)
            if entry is None:
                return self.async_create_entry(data={})
            await self.hass.config_entries.async_reload(self._entry_id)
            if entry.state is not ConfigEntryState.LOADED:
                # Aborting keeps the issue (and its instructions) open.
                return self.async_abort(
                    reason="still_unregistered",
                    description_placeholders=self._placeholders,
                )
            return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="confirm", description_placeholders=self._placeholders
        )


class RenewCertificateFlow(RepairsFlow):
    """Renew a certificate that expires soon; it stays in use meanwhile."""

    def __init__(self, entry_id: str) -> None:
        """Initialize the flow."""
        self._entry_id = entry_id

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Choose the certificate to renew with."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            return self.async_create_entry(data={})
        return self.async_show_menu(
            step_id="init",
            menu_options=["generate_identity", "import_identity"],
            description_placeholders=renewal.placeholders(entry),
        )

    async def async_step_generate_identity(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Generate a new client certificate."""
        identity = await self.hass.async_add_executor_job(generate_identity)
        return await self._async_renew(identity)

    async def async_step_import_identity(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Import a client certificate already registered with the utility."""
        errors: dict[str, str] = {}
        placeholders = {"problems": ""}
        if user_input is not None:
            try:
                identity = load_identity(
                    user_input[CONF_CERTIFICATE], user_input[CONF_PRIVATE_KEY]
                )
            except InvalidIdentity as err:
                errors["base"] = "invalid_identity"
                placeholders["problems"] = "; ".join(err.problems) or str(err)
            else:
                return await self._async_renew(identity)
        return self.async_show_form(
            step_id="import_identity",
            data_schema=renewal.IMPORT_SCHEMA,
            errors=errors,
            description_placeholders=placeholders,
        )

    async def _async_renew(self, identity: Identity) -> RepairsFlowResult:
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            return self.async_create_entry(data={})
        if renewal.pending_identity(entry) is not None:
            # Staged meanwhile (Reconfigure): replacing it would drop its key.
            return self.async_abort(reason="renewal_already_pending")
        try:
            current = Identity.from_dict(entry.data[CONF_IDENTITY])
        except InvalidIdentity:
            return self.async_abort(reason="invalid_identity")
        if identity.lfdi == current.lfdi:
            return self.async_abort(reason="same_identity")
        if await renewal.async_stage(self.hass, entry, identity):
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
            return self.async_create_entry(data={})
        # This issue is gone now; the pending one shows the LFDI from here on.
        return self.async_abort(
            reason="renewal_pending",
            description_placeholders=renewal.placeholders(entry),
        )


class RenewalPendingFlow(RepairsFlow):
    """Check whether the utility has registered the renewed certificate."""

    def __init__(self, entry_id: str) -> None:
        """Initialize the flow."""
        self._entry_id = entry_id

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Start the flow."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Switch to the renewed certificate if the meter accepts it now."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None or renewal.pending_identity(entry) is None:
            return self.async_create_entry(data={})
        placeholders = renewal.placeholders(entry)
        if user_input is not None:
            try:
                switched = await renewal.async_try_pending(
                    self.hass, entry, raise_errors=True
                )
            except MeterIdentityMismatch:
                return self.async_abort(reason="wrong_device")
            except MeterError:
                return self.async_abort(reason="cannot_connect")
            if not switched:
                # Aborting keeps the issue (and its instructions) open.
                return self.async_abort(
                    reason="still_unregistered",
                    description_placeholders=placeholders,
                )
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
            return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="confirm", description_placeholders=placeholders
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create the fix flow for an issue."""
    data = data or {}
    entry_id = str(data.get("entry_id", ""))
    placeholders = {
        "lfdi": str(data.get("lfdi", "")),
        "sfdi": str(data.get("sfdi", "")),
        **UTILITY_PLACEHOLDERS,
    }
    if issue_id.startswith("certificate_expiring_"):
        return RenewCertificateFlow(entry_id)
    if issue_id.startswith("renewal_pending_"):
        return RenewalPendingFlow(entry_id)
    return AwaitingRegistrationFlow(entry_id, placeholders)
