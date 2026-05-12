"""
FastAPI application — main entry point for the backend.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .api.routes import router
from .api.websocket import ws_router
from .storage import StorageService


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup/shutdown."""
    # Initialize storage
    data_root = Path(os.environ.get("CAD_DATA_ROOT", "data"))
    app.state.storage = StorageService(data_root)

    # Initialize LLM service (lazy — only if needed)
    app.state.llm = None

    print(f"✓ CAD Agent backend started (data: {data_root.absolute()})")
    yield
    print("✓ CAD Agent backend shutting down")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="AI-Native CAD Agent",
        description="Local-first AI-powered CAD and 3D-printing system",
        version="0.1.0",
        lifespan=lifespan,
    )

    # CORS — allow frontend dev server
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # API routes
    app.include_router(router, prefix="/api")
    app.include_router(ws_router)

    # Serve data files (glTF, STEP, STL, etc.)
    data_root = Path(os.environ.get("CAD_DATA_ROOT", "data"))
    data_root.mkdir(parents=True, exist_ok=True)
    app.mount("/data", StaticFiles(directory=str(data_root)), name="data")

    return app


app = create_app()
