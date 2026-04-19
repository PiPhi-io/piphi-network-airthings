from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


DEVICE_TYPE_NAMES = {
    "WAVE_PLUS": "Airthings Wave Plus",
    "WAVE_RADON": "Airthings Wave Radon",
    "WAVE_MINI": "Airthings Wave Mini",
    "WAVE_ENHANCE": "Airthings Wave Enhance",
    "VIEW_PLUS": "Airthings View Plus",
    "VIEW_RADON": "Airthings View Radon",
    "CORENTIUM_HOME_2": "Airthings Corentium Home 2",
}

_DEFAULT_UNITS = {
    "radon_short_term_bqm3": "Bq/m3",
    "radon_long_term_bqm3": "Bq/m3",
    "temperature_c": "C",
    "humidity_percent": "%",
    "pressure_hpa": "hPa",
    "co2_ppm": "ppm",
    "voc_ppb": "ppb",
    "pm1_ugm3": "ug/m3",
    "pm25_ugm3": "ug/m3",
    "battery_percent": "%",
    "rssi_dbm": "dBm",
}


def _coerce_datetime(value: Any) -> str:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC).isoformat()
    if isinstance(value, str) and value:
        return value
    return datetime.now(tz=UTC).isoformat()


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


@dataclass(slots=True)
class AirthingsCloudDevice:
    serial_number: str
    name: str
    device_type: str | None = None
    sensors: list[str] = field(default_factory=list)
    home: str | None = None
    segment_name: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def model_name(self) -> str:
        if self.device_type and self.device_type in DEVICE_TYPE_NAMES:
            return DEVICE_TYPE_NAMES[self.device_type]
        if self.device_type:
            return self.device_type.replace("_", " ").title()
        return "Airthings Device"

    def to_discovery_record(self, *, client_id: str, client_secret: str) -> dict[str, Any]:
        return {
            "id": self.serial_number,
            "serial_number": self.serial_number,
            "device_id": self.serial_number,
            "name": self.name or self.model_name,
            "device_model": self.device_type,
            "model": self.device_type,
            "client_id": client_id,
            "client_secret": client_secret,
            "metadata": {
                "serial_number": self.serial_number,
                "home": self.home,
                "segment_name": self.segment_name,
                "device_type": self.device_type,
                "sensors": list(self.sensors),
            },
        }


@dataclass(slots=True)
class AirthingsLatestSample:
    serial_number: str
    recorded_at: str
    metrics: dict[str, float | int | None]
    units: dict[str, str]
    relay_device_type: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api_payload(cls, serial_number: str, payload: dict[str, Any]) -> "AirthingsLatestSample":
        if isinstance(payload.get("sensors"), list):
            sensor_map = {
                "radonShortTermAvg": ("radon_short_term_bqm3", _coerce_float),
                "radonLongTermAvg": ("radon_long_term_bqm3", _coerce_float),
                "temp": ("temperature_c", _coerce_float),
                "humidity": ("humidity_percent", _coerce_float),
                "pressure": ("pressure_hpa", _coerce_float),
                "co2": ("co2_ppm", _coerce_float),
                "voc": ("voc_ppb", _coerce_float),
                "pm1": ("pm1_ugm3", _coerce_float),
                "pm25": ("pm25_ugm3", _coerce_float),
                "rssi": ("rssi_dbm", _coerce_int),
            }
            metrics: dict[str, float | int | None] = {key: None for key in _DEFAULT_UNITS}
            units = dict(_DEFAULT_UNITS)
            for sensor in payload["sensors"]:
                if not isinstance(sensor, dict):
                    continue
                sensor_type = str(sensor.get("sensorType") or sensor.get("type") or "").strip()
                mapping = sensor_map.get(sensor_type)
                if mapping is None:
                    continue
                metric_key, coercer = mapping
                metrics[metric_key] = coercer(sensor.get("value"))
                sensor_unit = str(sensor.get("unit") or "").strip()
                if sensor_unit:
                    units[metric_key] = sensor_unit
            metrics["battery_percent"] = _coerce_int(
                payload.get("batteryPercentage") or payload.get("battery")
            )
            recorded_at = _coerce_datetime(payload.get("recorded") or payload.get("time"))
            return cls(
                serial_number=serial_number,
                recorded_at=recorded_at,
                metrics=metrics,
                units=units,
                relay_device_type=payload.get("relayDeviceType"),
                raw=dict(payload),
            )

        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        metrics = {
            "radon_short_term_bqm3": _coerce_float(data.get("radonShortTermAvg")),
            "radon_long_term_bqm3": _coerce_float(data.get("radonLongTermAvg")),
            "temperature_c": _coerce_float(data.get("temp")),
            "humidity_percent": _coerce_float(data.get("humidity")),
            "pressure_hpa": _coerce_float(data.get("pressure")),
            "co2_ppm": _coerce_float(data.get("co2")),
            "voc_ppb": _coerce_float(data.get("voc")),
            "pm1_ugm3": _coerce_float(data.get("pm1")),
            "pm25_ugm3": _coerce_float(data.get("pm25")),
            "battery_percent": _coerce_int(data.get("battery")),
            "rssi_dbm": _coerce_int(data.get("rssi")),
        }
        units = {**_DEFAULT_UNITS}
        recorded_at = _coerce_datetime(data.get("recorded") or data.get("time"))
        return cls(
            serial_number=serial_number,
            recorded_at=recorded_at,
            metrics=metrics,
            units=units,
            relay_device_type=data.get("relayDeviceType"),
            raw=dict(data),
        )

    def state_payload(self) -> dict[str, Any]:
        return {
            **self.metrics,
            "sampled_at": self.recorded_at,
            "relay_device_type": self.relay_device_type,
            "metadata": {
                "raw": self.raw,
            },
        }


def capabilities_for_device(device: AirthingsCloudDevice, sample: AirthingsLatestSample | None = None) -> list[str]:
    candidates = [
        "radon_short_term_bqm3",
        "radon_long_term_bqm3",
        "temperature_c",
        "humidity_percent",
        "pressure_hpa",
        "co2_ppm",
        "voc_ppb",
        "pm1_ugm3",
        "pm25_ugm3",
        "battery_percent",
    ]
    if sample is not None:
        present = [capability for capability in candidates if sample.metrics.get(capability) is not None]
    else:
        sensor_map = {
            "radonShortTermAvg": "radon_short_term_bqm3",
            "radonLongTermAvg": "radon_long_term_bqm3",
            "temp": "temperature_c",
            "humidity": "humidity_percent",
            "pressure": "pressure_hpa",
            "co2": "co2_ppm",
            "voc": "voc_ppb",
            "pm1": "pm1_ugm3",
            "pm25": "pm25_ugm3",
            "battery": "battery_percent",
        }
        present = []
        for sensor in device.sensors:
            capability = sensor_map.get(sensor)
            if capability and capability not in present:
                present.append(capability)
        if "battery_percent" not in present:
            present.append("battery_percent")
    if "refresh" not in present:
        present.append("refresh")
    return present
