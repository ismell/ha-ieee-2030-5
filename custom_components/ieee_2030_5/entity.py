"""Base entity for the IEEE 2030.5 Smart Meter integration."""

from aio2030dot5 import manufacturer_name
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_METER_LFDI, DOMAIN
from .coordinator import MeterCoordinator


class MeterEntity(CoordinatorEntity[MeterCoordinator]):
    """An entity of one meter."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: MeterCoordinator, key: str) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        profile = coordinator.profile
        self._attr_unique_id = f"{entry.unique_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_METER_LFDI])},
            translation_key="meter",
            manufacturer=manufacturer_name(profile.manufacturer_id),
            model=profile.model,
            serial_number=profile.serial_number,
            hw_version=profile.hardware_version,
            sw_version=profile.software_version,
        )
