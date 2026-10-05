from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from fastapi import HTTPException
from piphi_runtime_kit_python import build_runtime_auth_headers

from piphi_network_airthings.cloud.client import AirthingsCloudAuthError, AirthingsCloudRequestError
import piphi_network_airthings.runtime as runtime_module
from piphi_network_airthings.app import runtime_port_from_environment


def test_runtime_port_defaults_and_accepts_disposable_qualification_override(monkeypatch) -> None:
    monkeypatch.delenv("PIPHI_RUNTIME_PORT", raising=False)
    assert runtime_port_from_environment() == 3669

    monkeypatch.setenv("PIPHI_RUNTIME_PORT", "37669")
    assert runtime_port_from_environment() == 37669


@pytest.mark.parametrize("value", ["0", "65536", "not-a-port"])
def test_runtime_port_rejects_invalid_values(monkeypatch, value: str) -> None:
    monkeypatch.setenv("PIPHI_RUNTIME_PORT", value)

    with pytest.raises(ValueError, match="between 1 and 65535"):
        runtime_port_from_environment()


@pytest.mark.asyncio
async def test_runtime_data_endpoints_require_complete_authentication(
    unauthenticated_client,
) -> None:
    for path in ("/diagnostics", "/entities", "/state", "/events"):
        response = await unauthenticated_client.get(path)
        assert response.status_code == 401, path
        assert response.json()["detail"] == "Complete runtime authentication is required"

    assert (await unauthenticated_client.get("/health")).status_code == 200
    assert (await unauthenticated_client.get("/ui-config")).status_code == 200


@pytest.mark.asyncio
async def test_runtime_auth_bootstraps_once_and_rejects_takeover(
    async_client,
    fake_cloud_client,
) -> None:
    fake_cloud_client.devices = {}
    bootstrap = await async_client.post(
        "/discover",
        json={"inputs": {"client_id": "client-1", "client_secret": "secret-1"}},
    )
    assert bootstrap.status_code == 200

    takeover = await async_client.post(
        "/discover",
        json={"inputs": {"client_id": "client-2", "client_secret": "secret-2"}},
        headers=build_runtime_auth_headers(
            container_id="container-attacker",
            internal_token="attacker-token",
        ),
    )

    assert takeover.status_code == 401
    assert takeover.json()["detail"] == "Invalid runtime authentication"
    assert runtime_module.runtime.auth.resolve() == ("container-1", "runtime-token-1")


@pytest.mark.asyncio
async def test_runtime_contract_declares_exact_automation_endpoints(async_client) -> None:
    response = await async_client.get("/contract")

    assert response.status_code == 200
    payload = response.json()
    assert payload["integration_id"] == "airthings-consumer-cloud-api"
    assert payload["endpoints"] == {
        "health": "/health",
        "entities": "/entities",
        "events": "/events",
        "command": "/command",
        "state": "/state",
        "config": "/config",
        "config_sync": "/configs/sync",
    }
    assert set(payload["required"]) == set(payload["endpoints"])


@pytest.mark.asyncio
async def test_config_sync_rejects_header_payload_scope_mismatch(async_client) -> None:
    response = await async_client.post(
        "/configs/sync",
        json={
            "container_id": "different-container",
            "integration_id": "airthings-consumer-cloud-api",
            "generation": 1,
            "reason": "scope_test",
            "configs": [],
        },
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Runtime container scope does not match payload"


@pytest.mark.asyncio
async def test_wrong_auth_mutating_endpoints_have_no_side_effects(
    async_client,
    fake_cloud_client,
) -> None:
    serial_number = "2930046980"
    fake_cloud_client.devices[serial_number] = {
        "serialNumber": serial_number,
        "name": "Basement",
        "type": "WAVE_MINI",
        "sensors": ["temp"],
    }
    fake_cloud_client.samples[serial_number] = {
        "data": {"recorded": "2026-09-29T12:00:00+00:00", "temp": 21.4}
    }
    valid_config = {
        "id": "cfg-protected",
        "client_id": "client-1",
        "client_secret": "secret-1",
        "serial_number": serial_number,
    }
    assert (await async_client.post("/config", json=valid_config)).status_code == 200
    cloud_calls = (
        list(fake_cloud_client.list_devices_calls),
        list(fake_cloud_client.latest_sample_calls),
    )
    attacker_headers = build_runtime_auth_headers(
        container_id="container-attacker",
        internal_token="attacker-token",
    )
    requests = (
        ("/config", {**valid_config, "id": "cfg-attacker"}),
        (
            "/configs/sync",
            {
                "container_id": "container-attacker",
                "integration_id": "airthings-consumer-cloud-api",
                "generation": 2,
                "reason": "takeover",
                "configs": [],
            },
        ),
        ("/deconfigure", {"config": {"config_id": "cfg-protected"}}),
        ("/command", {"command": "refresh", "entity_id": "device:cfg-protected"}),
    )

    for path, payload in requests:
        response = await async_client.post(path, json=payload, headers=attacker_headers)
        assert response.status_code == 401, path

    assert runtime_module.registry.ids() == ["cfg-protected"]
    assert fake_cloud_client.list_devices_calls == cloud_calls[0]
    assert fake_cloud_client.latest_sample_calls == cloud_calls[1]


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
    assert state_payload["entries"]["cfg-1"]["latest_state"]["co2_ppm"] == 812.0
    assert (
        state_payload["entries"]["cfg-2"]["latest_state"]["radon_short_term_bqm3"]
        == 72.0
    )
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
async def test_same_device_configs_remain_isolated_by_config_id(
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
    assert diagnostics["active_config_ids"] == ["cfg-old", "cfg-current"]
    assert diagnostics["poll_task_ids"] == ["cfg-current", "cfg-old"]
    assert set(diagnostics["poll_status"]) == {"cfg-old", "cfg-current"}


@pytest.mark.asyncio
async def test_device_only_command_rejects_ambiguous_same_serial_configs(
    async_client,
    fake_cloud_client,
) -> None:
    serial_number = "2930046980"
    fake_cloud_client.devices = {
        serial_number: {
            "serialNumber": serial_number,
            "name": "Shared monitor",
            "type": "WAVE_MINI",
            "sensors": ["temp"],
        }
    }
    fake_cloud_client.samples[serial_number] = {
        "data": {"recorded": "2026-09-29T12:00:00+00:00", "temp": 21.4}
    }
    for config_id, client_id in (("cfg-home", "home"), ("cfg-cabin", "cabin")):
        response = await async_client.post(
            "/config",
            json={
                "id": config_id,
                "client_id": client_id,
                "client_secret": f"{client_id}-secret",
                "serial_number": serial_number,
            },
        )
        assert response.status_code == 200
    initial_reads = len(fake_cloud_client.latest_sample_calls)

    response = await async_client.post(
        "/command",
        json={"command": "refresh", "device_id": serial_number},
    )

    assert response.status_code == 409
    assert "matches multiple configurations" in response.json()["detail"]
    assert len(fake_cloud_client.latest_sample_calls) == initial_reads


@pytest.mark.asyncio
async def test_refresh_rejects_unknown_args_without_logging_secret_values(
    async_client,
    fake_cloud_client,
    caplog,
) -> None:
    sentinel = "must-never-reach-airthings-logs"
    fake_cloud_client.devices = {
        "2930046980": {
            "serialNumber": "2930046980",
            "name": "Office",
            "type": "WAVE_MINI",
            "sensors": ["temp"],
        }
    }
    fake_cloud_client.samples["2930046980"] = {
        "data": {"recorded": "2026-09-29T12:00:00+00:00", "temp": 21.4}
    }
    configured = await async_client.post(
        "/config",
        json={
            "id": "cfg-1",
            "container_id": "container-1",
            "client_id": "client-1",
            "client_secret": "secret-1",
            "serial_number": "2930046980",
        },
    )
    assert configured.status_code == 200
    caplog.clear()

    with caplog.at_level("INFO"):
        response = await async_client.post(
            "/command",
            json={
                "command": "refresh",
                "entity_id": "device:cfg-1",
                "args": {
                    "config_id": "cfg-1",
                    "credentials": {"Authorization": sentinel},
                },
            },
        )

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "Refresh accepts only the config_id argument."
    assert sentinel not in caplog.text
    assert "Authorization" not in caplog.text


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

    assert state_payload["entries"]["cfg-1"]["config_id"] == "cfg-1"
    assert state_payload["entries"]["cfg-1"]["device_id"] == "2930046980"
    assert state_payload["entries"]["cfg-1"]["latest_state"]["connected"] is False
    assert (
        state_payload["entries"]["cfg-1"]["latest_state"]["last_error"]
        == "Airthings consumer cloud request failed."
    )
    assert any(event["event_type"] == "airthings.cloud.initial_read.failed" for event in events_payload["events"])
    failed_event = next(
        event
        for event in events_payload["events"]
        if event["event_type"] == "airthings.cloud.initial_read.failed"
    )
    assert failed_event["payload"]["error"] == "Airthings consumer cloud request failed."
    assert "cfg-1" in diagnostics_payload["diagnostics"]["poll_task_ids"]


@pytest.mark.asyncio
async def test_poll_events_emit_once_per_offline_and_recovery_transition(
    async_client,
    fake_cloud_client,
) -> None:
    serial_number = "2930046980"
    fake_cloud_client.devices = {
        serial_number: {
            "serialNumber": serial_number,
            "name": "Basement Wave Plus",
            "type": "WAVE_PLUS",
            "sensors": ["temp"],
        }
    }
    fake_cloud_client.samples[serial_number] = {
        "data": {"recorded": "2026-09-29T12:00:00+00:00", "temp": 21.4}
    }
    response = await async_client.post(
        "/config",
        json={
            "id": "cfg-transitions",
            "client_id": "client-1",
            "client_secret": "secret-1",
            "serial_number": serial_number,
        },
    )
    assert response.status_code == 200
    runtime_module.registry.recent_events.clear()

    fake_cloud_client.latest_sample_failures[serial_number] = AirthingsCloudRequestError(
        "cloud unavailable"
    )
    await runtime_module.poll_once("cfg-transitions")
    await runtime_module.poll_once("cfg-transitions")
    fake_cloud_client.latest_sample_failures.clear()
    await runtime_module.poll_once("cfg-transitions")
    await runtime_module.poll_once("cfg-transitions")

    event_types = [event["event_type"] for event in runtime_module.registry.recent_events]
    assert event_types.count("airthings.cloud.sample.failed") == 1
    assert event_types.count("airthings.cloud.sample.recovered") == 1
    assert "airthings.cloud.sample.updated" not in event_types


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
