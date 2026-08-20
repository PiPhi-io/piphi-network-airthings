from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from piphi_runtime_kit_python import (
    AutomationActionRequest,
    AutomationActionResult,
    AutomationRegistry,
    IntegrationCommandRequest,
    IntegrationDiscoveryRequest,
    IntegrationDiscoveryResponse,
    IntegrationEventListResponse,
    RuntimeConfig,
    RuntimeConfigApplyResponse,
    RuntimeConfigRemoveResponse,
    RuntimeConfigSnapshot,
    RuntimeConfigSyncResponse,
    RuntimeDiagnosticsResponse,
    RuntimeHealthResponse,
    SQLiteAutomationIdempotencyStore,
    build_config_apply_response,
    build_config_remove_response,
    build_discovery_response,
    build_event_list_response,
    build_local_event_record,
    create_runtime_starter,
    create_tracked_task,
    resolve_core_base_url,
    schedule_event_delivery,
    schedule_telemetry_delivery,
    validate_typed_configs,
)
from piphi_runtime_kit_python.fastapi import (
    dispatch_automation_action_from_fastapi,
    sync_runtime_auth_from_fastapi_payload,
)
from piphi_runtime_kit_python.runtime.errors import CoreDeliveryError

from .cloud.client import (
    AirthingsCloudAuthError,
    AirthingsCloudClient,
    AirthingsCloudError,
    AirthingsCloudRateLimitError,
    AirthingsCloudRequestError,
    AirthingsCredentials,
)
from .cloud.models import (
    MOLD_RISK_METRIC_KEY,
    AirthingsCloudDevice,
    AirthingsLatestSample,
    capabilities_for_device,
    supports_mold_risk,
)
from .manifest import load_manifest


manifest = load_manifest()
INTEGRATION_ID = str(manifest.get("id") or "airthings-consumer-cloud-api")
INTEGRATION_NAME = str(manifest.get("name") or "Airthings (Consumer Cloud)")
INTEGRATION_VERSION = str(manifest.get("version") or "0.1.0")
DEFAULT_POLL_INTERVAL_SECONDS = 300
MOLD_RISK_WINDOW_HOURS = 48
logger = logging.getLogger(__name__)

starter = create_runtime_starter(
    integration_id=INTEGRATION_ID,
    integration_name=INTEGRATION_NAME,
    version=INTEGRATION_VERSION,
    core_base_url=resolve_core_base_url("http://127.0.0.1:31419"),
)
runtime = starter.runtime
registry = starter.registry
telemetry_client = starter.telemetry_client
event_client = starter.event_client
config_sync = starter.config_sync
cloud_client = AirthingsCloudClient()
router = APIRouter()
_automation_ledger_path = Path(
    os.getenv(
        "PIPHI_AUTOMATION_LEDGER_PATH",
        "/.piphinetwork/automation-actions.sqlite3",
    )
)
automation_registry = AutomationRegistry(
    idempotency_store=SQLiteAutomationIdempotencyStore(_automation_ledger_path)
)
poll_tasks: dict[str, asyncio.Task[Any]] = {}
poll_status: dict[str, dict[str, Any]] = {}


async def _refresh_registered_device(
    action_request: AutomationActionRequest,
) -> AutomationActionResult:
    config_id = str(action_request.config_id or "").strip()
    try:
        sample = await _read_and_store(config_id)
    except AirthingsCloudError as exc:
        try:
            _raise_http_for_cloud_error(exc)
        except HTTPException as http_exc:
            return AutomationActionResult.failure(
                str(http_exc.detail),
                retryable=http_exc.status_code >= 500 or http_exc.status_code == 429,
                metadata={"status_code": http_exc.status_code},
            )
        return AutomationActionResult.failure(str(exc), metadata={"status_code": 500})
    return AutomationActionResult.success(
        {
            "status": "ok",
            "config_id": config_id,
            "sampled_at": sample.recorded_at,
            "state": registry.state_snapshots.get(config_id, {}).get("state"),
        }
    )


automation_registry.action("refresh")(_refresh_registered_device)


class AirthingsCloudConfig(RuntimeConfig):
    client_id: str
    client_secret: str
    serial_number: str
    device_name: str | None = None
    device_model: str | None = None
    poll_interval_seconds: int = Field(default=DEFAULT_POLL_INTERVAL_SECONDS, ge=60, le=86400)


class DeconfigurePayload(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)


def set_cloud_client(client: AirthingsCloudClient) -> None:
    global cloud_client
    cloud_client = client


async def reset_runtime_state() -> None:
    logger.info(
        "airthings_runtime_reset_started active_configs=%s active_poll_tasks=%s recent_events=%s",
        len(registry.entries),
        len(poll_tasks),
        len(registry.recent_events),
    )
    for task in list(poll_tasks.values()):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    poll_tasks.clear()
    poll_status.clear()
    registry.entries.clear()
    registry.state_snapshots.clear()
    registry.recent_events.clear()
    runtime.auth.container_id = ""
    runtime.auth.internal_token = ""
    runtime.process_state.background_tasks.clear()
    runtime.process_state.current_generation = None
    logger.info("airthings_runtime_reset_completed")


def _credentials(config: AirthingsCloudConfig) -> AirthingsCredentials:
    return AirthingsCredentials(client_id=config.client_id, client_secret=config.client_secret)


def _entry_credentials(entry: dict[str, Any]) -> AirthingsCredentials:
    return AirthingsCredentials(
        client_id=str(entry["client_id"]),
        client_secret=str(entry["client_secret"]),
    )


def _config_id(config: AirthingsCloudConfig) -> str:
    return str(getattr(config, "config_id", None) or config.id)


def _device_id(config: AirthingsCloudConfig) -> str:
    return str(config.serial_number).strip()


def _entry_log_label(entry: dict[str, Any]) -> str:
    return (
        f"config_id={entry.get('config_id')} "
        f"serial_number={entry.get('serial_number')} "
        f"device_model={entry.get('device_model') or 'unknown'}"
    )


def _entry_name(entry: dict[str, Any]) -> str:
    return str(entry.get("device_name") or entry.get("serial_number") or entry.get("config_id"))


def _config_log_label(config: AirthingsCloudConfig) -> str:
    return (
        f"config_id={_config_id(config)} "
        f"serial_number={config.serial_number} "
        f"poll_interval_seconds={config.poll_interval_seconds}"
    )


def _filtered_telemetry_payload(
    *,
    metrics: dict[str, Any],
    units: dict[str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, str] | None, list[str]]:
    filtered_metrics = {
        key: value
        for key, value in metrics.items()
        if value is not None
    }
    dropped_metrics = sorted(key for key, value in metrics.items() if value is None)
    if not units:
        return filtered_metrics, None, dropped_metrics
    filtered_units = {
        key: value
        for key, value in units.items()
        if key in filtered_metrics
    }
    return filtered_metrics, filtered_units or None, dropped_metrics


def _parse_sample_timestamp(recorded_at: str) -> datetime:
    normalized = str(recorded_at).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _mold_history(entry: dict[str, Any]) -> list[dict[str, Any]]:
    history = entry.get("mold_history")
    if isinstance(history, list):
        return history
    created: list[dict[str, Any]] = []
    entry["mold_history"] = created
    return created


def _mold_exposure_score(*, temperature_c: float, humidity_percent: float) -> float:
    humidity_factor = min(max((humidity_percent - 75.0) / 15.0, 0.0), 1.0)
    if temperature_c < 5.0 or temperature_c > 35.0:
        temperature_factor = 0.35
    elif temperature_c < 10.0 or temperature_c > 30.0:
        temperature_factor = 0.6
    else:
        temperature_factor = 1.0
    return humidity_factor * temperature_factor


def _compute_mold_risk_level(
    *,
    entry: dict[str, Any],
    sample: AirthingsLatestSample,
) -> int | None:
    if not supports_mold_risk(entry.get("device_model")):
        return None

    temperature_c = sample.metrics.get("temperature_c")
    humidity_percent = sample.metrics.get("humidity_percent")
    if temperature_c is None or humidity_percent is None:
        return None

    sample_time = _parse_sample_timestamp(sample.recorded_at)
    cutoff = sample_time - timedelta(hours=MOLD_RISK_WINDOW_HOURS)
    history = _mold_history(entry)
    retained = [
        item for item in history
        if isinstance(item, dict)
        and "recorded_at" in item
        and _parse_sample_timestamp(str(item["recorded_at"])) >= cutoff
    ]
    retained = [
        item for item in retained
        if str(item.get("recorded_at")) != sample.recorded_at
    ]
    retained.append(
        {
            "recorded_at": sample.recorded_at,
            "temperature_c": float(temperature_c),
            "humidity_percent": float(humidity_percent),
        }
    )
    retained.sort(key=lambda item: str(item["recorded_at"]))
    history[:] = retained

    window_seconds = float(timedelta(hours=MOLD_RISK_WINDOW_HOURS).total_seconds())
    poll_interval_seconds = max(int(entry.get("poll_interval_seconds") or DEFAULT_POLL_INTERVAL_SECONDS), 60)
    weighted_exposure = 0.0
    covered_seconds = 0.0

    for index, item in enumerate(retained):
        current_time = _parse_sample_timestamp(str(item["recorded_at"]))
        if index + 1 < len(retained):
            next_time = _parse_sample_timestamp(str(retained[index + 1]["recorded_at"]))
            interval_seconds = max((next_time - current_time).total_seconds(), 0.0)
        else:
            interval_seconds = float(poll_interval_seconds)
        weighted_exposure += _mold_exposure_score(
            temperature_c=float(item["temperature_c"]),
            humidity_percent=float(item["humidity_percent"]),
        ) * interval_seconds
        covered_seconds += interval_seconds

    if covered_seconds <= 0:
        return None

    average_exposure = weighted_exposure / covered_seconds
    coverage_factor = min(covered_seconds / window_seconds, 1.0)
    risk_level = round(max(min(average_exposure * coverage_factor * 10.0, 10.0), 0.0))
    logger.info(
        "airthings_mold_risk_computed %s sampled_at=%s risk_level=%s coverage_hours=%.2f",
        _entry_log_label(entry),
        sample.recorded_at,
        risk_level,
        covered_seconds / 3600.0,
    )
    return int(risk_level)


def _append_runtime_event(
    *,
    event_type: str,
    device: dict[str, Any],
    payload: dict[str, Any] | None = None,
    severity: str = "info",
) -> None:
    registry.append_event(
        build_local_event_record(
            event_type=event_type,
            device=device,
            payload=payload,
            source=INTEGRATION_ID,
            severity=severity,
        )
    )


def _handle_event_delivery_error(exc: Exception, context: dict[str, Any]) -> None:
    if isinstance(exc, CoreDeliveryError):
        logger.warning(
            "Airthings cloud event delivery failed for event=%s device=%s error=%s",
            context.get("event_type"),
            context.get("device_id"),
            exc,
        )
        return
    logger.warning(
        "Airthings cloud event delivery raised unexpected error for event=%s device=%s error=%r",
        context.get("event_type"),
        context.get("device_id"),
        exc,
    )


def _handle_event_delivery_skipped(reason: str, context: dict[str, Any]) -> None:
    logger.warning(
        "Airthings cloud event delivery skipped for event=%s device=%s reason=%s",
        context.get("event_type"),
        context.get("device_id"),
        reason,
    )


def _handle_telemetry_delivery_error(exc: Exception, context: dict[str, Any]) -> None:
    if isinstance(exc, CoreDeliveryError):
        logger.warning(
            "Airthings cloud telemetry delivery failed for device=%s error=%s",
            context.get("device_id"),
            exc,
        )
        return
    logger.warning(
        "Airthings cloud telemetry delivery raised unexpected error for device=%s error=%r",
        context.get("device_id"),
        exc,
    )


def _handle_telemetry_delivery_skipped(reason: str, context: dict[str, Any]) -> None:
    logger.warning(
        "Airthings cloud telemetry delivery skipped for device=%s reason=%s",
        context.get("device_id"),
        reason,
    )


def _schedule_runtime_event_delivery(
    *,
    event_type: str,
    device: dict[str, Any],
    payload: dict[str, Any] | None = None,
    severity: str = "info",
) -> None:
    _append_runtime_event(
        event_type=event_type,
        device=device,
        payload=payload,
        severity=severity,
    )
    schedule_event_delivery(
        process_state=runtime.process_state,
        event_client=event_client,
        auth_context=runtime.auth,
        event_type=event_type,
        device=device,
        payload=payload,
        source=INTEGRATION_ID,
        severity=severity,
        on_error=_handle_event_delivery_error,
        on_skipped=_handle_event_delivery_skipped,
    )


def _update_poll_status(config_id: str, **updates: Any) -> None:
    current = poll_status.get(config_id, {})
    current.update(updates)
    poll_status[config_id] = current


async def _fetch_latest_sample(entry: dict[str, Any]) -> tuple[AirthingsCloudDevice | None, AirthingsLatestSample]:
    sample = await cloud_client.latest_sample(
        credentials=_entry_credentials(entry),
        serial_number=str(entry["serial_number"]),
    )
    return None, sample


def _update_state_snapshot(
    *,
    config_id: str,
    entry: dict[str, Any],
    sample: AirthingsLatestSample,
) -> None:
    derived_metrics = dict(sample.metrics)
    derived_units = dict(sample.units)
    mold_risk_level = _compute_mold_risk_level(entry=entry, sample=sample)
    if mold_risk_level is not None:
        derived_metrics[MOLD_RISK_METRIC_KEY] = mold_risk_level
        derived_units[MOLD_RISK_METRIC_KEY] = "score"

    state_payload = sample.state_payload()
    if mold_risk_level is not None:
        state_payload[MOLD_RISK_METRIC_KEY] = mold_risk_level
    registry.update_state(
        config_id,
        {
            **state_payload,
            "connected": True,
            "serial_number": entry["serial_number"],
            "device_model": entry.get("device_model"),
            "name": _entry_name(entry),
            "last_error": None,
        },
    )
    metrics = {
        **derived_metrics,
        "connected": True,
        "read_failed": False,
    }
    filtered_metrics, filtered_units, dropped_metrics = _filtered_telemetry_payload(
        metrics=metrics,
        units=derived_units,
    )
    if dropped_metrics:
        logger.info(
            "airthings_telemetry_filtered %s sampled_at=%s sent_metrics=%s dropped_metrics=%s",
            _entry_log_label(entry),
            sample.recorded_at,
            sorted(filtered_metrics.keys()),
            dropped_metrics,
        )
    schedule_telemetry_delivery(
        process_state=runtime.process_state,
        telemetry_client=telemetry_client,
        auth_context=runtime.auth,
        config_id=str(entry["config_id"]),
        device_id=str(entry["serial_number"]),
        metrics=filtered_metrics,
        container_id=entry.get("container_id"),
        units=filtered_units,
        timestamp=sample.recorded_at,
        on_error=_handle_telemetry_delivery_error,
        on_skipped=_handle_telemetry_delivery_skipped,
    )
    logger.info(
        "airthings_sample_delivery_scheduled %s sampled_at=%s metric_count=%s",
        _entry_log_label(entry),
        sample.recorded_at,
        len(filtered_metrics),
    )


async def _read_and_store(config_id: str) -> AirthingsLatestSample:
    entry = registry.get(config_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"unknown config_id={config_id}")
    logger.info("Polling Airthings cloud latest sample for %s", _entry_log_label(entry))
    _update_poll_status(config_id, last_poll_started=asyncio.get_running_loop().time())
    try:
        _device, sample = await _fetch_latest_sample(entry)
    except AirthingsCloudError as exc:
        _update_poll_status(config_id, last_poll_error=str(exc))
        raise
    _update_state_snapshot(config_id=config_id, entry=entry, sample=sample)
    _update_poll_status(
        config_id,
        last_poll_succeeded=sample.recorded_at,
        last_poll_error=None,
    )
    logger.info(
        "airthings_sample_stored %s sampled_at=%s metric_count=%s",
        _entry_log_label(entry),
        sample.recorded_at,
        len([value for value in sample.metrics.values() if value is not None]),
    )
    return sample


async def _poll_config(config_id: str, interval_seconds: int) -> None:
    logger.info("airthings_poll_started config_id=%s interval_seconds=%s", config_id, interval_seconds)
    first_iteration = True
    while True:
        try:
            if first_iteration:
                _update_poll_status(
                    config_id,
                    next_poll_due=(
                        asyncio.get_running_loop().time() + interval_seconds
                    ),
                )
                first_iteration = False
                await asyncio.sleep(interval_seconds)
            sample = await _read_and_store(config_id)
            _update_poll_status(
                config_id,
                next_poll_due=(
                    asyncio.get_running_loop().time() + interval_seconds
                ),
            )
            _schedule_runtime_event_delivery(
                event_type="airthings.cloud.sample.updated",
                device=registry.get(config_id) or {"config_id": config_id},
                payload={
                    "recorded_at": sample.recorded_at,
                },
            )
        except asyncio.CancelledError:
            logger.info("airthings_poll_stopped config_id=%s", config_id)
            raise
        except Exception as exc:
            entry = registry.get(config_id)
            if entry is not None:
                registry.update_state(
                    config_id,
                    {
                        "connected": False,
                        "serial_number": entry["serial_number"],
                        "device_model": entry.get("device_model"),
                        "name": _entry_name(entry),
                        "last_error": str(exc),
                    },
                )
                schedule_telemetry_delivery(
                    process_state=runtime.process_state,
                    telemetry_client=telemetry_client,
                    auth_context=runtime.auth,
                    config_id=str(config_id),
                    device_id=str(entry["serial_number"]),
                    metrics={"connected": False, "read_failed": True},
                    container_id=entry.get("container_id"),
                    timestamp=registry.state_snapshots.get(config_id, {}).get("last_updated"),
                    on_error=_handle_telemetry_delivery_error,
                    on_skipped=_handle_telemetry_delivery_skipped,
                )
                _schedule_runtime_event_delivery(
                    event_type="airthings.cloud.sample.failed",
                    device=entry,
                    payload={"error": str(exc)},
                    severity="warning",
                )
                logger.warning("Airthings cloud poll failed for %s error=%s", _entry_log_label(entry), exc)
        await asyncio.sleep(interval_seconds)


async def _ensure_known_device(config: AirthingsCloudConfig) -> AirthingsCloudDevice:
    try:
        devices = await cloud_client.list_devices(_credentials(config))
    except AirthingsCloudError as exc:
        _raise_http_for_cloud_error(exc)
    for device in devices:
        if device.serial_number == config.serial_number:
            logger.info(
                "airthings_device_matched %s discovered_name=%s discovered_model=%s available_sensors=%s",
                _config_log_label(config),
                device.name,
                device.device_type,
                list(device.sensors),
            )
            return device
    raise HTTPException(
        status_code=404,
        detail=f"Airthings serial number {config.serial_number} was not found for the provided cloud credentials.",
    )


async def apply_config(config: AirthingsCloudConfig) -> dict[str, Any]:
    config_id = _config_id(config)
    logger.info("airthings_config_apply_started %s", _config_log_label(config))
    await remove_config(config_id)
    device = await _ensure_known_device(config)
    entry = {
        "config_id": config_id,
        "device_id": _device_id(config),
        "container_id": getattr(config, "container_id", None),
        "integration_id": getattr(config, "integration_id", None) or INTEGRATION_ID,
        "client_id": config.client_id,
        "client_secret": config.client_secret,
        "serial_number": config.serial_number,
        "device_name": config.device_name or device.name,
        "device_model": config.device_model or device.device_type,
        "poll_interval_seconds": config.poll_interval_seconds,
        "config": config.model_dump(),
        "known_sensors": list(device.sensors),
    }
    registry.set(config_id, entry)
    registry.update_state(
        config_id,
        {
            "connected": False,
            "serial_number": entry["serial_number"],
            "device_model": entry.get("device_model"),
            "name": _entry_name(entry),
        },
    )
    _update_poll_status(config_id, configured_at=registry.state_snapshots[config_id]["last_updated"])
    logger.info(
        "airthings_config_registered %s device_name=%s known_sensors=%s",
        _entry_log_label(entry),
        _entry_name(entry),
        entry["known_sensors"],
    )

    initial_error: str | None = None
    try:
        await _read_and_store(config_id)
        logger.info("airthings_initial_read_succeeded %s", _entry_log_label(entry))
    except Exception as exc:
        initial_error = str(exc)
        registry.update_state(
            config_id,
            {
                "connected": False,
                "serial_number": entry["serial_number"],
                "device_model": entry.get("device_model"),
                "name": _entry_name(entry),
                "last_error": initial_error,
            },
        )
        _schedule_runtime_event_delivery(
            event_type="airthings.cloud.initial_read.failed",
            device=entry,
            payload={"error": initial_error},
            severity="warning",
        )
        logger.warning("Initial Airthings cloud read failed for %s error=%s", _entry_log_label(entry), exc)

    poll_tasks[config_id] = create_tracked_task(
        _poll_config(config_id, entry["poll_interval_seconds"]),
        process_state=runtime.process_state,
    )
    logger.info(
        "airthings_config_apply_completed %s initial_error=%s poll_task_active=%s",
        _entry_log_label(entry),
        initial_error or "none",
        config_id in poll_tasks,
    )
    _schedule_runtime_event_delivery(
        event_type="device.configured",
        device=entry,
        payload={
            "serial_number": entry["serial_number"],
            "device_model": entry.get("device_model"),
            "initial_error": initial_error,
        },
    )
    return entry


async def remove_config(config_id: str) -> bool:
    logger.info("airthings_config_remove_started config_id=%s", config_id)
    task = poll_tasks.pop(config_id, None)
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    removed = registry.remove(config_id)
    poll_status.pop(config_id, None)
    if removed is None:
        logger.info("airthings_config_remove_skipped config_id=%s reason=not_found", config_id)
        return False
    _schedule_runtime_event_delivery(
        event_type="device.deconfigured",
        device=removed,
        payload={"serial_number": removed.get("serial_number")},
    )
    logger.info("airthings_config_remove_completed %s", _entry_log_label(removed))
    return True


async def _discover_devices(
    *,
    client_id: str,
    client_secret: str,
    serial_number: str | None = None,
) -> list[dict[str, Any]]:
    credentials = AirthingsCredentials(client_id=client_id, client_secret=client_secret)
    devices = await cloud_client.list_devices(credentials)
    if serial_number:
        devices = [device for device in devices if device.serial_number == serial_number]
    logger.info(
        "airthings_discovery_completed client_id=%s serial_filter=%s discovered=%s",
        client_id,
        serial_number or "none",
        len(devices),
    )
    return [
        device.to_discovery_record(client_id=client_id, client_secret=client_secret)
        for device in devices
    ]


def _raise_http_for_cloud_error(exc: AirthingsCloudError) -> None:
    if isinstance(exc, AirthingsCloudAuthError):
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    if isinstance(exc, AirthingsCloudRateLimitError):
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    if isinstance(exc, AirthingsCloudRequestError):
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    raise exc


async def _extract_discovery_inputs(
    request: Request,
    payload: IntegrationDiscoveryRequest | None,
) -> dict[str, Any]:
    if payload is not None and isinstance(payload.inputs, dict) and payload.inputs:
        return dict(payload.inputs)

    try:
        raw_payload = await request.json()
    except json.JSONDecodeError:
        return {}

    if not isinstance(raw_payload, dict):
        return {}

    raw_inputs = raw_payload.get("inputs")
    if isinstance(raw_inputs, dict):
        return dict(raw_inputs)

    return {
        key: value
        for key, value in raw_payload.items()
        if key not in {"container_id", "driver_pid"}
    }


def _build_entities_payload() -> list[dict[str, Any]]:
    entities: list[dict[str, Any]] = []
    for config_id, entry in registry.entries.items():
        latest_state = registry.state_snapshots.get(config_id, {}).get("state", {})
        sample = None
        if latest_state:
            sample = AirthingsLatestSample(
                serial_number=str(entry["serial_number"]),
                recorded_at=str(latest_state.get("sampled_at") or ""),
                metrics={
                    key: latest_state.get(key)
                    for key in (
                        "radon_short_term_bqm3",
                        "radon_long_term_bqm3",
                        MOLD_RISK_METRIC_KEY,
                        "temperature_c",
                        "humidity_percent",
                        "pressure_hpa",
                        "co2_ppm",
                        "voc_ppb",
                        "pm1_ugm3",
                        "pm25_ugm3",
                        "battery_percent",
                        "rssi_dbm",
                    )
                },
                units={},
                relay_device_type=latest_state.get("relay_device_type"),
                raw=latest_state.get("metadata", {}).get("raw", {}),
            )
        device = AirthingsCloudDevice(
            serial_number=str(entry["serial_number"]),
            name=_entry_name(entry),
            device_type=entry.get("device_model"),
            sensors=list(entry.get("known_sensors") or []),
        )
        entities.append(
            {
                "id": f"device:{config_id}",
                "name": _entry_name(entry),
                "config_id": config_id,
                "device_id": entry["serial_number"],
                "device_class": "air_quality_monitor",
                "entity_type": "sensor",
                "capabilities": capabilities_for_device(device, sample),
                "available_commands": [
                    {
                        "id": "refresh",
                        "label": "Refresh",
                        "description": "Fetch the latest Airthings cloud sample right now.",
                        "kind": "action",
                    }
                ],
                "dashboard": {
                    "allowed_widgets": ["sensor-card", "stat", "line-chart"],
                    "default_widget": "sensor-card",
                    "recommended_widgets": ["sensor-card", "stat"],
                },
                "metadata": {
                    "serial_number": entry["serial_number"],
                    "device_model": entry.get("device_model"),
                },
            }
        )
    return entities


@router.get("/health")
async def health() -> RuntimeHealthResponse:
    return starter.health_response(
        metadata={
            "active_configs": len(registry.ids()),
            "poll_task_count": len(poll_tasks),
        }
    )


@router.get("/diagnostics")
async def diagnostics() -> RuntimeDiagnosticsResponse:
    return starter.diagnostics_response(
        diagnostics={
            "active_config_ids": registry.ids(),
            "recent_event_count": len(registry.recent_events),
            "poll_task_ids": sorted(poll_tasks.keys()),
            "poll_status": poll_status,
            "state_snapshots": registry.state_snapshots,
        }
    )


@router.get("/ui")
@router.get("/ui-config")
async def ui_config() -> dict[str, Any]:
    return {
        "schema": {
            "title": "Airthings Consumer Cloud Setup",
            "description": "Connect PiPhi to an Airthings consumer account device using your Airthings Consumer API client credentials.",
            "type": "object",
            "required": ["client_id", "client_secret", "serial_number"],
            "properties": {
                "client_id": {
                    "type": "string",
                    "title": "Client ID",
                    "description": "Client id from the Airthings dashboard integrations page.",
                },
                "client_secret": {
                    "type": "string",
                    "title": "Client Secret",
                    "description": "Client secret from the Airthings dashboard integrations page.",
                },
                "serial_number": {
                    "type": "string",
                    "title": "Serial Number",
                    "description": "Airthings serial number for the monitor you want PiPhi to track.",
                },
                "device_name": {
                    "type": "string",
                    "title": "Display Name",
                },
                "device_model": {
                    "type": "string",
                    "title": "Device Model",
                },
                "poll_interval_seconds": {
                    "type": "integer",
                    "title": "Poll Interval Seconds",
                    "default": DEFAULT_POLL_INTERVAL_SECONDS,
                    "minimum": 60,
                },
            },
        },
        "uiSchema": {
            "client_secret": {
                "ui:widget": "password",
            },
            "serial_number": {
                "placeholder": "2930046980",
            },
            "poll_interval_seconds": {
                "help": "Airthings cloud samples are typically updated every 5 minutes, so lower values often do not produce fresher data.",
            },
        },
    }


@router.get("/discover", response_model=IntegrationDiscoveryResponse)
@router.post("/discover", response_model=IntegrationDiscoveryResponse)
async def discover(
    request: Request,
    payload: IntegrationDiscoveryRequest | None = None,
) -> IntegrationDiscoveryResponse:
    inputs = await _extract_discovery_inputs(request, payload)
    client_id = str(inputs.get("client_id") or "").strip()
    client_secret = str(inputs.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        raise HTTPException(status_code=400, detail="Discovery requires client_id and client_secret.")
    logger.info(
        "airthings_discovery_started client_id=%s serial_filter=%s",
        client_id,
        str(inputs.get("serial_number") or "").strip() or "none",
    )
    try:
        devices = await _discover_devices(
            client_id=client_id,
            client_secret=client_secret,
            serial_number=str(inputs.get("serial_number") or "").strip() or None,
        )
    except AirthingsCloudError as exc:
        _raise_http_for_cloud_error(exc)
    return build_discovery_response(devices)


@router.post("/config")
async def config(payload: AirthingsCloudConfig, request: Request) -> RuntimeConfigApplyResponse:
    sync_runtime_auth_from_fastapi_payload(runtime, request, payload)
    logger.info("airthings_config_request_received %s", _config_log_label(payload))
    entry = await apply_config(payload)
    return build_config_apply_response(
        config_id=_config_id(payload),
        container_id=entry.get("container_id"),
        metadata={
            "serial_number": entry["serial_number"],
            "device_model": entry.get("device_model"),
        },
    )


async def apply_runtime_config_snapshot(payload: RuntimeConfigSnapshot) -> RuntimeConfigSyncResponse:
    logger.info(
        "airthings_config_sync_started container_id=%s generation=%s incoming_configs=%s reason=%s",
        payload.container_id,
        payload.generation,
        len(payload.configs),
        payload.reason or "unknown",
    )
    typed_snapshot = payload.model_copy(
        update={
            "configs": validate_typed_configs(
                [
                    config.model_dump() if hasattr(config, "model_dump") else config
                    for config in payload.configs
                ],
                AirthingsCloudConfig,
            ),
        }
    )
    result = await config_sync.apply_snapshot(
        snapshot=typed_snapshot,
        active_config_ids=registry.ids(),
        apply_config=apply_config,
        remove_config=remove_config,
        get_active_config_ids=registry.ids,
    )
    logger.info(
        "airthings_config_sync_completed container_id=%s generation=%s applied=%s removed=%s active=%s status=%s",
        payload.container_id,
        payload.generation,
        result.applied,
        result.removed,
        result.active_config_ids,
        result.status,
    )
    return result


@router.post("/configs/sync")
@router.post("/config/sync")
async def configs_sync(payload: RuntimeConfigSnapshot, request: Request) -> RuntimeConfigSyncResponse:
    sync_runtime_auth_from_fastapi_payload(runtime, request, payload)
    return await apply_runtime_config_snapshot(payload)


@router.post("/deconfigure")
async def deconfigure(payload: DeconfigurePayload, request: Request) -> RuntimeConfigRemoveResponse:
    sync_runtime_auth_from_fastapi_payload(runtime, request, payload)
    config_id = str(payload.config.get("config_id") or payload.config.get("id") or "").strip()
    if not config_id:
        raise HTTPException(status_code=400, detail="config_id is required.")
    removed = await remove_config(config_id)
    logger.info("airthings_deconfigure_request_completed config_id=%s removed=%s", config_id, removed)
    return build_config_remove_response(config_id=config_id, removed=removed)


@router.get("/entities")
async def entities() -> dict[str, Any]:
    return starter.entities_response(entities=_build_entities_payload()).model_dump()


@router.get("/state")
async def state() -> dict[str, Any]:
    return {
        "state": registry.state_snapshots,
    }


@router.get("/events", response_model=IntegrationEventListResponse)
async def events() -> IntegrationEventListResponse:
    return build_event_list_response(registry.recent_events)


@router.post("/command")
async def command(payload: IntegrationCommandRequest, request: Request) -> dict[str, Any]:
    sync_runtime_auth_from_fastapi_payload(runtime, request, payload)
    logger.info(
        "airthings_command_received command=%s device_id=%s entity_id=%s args=%s",
        payload.command,
        payload.device_id,
        payload.entity_id,
        payload.args,
    )
    config_id = str(payload.args.get("config_id") or "").strip()
    if not config_id and payload.entity_id and payload.entity_id.startswith("device:"):
        config_id = payload.entity_id.split(":", 1)[1]
    if not config_id and payload.device_id:
        for candidate_id, entry in registry.entries.items():
            if str(entry.get("serial_number")) == str(payload.device_id):
                config_id = candidate_id
                break
    if not config_id:
        raise HTTPException(status_code=400, detail="Command must include config_id, entity_id, or device_id.")
    if payload.command != "refresh":
        raise HTTPException(status_code=400, detail=f"Unsupported command: {payload.command}")
    result = await dispatch_automation_action_from_fastapi(
        automation_registry,
        request,
        {
            **payload.model_dump(mode="python"),
            "config_id": config_id,
        },
    )
    if not result.ok:
        raise HTTPException(
            status_code=int(result.metadata.get("status_code") or 503),
            detail=result.error,
        )
    logger.info(
        "airthings_command_completed command=%s config_id=%s sampled_at=%s",
        payload.command,
        config_id,
        result.result.get("sampled_at"),
    )
    return {**result.result, "replayed": result.replayed}
