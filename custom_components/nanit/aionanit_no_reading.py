"""Carry wealthystudent/ha-nanit's no-reading fix until aionanit releases it.

The camera sends -1000 for a temperature or humidity it has no reading for
(the Nanit app shows "--"), and aionanit 1.12.4 reports it as -1000.0. The fix
belongs in aionanit's parser, which a custom integration cannot replace while
the released wheel is installed, so this is that parser, verbatim from the
upstream branch `fix/sensor-no-reading`, patched over the released one at
import. Delete this file and its import once the integration requires an
aionanit that has it.
"""

from __future__ import annotations

from aionanit import camera as _camera
from aionanit import parsers as _parsers
from aionanit.models import SensorState
from aionanit.proto import SensorData

# What a camera sends for temperature or humidity when it has no reading (the
# Nanit app shows "--"): -1000, as value_milli or as value.
_NO_READING_MILLI = -1_000_000
_NO_READING = -1000


def _parse_reading(sd: SensorData, current: float | None) -> float | None:
    """Return a temperature or humidity reading, or None if the camera has none."""
    if sd.value_milli:
        return None if sd.value_milli == _NO_READING_MILLI else sd.value_milli / 1000.0
    if sd.value:
        return None if sd.value == _NO_READING else float(sd.value)
    return current


def _parse_sensor_data(
    sensor_data_list: list[SensorData],
    current: SensorState,
) -> SensorState:
    from aionanit.proto import SensorType as ProtoSensorType

    temperature = current.temperature
    humidity = current.humidity
    light = current.light
    sound_alert = current.sound_alert
    motion_alert = current.motion_alert
    night = current.night

    for sd in sensor_data_list:
        if sd.sensor_type == ProtoSensorType.TEMPERATURE:
            temperature = _parse_reading(sd, temperature)
        elif sd.sensor_type == ProtoSensorType.HUMIDITY:
            humidity = _parse_reading(sd, humidity)
        elif sd.sensor_type == ProtoSensorType.LIGHT:
            light = sd.value
        elif sd.sensor_type == ProtoSensorType.SOUND:
            sound_alert = sd.is_alert
        elif sd.sensor_type == ProtoSensorType.MOTION:
            motion_alert = sd.is_alert
        elif sd.sensor_type == ProtoSensorType.NIGHT:
            night = bool(sd.value)

    return SensorState(
        temperature=temperature,
        humidity=humidity,
        light=light,
        sound_alert=sound_alert,
        motion_alert=motion_alert,
        night=night,
    )


_parsers._parse_sensor_data = _parse_sensor_data
_camera._parse_sensor_data = _parse_sensor_data
