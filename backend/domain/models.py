"""
Domain models — framework-independent Pydantic schemas.

These represent the core business objects used across
all backend modules (API, agent, CAD, storage).
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class FailureType(str, enum.Enum):
    SYNTAX_ERROR = "syntax_error"
    EXECUTION_ERROR = "execution_error"
    GEOMETRY_INVALID = "geometry_invalid"
    CONSTRAINT_VIOLATION = "constraint_violation"
    CRITIQUE_FAILED = "critique_failed"
    TIMEOUT = "timeout"


class RepairStage(str, enum.Enum):
    PLANNING = "planning"
    GENERATING = "generating"
    EXECUTING = "executing"
    VALIDATING = "validating"
    TESSELLATING = "tessellating"
    RENDERING = "rendering"
    CRITIQUING = "critiquing"
    REPAIRING = "repairing"
    DONE = "done"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Constraints
# ---------------------------------------------------------------------------

class HardConstraints(BaseModel):
    """Deterministic constraints validated post-generation."""
    max_x_mm: float = 256.0
    max_y_mm: float = 256.0
    max_z_mm: float = 256.0
    min_wall_thickness_mm: float = 1.2
    max_file_size_mb: float = 100.0


class SoftConstraints(BaseModel):
    """Guidelines injected into LLM prompt."""
    overhang_angle_max: Optional[float] = 45.0
    prefer_fillets: bool = True
    prefer_chamfers: bool = False
    material: str = "PLA"
    notes: str = ""


# ---------------------------------------------------------------------------
# Geometry / Artifacts
# ---------------------------------------------------------------------------

class GeometryIssue(BaseModel):
    issue_type: str
    severity: str  # "error", "warning", "info"
    description: str
    location_hint: str = ""


class CritiqueReport(BaseModel):
    issues: list[GeometryIssue] = Field(default_factory=list)
    overall_printability: float = 0.0  # 0.0 - 1.0
    suggested_repairs: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    matches_intent: bool = True
    repair_prompt: str = ""  # actionable repair instructions from vision model


class GeometryStats(BaseModel):
    """Measurements from geometry analysis, injected into critique/repair prompts."""
    bbox_x_mm: Optional[float] = None
    bbox_y_mm: Optional[float] = None
    bbox_z_mm: Optional[float] = None
    volume_mm3: Optional[float] = None
    surface_area_mm2: Optional[float] = None
    solid_count: int = 0
    face_count: int = 0
    edge_count: int = 0
    is_closed: bool = False
    estimated_mass_g: Optional[float] = None


class ModelMetadata(BaseModel):
    """Metadata stored alongside each model revision."""
    model_id: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    prompt: str = ""
    cad_source: str = ""
    has_step: bool = False
    has_stl: bool = False
    has_glb: bool = False
    has_render: bool = False
    render_paths: dict[str, str] = Field(default_factory=dict)  # view_name → file_path
    critique: Optional[CritiqueReport] = None
    geometry_stats: Optional[GeometryStats] = None
    failure_type: Optional[FailureType] = None
    failure_message: str = ""
    iteration: int = 0
    vision_score: Optional[float] = None  # latest vision critique score


class ProjectConfig(BaseModel):
    """Project-level configuration."""
    project_id: str
    name: str = "Untitled Project"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    hard_constraints: HardConstraints = Field(default_factory=HardConstraints)
    soft_constraints: SoftConstraints = Field(default_factory=SoftConstraints)


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

class ChatMessage(BaseModel):
    role: str  # "user" or "assistant"
    content: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    model_id: Optional[str] = None  # linked model if applicable


# ---------------------------------------------------------------------------
# Interaction Context
# ---------------------------------------------------------------------------

class SelectionContext(BaseModel):
    """Context about a selected feature in the viewport."""
    feature_name: str
    point: Optional[list[float]] = None


# ---------------------------------------------------------------------------
# Agent state (for structured pipeline state tracking)
# ---------------------------------------------------------------------------

class AgentState(BaseModel):
    """Structured state for the CAD generation pipeline."""
    user_goal: str = ""
    current_iteration: int = 0
    max_iterations: int = 5
    cad_source: str = ""
    last_error: str = ""
    failure_history: list[dict[str, Any]] = Field(default_factory=list)
    critique_results: list[CritiqueReport] = Field(default_factory=list)
    render_paths: dict[str, str] = Field(default_factory=dict)
    geometry_stats: Optional[GeometryStats] = None
    final_model_id: Optional[str] = None
    success: bool = False


# ---------------------------------------------------------------------------
# WebSocket message types
# ---------------------------------------------------------------------------

class WSStatusMessage(BaseModel):
    type: str = "status"
    stage: str
    message: str


class WSModelReady(BaseModel):
    type: str = "model_ready"
    model_id: str
    glb_url: str


class WSChatChunk(BaseModel):
    type: str = "llm_chunk"
    content: str


class WSChatResponse(BaseModel):
    type: str = "chat_response"
    content: str


class WSError(BaseModel):
    type: str = "error"
    message: str
    failure_type: Optional[str] = None


class WSCritiqueResult(BaseModel):
    type: str = "critique_result"
    issues: list[GeometryIssue] = Field(default_factory=list)
    score: float = 0.0
    matches_intent: bool = True
    repair_prompt: str = ""
    render_urls: dict[str, str] = Field(default_factory=dict)  # view → REST URL
