"""ASGI entrypoint module for running the Etsy tool locally."""

from __future__ import annotations

import uvicorn

from app.config import get_settings
from app.main import app


if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run("main:app", host=settings.host, port=settings.port, reload=settings.debug)
