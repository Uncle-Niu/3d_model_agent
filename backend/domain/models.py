"""
Domain models — framework-independent Pydantic schemas.

These represent the core business objects used across
all backend modules (API, agent, CAD, storage).
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Any, Optional, Union

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

class CadParameter(BaseModel):
    name: str
    value: Union[float, int, str, bool]
    type: str  # 'float', 'int', 'str', 'bool'
    description: Optional[str] = None
    min_value: Optional[float] = None
    max_value: Optional[float] = None

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


class GlobalSettings(BaseModel):
    """Editable global defaults for new projects."""
    hard_constraints: HardConstraints = Field(default_factory=HardConstraints)
    soft_constraints: SoftConstraints = Field(default_factory=SoftConstraints)


# ---------------------------------------------------------------------------
# Geometry / Artifacts
# ---------------------------------------------------------------------------

class GeometryIssue(BaseModel):
    issue_type: str
    severity: str  # "error", "warning", "info"
    description: str
    location_hint: str = ""


class DesignComponent(BaseModel):
    """One sub-shape in the decomposition of a design."""
    name: str
    description: str
    primitive: str = ""  # e.g. "box", "cylinder", "extruded_polygon"
    dimensions: dict[str, float] = Field(default_factory=dict)  # named dims in mm
    position: Optional[list[float]] = None  # [x, y, z] center in mm
    orientation: str = ""  # e.g. "axis=Z" or free-form
    operation: str = ""    # union | cut | intersect | base | pattern | fillet


class DesignPlan(BaseModel):
    """Structured plan produced before code generation.

    The agent uses this as a contract — both the code generator and the vision
    critique receive it so the LLM and the verifier are evaluating against the
    *same* explicit goal rather than just the user's free-form prompt.
    """
    summary: str = ""                      # one-paragraph goal statement
    overall_dimensions_mm: Optional[list[float]] = None  # [x, y, z]
    components: list[DesignComponent] = Field(default_factory=list)
    key_features: list[str] = Field(default_factory=list)   # feature checklist
    assumptions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    parameters: dict[str, float] = Field(default_factory=dict)
    raw_reasoning: str = ""                # the model's free-form thinking
    raw_text: str = ""                     # full raw planner response (for debug)


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
    center_of_mass_x: Optional[float] = None
    center_of_mass_y: Optional[float] = None
    center_of_mass_z: Optional[float] = None
    small_feature_count: int = 0
    tiny_face_count: int = 0
    sharp_corner_count: int = 0
    thin_pin_count: int = 0


class ManufacturabilityIssue(BaseModel):
    issue_type: str  # "thin_wall", "sharp_corner", "thin_pin", etc.
    severity: str    # "error", "warning"
    description: str
    location_hint: str = ""


class ManufacturabilityReport(BaseModel):
    issues: list[ManufacturabilityIssue] = Field(default_factory=list)
    is_printable: bool = True
    score: float = 1.0  # 0.0 - 1.0


class AssemblyPart(BaseModel):
    """Metadata for a single part within an assembly."""
    name: str
    color: Optional[str] = None
    material: Optional[str] = None
    geometry_stats: Optional[GeometryStats] = None
    manufacturability: Optional[ManufacturabilityReport] = None
    visible: bool = True


class AssemblyManifest(BaseModel):
    """Manifest of all parts in a model."""
    parts: list[AssemblyPart] = Field(default_factory=list)
    total_parts: int = 0


class ModelMetadata(BaseModel):
    """Metadata stored alongside each model revision."""
    model_id: str
    parent_model_id: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    prompt: str = ""
    cad_source: str = ""
    has_step: bool = False
    has_stl: bool = False
    has_glb: bool = False
    has_render: bool = False
    render_paths: dict[str, str] = Field(default_factory=dict)  # view_name → file_path
    critique: Optional[CritiqueReport] = None
    geometry_stats: Optional[GeometryStats] = None
    manufacturability: Optional[ManufacturabilityReport] = None
    failure_type: Optional[FailureType] = None
    failure_message: str = ""
    iteration: int = 0
    vision_score: Optional[float] = None  # latest vision critique score
    assembly: Optional[AssemblyManifest] = None
    citations: list[SearchResult] = Field(default_factory=list)
    plan: Optional[DesignPlan] = None  # structured design plan used for this attempt
    # True when this is the final accepted result of a turn. False for
    # in-progress iterations that were either superseded by a later repair
    # attempt or that failed to produce valid geometry.
    is_final: bool = False
    # The chat thread this model was generated for (when known) — lets the UI
    # group versions per turn and lock WIP iterations to their originating turn.
    thread_id: Optional[str] = None
    # The turn (1-based index of user messages in the thread) this model belongs
    # to, when known. Useful for grouping iterations of one turn.
    turn_index: Optional[int] = None


class ProjectConfig(BaseModel):
    """Project-level configuration."""
    project_id: str
    name: str = "Untitled Project"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    hard_constraints: HardConstraints = Field(default_factory=HardConstraints)
    soft_constraints: SoftConstraints = Field(default_factory=SoftConstraints)


class ImportedFile(BaseModel):
    """Metadata for an imported CAD file."""
    import_id: str
    name: str
    filename: str
    extension: str
    size_bytes: int
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    has_glb: bool = False
    glb_url: Optional[str] = None


class ImportResponse(BaseModel):
    success: bool
    message: str
    import_data: Optional[ImportedFile] = None


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

class PipelineStep(BaseModel):
    """A discrete step in the generation/repair pipeline."""
    stage: str
    message: str
    details: Optional[str] = None
    data: Optional[dict[str, Any]] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ChatMessage(BaseModel):
    role: str  # "user" or "assistant"
    content: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    model_id: Optional[str] = None  # linked model if applicable
    steps: list[PipelineStep] = Field(default_factory=list)


class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str
    source: str = "web"


class WebSearchProvider(str, enum.Enum):
    DUCKDUCKGO = "duckduckgo"
    BRAVE = "brave"
    SEARXNG = "searxng"


# ---------------------------------------------------------------------------
# Local-LLM knowledge recall
# ---------------------------------------------------------------------------
#
# Before falling back to a web search, the agent queries several local LLMs
# (different providers, different training corpora) and aggregates their
# answers. The "consensus" of two or more models is treated as a trustworthy
# fact; fields that no two models agree on stay in `uncertain_fields` and are
# what the web search (if any) actually targets.

class FieldValue(BaseModel):
    """A single fact about a subject — what the LLM thinks the value is, plus
    how confident it claims to be. `value` can be a number, string, or list,
    depending on the field. `null` means the model declined to answer."""
    value: Any = None
    confidence: float = 0.0
    note: Optional[str] = None


class ModelRecallResponse(BaseModel):
    """One model's raw answer about a subject. Stored verbatim so we can
    inspect the source of any field in the UI ('which model said the camera
    bump was 38mm?')."""
    model: str
    subject: str
    fields: dict[str, FieldValue] = Field(default_factory=dict)
    latency_s: float = 0.0
    raw_response: str = ""
    error: Optional[str] = None


class KnowledgeConsensus(BaseModel):
    """Aggregated facts about a subject after at least two models agreed
    (or the chain ran out). `fields` is the merged result; uncertain fields
    are listed separately so the orchestrator can decide whether to do a
    web search to fill them."""
    subject: str
    fields: dict[str, FieldValue] = Field(default_factory=dict)
    contributing_models: list[str] = Field(default_factory=list)
    uncertain_fields: list[str] = Field(default_factory=list)
    all_responses: list[ModelRecallResponse] = Field(default_factory=list)

    def is_complete(self, required_fields: list[str], min_ratio: float = 0.7) -> bool:
        """True when enough required fields have consensus values to skip web."""
        if not required_fields:
            return bool(self.fields)
        hits = sum(1 for f in required_fields if f in self.fields and self.fields[f].value is not None)
        return hits / len(required_fields) >= min_ratio


class RecallSubject(BaseModel):
    """A single 'thing' the agent wants to look up. Subjects are identified
    by the planner LLM from the user request — e.g. for "iphone 16 pro max
    holder" the subject is "iPhone 16 Pro Max" and the fields are the
    mechanical specs needed to design a holder for it."""
    subject: str
    fields: list[str] = Field(default_factory=list)
    reasoning: Optional[str] = None


class CadFeature(BaseModel):
    """Represents a specific CAD operation/feature in the source code."""
    id: str
    name: str
    type: str  # "box", "fillet", "hole", etc.
    line_start: int
    line_end: int
    parent_id: Optional[str] = None
    center: Optional[list[float]] = None

class FeatureManifest(BaseModel):
    """Collection of all features and parameters for a model."""
    features: list[CadFeature] = Field(default_factory=list)
    parameters: list[CadParameter] = Field(default_factory=list)

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
    details: Optional[str] = None
    data: Optional[dict[str, Any]] = None


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
    model_id: Optional[str] = None
    steps: list[PipelineStep] = Field(default_factory=list)


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
