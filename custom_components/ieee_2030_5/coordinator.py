"""Coordinator for the IEEE 2030.5 Smart Meter integration."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
import time
from typing import Any, override

from aio2030dot5 import (
    Channel,
    Identity,
    Meter,
    MeterAuthError,
    MeterBusy,
    MeterError,
    MeterIdentityMismatch,
    MeterNotFound,
    MeterProfile,
    MeterSnapshot,
    ProfileError,
    ReadingGuard,
    Statistic,
    format_lfdi,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import renewal
from .const import (
    CONF_METER_LFDI,
    CONF_REGISTERED,
    DOMAIN,
    LOGGER,
    SCAN_INTERVAL,
    SLOW_INTERVAL,
    UTILITY_PLACEHOLDERS,
    awaiting_registration_issue_id,
)
from .statistics import IntervalStatistics

type Ieee20305ConfigEntry = ConfigEntry[MeterCoordinator]

STORAGE_VERSION = 1
# Re-walking the meter's resource tree costs ~30 requests: at most this often.
REDISCOVER_INTERVAL = 3600.0
# Read on every poll; the rest of the meter's channels every SLOW_INTERVAL.
FAST_KEYS = ("power", "energy_summation_delivered", "energy_summation_received")


@dataclass
class MeterData:
    """The latest accepted readings."""

    values: dict[str, float | None] = field(default_factory=dict)
    # ToU register key -> tier -> value; tier 0 (the total) is not kept
    tou: dict[str, dict[int, float | None]] = field(default_factory=dict)
    clock_offset: float | None = None
    # The raw reading, for diagnostics; not part of what entities show.
    snapshot: MeterSnapshot | None = field(default=None, compare=False)


def store_for(hass: HomeAssistant, entry: ConfigEntry) -> Store[dict[str, Any]]:
    """Where the meter's profile and guard state are kept."""
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.unique_id}")


class MeterCoordinator(DataUpdateCoordinator[MeterData]):
    """Polls one meter; every poll is one batch on one connection."""

    config_entry: Ieee20305ConfigEntry
    profile: MeterProfile

    def __init__(
        self,
        hass: HomeAssistant,
        entry: Ieee20305ConfigEntry,
        meter: Meter,
        identity: Identity,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{entry.unique_id}",
            update_interval=SCAN_INTERVAL,
            always_update=False,
        )
        self.meter = meter
        self.identity = identity
        self.guard = ReadingGuard()
        self.present: set[str] = set()  # channels that returned a value
        # ToU register key -> the tiers it lists / the tiers holding a value
        self.tou_listed: dict[str, list[int]] = {}
        self.tou_tiers: dict[str, list[int]] = {}
        self._store = store_for(hass, entry)
        self._last_slow = 0.0
        self._last_discovery = -REDISCOVER_INTERVAL
        self._initial: MeterSnapshot | None = None
        self._tiers_changed = False
        self._statistics_task: asyncio.Task[None] | None = None

    async def async_prepare(self) -> None:
        """Load or discover the meter's profile and read every channel once.

        Raises the setup outcome directly, not through the first refresh,
        which would drop the translated reason.
        """
        stored = await self._store.async_load() or {}
        self.guard.restore(stored.get("guard"))
        profile = self._cached_profile(stored.get("profile"))
        known_tiers = _known_tiers(stored.get("tou_tiers"))
        try:
            if profile is None:
                profile = await self._discover()
            snapshot = await self._read_all(profile)
            if any(isinstance(e, MeterNotFound) for e in snapshot.errors.values()):
                # The cached tree is stale (e.g. a firmware update).
                profile = await self._discover()
                snapshot = await self._read_all(profile)
        except MeterAuthError as err:
            if await self._renewed():
                raise ConfigEntryNotReady(
                    translation_domain=DOMAIN, translation_key="certificate_renewed"
                ) from err
            if (awaiting := self._awaiting_renewal()) is not None:
                raise ConfigEntryNotReady(
                    translation_domain=DOMAIN,
                    translation_key="awaiting_renewal",
                    translation_placeholders=awaiting,
                ) from err
            raise self._auth_error() from err
        except MeterError as err:
            raise ConfigEntryNotReady(
                translation_domain=DOMAIN,
                translation_key="connection_error",
                translation_placeholders={"host": self.meter.host, "error": str(err)},
            ) from err
        self.profile = profile
        self.present = {k for k, m in snapshot.values.items() if m.value is not None}
        self.tou_listed = {
            key: sorted(t for t in register if t > 0)
            for key, register in snapshot.tou.items()
        }
        # Tiers that ever held a value: one empty at this read keeps its sensor.
        self.tou_tiers = {
            key: sorted(set(reg.tiers) | set(known_tiers.get(key, [])))
            for key, reg in snapshot.tou.items()
        }
        self._initial = snapshot
        self._last_slow = time.monotonic()
        self._registered()
        renewal.async_update_issues(self.hass, self.config_entry)
        self._save()

    def _awaiting_renewal(self) -> dict[str, str] | None:
        """The current certificate is refused, but a renewal is pending.

        Keep checking for its registration rather than ask to reauthenticate.
        """
        if (pending := renewal.pending_identity(self.config_entry)) is None:
            return None
        return {"lfdi": format_lfdi(pending.lfdi)}

    def _auth_error(self) -> Exception:
        entry = self.config_entry
        renewal.async_update_issues(self.hass, entry)
        if entry.data[CONF_REGISTERED]:
            return ConfigEntryAuthFailed(
                translation_domain=DOMAIN,
                translation_key="auth_failed",
                translation_placeholders={"host": self.meter.host},
            )
        # Expected until the utility has registered our certificate.
        placeholders = {
            "lfdi": format_lfdi(self.identity.lfdi),
            "sfdi": entry.unique_id or "",
        }
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            awaiting_registration_issue_id(entry.entry_id),
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="awaiting_registration",
            translation_placeholders={**placeholders, **UTILITY_PLACEHOLDERS},
            data={"entry_id": entry.entry_id, **placeholders},
        )
        return ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="awaiting_registration",
            translation_placeholders=placeholders,
        )

    def _registered(self) -> None:
        """Our certificate works: the registration (if pending) is done."""
        entry = self.config_entry
        if not entry.data[CONF_REGISTERED]:
            self.hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_REGISTERED: True}
            )
        ir.async_delete_issue(
            self.hass, DOMAIN, awaiting_registration_issue_id(entry.entry_id)
        )

    def _cached_profile(self, data: Any) -> MeterProfile | None:
        if not isinstance(data, dict):
            return None
        try:
            profile = MeterProfile.from_dict(data)
        except ProfileError as err:
            LOGGER.debug("Discarding the cached meter profile: %s", err)
            return None
        if profile.certificate_lfdi != self.config_entry.data[CONF_METER_LFDI]:
            return None
        return profile

    async def _discover(self) -> MeterProfile:
        self._last_discovery = time.monotonic()
        return await self.meter.discover()

    async def _read_all(self, profile: MeterProfile) -> MeterSnapshot:
        return await self.meter.read(
            profile, _readable(profile.channels), include_time=True, partial=True
        )

    @override
    async def _async_update_data(self) -> MeterData:
        """Read the meter."""
        if (snapshot := self._initial) is not None:
            self._initial = None
        else:
            # Slow channels are due by time, and only a successful read
            # resets the clock: a failed slow poll is retried on the next.
            slow = time.monotonic() - self._last_slow >= SLOW_INTERVAL.total_seconds()
            channels = [
                c
                for c in _readable(self.profile.channels)
                if (c.key in FAST_KEYS or slow)
                and (c.key in self.present or c.key in self.tou_listed)
            ]
            try:
                snapshot = await self.meter.read(
                    self.profile, channels, include_time=slow, partial=True
                )
            except MeterAuthError as err:
                if await self._renewed():
                    self.hass.config_entries.async_schedule_reload(
                        self.config_entry.entry_id
                    )
                    raise UpdateFailed(
                        translation_domain=DOMAIN,
                        translation_key="certificate_renewed",
                    ) from err
                if (awaiting := self._awaiting_renewal()) is not None:
                    raise UpdateFailed(
                        translation_domain=DOMAIN,
                        translation_key="awaiting_renewal",
                        translation_placeholders=awaiting,
                        retry_after=SLOW_INTERVAL.total_seconds(),
                    ) from err
                raise self._update_error(err) from err
            except MeterError as err:
                raise self._update_error(err) from err
            if any(isinstance(e, MeterNotFound) for e in snapshot.errors.values()):
                await self._rediscover()
            if slow:
                self._last_slow = time.monotonic()
                self.async_import_statistics()
                await self._check_renewal()
                # The guard's state, a few times an hour: an SD card wears.
                self._save()
        data = self._accept(snapshot)
        if self._tiers_changed:
            # Remember the new tier before setting up again with its sensor:
            # one that reads empty at setup must not be news every time.
            self._tiers_changed = False
            await self._store.async_save(self._stored())
            self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
        return data

    @callback
    def async_import_statistics(self) -> None:
        """Import the meter's newly completed hours, in the background.

        The first run brings in the whole stored history (about 30 days).
        """
        if "recorder" not in self.hass.config.components or (
            self._statistics_task is not None and not self._statistics_task.done()
        ):
            return
        importer = IntervalStatistics(
            self.hass, self.config_entry, self.meter, self.profile
        )
        self._statistics_task = self.config_entry.async_create_background_task(
            self.hass, importer.async_update(), f"{DOMAIN} interval statistics"
        )

    async def _renewed(self) -> bool:
        """The meter refused our certificate: has a renewal taken over?"""
        return await renewal.async_try_pending(self.hass, self.config_entry)

    async def _check_renewal(self) -> None:
        """Switch to a pending certificate once the meter accepts it."""
        entry = self.config_entry
        if await renewal.async_try_pending(self.hass, entry):
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
        else:
            # The reminder starts once the expiry is near.
            renewal.async_update_issues(self.hass, entry)

    async def _rediscover(self) -> None:
        """Re-walk the meter after a profiled path vanished.

        At most hourly; on success the entry is set up again on the new tree.
        """
        if time.monotonic() - self._last_discovery < REDISCOVER_INTERVAL:
            return
        LOGGER.info("The meter's resource tree changed; rediscovering it")
        try:
            self.profile = await self._discover()
        except MeterError as err:
            LOGGER.debug("Rediscovery failed: %s", err)
            return
        self._save()
        self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)

    def _update_error(self, err: MeterError) -> Exception:
        placeholders = {"host": self.meter.host}
        if isinstance(err, MeterAuthError):
            return ConfigEntryAuthFailed(
                translation_domain=DOMAIN,
                translation_key="auth_failed",
                translation_placeholders=placeholders,
            )
        if isinstance(err, MeterIdentityMismatch):
            return UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="identity_mismatch",
                translation_placeholders=placeholders,
            )
        if isinstance(err, MeterBusy):
            return UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="meter_busy",
                translation_placeholders=placeholders,
                retry_after=300,
            )
        return UpdateFailed(
            translation_domain=DOMAIN,
            translation_key="connection_error",
            translation_placeholders={**placeholders, "error": str(err)},
        )

    def _accept(self, snapshot: MeterSnapshot) -> MeterData:
        """Merge a snapshot into the last data, through the guard.

        A reading the meter flags questionable, one the guard rejects, or a
        channel that failed this poll is unknown until a good one arrives.
        """
        previous = self.data or MeterData()
        for key, err in snapshot.errors.items():
            LOGGER.debug("Reading %s failed: %s", key, err)
        accepted = self.guard.accept(snapshot, self.profile)
        for key in sorted(accepted.rebased):
            LOGGER.warning(
                "The meter's %s register moved to a new baseline"
                " (the meter was replaced or reset)",
                key,
            )
        values = {**previous.values, **accepted.values}
        tou = {key: dict(tiers) for key, tiers in previous.tou.items()}
        for key, register in accepted.tou.items():
            tiers = tou.setdefault(key, {})
            for tier, value in register.items():
                if tier == 0:  # the total: the plain summation channel
                    continue
                if value is not None and tier not in self.tou_tiers.get(key, []):
                    self._new_tier(key, tier)
                tiers[tier] = value
        clock_offset = previous.clock_offset
        if snapshot.clock_skew is not None:
            clock_offset = round(snapshot.clock_skew.total_seconds(), 1)
        return MeterData(values, tou, clock_offset, snapshot)

    def _new_tier(self, key: str, tier: int) -> None:
        """A tier empty at setup (another season's rate) started counting."""
        LOGGER.info("Time-of-use tier %s of %s started counting", tier, key)
        self.tou_tiers.setdefault(key, []).append(tier)
        self._tiers_changed = True

    def _save(self) -> None:
        self._store.async_delay_save(self._stored, 10)

    def _stored(self) -> dict[str, Any]:
        return {
            "profile": self.profile.to_dict(),
            "guard": self.guard.to_dict(),
            "tou_tiers": self.tou_tiers,
        }

    @property
    def certificate_expiry(self) -> datetime:
        """When our client certificate expires."""
        return self.identity.not_after


def _known_tiers(data: Any) -> dict[str, list[int]]:
    """The stored tiers per ToU register; anything malformed is ignored."""
    if not isinstance(data, dict):
        return {}
    return {
        key: [t for t in tiers if isinstance(t, int)]
        for key, tiers in data.items()
        if isinstance(key, str) and isinstance(tiers, list)
    }


def _readable(channels: tuple[Channel, ...]) -> list[Channel]:
    """Channels with a current value (interval data is not polled)."""
    return [c for c in channels if c.statistic != Statistic.INTERVAL]
