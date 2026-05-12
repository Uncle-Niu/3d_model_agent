"""
Run the backend server.

Usage:
    python -m backend.run
"""

import os

import uvicorn
from dotenv import load_dotenv

load_dotenv()

if __name__ == "__main__":
    host = os.environ.get("BACKEND_HOST", "0.0.0.0")
    port = int(os.environ.get("BACKEND_PORT", "8000"))

    uvicorn.run(
        "backend.app:app",
        host=host,
        port=port,
        reload=True,
        reload_dirs=["backend"],
    )
