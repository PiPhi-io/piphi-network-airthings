from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from piphi_runtime_kit_python import runtime_lifespan

from . import runtime as runtime_module


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
        on_shutdown=shutdown_sync,
    ):
        yield
