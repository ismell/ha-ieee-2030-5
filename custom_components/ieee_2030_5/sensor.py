"""Sensors for the IEEE 2030.5 Smart Meter integration."""

from datetime import datetime
from typing import override

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    EntityCategory,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfReactiveEnergy,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import TOU_DELIVERED, TOU_RECEIVED
from .coordinator import Ieee20305ConfigEntry, MeterCoordinator
from .entity import MeterEntity

# The coordinator polls; entities only read its data.
PARALLEL_UPDATES = 0


def _register(
    key: str, translation_key: str, *, enabled: bool = True
) -> SensorEntityDescription:
    return SensorEntityDescription(
        key=key,
        translation_key=translation_key,
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=0,
        entity_registry_enabled_default=enabled,
    )


def _peak(key: str, translation_key: str) -> SensorEntityDescription:
    return SensorEntityDescription(
        key=key,
        translation_key=translation_key,
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
    )


def _reactive(key: str, translation_key: str) -> SensorEntityDescription:
    return SensorEntityDescription(
        key=key,
        translation_key=translation_key,
        device_class=SensorDeviceClass.REACTIVE_ENERGY,
        native_unit_of_measurement=UnitOfReactiveEnergy.VOLT_AMPERE_REACTIVE_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=0,
        entity_registry_enabled_default=False,
    )


def _apparent(key: str, translation_key: str) -> SensorEntityDescription:
    # Home Assistant has no apparent-energy device class.
    return SensorEntityDescription(
        key=key,
        translation_key=translation_key,
        native_unit_of_measurement="VAh",
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=0,
        entity_registry_enabled_default=False,
    )


def _power_factor(key: str, translation_key: str | None) -> SensorEntityDescription:
    return SensorEntityDescription(
        key=key,
        translation_key=translation_key,
        device_class=SensorDeviceClass.POWER_FACTOR,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
    )


# Keyed by the library's channel keys, which are stable across versions.
CHANNEL_SENSORS: dict[str, SensorEntityDescription] = {
    d.key: d
    for d in (
        SensorEntityDescription(
            key="power",
            device_class=SensorDeviceClass.POWER,
            native_unit_of_measurement=UnitOfPower.WATT,
            state_class=SensorStateClass.MEASUREMENT,
        ),
        _register("energy_summation_delivered", "energy_imported"),
        _register("energy_summation_received", "energy_exported"),
        _reactive("reactive_energy_summation_delivered", "reactive_energy_imported"),
        _reactive("reactive_energy_summation_received", "reactive_energy_exported"),
        _apparent("apparent_energy_summation_delivered", "apparent_energy_imported"),
        _apparent("apparent_energy_summation_received", "apparent_energy_exported"),
        _peak("power_maximum_delivered", "peak_demand_imported"),
        _peak("power_maximum_received", "peak_demand_exported"),
        _power_factor("power_factor", None),
        _power_factor("power_factor_phase_a", "power_factor_phase_a"),
        _power_factor("power_factor_phase_b", "power_factor_phase_b"),
        _power_factor("power_factor_phase_c", "power_factor_phase_c"),
    )
}

CLOCK_OFFSET = SensorEntityDescription(
    key="clock_offset",
    translation_key="clock_offset",
    device_class=SensorDeviceClass.DURATION,
    native_unit_of_measurement=UnitOfTime.SECONDS,
    state_class=SensorStateClass.MEASUREMENT,
    entity_category=EntityCategory.DIAGNOSTIC,
    entity_registry_enabled_default=False,
)
CERTIFICATE_EXPIRY = SensorEntityDescription(
    key="client_certificate_expiry",
    translation_key="client_certificate_expiry",
    device_class=SensorDeviceClass.TIMESTAMP,
    entity_category=EntityCategory.DIAGNOSTIC,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Ieee20305ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the meter's sensors."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        ChannelSensor(coordinator, description)
        for key, description in CHANNEL_SENSORS.items()
        # A channel that never returned a value (an unused phase, say) would
        # stay unknown forever.
        if key in coordinator.present
    ]
    for key, enabled in ((TOU_DELIVERED, True), (TOU_RECEIVED, False)):
        tiers = coordinator.tou_tiers.get(key, [])
        entities.extend(
            TouTierSensor(coordinator, key, tier, enabled) for tier in tiers
        )
    entities.append(ClockOffsetSensor(coordinator, CLOCK_OFFSET))
    entities.append(CertificateExpirySensor(coordinator, CERTIFICATE_EXPIRY))
    async_add_entities(entities)


class ChannelSensor(MeterEntity, SensorEntity):
    """One of the meter's readings."""

    def __init__(
        self, coordinator: MeterCoordinator, description: SensorEntityDescription
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    @override
    def native_value(self) -> float | None:
        """Return the reading."""
        return self.coordinator.data.values.get(self.entity_description.key)


class TouTierSensor(MeterEntity, SensorEntity):
    """One tier of a time-of-use register."""

    def __init__(
        self, coordinator: MeterCoordinator, key: str, tier: int, enabled: bool
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, f"{key}_tier_{tier}")
        direction = "imported" if key == TOU_DELIVERED else "exported"
        self.entity_description = _register(
            f"{key}_tier_{tier}", f"energy_{direction}_tier", enabled=enabled
        )
        self._attr_translation_placeholders = {"tier": str(tier)}
        self._register_key = key
        self._tier = tier

    @property
    @override
    def native_value(self) -> float | None:
        """Return the tier's register."""
        return self.coordinator.data.tou.get(self._register_key, {}).get(self._tier)


class ClockOffsetSensor(MeterEntity, SensorEntity):
    """How far the meter's clock is ahead of Home Assistant's."""

    def __init__(
        self, coordinator: MeterCoordinator, description: SensorEntityDescription
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    @override
    def native_value(self) -> float | None:
        """Return the clock offset."""
        return self.coordinator.data.clock_offset


class CertificateExpirySensor(MeterEntity, SensorEntity):
    """When the client certificate registered with the utility expires."""

    def __init__(
        self, coordinator: MeterCoordinator, description: SensorEntityDescription
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    @override
    def native_value(self) -> datetime:
        """Return the expiry."""
        return self.coordinator.certificate_expiry
