"""Hourly energy statistics from the meter's own 15-minute interval data.

The meter keeps about 30 days of interval readings: energy it accounted for
itself, so they have no gaps where Home Assistant could not poll. They are
imported as external statistics (one per direction), the stored history once
and then each new hour, continuing the running sum. Each read is its own short
batch, so the regular polls get the meter in between.
"""

from datetime import datetime, timedelta
from itertools import batched

from aio2030dot5 import (
    Channel,
    IntervalValue,
    Meter,
    MeterError,
    MeterProfile,
    Statistic,
    hourly_energy,
)
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter

from .const import DOMAIN, LOGGER

# Interval channel key -> statistic name.
INTERVAL_STATISTICS = {
    "energy_interval_delivered": "energy_imported",
    "energy_interval_received": "energy_exported",
}
NAMES = {"energy_imported": "energy imported", "energy_exported": "energy exported"}
# Reading sets per batch: a set is 12 h of intervals, about two requests.
SETS_PER_READ = 2
HOUR = timedelta(hours=1)


def statistic_id(entry: ConfigEntry, name: str) -> str:
    """The external statistic of one direction of a meter."""
    return f"{DOMAIN}:{entry.unique_id}_{name}"


class IntervalStatistics:
    """Imports the meter's completed hours into long-term statistics."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        meter: Meter,
        profile: MeterProfile,
    ) -> None:
        """Initialize the importer."""
        self.hass = hass
        self.entry = entry
        self.meter = meter
        self.profile = profile

    async def async_update(self) -> None:
        """Import every completed hour not imported yet."""
        for key, name in INTERVAL_STATISTICS.items():
            channel = self.profile.channel(key)
            if channel is None or channel.statistic != Statistic.INTERVAL:
                continue
            try:
                await self._async_import(channel, name)
            except MeterError as err:
                LOGGER.debug("Cannot read the meter's interval history: %s", err)
                return  # the next slow poll tries again

    async def _async_import(self, channel: Channel, name: str) -> None:
        stat_id = statistic_id(self.entry, name)
        last = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, stat_id, True, {"sum"}
        )
        since: datetime | None = None
        total = 0.0
        if rows := last.get(stat_id):
            since = dt_util.utc_from_timestamp(rows[0]["start"]) + HOUR
            total = rows[0].get("sum") or 0.0
        sets = await self.meter.list_interval_sets(channel)  # newest first
        wanted = [
            s
            for s in reversed(sets)
            if since is None
            or s.start is None
            or s.duration is None
            or s.start + timedelta(seconds=s.duration) > since
        ]
        # Import as the history comes in, so a read that fails halfway keeps
        # what came before it; the next run continues from there.
        chunks = list(batched(wanted, SETS_PER_READ, strict=False))
        carry: list[IntervalValue] = []
        for index, chunk in enumerate(chunks):
            values = carry + await self.meter.read_interval_sets(channel, chunk)
            carry = []
            if index < len(chunks) - 1 and values:
                # The newest hour may go on in the next sets: sum it with them.
                newest = values[-1].start.replace(minute=0, second=0, microsecond=0)
                carry = [v for v in values if v.start >= newest]
                values = [v for v in values if v.start < newest]
            try:
                hours = hourly_energy(values)
            except ValueError as err:
                LOGGER.warning("Cannot sum the meter's %s intervals: %s", name, err)
                return
            statistics: list[StatisticData] = []
            # Complete hours only: the newest is still filling, and an hour
            # the meter missed intervals of is left out, not undercounted.
            for hour in hours:
                if since is not None and hour.start < since:
                    continue  # a set still filling was imported in part already
                if hour.unit not in EnergyConverter.VALID_UNITS:
                    LOGGER.warning(
                        "The meter's %s is in %s, not energy", name, hour.unit
                    )
                    return
                total += EnergyConverter.convert(
                    hour.value, hour.unit, UnitOfEnergy.WATT_HOUR
                )
                statistics.append(StatisticData(start=hour.start, sum=total))
            if statistics:
                self._add(stat_id, name, statistics)
                since = statistics[-1]["start"] + HOUR

    def _add(self, stat_id: str, name: str, statistics: list[StatisticData]) -> None:
        LOGGER.debug("Importing %s hours of %s", len(statistics), stat_id)
        async_add_external_statistics(
            self.hass,
            StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                name=f"Electric meter {self.entry.unique_id} {NAMES[name]}",
                source=DOMAIN,
                statistic_id=stat_id,
                unit_class=EnergyConverter.UNIT_CLASS,
                unit_of_measurement=UnitOfEnergy.WATT_HOUR,
            ),
            statistics,
        )
