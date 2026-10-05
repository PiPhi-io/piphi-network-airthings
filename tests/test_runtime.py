from __future__ import annotations

from datetime import datetime

from piphi_network_airthings.cloud.models import (
    MOLD_RISK_METRIC_KEY,
    AirthingsCloudDevice,
    AirthingsLatestSample,
    capabilities_for_device,
)
from piphi_network_airthings.runtime import _compute_mold_risk_level, _filtered_telemetry_payload


def test_filtered_telemetry_payload_drops_none_metrics_and_unmatched_units() -> None:
    metrics, units, dropped_metrics = _filtered_telemetry_payload(
        metrics={
            "radon_short_term_bqm3": 61.0,
            "co2_ppm": None,
            "connected": True,
            "read_failed": False,
            "rssi_dbm": None,
        },
        units={
            "radon_short_term_bqm3": "Bq/m3",
            "co2_ppm": "ppm",
            "rssi_dbm": "dBm",
        },
    )

    assert metrics == {
        "radon_short_term_bqm3": 61.0,
        "connected": True,
        "read_failed": False,
    }
    assert units == {
        "radon_short_term_bqm3": "Bq/m3",
    }
    assert dropped_metrics == ["co2_ppm", "rssi_dbm"]


def test_compute_mold_risk_level_for_wave_mini_uses_rolling_history() -> None:
    entry = {
        "config_id": "cfg-1",
        "serial_number": "2950068674",
        "device_model": "WAVE_MINI",
        "poll_interval_seconds": 300,
    }

    first = AirthingsLatestSample(
        serial_number="2950068674",
        recorded_at="2026-04-17T00:00:00+00:00",
        metrics={"temperature_c": 22.0, "humidity_percent": 92.0},
        units={},
    )
    second = AirthingsLatestSample(
        serial_number="2950068674",
        recorded_at="2026-04-19T00:00:00+00:00",
        metrics={"temperature_c": 22.0, "humidity_percent": 92.0},
        units={},
    )

    assert _compute_mold_risk_level(entry=entry, sample=first) is not None
    risk_level = _compute_mold_risk_level(entry=entry, sample=second)

    assert risk_level is not None
    assert 1 <= risk_level <= 10


def test_capabilities_for_wave_mini_include_mold_risk() -> None:
    sample = AirthingsLatestSample(
        serial_number="2950068674",
        recorded_at="2026-04-19T00:00:00+00:00",
        metrics={MOLD_RISK_METRIC_KEY: 4},
        units={},
    )

    capabilities = capabilities_for_device(
        device=AirthingsCloudDevice(
            serial_number="2950068674",
            name="Mini",
            device_type="WAVE_MINI",
            sensors=[],
        ),
        sample=sample,
    )

    assert MOLD_RISK_METRIC_KEY in capabilities


def test_sample_rejects_nonfinite_and_out_of_range_metrics_without_raw_payload() -> None:
    sample = AirthingsLatestSample.from_api_payload(
        "2930046980",
        {
            "data": {
                "recorded": "2026-09-29T12:00:00+00:00",
                "temp": "NaN",
                "humidity": 101,
                "co2": 850,
                "battery": -1,
                "privateUpstreamField": "must-not-leak",
            }
        },
    )

    assert sample.metrics["temperature_c"] is None
    assert sample.metrics["humidity_percent"] is None
    assert sample.metrics["co2_ppm"] == 850.0
    assert sample.metrics["battery_percent"] is None
    assert sample.state_payload()["metadata"] == {"serial_number": "2930046980"}
    assert "privateUpstreamField" not in str(sample.state_payload())


def test_sample_rejects_overflowing_numbers_and_uses_safe_measurement_time() -> None:
    sample = AirthingsLatestSample.from_api_payload(
        "2930046980",
        {
            "data": {
                "recorded": 10**10000,
                "co2": 10**10000,
                "battery": "1e999",
                "rssi": "-1e999",
            }
        },
    )

    assert sample.metrics["co2_ppm"] is None
    assert sample.metrics["battery_percent"] is None
    assert sample.metrics["rssi_dbm"] is None
    assert datetime.fromisoformat(sample.recorded_at).tzinfo is not None
