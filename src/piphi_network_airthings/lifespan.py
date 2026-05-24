from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from piphi_runtime_kit_python import runtime_lifespan

from . import runtime as runtime_module

CORE_REQUEST_TIMEOUT_SECONDS = 10.0


async def startup_sync(_runtime_context, core_http_client) -> None:
    result = await runtime_module.starter.rehydrate_configs(
        client=core_http_client,
        apply_snapshot=runtime_module.apply_runtime_config_snapshot,
        config_model=runtime_module.AirthingsCloudConfig,
        snapshot_model=runtime_module.RuntimeConfigSnapshot,
        timeout_seconds=CORE_REQUEST_TIMEOUT_SECONDS,
    )

    if result.snapshot_applied:
        runtime_module.logger.info(
            "airthings_startup_rehydrate_complete loaded=%s generation=%s source=snapshot",
            result.snapshot_config_count,
            result.snapshot_generation,
        )

    if result.core_applied:
        runtime_module.logger.info(
            "airthings_startup_rehydrate_complete loaded=%s generation=%s source=core",
            result.core_config_count,
            result.core_generation,
        )
        return

    if result.core_error:
        runtime_module.logger.warning(
            "airthings_startup_core_rehydrate_failed error=%s",
            result.core_error,
        )
    elif result.missing_runtime_auth:
        runtime_module.logger.warning(
            "airthings_startup_missing_runtime_credentials standalone_mode=true"
        )
    elif result.core_attempted:
        runtime_module.logger.info("airthings_startup_rehydrate_no_configs")


async def shutdown_sync(_runtime_context) -> None:
    try:
        await runtime_module.cloud_client.aclose()
    finally:
        await runtime_module.reset_runtime_state()


@asynccontextmanager
async def lifespan(app: FastAPI):
    del app
    async with runtime_lifespan(
        runtime_module.runtime,
        on_startup=startup_sync,
        on_shutdown=shutdown_sync,
        core_client_timeout_seconds=CORE_REQUEST_TIMEOUT_SECONDS,
    ):
        yield
