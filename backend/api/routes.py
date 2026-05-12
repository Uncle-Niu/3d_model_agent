"""
REST API routes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..domain.models import (
    HardConstraints,
    ModelMetadata,
    ProjectConfig,
    SoftConstraints,
)
from ..storage import StorageService

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class CreateProjectRequest(BaseModel):
    name: str = "Untitled Project"
    hard_constraints: Optional[HardConstraints] = None
    soft_constraints: Optional[SoftConstraints] = None


class ProjectResponse(BaseModel):
    project_id: str
    name: str
    created_at: datetime
    updated_at: datetime
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_storage(request: Request) -> StorageService:
    return request.app.state.storage


def _generate_project_id() -> str:
    import uuid
    return str(uuid.uuid4())[:8]


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

    return ProjectResponse(
        project_id=config.project_id,
        name=config.name,
        created_at=config.created_at,
        updated_at=config.updated_at,
        hard_constraints=config.hard_constraints,
        soft_constraints=config.soft_constraints,
    )


@router.get("/projects", response_model=list[ProjectResponse])
async def list_projects(request: Request):
    """List all projects."""
    storage = _get_storage(request)
    projects = storage.list_projects()
    return [
        ProjectResponse(
            project_id=p.project_id,
            name=p.name,
            created_at=p.created_at,
            updated_at=p.updated_at,
            hard_constraints=p.hard_constraints,
            soft_constraints=p.soft_constraints,
        )
        for p in projects
    ]


@router.get("/projects/{project_id}", response_model=ProjectResponse)
async def get_project(project_id: str, request: Request):
    """Get project details."""
    storage = _get_storage(request)
    config = storage.get_project(project_id)
    if not config:
        raise HTTPException(status_code=404, detail="Project not found")
    return ProjectResponse(
        project_id=config.project_id,
        name=config.name,
        created_at=config.created_at,
        updated_at=config.updated_at,
        hard_constraints=config.hard_constraints,
        soft_constraints=config.soft_constraints,
    )


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
    return ProjectResponse(
        project_id=config.project_id,
        name=config.name,
        created_at=config.created_at,
        updated_at=config.updated_at,
        hard_constraints=config.hard_constraints,
        soft_constraints=config.soft_constraints,
    )


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


# ---------------------------------------------------------------------------
# Chat history
# ---------------------------------------------------------------------------

@router.get("/projects/{project_id}/history")
async def get_chat_history(project_id: str, request: Request):
    """Get chat history for a project."""
    storage = _get_storage(request)
    config = storage.get_project(project_id)
    if not config:
        raise HTTPException(status_code=404, detail="Project not found")

    history = storage.get_chat_history(project_id)
    return [msg.model_dump(mode="json") for msg in history]
