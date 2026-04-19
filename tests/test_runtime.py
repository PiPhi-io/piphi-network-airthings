from __future__ import annotations

from piphi_network_airthings.runtime import _filtered_telemetry_payload


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
