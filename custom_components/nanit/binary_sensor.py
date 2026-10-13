"""Binary sensor platform for Nanit."""

from __future__ import annotations

import time as time_mod
from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import STATE_OFF, STATE_ON, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from aionanit.models import CameraState, CloudEvent, ConnectionState

from . import NanitConfigEntry
from .const import CLOUD_EVENT_WINDOW, is_read_only
from .coordinator import NanitCloudCoordinator, NanitPushCoordinator, NanitSoundLightCoordinator
from .entity import NanitCloudEntity, NanitEntity, NanitSoundLightEntity, camera_power_on

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class NanitBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describe a Nanit binary sensor."""

    value_fn: Callable[[CameraState], bool | None]
    always_available: bool = False


BINARY_SENSORS: tuple[NanitBinarySensorEntityDescription, ...] = (
    NanitBinarySensorEntityDescription(
        key="connectivity",
        translation_key="connectivity",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda state: state.connection.state == ConnectionState.CONNECTED,
        always_available=True,
    ),
)


# --- Cloud-based binary sensors (motion + sound from Nanit cloud events) ---


@dataclass(frozen=True, kw_only=True)
class NanitCloudBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describe a Nanit cloud binary sensor."""

    event_type: str  # "MOTION" or "SOUND" (uppercase, matches API)


CLOUD_BINARY_SENSORS: tuple[NanitCloudBinarySensorEntityDescription, ...] = (
    NanitCloudBinarySensorEntityDescription(
        key="cloud_motion",
        translation_key="motion",
        device_class=BinarySensorDeviceClass.MOTION,
        entity_registry_enabled_default=True,
        event_type="MOTION",
    ),
    NanitCloudBinarySensorEntityDescription(
        key="cloud_sound",
        translation_key="sound",
        device_class=BinarySensorDeviceClass.SOUND,
        entity_registry_enabled_default=True,
        event_type="SOUND",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NanitConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Nanit binary sensors for all devices on the account."""
    entities: list[BinarySensorEntity] = []

    read_only = is_read_only(entry)
    for cam_data in entry.runtime_data.cameras.values():
        for push_desc in BINARY_SENSORS:
            entities.append(NanitBinarySensor(cam_data.push_coordinator, push_desc))

        # Read-only mode has no power switch; this sensor shows its state instead.
        if read_only:
            entities.append(NanitCameraPowerSensor(cam_data.push_coordinator))

        if cam_data.cloud_coordinator is not None:
            for cloud_desc in CLOUD_BINARY_SENSORS:
                entities.append(NanitCloudBinarySensor(cam_data.cloud_coordinator, cloud_desc))

    # Sound & Light sensors
    for speaker_data in entry.runtime_data.speakers.values():
        entities.append(NanitSLConnectivitySensor(speaker_data.coordinator))
        entities.append(NanitSLChargingSensor(speaker_data.coordinator))

    async_add_entities(entities)


class NanitBinarySensor(NanitEntity, BinarySensorEntity):
    """Nanit binary sensor entity."""

    entity_description: NanitBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: NanitPushCoordinator,
        description: NanitBinarySensorEntityDescription,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.camera.uid}_{description.key}"

    @property
    def available(self) -> bool:
        """Return entity availability.

        Entities flagged ``always_available`` (e.g. connectivity) stay
        available even when the camera is disconnected so they can
        report the disconnected state instead of going unavailable.
        """
        if self.entity_description.always_available:
            return self.coordinator.last_update_success and self.coordinator.data is not None
        return super().available

    @property
    def is_on(self) -> bool | None:
        """Return true if the binary sensor is on.

        The connectivity sensor uses the coordinator's debounced ``connected``
        flag instead of the raw WebSocket state so that brief disconnections
        during pre-emptive token refresh (every ~55 min) do not cause the
        entity to flap between connected/disconnected.
        """
        if self.coordinator.data is None:
            return None
        if self.entity_description.key == "connectivity":
            return bool(self.coordinator.connected)
        return self.entity_description.value_fn(self.coordinator.data)


class NanitCameraPowerSensor(NanitEntity, RestoreEntity, BinarySensorEntity):
    """Camera power (on = awake, off = asleep), read-only mode only.

    Stands in for the camera power switch, which read-only mode removes. It
    follows the switch's state rules: the same value, the last known value
    kept while the camera reports none, and restored on startup.
    """

    _attr_translation_key = "camera_power"
    _attr_device_class = BinarySensorDeviceClass.POWER

    def __init__(self, coordinator: NanitPushCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.camera.uid}_camera_power"
        self._attr_is_on = (
            camera_power_on(coordinator.data) if coordinator.data is not None else None
        )

    async def async_added_to_hass(self) -> None:
        """Restore the last known state if the camera hasn't reported one yet."""
        await super().async_added_to_hass()
        if (
            self._attr_is_on is None
            and (last_state := await self.async_get_last_state()) is not None
            and last_state.state in (STATE_ON, STATE_OFF)
        ):
            self._attr_is_on = last_state.state == STATE_ON

    def _handle_coordinator_update(self) -> None:
        """Take the camera's power state; keep the last known one if it has none."""
        if self.coordinator.data is not None:
            new_value = camera_power_on(self.coordinator.data)
            if new_value is not None:
                self._attr_is_on = new_value
        self.async_write_ha_state()


class NanitCloudBinarySensor(NanitCloudEntity, BinarySensorEntity):
    """Cloud-based binary sensor that detects motion/sound from Nanit cloud events.

    Polls the cloud API every 30s and checks for events within a 5-minute window.
    If an event of the matching type is found within the window, the sensor is ON.
    Otherwise it is OFF (automatically clears after 5 minutes from the last event).
    """

    entity_description: NanitCloudBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: NanitCloudCoordinator,
        description: NanitCloudBinarySensorEntityDescription,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.baby.camera_uid}_{description.key}"

    @property
    def is_on(self) -> bool | None:
        """Return true if a matching event was found within the detection window."""
        if self.coordinator.data is None:
            return None

        events: list[CloudEvent] = self.coordinator.data
        if not events:
            return False

        now = time_mod.time()
        cutoff = now - CLOUD_EVENT_WINDOW
        target_type = self.entity_description.event_type

        for event in events:
            if event.event_type.upper() != target_type:
                continue
            if event.timestamp >= cutoff:
                return True

        return False


class NanitSLConnectivitySensor(NanitSoundLightEntity, BinarySensorEntity):
    """Connectivity binary sensor for the Sound & Light Machine."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "sl_connectivity"

    def __init__(
        self,
        coordinator: NanitSoundLightCoordinator,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.sound_light.speaker_uid}_sl_connectivity"

    @property
    def available(self) -> bool:
        """Always available so it can report disconnected state."""
        return self.coordinator.last_update_success and self.coordinator.data is not None

    @property
    def is_on(self) -> bool:
        """Return True when the S&L WebSocket is connected."""
        result: bool = self.coordinator.connected
        return result


class NanitSLChargingSensor(NanitSoundLightEntity, BinarySensorEntity):
    """Battery charging indicator for the Sound & Light Machine."""

    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING

    def __init__(
        self,
        coordinator: NanitSoundLightCoordinator,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.sound_light.speaker_uid}_sl_charging"

    @property
    def is_on(self) -> bool | None:
        """Return True when the device reports it's charging."""
        if self.coordinator.data is None:
            return None
        result: bool | None = self.coordinator.data.battery_charging
        return result
