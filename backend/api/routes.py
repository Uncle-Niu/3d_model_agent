"""
REST API routes.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import datetime
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..cad.engine import process_cadquery_code
from ..domain.models import (
    FailureType,
    HardConstraints,
    ModelMetadata,
    ProjectConfig,
    SoftConstraints,
)
from ..storage import StorageService

router = APIRouter()


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@router.get("/health")
async def health_check():
    """Check system health: Ollama connectivity, available models."""
    llm_base = os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1")
    llm_model = os.environ.get("LLM_MODEL", "qwen3.6:27b")
    ollama_base = llm_base.replace("/v1", "")

    result = {
        "status": "ok",
        "llm_base_url": llm_base,
        "llm_model": llm_model,
        "ollama_connected": False,
        "available_models": [],
        "model_available": False,
    }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{ollama_base}/api/tags")
            if resp.status_code == 200:
                result["ollama_connected"] = True
                data = resp.json()
                models = [m["name"] for m in data.get("models", [])]
                result["available_models"] = models
                result["model_available"] = llm_model in models
    except Exception as e:
        result["status"] = "degraded"
        result["ollama_error"] = str(e)

    return result

# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class CreateProjectRequest(BaseModel):
    name: str = "Untitled Project"
    hard_constraints: Optional[HardConstraints] = None
    soft_constraints: Optional[SoftConstraints] = None


class UpdateProjectRequest(BaseModel):
    name: Optional[str] = None


class ProjectResponse(BaseModel):
    project_id: str
    name: str
    created_at: datetime
    updated_at: datetime
    project_path: str
    hard_constraints: HardConstraints
    soft_constraints: SoftConstraints


class UpdateConstraintsRequest(BaseModel):
    hard_constraints: Optional[HardConstraints] = None
    soft_constraints: Optional[SoftConstraints] = None


class ModelResponse(BaseModel):
    model_id: str
    created_at: datetime
    prompt: str
    has_step: bool
    has_stl: bool
    has_glb: bool
    iteration: int


class ExecuteSourceRequest(BaseModel):
    source: str
    prompt: str = "Manual source edit"


class ExecuteSourceResponse(BaseModel):
    success: bool
    message: str
    model: ModelResponse
    glb_url: Optional[str] = None
    violations: list[str] = []


class CreateChatThreadRequest(BaseModel):
    title: str = "New chat"


class UpdateChatThreadRequest(BaseModel):
    title: str


class ChatThreadSummaryResponse(BaseModel):
    thread_id: str
    title: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    message_count: int
    last_message: Optional[dict] = None


class ChatThreadResponse(BaseModel):
    thread_id: str
    title: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    messages: list[dict]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_storage(request: Request) -> StorageService:
    return request.app.state.storage


def _generate_project_id() -> str:
    import uuid
    return str(uuid.uuid4())[:8]


def _project_response(storage: StorageService, config: ProjectConfig) -> ProjectResponse:
    return ProjectResponse(
        project_id=config.project_id,
        name=config.name,
        created_at=config.created_at,
        updated_at=config.updated_at,
        project_path=str(storage.get_project_dir(config.project_id).resolve()),
        hard_constraints=config.hard_constraints,
        soft_constraints=config.soft_constraints,
    )


# ---------------------------------------------------------------------------
# Project endpoints
# ---------------------------------------------------------------------------

@router.post("/projects", response_model=ProjectResponse)
async def create_project(body: CreateProjectRequest, request: Request):
    """Create a new project."""
    storage = _get_storage(request)
    project_id = _generate_project_id()

    config = ProjectConfig(
        project_id=project_id,
        name=body.name,
        hard_constraints=body.hard_constraints or HardConstraints(),
        soft_constraints=body.soft_constraints or SoftConstraints(),
    )
    storage.create_project(config)

    return _project_response(storage, config)


@router.get("/projects", response_model=list[ProjectResponse])
async def list_projects(request: Request):
    """List all projects."""
    storage = _get_storage(request)
    projects = storage.list_projects()
    return [
        _project_response(storage, p)
        for p in projects
    ]


@router.get("/projects/{project_id}", response_model=ProjectResponse)
async def get_project(project_id: str, request: Request):
    """Get project details."""
    storage = _get_storage(request)
    config = storage.get_project(project_id)
    if not config:
        raise HTTPException(status_code=404, detail="Project not found")
    return _project_response(storage, config)


@router.put("/projects/{project_id}", response_model=ProjectResponse)
async def update_project(project_id: str, body: UpdateProjectRequest, request: Request):
    """Update project details."""
    storage = _get_storage(request)
    config = storage.get_project(project_id)
    if not config:
        raise HTTPException(status_code=404, detail="Project not found")

    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="Project name cannot be empty")
        config.name = name

    config.updated_at = datetime.utcnow()
    storage.update_project(config)
    return _project_response(storage, config)


@router.delete("/projects/{project_id}")
async def delete_project(project_id: str, request: Request):
    """Delete a project."""
    storage = _get_storage(request)
    config = storage.get_project(project_id)
    if not config:
        raise HTTPException(status_code=404, detail="Project not found")
    storage.delete_project(project_id)
    return {"ok": True}


@router.post("/projects/{project_id}/open_folder")
async def open_project_folder(project_id: str, request: Request):
    """Open the project folder on the local machine running the backend."""
    storage = _get_storage(request)
    config = storage.get_project(project_id)
    if not config:
        raise HTTPException(status_code=404, detail="Project not found")

    project_dir = storage.get_project_dir(project_id).resolve()
    if sys.platform.startswith("win"):
        os.startfile(project_dir)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(project_dir)])
    else:
        subprocess.Popen(["xdg-open", str(project_dir)])

    return {"ok": True, "path": str(project_dir)}


@router.put("/projects/{project_id}/constraints", response_model=ProjectResponse)
async def update_constraints(
    project_id: str, body: UpdateConstraintsRequest, request: Request
):
    """Update project constraints."""
    storage = _get_storage(request)
    config = storage.get_project(project_id)
    if not config:
        raise HTTPException(status_code=404, detail="Project not found")

    if body.hard_constraints:
        config.hard_constraints = body.hard_constraints
    if body.soft_constraints:
        config.soft_constraints = body.soft_constraints
    config.updated_at = datetime.utcnow()

    storage.update_project(config)
    return _project_response(storage, config)


# ---------------------------------------------------------------------------
# Model endpoints
# ---------------------------------------------------------------------------

@router.get("/projects/{project_id}/models", response_model=list[ModelResponse])
async def list_models(project_id: str, request: Request):
    """List all models for a project."""
    storage = _get_storage(request)
    config = storage.get_project(project_id)
    if not config:
        raise HTTPException(status_code=404, detail="Project not found")

    models = storage.list_models(project_id)
    return [
        ModelResponse(
            model_id=m.model_id,
            created_at=m.created_at,
            prompt=m.prompt,
            has_step=m.has_step,
            has_stl=m.has_stl,
            has_glb=m.has_glb,
            iteration=m.iteration,
        )
        for m in models
    ]


@router.get("/projects/{project_id}/models/{model_id}/glb")
async def get_model_glb(project_id: str, model_id: str, request: Request):
    """Download glTF binary for viewport rendering."""
    storage = _get_storage(request)
    file_path = storage.get_model_file_path(project_id, model_id, "model.glb")
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="glTF file not found")
    return FileResponse(
        path=str(file_path),
        media_type="model/gltf-binary",
        filename=f"{model_id}.glb",
    )


@router.get("/projects/{project_id}/models/{model_id}/step")
async def get_model_step(project_id: str, model_id: str, request: Request):
    """Download STEP file."""
    storage = _get_storage(request)
    file_path = storage.get_model_file_path(project_id, model_id, "model.step")
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="STEP file not found")
    return FileResponse(
        path=str(file_path),
        media_type="application/step",
        filename=f"{model_id}.step",
    )


@router.get("/projects/{project_id}/models/{model_id}/stl")
async def get_model_stl(project_id: str, model_id: str, request: Request):
    """Download STL file."""
    storage = _get_storage(request)
    file_path = storage.get_model_file_path(project_id, model_id, "model.stl")
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="STL file not found")
    return FileResponse(
        path=str(file_path),
        media_type="application/sla",
        filename=f"{model_id}.stl",
    )


@router.get("/projects/{project_id}/models/{model_id}/source")
async def get_model_source(project_id: str, model_id: str, request: Request):
    """Get the CadQuery source code for a model."""
    storage = _get_storage(request)
    file_path = storage.get_model_file_path(project_id, model_id, "source.py")
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Source file not found")
    return FileResponse(
        path=str(file_path),
        media_type="text/plain",
        filename=f"{model_id}_source.py",
    )


@router.post("/projects/{project_id}/models/execute_source", response_model=ExecuteSourceResponse)
async def execute_model_source(project_id: str, body: ExecuteSourceRequest, request: Request):
    """Execute edited CadQuery source and save it as a new model checkpoint."""
    storage = _get_storage(request)
    config = storage.get_project(project_id)
    if not config:
        raise HTTPException(status_code=404, detail="Project not found")

    source = body.source.strip()
    if not source:
        raise HTTPException(status_code=400, detail="Source code cannot be empty")

    model_id = storage.next_model_id(project_id)
    model_dir = storage.create_model_dir(project_id, model_id)
    result = await asyncio.get_event_loop().run_in_executor(
        None,
        process_cadquery_code,
        source,
        model_dir,
        "part",
        config.hard_constraints,
    )

    metadata = ModelMetadata(
        model_id=model_id,
        prompt=body.prompt,
        cad_source=source,
        has_step="step" in result.get("files", {}),
        has_stl="stl" in result.get("files", {}),
        has_glb="glb" in result.get("files", {}),
        failure_type=None if result["success"] else FailureType.EXECUTION_ERROR,
        failure_message="" if result["success"] else result["message"],
        iteration=0,
    )
    storage.save_model_metadata(project_id, metadata)
    if "source" not in result.get("files", {}):
        storage.save_model_text(project_id, model_id, "source.py", source)

    model = ModelResponse(
        model_id=metadata.model_id,
        created_at=metadata.created_at,
        prompt=metadata.prompt,
        has_step=metadata.has_step,
        has_stl=metadata.has_stl,
        has_glb=metadata.has_glb,
        iteration=metadata.iteration,
    )
    return ExecuteSourceResponse(
        success=result["success"],
        message=result["message"],
        model=model,
        glb_url=f"/api/projects/{project_id}/models/{model_id}/glb" if metadata.has_glb else None,
        violations=result.get("violations", []),
    )


# ---------------------------------------------------------------------------
# Chat history
# ---------------------------------------------------------------------------

@router.get("/projects/{project_id}/history")
async def get_chat_history(project_id: str, request: Request, thread_id: Optional[str] = None):
    """Get chat history for a project."""
    storage = _get_storage(request)
    config = storage.get_project(project_id)
    if not config:
        raise HTTPException(status_code=404, detail="Project not found")

    history = (
        storage.get_chat_thread_messages(project_id, thread_id)
        if thread_id
        else storage.get_chat_history(project_id)
    )
    return [msg.model_dump(mode="json") for msg in history]


@router.get("/projects/{project_id}/chat_threads", response_model=list[ChatThreadSummaryResponse])
async def list_chat_threads(project_id: str, request: Request):
    """List chat threads for a project."""
    storage = _get_storage(request)
    config = storage.get_project(project_id)
    if not config:
        raise HTTPException(status_code=404, detail="Project not found")
    return storage.list_chat_threads(project_id)


@router.post("/projects/{project_id}/chat_threads", response_model=ChatThreadResponse)
async def create_chat_thread(project_id: str, body: CreateChatThreadRequest, request: Request):
    """Create a chat thread for a project."""
    storage = _get_storage(request)
    config = storage.get_project(project_id)
    if not config:
        raise HTTPException(status_code=404, detail="Project not found")
    return storage.create_chat_thread(project_id, body.title)


@router.get("/projects/{project_id}/chat_threads/{thread_id}", response_model=ChatThreadResponse)
async def get_chat_thread(project_id: str, thread_id: str, request: Request):
    """Get a chat thread with messages."""
    storage = _get_storage(request)
    config = storage.get_project(project_id)
    if not config:
        raise HTTPException(status_code=404, detail="Project not found")
    thread = storage.get_chat_thread(project_id, thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Chat thread not found")
    return thread


@router.put("/projects/{project_id}/chat_threads/{thread_id}", response_model=ChatThreadResponse)
async def update_chat_thread(project_id: str, thread_id: str, body: UpdateChatThreadRequest, request: Request):
    """Rename a chat thread."""
    storage = _get_storage(request)
    config = storage.get_project(project_id)
    if not config:
        raise HTTPException(status_code=404, detail="Project not found")

    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Chat title cannot be empty")

    thread = storage.rename_chat_thread(project_id, thread_id, title)
    if not thread:
        raise HTTPException(status_code=404, detail="Chat thread not found")
    return thread


@router.delete("/projects/{project_id}/chat_threads/{thread_id}")
async def delete_chat_thread(project_id: str, thread_id: str, request: Request):
    """Delete a chat thread."""
    storage = _get_storage(request)
    config = storage.get_project(project_id)
    if not config:
        raise HTTPException(status_code=404, detail="Project not found")
    storage.delete_chat_thread(project_id, thread_id)
    return {"ok": True}
