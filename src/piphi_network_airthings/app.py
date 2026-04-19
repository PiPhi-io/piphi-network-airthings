from __future__ import annotations

import logging
import multiprocessing

from fastapi import FastAPI

from .lifespan import lifespan
from .runtime import router


app = FastAPI(lifespan=lifespan)
app.include_router(router)


if __name__ == "__main__":
    import uvicorn

    config = {
        "version": 1,
        "formatters": {"default": {"format": "%(asctime)s [%(levelname)s] %(message)s"}},
        "handlers": {"default": {"class": "logging.StreamHandler", "formatter": "default"}},
        "root": {"handlers": ["default"], "level": "INFO"},
    }
    multiprocessing.freeze_support()
    uvicorn.run(app, host="0.0.0.0", port=3669, log_config=config)
