"""Config flow for the IEEE 2030.5 Smart Meter integration.

A meter only talks to clients whose certificate the utility has registered
(for Xcel Energy: under Meters and devices in My Account). The flow therefore:

1. identifies the meter -- its certificate proves who it is, even while it
   refuses ours (``probe_tls``);
2. generates a client certificate, or imports one the user already registered;
3. if the meter refuses it, creates the entry at once and shows the LFDI to
   register: the private key must be kept while the utility processes the
   registration, which can take days. Setup then waits (a repair issue) until
   the meter accepts the certificate.

Reconfiguring renews the certificate the same way, but without the wait: the
new one is kept as pending while the current one keeps working (renewal.py).
"""

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, override

from aio2030dot5 import (
    Identity,
    InvalidIdentity,
    MeterAuthError,
    MeterError,
    MeterProtocolError,
    PeerInfo,
    format_lfdi,
    generate_identity,
    load_identity,
    parse_advertisement,
)
from homeassistant.config_entries import (
    SOURCE_IGNORE,
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
import voluptuous as vol

from . import renewal
from .const import (
    CONF_CERTIFICATE,
    CONF_DCAP_PATH,
    CONF_IDENTITY,
    CONF_METER_LFDI,
    CONF_METER_SFDI,
    CONF_PENDING_IDENTITY,
    CONF_PRIVATE_KEY,
    CONF_REGISTERED,
    DEFAULT_DCAP_PATH,
    DEFAULT_PORT,
    DOMAIN,
    LOGGER,
    UTILITY_PLACEHOLDERS,
)


def _host_schema(host: str | None = None, port: int = DEFAULT_PORT) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=host or vol.UNDEFINED): str,
            vol.Optional(CONF_PORT, default=port): int,
        }
    )


class Ieee20305ConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for an IEEE 2030.5 meter."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._host = ""
        self._port = DEFAULT_PORT
        self._dcap_path = DEFAULT_DCAP_PATH
        self._identity: Identity | None = None
        self._meter_lfdi: str | None = None  # known once the meter showed it
        self._discarding: dict[str, Any] | None = None

    async def _probe(self, identity: Identity, host: str, port: int) -> PeerInfo:
        return await renewal.async_probe(host, port, self._dcap_path, identity)

    def _existing(self, unique_id: str) -> ConfigEntry | None:
        entry = self.hass.config_entries.async_entry_for_domain_unique_id(
            DOMAIN, unique_id
        )
        return None if entry is None or entry.source == SOURCE_IGNORE else entry

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Identify the meter at a host."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._host, self._port = user_input[CONF_HOST], user_input[CONF_PORT]
            # Any certificate will do to read the meter's identity: it shows
            # its own before deciding about ours.
            throwaway = await self.hass.async_add_executor_job(generate_identity)
            try:
                peer = await self._probe(throwaway, self._host, self._port)
            except MeterAuthError:
                errors["base"] = "cannot_identify"
            except MeterError as err:
                LOGGER.debug("Cannot reach %s:%s: %s", self._host, self._port, err)
                errors["base"] = "cannot_connect"
            else:
                # A discovered flow for this meter may be open: this one wins.
                await self.async_set_unique_id(peer.sfdi, raise_on_progress=False)
                entry = self._existing(peer.sfdi)
                if entry is not None:
                    if peer.lfdi != entry.data[CONF_METER_LFDI]:
                        # Same SFDI, another certificate: not the configured meter.
                        return self.async_abort(reason="wrong_device")
                    self._abort_if_unique_id_configured(
                        updates={CONF_HOST: self._host, CONF_PORT: self._port}
                    )
                self._meter_lfdi = peer.lfdi
                return await self.async_step_identity()
        return self.async_show_form(
            step_id="user", data_schema=_host_schema(), errors=errors
        )

    @override
    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle a meter found by mDNS."""
        try:
            adv = parse_advertisement(
                discovery_info.name,
                discovery_info.host,
                discovery_info.port or DEFAULT_PORT,
                discovery_info.properties,
            )
        except MeterProtocolError:
            return self.async_abort(reason="not_ieee_2030_5")
        if adv.sfdi is None:
            return self.async_abort(reason="not_ieee_2030_5")
        await self.async_set_unique_id(adv.sfdi)
        entry = self._existing(adv.sfdi)
        if (
            entry is not None
            and (entry.data[CONF_HOST], entry.data[CONF_PORT]) != (adv.host, adv.port)
            # Anything can claim a meter's SFDI: follow the meter to a new
            # address only once the device there proves to be it.
            and await self._is_meter_of(entry, adv.host, adv.port)
        ):
            self._abort_if_unique_id_configured(
                updates={
                    CONF_HOST: adv.host,
                    CONF_PORT: adv.port,
                    CONF_DCAP_PATH: adv.dcap_path,
                }
            )
        self._abort_if_unique_id_configured()
        self._host, self._port, self._dcap_path = adv.host, adv.port, adv.dcap_path
        self.context["title_placeholders"] = {"sfdi": adv.sfdi}
        self._set_confirm_only()
        return await self.async_step_discovery_confirm()

    async def _is_meter_of(self, entry: ConfigEntry, host: str, port: int) -> bool:
        try:
            identity = Identity.from_dict(entry.data[CONF_IDENTITY])
            peer = await self._probe(identity, host, port)
        except MeterError as err:
            LOGGER.debug("Cannot verify the meter at %s:%s: %s", host, port, err)
            return False
        pinned: str = entry.data[CONF_METER_LFDI]
        return peer.lfdi == pinned

    async def async_step_discovery_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm setting up a discovered meter."""
        if user_input is not None:
            return await self.async_step_identity()
        return self.async_show_form(
            step_id="discovery_confirm",
            description_placeholders={"sfdi": self.unique_id or "", "host": self._host},
        )

    async def async_step_identity(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose how to get a client certificate."""
        return self.async_show_menu(
            step_id="identity",
            menu_options=["generate_identity", "import_identity"],
            description_placeholders=UTILITY_PLACEHOLDERS,
        )

    async def async_step_generate_identity(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Generate a new client certificate."""
        identity = await self.hass.async_add_executor_job(generate_identity)
        if self.source == SOURCE_RECONFIGURE:
            return await self._async_renew(identity)
        self._identity = identity
        return await self.async_step_connect()

    async def async_step_import_identity(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
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
                if self.source == SOURCE_RECONFIGURE:
                    return await self._async_renew(identity)
                self._identity = identity
                return await self.async_step_connect()
        return self.async_show_form(
            step_id="import_identity",
            data_schema=renewal.IMPORT_SCHEMA,
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_connect(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Present the certificate to the meter (again, on retry)."""
        if TYPE_CHECKING:
            assert self._identity is not None  # every way here sets it
        identity = self._identity
        errors: dict[str, str] = {}
        try:
            peer = await self._probe(identity, self._host, self._port)
        except MeterAuthError:
            errors["base"] = "cannot_identify"
        except MeterError as err:
            LOGGER.debug("Cannot reach %s:%s: %s", self._host, self._port, err)
            errors["base"] = "cannot_connect"
        else:
            expected = self._meter_lfdi
            if self.source == SOURCE_REAUTH:
                expected = self._get_reauth_entry().data[CONF_METER_LFDI]
            if peer.sfdi != self.unique_id or (expected and peer.lfdi != expected):
                return self.async_abort(reason="wrong_device")
            self._meter_lfdi = peer.lfdi
            return self._async_finish(identity, registered=peer.client_accepted)
        return self.async_show_form(step_id="connect", errors=errors)

    def _async_finish(
        self, identity: Identity, *, registered: bool
    ) -> ConfigFlowResult:
        placeholders = {
            "lfdi": format_lfdi(identity.lfdi),
            "sfdi": self.unique_id or "",
            **UTILITY_PLACEHOLDERS,
        }
        if self.source == SOURCE_REAUTH:
            entry = self._get_reauth_entry()
            if registered:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_IDENTITY: identity.to_dict(),
                        CONF_REGISTERED: True,
                    },
                )
            self.hass.config_entries.async_update_entry(
                entry,
                data={
                    **entry.data,
                    CONF_IDENTITY: identity.to_dict(),
                    CONF_REGISTERED: False,
                },
            )
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
            return self.async_abort(
                reason="register_certificate", description_placeholders=placeholders
            )
        # Created at once even when the certificate still has to be
        # registered: the private key must not be lost meanwhile.
        return self.async_create_entry(
            title=f"Electric meter {self.unique_id}",
            data={
                CONF_HOST: self._host,
                CONF_PORT: self._port,
                CONF_DCAP_PATH: self._dcap_path,
                CONF_METER_SFDI: self.unique_id,
                CONF_METER_LFDI: self._meter_lfdi,
                CONF_IDENTITY: identity.to_dict(),
                CONF_REGISTERED: registered,
            },
            description=None if registered else "register_certificate",
            description_placeholders=None if registered else placeholders,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """The meter refused a certificate it used to accept."""
        self._host = entry_data[CONF_HOST]
        self._port = entry_data[CONF_PORT]
        self._dcap_path = entry_data[CONF_DCAP_PATH]
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Retry the current certificate, or replace it."""
        return self.async_show_menu(
            step_id="reauth_confirm",
            menu_options=["reauth_retry", "generate_identity", "import_identity"],
            description_placeholders=UTILITY_PLACEHOLDERS,
        )

    async def async_step_reauth_retry(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Try the current certificate again (e.g. after re-registering it)."""
        try:
            identity = Identity.from_dict(self._get_reauth_entry().data[CONF_IDENTITY])
        except InvalidIdentity:
            return self.async_abort(reason="invalid_identity")
        if identity.expired():
            return self.async_abort(reason="certificate_expired")
        self._identity = identity
        return await self.async_step_connect()

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the meter's address, or renew the client certificate."""
        entry = self._get_reconfigure_entry()
        options = ["address"]
        # One renewal at a time: replacing a pending one would drop its key,
        # which only an explicit, confirmed discard may do.
        if renewal.pending_identity(entry) is None:
            options.append("renew_identity")
        if CONF_PENDING_IDENTITY in entry.data:
            options.append("discard_renewal")
        return self.async_show_menu(step_id="reconfigure", menu_options=options)

    async def async_step_address(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the meter's address."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            host, port = user_input[CONF_HOST], user_input[CONF_PORT]
            self._dcap_path = entry.data[CONF_DCAP_PATH]
            try:
                identity = Identity.from_dict(entry.data[CONF_IDENTITY])
            except InvalidIdentity:
                return self.async_abort(reason="invalid_identity")
            try:
                peer = await self._probe(identity, host, port)
            except MeterAuthError:
                errors["base"] = "cannot_identify"
            except MeterError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(peer.sfdi)
                self._abort_if_unique_id_mismatch(reason="wrong_device")
                if peer.lfdi != entry.data[CONF_METER_LFDI]:
                    return self.async_abort(reason="wrong_device")
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_HOST: host, CONF_PORT: port}
                )
        return self.async_show_form(
            step_id="address",
            data_schema=_host_schema(entry.data[CONF_HOST], entry.data[CONF_PORT]),
            errors=errors,
        )

    async def async_step_renew_identity(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the certificate to renew with."""
        entry = self._get_reconfigure_entry()
        try:
            expiry = Identity.from_dict(entry.data[CONF_IDENTITY]).not_after
        except InvalidIdentity:
            return self.async_abort(reason="invalid_identity")
        return self.async_show_menu(
            step_id="renew_identity",
            menu_options=["generate_identity", "import_identity"],
            description_placeholders={"expiry": expiry.date().isoformat()},
        )

    async def _async_renew(self, identity: Identity) -> ConfigFlowResult:
        """Keep the new certificate as pending; it takes over once accepted."""
        entry = self._get_reconfigure_entry()
        if renewal.pending_identity(entry) is not None:
            # Staged meanwhile (from its repair): replacing it would drop its key.
            return self.async_abort(reason="renewal_already_pending")
        if identity.lfdi == Identity.from_dict(entry.data[CONF_IDENTITY]).lfdi:
            return self.async_abort(reason="same_identity")
        if await renewal.async_stage(self.hass, entry, identity):
            return self.async_update_reload_and_abort(entry, reason="renewal_complete")
        return self.async_abort(
            reason="renewal_pending",
            description_placeholders=renewal.placeholders(entry),
        )

    async def async_step_discard_renewal(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Forget the pending certificate, once confirmed: its key is lost."""
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            if not renewal.async_discard(self.hass, entry, self._discarding):
                return self.async_abort(reason="renewal_changed")
            return self.async_abort(reason="renewal_discarded")
        # Only the certificate shown here may go, not one staged meanwhile.
        self._discarding = entry.data.get(CONF_PENDING_IDENTITY)
        return self.async_show_form(
            step_id="discard_renewal",
            description_placeholders=renewal.placeholders(entry),
        )
