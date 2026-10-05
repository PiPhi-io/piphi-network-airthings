from __future__ import annotations

import logging
import multiprocessing
import os

from fastapi import FastAPI

from .lifespan import lifespan
from .runtime import router


app = FastAPI(lifespan=lifespan)
app.include_router(router)


def runtime_port_from_environment() -> int:
    raw_port = str(os.getenv("PIPHI_RUNTIME_PORT") or "3669").strip()
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ValueError("PIPHI_RUNTIME_PORT must be an integer between 1 and 65535.") from exc
    if port < 1 or port > 65535:
        raise ValueError("PIPHI_RUNTIME_PORT must be an integer between 1 and 65535.")
    return port


if __name__ == "__main__":
    import uvicorn

    config = {
        "version": 1,
        "formatters": {"default": {"format": "%(asctime)s [%(levelname)s] %(message)s"}},
        "handlers": {"default": {"class": "logging.StreamHandler", "formatter": "default"}},
        "root": {"handlers": ["default"], "level": "INFO"},
    }
    multiprocessing.freeze_support()
    uvicorn.run(app, host="0.0.0.0", port=runtime_port_from_environment(), log_config=config)
