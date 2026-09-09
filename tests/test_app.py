from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from fastapi import HTTPException

from piphi_network_airthings.cloud.client import AirthingsCloudAuthError, AirthingsCloudRequestError
import piphi_network_airthings.runtime as runtime_module


@pytest.mark.asyncio
async def test_discover_returns_cloud_devices(async_client, fake_cloud_client) -> None:
    fake_cloud_client.devices = {
        "2930046980": {
            "serialNumber": "2930046980",
            "name": "Basement Wave Plus",
            "type": "WAVE_PLUS",
            "sensors": ["radonShortTermAvg", "temp", "humidity", "co2", "voc", "pressure"],
        }
    }

    response = await async_client.post(
        "/discover",
        json={
            "inputs": {
                "client_id": "client-1",
                "client_secret": "secret-1",
            }
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["devices"][0]["serial_number"] == "2930046980"
    assert payload["devices"][0]["device_model"] == "WAVE_PLUS"
    assert payload["devices"][0]["client_id"] == "client-1"


@pytest.mark.asyncio
async def test_discover_accepts_flat_core_payload(async_client, fake_cloud_client) -> None:
    fake_cloud_client.devices = {
        "2930046980": {
            "serialNumber": "2930046980",
            "name": "Basement Wave Plus",
            "type": "WAVE_PLUS",
            "sensors": ["radonShortTermAvg", "temp", "humidity"],
        }
    }

    response = await async_client.post(
        "/discover",
        json={
            "client_id": "client-1",
            "client_secret": "secret-1",
            "serial_number": "2930046980",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["devices"]) == 1
    assert payload["devices"][0]["serial_number"] == "2930046980"
    assert fake_cloud_client.list_devices_calls == ["client-1"]


@pytest.mark.asyncio
async def test_config_sync_configures_and_refreshes_multiple_devices(async_client, fake_cloud_client) -> None:
    fake_cloud_client.devices = {
        "2930046980": {
            "serialNumber": "2930046980",
            "name": "Basement Wave Plus",
            "type": "WAVE_PLUS",
            "sensors": ["radonShortTermAvg", "temp", "humidity", "co2", "voc", "pressure"],
        },
        "2950123456": {
            "serialNumber": "2950123456",
            "name": "Office Wave Radon",
            "type": "WAVE_RADON",
            "sensors": ["radonShortTermAvg", "temp", "humidity"],
        },
    }
    fake_cloud_client.samples = {
        "2930046980": {
            "data": {
                "recorded": "2026-04-18T18:30:00+00:00",
                "radonShortTermAvg": 55,
                "temp": 21.4,
                "humidity": 44,
                "co2": 812,
                "voc": 88,
                "pressure": 1011.5,
                "battery": 94,
            }
        },
        "2950123456": {
            "data": {
                "recorded": "2026-04-18T18:31:00+00:00",
                "radonShortTermAvg": 72,
                "temp": 19.2,
                "humidity": 40,
                "battery": 91,
            }
        },
    }

    response = await async_client.post(
        "/configs/sync",
        json={
            "container_id": "container-1",
            "integration_id": "airthings-consumer-cloud-api",
            "generation": 4,
            "reason": "test_sync",
            "configs": [
                {
                    "id": "cfg-1",
                    "client_id": "client-1",
                    "client_secret": "secret-1",
                    "serial_number": "2930046980",
                },
                {
                    "id": "cfg-2",
                    "client_id": "client-1",
                    "client_secret": "secret-1",
                    "serial_number": "2950123456",
                },
            ],
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "synced"

    entities_response = await async_client.get("/entities")
    state_response = await async_client.get("/state")
    idempotency_headers = {
        "X-PiPhi-Idempotency-Key": "airthings-refresh-idempotency-1"
    }
    refresh_response = await async_client.post(
        "/command",
        json={"command": "refresh", "entity_id": "device:cfg-1"},
        headers=idempotency_headers,
    )
    replay_response = await async_client.post(
        "/command",
        json={"command": "refresh", "entity_id": "device:cfg-1"},
        headers=idempotency_headers,
    )

    entities_payload = entities_response.json()
    state_payload = state_response.json()

    assert len(entities_payload["entities"]) == 2
    assert state_payload["state"]["cfg-1"]["state"]["co2_ppm"] == 812.0
    assert state_payload["state"]["cfg-2"]["state"]["radon_short_term_bqm3"] == 72.0
    assert refresh_response.status_code == 200
    assert replay_response.status_code == 200
    assert refresh_response.json()["replayed"] is False
    assert replay_response.json()["replayed"] is True
    assert fake_cloud_client.latest_sample_calls.count("2930046980") == 2
    assert refresh_response.json()["state"]["temperature_c"] == 21.4


@pytest.mark.asyncio
async def test_unchanged_cloud_sample_keeps_measurement_time_and_sends_fresh_poll_heartbeat(
    async_client,
    fake_cloud_client,
    monkeypatch,
) -> None:
    fake_cloud_client.devices = {
        "2930046980": {
            "serialNumber": "2930046980",
            "name": "Basement Wave Plus",
            "type": "WAVE_PLUS",
            "sensors": ["temp", "humidity"],
        }
    }
    sampled_at = "2026-04-18T18:30:00+00:00"
    fake_cloud_client.samples = {
        "2930046980": {
            "data": {
                "recorded": sampled_at,
                "temp": 21.4,
                "humidity": 44,
            }
        }
    }
    deliveries: list[dict] = []

    def capture_delivery(**kwargs):
        deliveries.append(kwargs)
        task = asyncio.get_running_loop().create_future()
        task.set_result(True)
        return task

    monkeypatch.setattr(runtime_module, "schedule_telemetry_delivery", capture_delivery)

    response = await async_client.post(
        "/config",
        json={
            "id": "cfg-heartbeat",
            "client_id": "client-1",
            "client_secret": "secret-1",
            "serial_number": "2930046980",
            "poll_interval_seconds": 300,
        },
    )
    assert response.status_code == 200
    await runtime_module._read_and_store("cfg-heartbeat")
    await asyncio.sleep(0)

    measurement_deliveries = [
        delivery for delivery in deliveries
        if "temperature_c" in delivery["metrics"]
    ]
    heartbeat_deliveries = [
        delivery for delivery in deliveries
        if delivery["metrics"] == {"connected": True, "read_failed": False}
    ]
    assert len(measurement_deliveries) == 1
    assert measurement_deliveries[0]["timestamp"] == sampled_at
    assert len(heartbeat_deliveries) == 2
    assert all(delivery["timestamp"] != sampled_at for delivery in heartbeat_deliveries)
    assert all(datetime.fromisoformat(delivery["timestamp"]).tzinfo is not None for delivery in heartbeat_deliveries)

    diagnostics = (await async_client.get("/diagnostics")).json()["diagnostics"]
    status = diagnostics["poll_status"]["cfg-heartbeat"]
    state = diagnostics["state_snapshots"]["cfg-heartbeat"]["state"]
    assert status["last_polled_at"] == heartbeat_deliveries[-1]["timestamp"]
    assert status["last_sample_recorded_at"] == sampled_at
    assert state["last_polled_at"] == heartbeat_deliveries[-1]["timestamp"]
    assert state["sampled_at"] == sampled_at
    assert status["last_core_heartbeat_at"] >= heartbeat_deliveries[-1]["timestamp"]

    health = (await async_client.get("/health")).json()["metadata"]
    assert health["last_polled_at"] == status["last_polled_at"]
    assert health["last_core_heartbeat_at"] == status["last_core_heartbeat_at"]
    assert health["last_sample_recorded_at"] == sampled_at


@pytest.mark.asyncio
async def test_reapplying_config_preserves_measurement_deduplication(
    async_client,
    fake_cloud_client,
    monkeypatch,
) -> None:
    sampled_at = "2026-09-09T18:19:38+00:00"
    fake_cloud_client.devices = {
        "2930046980": {
            "serialNumber": "2930046980",
            "name": "Office",
            "type": "WAVE_MINI",
            "sensors": ["temp"],
        }
    }
    fake_cloud_client.samples["2930046980"] = {
        "data": {"recorded": sampled_at, "temp": 24.4}
    }
    deliveries: list[dict] = []

    def capture_delivery(**kwargs):
        deliveries.append(kwargs)
        task = asyncio.get_running_loop().create_future()
        task.set_result(True)
        return task

    monkeypatch.setattr(runtime_module, "schedule_telemetry_delivery", capture_delivery)
    config_payload = {
        "id": "cfg-office",
        "client_id": "client-1",
        "client_secret": "secret-1",
        "serial_number": "2930046980",
        "poll_interval_seconds": 300,
    }

    assert (await async_client.post("/config", json=config_payload)).status_code == 200
    await asyncio.sleep(0)
    assert (await async_client.post("/config", json=config_payload)).status_code == 200
    await asyncio.sleep(0)

    measurement_deliveries = [
        delivery for delivery in deliveries
        if "temperature_c" in delivery["metrics"]
    ]
    assert len(measurement_deliveries) == 1
    assert runtime_module.registry.get("cfg-office")["last_delivered_sample_at"] == sampled_at


@pytest.mark.asyncio
async def test_new_config_for_same_device_removes_obsolete_runtime_config(
    async_client,
    fake_cloud_client,
) -> None:
    fake_cloud_client.devices = {
        "2930046980": {
            "serialNumber": "2930046980",
            "name": "Office",
            "type": "WAVE_MINI",
            "sensors": ["temp"],
        }
    }
    fake_cloud_client.samples["2930046980"] = {
        "data": {"recorded": "2026-09-09T18:19:38+00:00", "temp": 24.4}
    }
    common_payload = {
        "client_id": "client-1",
        "client_secret": "secret-1",
        "serial_number": "2930046980",
        "poll_interval_seconds": 300,
    }

    assert (
        await async_client.post("/config", json={"id": "cfg-old", **common_payload})
    ).status_code == 200
    assert (
        await async_client.post("/config", json={"id": "cfg-current", **common_payload})
    ).status_code == 200

    diagnostics = (await async_client.get("/diagnostics")).json()["diagnostics"]
    assert diagnostics["active_config_ids"] == ["cfg-current"]
    assert diagnostics["poll_task_ids"] == ["cfg-current"]
    assert "cfg-old" not in diagnostics["poll_status"]


@pytest.mark.asyncio
async def test_invalid_replacement_keeps_existing_device_config(
    async_client,
    fake_cloud_client,
    monkeypatch,
) -> None:
    fake_cloud_client.devices = {
        "2930046980": {
            "serialNumber": "2930046980",
            "name": "Office",
            "type": "WAVE_MINI",
            "sensors": ["temp"],
        }
    }
    fake_cloud_client.samples["2930046980"] = {
        "data": {"recorded": "2026-09-09T18:19:38+00:00", "temp": 24.4}
    }
    common_payload = {
        "client_id": "client-1",
        "client_secret": "secret-1",
        "serial_number": "2930046980",
        "poll_interval_seconds": 300,
    }
    assert (
        await async_client.post("/config", json={"id": "cfg-working", **common_payload})
    ).status_code == 200

    async def reject_replacement(_config):
        raise HTTPException(status_code=401, detail="credentials rejected")

    monkeypatch.setattr(runtime_module, "_ensure_known_device", reject_replacement)
    response = await async_client.post(
        "/config",
        json={"id": "cfg-replacement", **common_payload},
    )

    assert response.status_code == 401
    assert runtime_module.registry.ids() == ["cfg-working"]
    assert sorted(runtime_module.poll_tasks) == ["cfg-working"]


@pytest.mark.asyncio
async def test_failed_measurement_delivery_is_retried_on_next_poll(
    async_client,
    fake_cloud_client,
    monkeypatch,
) -> None:
    sampled_at = "2026-09-09T10:00:00+00:00"
    fake_cloud_client.devices = {
        "2930046980": {
            "serialNumber": "2930046980",
            "name": "Basement Wave Plus",
            "type": "WAVE_PLUS",
            "sensors": ["temp"],
        }
    }
    fake_cloud_client.samples["2930046980"] = {
        "data": {
            "recorded": sampled_at,
            "temp": 21.4,
        }
    }
    deliveries: list[dict] = []
    failed_first_measurement = False

    def capture_delivery(**kwargs):
        nonlocal failed_first_measurement
        deliveries.append(kwargs)
        task = asyncio.get_running_loop().create_future()
        is_measurement = "temperature_c" in kwargs["metrics"]
        if is_measurement and not failed_first_measurement:
            failed_first_measurement = True
            task.set_result(False)
        else:
            task.set_result(True)
        return task

    monkeypatch.setattr(runtime_module, "schedule_telemetry_delivery", capture_delivery)

    response = await async_client.post(
        "/config",
        json={
            "id": "cfg-retry",
            "client_id": "client-1",
            "client_secret": "secret-1",
            "serial_number": "2930046980",
            "poll_interval_seconds": 300,
        },
    )
    assert response.status_code == 200
    await asyncio.sleep(0)
    await runtime_module._read_and_store("cfg-retry")
    await asyncio.sleep(0)

    measurement_deliveries = [
        delivery for delivery in deliveries
        if "temperature_c" in delivery["metrics"]
    ]
    assert len(measurement_deliveries) == 2
    assert all(delivery["timestamp"] == sampled_at for delivery in measurement_deliveries)


@pytest.mark.asyncio
async def test_config_apply_records_initial_read_error(async_client, fake_cloud_client) -> None:
    fake_cloud_client.devices = {
        "2930046980": {
            "serialNumber": "2930046980",
            "name": "Basement Wave Plus",
            "type": "WAVE_PLUS",
            "sensors": ["radonShortTermAvg", "temp"],
        }
    }
    fake_cloud_client.latest_sample_failures["2930046980"] = AirthingsCloudRequestError("cloud unavailable")

    response = await async_client.post(
        "/config",
        json={
            "id": "cfg-1",
            "client_id": "client-1",
            "client_secret": "secret-1",
            "serial_number": "2930046980",
        },
    )

    assert response.status_code == 200

    state_response = await async_client.get("/state")
    events_response = await async_client.get("/events")
    diagnostics_response = await async_client.get("/diagnostics")

    state_payload = state_response.json()
    events_payload = events_response.json()
    diagnostics_payload = diagnostics_response.json()

    assert state_payload["state"]["cfg-1"]["state"]["connected"] is False
    assert "cloud unavailable" in state_payload["state"]["cfg-1"]["state"]["last_error"]
    assert any(event["event_type"] == "airthings.cloud.initial_read.failed" for event in events_payload["events"])
    assert "cfg-1" in diagnostics_payload["diagnostics"]["poll_task_ids"]


@pytest.mark.asyncio
async def test_discover_returns_auth_error_when_token_exchange_fails(async_client, fake_cloud_client) -> None:
    async def fail_list_devices(_credentials):
        raise AirthingsCloudAuthError("Airthings consumer cloud credentials were rejected.")

    fake_cloud_client.list_devices = fail_list_devices

    response = await async_client.post(
        "/discover",
        json={
            "inputs": {
                "client_id": "client-1",
                "client_secret": "secret-1",
            }
        },
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Airthings consumer cloud credentials were rejected."
