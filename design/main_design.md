# AI-Native CAD Agent System (Local-First)

## Overview

Build a local-first AI-powered CAD and 3D-printing system focused on:

- Parametric CAD generation
- Engineering-grade geometry
- AI-assisted iterative design
- Browser-based interaction
- Manufacturable 3D-printable outputs
- Hybrid local + cloud AI routing
- Geometry-aware feedback loops

The system should prioritize:

- Functional parts
- Parametric modeling
- Editable geometry
- STEP-based workflows
- Manufacturable outputs

The system should NOT depend on Blender.

Blender should be completely avoided initially.

This is NOT a DCC/art pipeline.

This is an AI-native CAD + 3D printing pipeline.

---

# Primary Goals

## Must Support

- Browser-based 3D viewport
- AI chat interaction
- Parametric CAD generation
- STEP export
- STL export for 3D printing
- Bambu Studio compatibility
- Local deployment
- Hybrid local/cloud AI models
- Automatic repair/refinement loops
- Geometry-aware workflows

---

# Important Architectural Principles

## 1. CAD-First, NOT Mesh-First

Do NOT use mesh generation as the primary representation.

Primary representations should be:

```text
CadQuery Source
    ↓
OpenCascade Geometry
    ↓
STEP
    ↓
STL (derived only)
```

STL should only exist as a final export format for slicing/printing.

---

## 2. Preserve Parametric Geometry

Always preserve:

- Parametric source
- Feature hierarchy
- Editable geometry
- Engineering semantics

Avoid irreversible mesh workflows.

---

## 3. STEP is the Core Asset

Use STEP as the canonical geometry format.

Reasons:

- Editable
- Engineering-grade
- Topology-aware
- Manufacturable
- Better AI reasoning
- Supports future feature editing

STL is only for printing.

---

## 4. Avoid Blender Completely

Do NOT use Blender for:

- Geometry generation
- Rendering
- CAD workflows
- Agent execution

Reasons:

- Too heavy
- Not CAD-first
- Poor topology semantics
- Overkill for engineering workflows
- Difficult automation
- Unnecessary complexity

---

## 5. Single-User, Local-First

The system is designed for single-user local deployment:

- No concurrent user support required
- No authentication or authorization
- Session is tied to WebSocket connection
- If server restarts, frontend reconnects and reloads project state from filesystem

---

# System Architecture

```text
Frontend (Browser)
    ↓
FastAPI Backend
    ↓
LangGraph Orchestrator
    ↓
CAD / AI Pipeline
    ├── Local Models
    ├── Cloud Models
    ├── CadQuery
    ├── OpenCascade
    ├── Rendering Service
    ├── Vision Critique
    └── Repair Loop
```

---

# Frontend Architecture

## Stack

- React
- TypeScript
- Vite
- Three.js
- React Three Fiber
- TailwindCSS
- Zustand

---

# Frontend Responsibilities

## 1. AI Chat Interface

Main interaction surface.

Should support:

- Natural language prompts
- Streaming responses
- Intermediate execution updates (stage indicators)
- Tool execution logs
- Critique feedback
- Repair explanations

---

## 2. Browser-Based 3D Viewport

This replaces any Blender dependency.

The viewport should support:

- Real-time model viewing
- Orbit controls
- Pan/zoom
- Wireframe mode
- Face highlighting
- Bounding boxes
- Measurement tools
- Section cuts
- Multiple camera views

Future support:

- Exploded views
- Assembly inspection
- Feature highlighting

---

## 3. Geometry Interaction

The user should be able to interact directly with geometry.

### Phase 1 (MVP): Assembly-Level Selection

Use CadQuery's Assembly hierarchy for selection.

Each logical feature is exported as a named Assembly child:

```python
assy = cq.Assembly()
assy.add(body, name="body")
assy.add(hole_1, name="hole_1")
assy.add(fillet_edge, name="fillet_top")
```

Three.js loads glTF and preserves the scene graph names:

```javascript
gltf.scene.traverse((child) => {
  if (child.isMesh) {
    child.userData.cadName = child.name; // "hole_1"
  }
});
```

On click, Raycaster finds the mesh and sends the feature name to the backend:

```json
{ "type": "selection", "feature_name": "hole_1", "point": [10, 5, 0] }
```

Backend resolves name and provides feature context to the LLM.

### Phase 2 (Future): Per-Face Topology Mapping

Build a custom tessellation pipeline that tracks which triangles belong to which OCCT TopoDS_Face.

Embed face IDs as glTF mesh groups.

Frontend maps clicked triangle back to a face ID.

Backend resolves face ID to OCCT topology and extracts metadata (face type, area, normal, neighbors).

This is significant engineering work and is deferred to Phase 2.

### Example Workflow

```text
User selects feature (e.g. hole_1)
    ↓
User prompt:
"Increase this hole diameter by 2mm"
    ↓
Agent receives:
- feature name
- feature metadata
- geometry context
- current CadQuery source
```

---

## 4. Design History

Maintain revision history:

- Prompt history
- CAD source revisions
- STEP revisions
- Render snapshots
- Critique reports

---

## 5. Project Settings & Global Defaults

Editable project settings and constraints displayed in UI.

### Three-Layer Configuration Model

1. **Project Level**: Constraints specific to the current project.
2. **Editable Global Defaults**: Saved fallback constraints for any new projects created. Users can save a project's constraints as the global defaults or reset their project to match these global defaults.
3. **Hardcoded Original Defaults**: The system's factory settings defined in the domain models. The editable global defaults can be reset back to these original values.

### Constraint Types

**Hard Constraints (Deterministic, Validated Post-Generation)**

Configured in UI, enforced by code after CAD generation:

- Print volume dimensions (default: 256 x 256 x 256 mm for Bambu A1)
- Minimum wall thickness (default: 1.2 mm for FDM)
- Maximum file size

```python
class HardConstraints(BaseModel):
    max_x_mm: float = 256.0
    max_y_mm: float = 256.0
    max_z_mm: float = 256.0
    min_wall_thickness_mm: float = 1.2
    max_file_size_mb: float = 100.0
```

Hard constraint violations reject geometry and trigger a repair iteration.

### Soft Constraints (Injected into LLM Prompt)

Passed to the LLM as guidelines, checked by vision critique:

- Overhang angle preferences
- Aesthetic preferences (fillets, chamfers)
- Material assumptions
- Strength requirements
- Tolerances

### Validation Responsibility

| Constraint Type | Validator |
|---|---|
| Build volume | Deterministic (bounding box check) |
| Wall thickness | Deterministic (OCCT distance check) |
| Overhang angle | Hybrid — deterministic + vision critique |
| Aesthetic / intent | Vision critique (LLM) |

---

# Frontend Rendering Pipeline

## DO NOT Render STEP Directly in Browser

### Tessellation Strategy: CadQuery Assembly Export

Use CadQuery's built-in `Assembly.export()` to convert STEP/BREP geometry to glTF binary (`.glb`).

Pipeline:

```text
CadQuery Shape
    ↓
Wrap in cq.Assembly (named parts)
    ↓
Assembly.export("model.glb", tolerance=0.01, angularTolerance=0.1)
    ↓
Serve .glb via REST endpoint
    ↓
Three.js GLTFLoader → viewport
```

### Tessellation Parameters

| Parameter | Default | Recommended | High Quality |
|-----------|---------|-------------|--------------|
| `tolerance` | 0.001 | 0.01 | 0.001 |
| `angularTolerance` | 0.1 | 0.1 | 0.05 |

- `tolerance=0.01` is good for most 3D-print-sized parts — fast and visually smooth
- For fine detail zoom, re-tessellate on demand with `tolerance=0.001`

### Tessellation Example

```python
import cadquery as cq

def shape_to_glb(shape, name="part", tolerance=0.01, angular_tolerance=0.1) -> str:
    """Convert CadQuery shape to glTF binary file."""
    assy = cq.Assembly()
    assy.add(shape, name=name, color=cq.Color("steelblue"))
    output_path = f"output/{name}.glb"
    assy.export(output_path, tolerance=tolerance, angularTolerance=angular_tolerance)
    return output_path
```

### Latency Target

Less than 2 seconds for typical 3D-print-sized parts.

### Alternative Tessellation Options (If Needed)

| Option | Use Case |
|---|---|
| OCP `RWGltf_CafWriter` | Full control, preserves XCAF metadata |
| `cascadio` library | Lightweight STEP → GLB conversion |

---

# Recommended Frontend Geometry Formats

| Format | Purpose |
|---|---|
| glTF (.glb) | Frontend rendering |
| STEP | Canonical geometry |
| STL | 3D printing export |

---

# Backend Architecture

## Stack

- Python 3.12+
- FastAPI
- LangGraph
- Pydantic
- CadQuery
- OpenCascade
- uvicorn

---

# Backend Modules

## 1. API Layer

### Communication Protocol

Use REST for data operations and WebSocket for real-time streaming.

#### REST Endpoints

```text
POST   /api/projects                        # Create project
GET    /api/projects                        # List projects
GET    /api/projects/{id}                   # Get project details

POST   /api/projects/{id}/chat              # Send chat message (returns WS channel)
GET    /api/projects/{id}/models            # List models in project
GET    /api/projects/{id}/models/{id}/glb   # Download glTF for viewport
GET    /api/projects/{id}/models/{id}/step  # Download STEP
GET    /api/projects/{id}/models/{id}/stl   # Download STL

PUT    /api/projects/{id}/constraints       # Update constraints
GET    /api/projects/{id}/history           # Get revision history
```

#### WebSocket Messages

Client to server:

```json
{ "type": "chat_message", "content": "Make a box with rounded edges" }
{ "type": "selection", "feature_name": "hole_1", "point": [10, 5, 0] }
```

Server to client:

```json
{ "type": "status", "stage": "planning", "message": "Understanding request..." }
{ "type": "status", "stage": "generating", "message": "Writing CadQuery code..." }
{ "type": "llm_chunk", "content": "partial response text..." }
{ "type": "status", "stage": "executing", "message": "Running CadQuery..." }
{ "type": "status", "stage": "tessellating", "message": "Converting to 3D preview..." }
{ "type": "model_ready", "model_id": "abc123", "glb_url": "/api/projects/.../glb" }
{ "type": "status", "stage": "critiquing", "message": "Analyzing geometry..." }
{ "type": "critique_result", "issues": [...], "score": 0.85 }
{ "type": "chat_response", "content": "Here's your box with 2mm fillets..." }
{ "type": "error", "message": "...", "failure_type": "execution_error" }
```

#### glTF Delivery Flow

1. Backend generates `.glb` file, saves to project directory
2. Sends `model_ready` WebSocket message with REST download URL
3. Frontend fetches `.glb` via REST GET and loads into Three.js viewport

### Directory

```text
/backend/api
```

---

## 2. Agent Orchestrator

Use LangGraph ONLY for:

- Workflow orchestration
- Retry logic
- State transitions
- Checkpointing

Avoid coupling domain logic to LangGraph internals.

### Directory

```text
/backend/agent
```

---

## 3. Domain Models

Framework-independent business/domain objects.

### Examples

```python
Part
Assembly
Constraint
GeometryArtifact
RenderArtifact
CritiqueResult
RepairTask
ToolResult
```

### Directory

```text
/backend/domain
```

---

## 4. CAD Engine

## Primary Technologies

- CadQuery
- OpenCascade (OCCT)

### Responsibilities

- Parametric CAD generation
- Boolean operations
- Fillets/chamfers
- STEP export
- STL export
- Assembly generation
- glTF tessellation

### Directory

```text
/backend/cad
```

---

## 5. CadQuery Code Generation Strategy

### Prompt Architecture

The system prompt sent to the LLM includes three key sections:

1. **CadQuery API reference** — curated subset of the most-used methods (not the full docs). Sourced from CadQuery documentation.
2. **Example library** — 10-15 curated CadQuery examples covering common patterns. These act as few-shot examples that teach the LLM the correct syntax.
3. **Output format spec** — LLM outputs only valid CadQuery Python, assigns result to a variable named `result`.

### System Prompt Structure

```text
You are a CAD engineer. Generate CadQuery Python code for the user's request.

## Rules
- Output ONLY valid CadQuery Python code
- Assign the final shape to a variable called `result`
- Use metric units (mm)
- Apply fillets/chamfers where appropriate for 3D printing
- Consider wall thickness minimum of 1.2mm for FDM printing

## CadQuery API Quick Reference
[curated API subset here]

## Examples
[10-15 working examples here]

## User Constraints
[injected from constraint panel]
```

### Example Library

Maintain a library of curated, working CadQuery scripts in `/backend/cad/examples/`.

Example categories:

- Basic primitives (box, cylinder, sphere)
- Enclosures with wall thickness
- Brackets and mounts
- Parts with holes, fillets, chamfers
- Snap-fit features
- Threaded holes
- Multi-body assemblies

Source examples from CadQuery documentation and tested custom scripts.

### Code Validation (Before Execution)

Validate generated code with AST analysis before execution:

```python
import ast

def validate_cadquery_code(code: str) -> tuple[bool, str]:
    """Basic safety and syntax checks before execution."""
    # 1. Syntax check
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, f"Syntax error: {e}"

    # 2. Check for dangerous imports/calls
    forbidden = {"subprocess", "shutil", "pathlib", "socket", "http", "urllib", "os", "sys"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in forbidden:
                    return False, f"Forbidden import: {alias.name}"
        if isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] in forbidden:
                return False, f"Forbidden import: {node.module}"

    # 3. Check `result` variable exists
    assigns = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)]
    has_result = any(
        isinstance(t, ast.Name) and t.id == "result"
        for a in assigns for t in a.targets
    )
    if not has_result:
        return False, "Code must assign to 'result' variable"

    return True, "OK"
```

### Execution Sandboxing

Execute generated CadQuery code in a restricted Python namespace.

Allow CadQuery and math operations. Block filesystem, network, and system access.

```python
import cadquery as cq
import math

SAFE_GLOBALS = {
    "__builtins__": {
        "range": range, "len": len, "int": int, "float": float,
        "str": str, "list": list, "dict": dict, "tuple": tuple,
        "abs": abs, "min": min, "max": max, "round": round,
        "enumerate": enumerate, "zip": zip, "map": map,
        "True": True, "False": False, "None": None,
        "print": print,
    },
    "cq": cq,
    "math": math,
}

def execute_cadquery(code: str, timeout: int = 30):
    """Execute CadQuery code in restricted namespace."""
    local_vars = {}
    exec(code, SAFE_GLOBALS, local_vars)
    return local_vars.get("result")
```

This is pragmatic for local-first single-user. No `os`, `subprocess`, `open`, or other dangerous builtins are available.

### Directory

```text
/backend/cad/examples
```

---

## 6. Geometry Validation

Validate geometry deterministically after CAD execution, before rendering or vision critique.

### Validation Checks

```text
CadQuery Source
    ↓
Execute (produce OCCT shape)
    ↓
Validate (manifold? closed? within print volume? min wall thickness?)
    ↓
Tessellate → glTF
    ↓
Render → Vision Critique
```

### Validation Responsibilities

- Shape is valid (not null, not empty)
- Shape is manifold and closed (watertight)
- Bounding box within hard constraint limits
- Minimum wall thickness check (where deterministic check is feasible)

Hard constraint violations trigger an automatic repair iteration.

### Directory

```text
/backend/validation
```

---

# OpenCascade Responsibilities

OpenCascade is the core geometry kernel.

Responsibilities include:

- BREP topology
- NURBS geometry
- Boolean operations
- STEP support
- Engineering geometry semantics
- Topology-aware operations

Critical for:

- Feature editing
- Geometry reasoning
- Face/edge selection
- Manufacturable CAD workflows

---

# Rendering Service

Avoid Blender entirely.

Recommended rendering approaches (current pipeline uses VTK, with fallback chain):

1. **VTK** — primary. Offscreen window, proper Z-buffer, multi-sampled AA.
   Renders solid opaque parts with feature-edge overlay (>15° crease angle).
2. **pyrender** — secondary, if installed and OSMesa/EGL is available.
3. **matplotlib 3D** — emergency fallback only. Painter's-algorithm depth sort
   produces visible artifacts on CAD meshes; the VLM should NOT see this if VTK
   is available.

Every render is post-processed with PIL annotations:

- View label (ISO / FRONT / RIGHT / TOP / SECTION_X / SECTION_Y) — top-left
- Real-world bounding box in mm — top-right
- Axis triad (+X red, +Y green, +Z blue) — bottom-left, view-correct
- ~10mm scale bar — bottom-right

These overlays let a multimodal LLM read scale, orientation, and view identity
without doing pixel measurement.

### Responsibilities

Generate:

- Perspective renders
- Orthographic renders
- Section cuts
- Technical previews
- Geometry snapshots

### Directory

```text
/backend/render
```

---

# Vision Critique System

Uses multimodal AI models to analyze rendered geometry.

### Responsibilities

Evaluate:

- Printability
- Thin walls
- Unsupported overhangs
- Structural issues
- Symmetry
- Constraint satisfaction
- Manufacturability

### Critique Report Schema

```python
class GeometryIssue(BaseModel):
    issue_type: str          # "thin_wall", "overhang", "non_manifold", etc.
    severity: str            # "error", "warning", "info"
    description: str
    location_hint: str       # approximate location description

class CritiqueReport(BaseModel):
    issues: list[GeometryIssue]
    overall_printability: float  # 0.0 - 1.0
    suggested_repairs: list[str]
    confidence: float            # 0.0 - 1.0
```

### Directory

```text
/backend/vision
```

---

# Automatic Repair Loop

Core AI workflow (current, post-2026-05-14):

```text
Plan (decompose intent → DesignPlan: components, dims, key-features checklist)
    ↓
Generate CAD (plan injected into prompt; named parameters required)
    ↓
Execute
    ↓
Validate (deterministic geometry checks)
    ↓
Tessellate → glTF
    ↓
Render (VTK offscreen, multi-angle, annotated)
    ↓
Vision Critique (uses the plan's key-features checklist as ground truth)
    ↓
Repair Geometry (triggered by low score OR matches_intent=false
                 OR any missing checklist feature)
    ↓
Repeat (plan is preserved across iterations)
```

The **plan is the contract** that the generator and the verifier share. A vision
verifier that doesn't know what to look for is mostly useless; giving it the
same explicit feature checklist the generator was given turns it into a real
acceptance test. The plan also makes the agent's reasoning legible — it streams
to the UI as `reasoning_chunk` events before code generation begins.

For thinking-mode models (qwen3.x) the planner allows thinking and surfaces it
as visible reasoning; the code generator appends `/no_think` so the token
budget is spent on code, not internal monolog.

## Failure Model

### Iteration Limits

```python
MAX_REPAIR_ITERATIONS = 5
LOCAL_MODEL_RETRIES = 3      # retries with local model before escalating
EXECUTION_TIMEOUT = 30       # seconds per CadQuery execution
TESSELLATION_TIMEOUT = 10    # seconds per tessellation
```

### Failure Taxonomy

| Failure Type | Example | Recovery |
|---|---|---|
| `syntax_error` | Invalid Python/CadQuery code | Re-generate with error message in prompt |
| `execution_error` | OCCT kernel exception, runtime error | Re-generate with traceback context |
| `geometry_invalid` | Non-manifold, open shell, zero volume | Repair with specific geometry fix prompt |
| `constraint_violation` | Exceeds print volume, wall too thin | Re-generate with constraint reminder |
| `critique_failed` | Vision model finds structural issues | Targeted repair based on critique report |
| `timeout` | Execution or tessellation timeout | Simplify geometry prompt, retry |

### Escalation Flow

```text
Attempt 1-3: Local model generates/repairs
    ↓ (still failing)
Attempt 4-5: Escalate to cloud model (Claude/GPT) with full failure history
    ↓ (still failing)
Surface to user: Show last best result + failure summary + ask for guidance
```

### Partial Success Handling

If geometry is valid but doesn't fully match user intent:

- Show the result in viewport with a warning banner
- Display critique report inline in chat
- Let user decide: accept, modify prompt, or retry

---

# AI Model Architecture

## Hybrid Local + Cloud Models

The system should support:

- Local models
- Cloud models
- OpenAI-compatible APIs
- Dynamic routing

---

# Local Models

Run via:

- Ollama
- vLLM
- llama.cpp

### Recommended Models

- qwen3.6:27b

### Responsibilities

- Basic CAD generation
- Edits
- Retry fixes
- Tool calling
- Formatting

---

# Cloud Models

Examples:

- GPT-5
- Claude Opus
- Gemini

### Responsibilities

- Difficult reasoning
- Complex geometry repair
- Vision critique
- Advanced planning
- Failure escalation (after local model retries exhausted)

---

# Model Routing Layer

Implement dynamic model routing.

Example routing:

| Task | Model |
|---|---|
| Simple edit | Local model |
| CAD generation | Local model |
| Vision critique | Gemini |
| Complex repair | Claude/GPT |
| Failure escalation | Cloud model |

### Directory

```text
/backend/models
```

---

# LangGraph Workflow

## Initial Architecture

Start with a single-agent workflow.

Do NOT start with multi-agent complexity.

Recommended workflow:

```text
Planner
    ↓
CAD Generator
    ↓
Executor
    ↓
Geometry Validator
    ↓
Tessellator
    ↓
Renderer
    ↓
Vision Critic
    ↓
Repair Loop (max 5 iterations)
```

---

# Persistent Structured State

Do NOT rely on raw chat history.

Use structured state objects.

Example:

```python
class AgentState(BaseModel):
    user_goal: str
    constraints: HardConstraints
    soft_constraints: list[str]
    cad_source: str
    geometry_artifacts: list[str]
    render_artifacts: list[str]
    critique_results: list[CritiqueReport]
    failure_history: list[dict]
    current_iteration: int
    max_iterations: int = 5
```

---

# Storage

## Filesystem-Based, Per-Project

No database required. Pure filesystem with JSON metadata files.

### Directory Layout

```text
data/
├── projects/
│   ├── project-abc/
│   │   ├── project.json           # Project metadata, constraints, settings
│   │   ├── models/
│   │   │   ├── model-001/
│   │   │   │   ├── source.py      # CadQuery source code
│   │   │   │   ├── model.step     # STEP file
│   │   │   │   ├── model.stl      # STL export
│   │   │   │   ├── model.glb      # glTF preview for viewport
│   │   │   │   ├── render.png     # Server-side render for critique
│   │   │   │   └── metadata.json  # Timestamps, prompt, critique results
│   │   │   ├── model-002/
│   │   │   │   └── ...
│   │   │   └── model-003/
│   │   │       └── ...
│   │   └── chat_history.json      # Conversation log
│   │
│   └── project-def/
│       └── ...
```

### Storage Rules

- Each project has its own directory
- Each generation/revision creates a new model directory (sequential: model-001, model-002, ...)
- Each project can have multiple model files
- UI can choose which model to render (defaults to latest)
- Project metadata stored in `project.json`
- Model metadata stored in `metadata.json`
- No database — purely filesystem-based
- Cleanup is manual for now

## Persist

- CadQuery source
- STEP files
- STL exports
- glTF previews
- Render images
- Critique reports
- Agent state snapshots

---

# Recommended Project Structure

```text
project/
├── frontend/
│   ├── src/
│   │   ├── components/
│   │   ├── viewport/
│   │   ├── chat/
│   │   ├── constraints/
│   │   ├── history/
│   │   ├── hooks/         # React hooks (WebSocket, geometry state)
│   │   ├── stores/        # Zustand stores
│   │   └── types/         # TypeScript types
│
├── backend/
│   ├── api/
│   ├── agent/
│   ├── domain/
│   ├── cad/
│   │   └── examples/      # Curated CadQuery example scripts
│   ├── render/
│   ├── vision/
│   ├── repair/
│   ├── models/
│   ├── validation/        # Geometry validation layer
│   ├── tessellation/      # STEP → glTF conversion
│   ├── storage/
│   └── config/            # Model routing config, constraint defaults
│
├── data/                  # Runtime data (projects, artifacts)
│
├── examples/
│
└── docker/
```

---

# 3D Printing Workflow

Final export workflow:

```text
CadQuery
    ↓
OpenCascade
    ↓
STEP
    ↓
STL Export
    ↓
Bambu Studio
    ↓
3D Printing
```

The generated STL files must be compatible with:

- Bambu Studio
- PrusaSlicer
- OrcaSlicer

Single-color STL export only for initial version.

---

# Zoo Gap Closure Plan

This section closes the main product gaps compared with Zoo Design Studio / Zookeeper while preserving the local-first CAD-agent architecture.

Explicitly out of scope for this section:

- Public API / SDK ecosystem
- Enterprise / account / organization polish

## 1. Feature Tree and Editable Parameters

### Goal

Expose generated CAD as an editable engineering model, not only as source code and exported files.

### Approach

Use a lightweight, source-adjacent feature manifest generated alongside CadQuery code.

Each successful model revision should persist:

```text
model-001/
    source.py
    model.step
    model.stl
    model.glb
    feature_manifest.json
    parameters.json
    metadata.json
```

### Feature Manifest Schema

```python
class CadParameter(BaseModel):
    name: str
    value: float | str | bool
    unit: str = "mm"
    description: str = ""
    min_value: float | None = None
    max_value: float | None = None


class CadFeature(BaseModel):
    feature_id: str
    name: str
    feature_type: str  # box, cylinder, hole, fillet, chamfer, shell, pattern, assembly_part
    parameters: list[str] = []
    parent_ids: list[str] = []
    cad_name: str = ""  # name exported into Assembly / glTF scene graph
    source_span: tuple[int, int] | None = None
    description: str = ""


class FeatureManifest(BaseModel):
    model_id: str
    parameters: list[CadParameter]
    features: list[CadFeature]
    root_feature_ids: list[str]
```

### Code Generation Requirement

The LLM must generate code with named top-level parameters:

```python
import cadquery as cq

length = 80
width = 40
height = 12
hole_diameter = 5
fillet_radius = 2

base = cq.Workplane("XY").box(length, width, height)
result = base.edges("|Z").fillet(fillet_radius)
```

The system should either:

1. Ask the LLM to return `source.py` plus `feature_manifest.json`, then validate both.
2. Or derive the manifest using AST parsing for simple parameter assignments and known CadQuery call chains.

Prefer option 1 initially because it is easier to implement and works with local models.

### UI

Add a left or right panel:

- Feature tree
- Parameter table
- Regenerate button after parameter edits
- Highlight feature in viewport when selected in tree
- Show source span for advanced users

Parameter edits should rewrite only literal assignments in `source.py`, execute the edited source, and save a new checkpoint.

## 2. Selection-Aware Agent

### Goal

Allow prompts like:

```text
Make this hole 2mm wider.
Chamfer this edge.
Move this mounting tab to the left.
```

### Phase 1: Assembly-Level Selection

Use named CadQuery `Assembly` children and preserve names through GLB export.

Frontend:

- Add Three.js raycasting in the viewport.
- On mesh click, read `mesh.name` and `mesh.userData.cadName`.
- Store active selection in Zustand.
- Visually highlight selected mesh.
- Send selection context with chat messages.

WebSocket message:

```json
{
  "type": "chat_message",
  "content": "Increase this hole diameter by 2mm",
  "selection": {
    "model_id": "model-004",
    "cad_name": "hole_1",
    "feature_id": "feature-hole-1",
    "point": [10.0, 5.0, 2.5]
  }
}
```

Backend:

- Resolve `feature_id` against `feature_manifest.json`.
- Inject selected feature metadata and current source into the edit prompt.
- Require the LLM to preserve all unrelated parameters and features.

### Phase 2: Topology-Level Selection

Add face/edge IDs later using a custom tessellation path from OpenCascade topology to glTF groups.

Deferred because it requires reliable mapping:

```text
TopoDS_Face / TopoDS_Edge -> tessellated triangles -> glTF primitive/group -> frontend raycast hit
```

## 3. Model-Derived Analysis Tools

### Goal

Give the agent deterministic geometry facts instead of asking the model to infer everything from source or screenshots.

### Initial Analysis Tools

Implement in `backend/validation` or `backend/analysis`:

- Bounding box dimensions
- Volume
- Surface area
- Center of mass
- Body count
- Face count / edge count
- Solid validity
- Watertightness / closed-shell check where available
- Minimum and maximum model dimensions
- Estimated material volume and print weight

Persist:

```python
class GeometryStats(BaseModel):
    bbox_x_mm: float
    bbox_y_mm: float
    bbox_z_mm: float
    volume_mm3: float
    surface_area_mm2: float
    center_of_mass: tuple[float, float, float]
    solid_count: int
    face_count: int
    edge_count: int
    estimated_mass_g: float | None = None
```

Save as:

```text
model-001/analysis.json
```

### Agent Usage

Before repair or critique, inject compact analysis:

```text
Geometry analysis:
- Bounding box: 80 x 40 x 12 mm
- Volume: 18400 mm3
- Center of mass: [0, 0, 0]
- Solid count: 1
- Constraint violations: none
```

## 4. Manufacturability and Design Review

### Goal

Move beyond "code ran successfully" to "part is likely printable and mechanically sane."

### Validation Layers

Use three layers, in order:

```text
CadQuery Source
    -> Execute
    -> Deterministic geometry validation
    -> Manufacturability heuristics
    -> Vision critique from rendered views
    -> Repair prompt if needed
```

### Deterministic Checks

Required checks:

- Build volume
- Solid count greater than zero
- Closed/watertight solid where OCCT exposes this reliably
- Degenerate dimensions
- Very small features below nozzle/tolerance threshold
- File size

Best-effort checks:

- Minimum wall thickness
- Unsupported overhangs
- Tiny isolated faces
- Very sharp internal corners
- Hole diameters below configured minimum
- Thin pins/tabs likely to break

### Wall Thickness Strategy

Start with approximate checks:

1. Sample points on the mesh or OCCT faces.
2. Cast inward/outward distance probes where feasible.
3. Flag regions below `min_wall_thickness_mm`.
4. Store approximate location hints.

This is acceptable for MVP because exact wall-thickness analysis is difficult and can be improved later.

### Overhang Strategy

For FDM printability:

- Assume a default print direction: positive Z.
- Compute face normals from tessellated mesh.
- Flag downward-facing surfaces whose angle exceeds `overhang_angle_max`.
- Ignore tiny faces under an area threshold.

### Report Schema

```python
class ManufacturabilityIssue(BaseModel):
    issue_type: str  # thin_wall, overhang, tiny_feature, non_manifold, weak_tab
    severity: str    # error, warning, info
    feature_id: str | None = None
    location_hint: str = ""
    measured_value: float | None = None
    threshold: float | None = None
    suggested_fix: str = ""


class ManufacturabilityReport(BaseModel):
    is_printable: bool
    score: float
    issues: list[ManufacturabilityIssue]
    assumptions: list[str]
```

Persist as:

```text
model-001/manufacturability.json
```

## 5. Vision-Based Validation

### Goal

Use multimodal critique as a second opinion after deterministic checks.

Vision is useful for:

- Overall shape sanity
- Missing requested features
- Obvious unsupported spans
- Symmetry mistakes
- Visual mismatch with user intent
- Detecting when generated geometry is valid but semantically wrong

Vision should not be the only validator for:

- Exact dimensions
- Minimum wall thickness
- Mechanical strength
- Watertightness

### Rendering Inputs

Generate server-side PNG renders for each successful model:

```text
model-001/renders/
    iso.png
    front.png
    right.png
    top.png
    section_x.png      # optional
    section_y.png      # optional
```

Use one of:

- Headless Three.js / Playwright screenshot
- VTK
- pygfx
- OpenCascade offscreen renderer

Initial recommendation: use headless Three.js or Playwright because the frontend already renders GLB.

### Local Vision Model

Use Ollama vision models first.

Current Ollama vision API pattern:

```bash
IMG=$(base64 < render.png | tr -d '\n')

curl -X POST http://localhost:11434/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3.6:27b",
    "messages": [{
      "role": "user",
      "content": "Review this CAD render for printability and whether it matches the request. Return JSON.",
      "images": ["'"$IMG"'"]
    }],
    "stream": false
  }'
```

Ollama also supports passing image paths through the official Python/JavaScript SDKs. The raw REST API expects base64-encoded images.

### Model Choice

**Default vision model: `qwen3.6:27b`** — the same model used for CAD code generation. This keeps the setup simple (one model serves both roles) and avoids requiring additional model downloads.

If `qwen3.6:27b` vision performance is insufficient (poor critique accuracy, hallucinated issues, or inability to parse renders), swap to one of these alternatives without changing the rest of the pipeline:

| Priority | Model | Reason to try |
|---|---|---|
| 1 (default) | `qwen3.6:27b` | Already pulled for code gen, good baseline |
| 2 | `qwen3-vl` variants | Dedicated vision-language model, stronger image reasoning |
| 3 | `gemma3:27b` or smaller `gemma3` | Alternative architecture, lighter VRAM in smaller variants |
| 4 | Cloud vision (Gemini, GPT-4o) | Best accuracy, requires API key and user opt-in |

The vision model is configured independently from the code-generation model so it can be changed without affecting the rest of the system:

```env
VISION_MODEL=qwen3.6:27b          # default — same as LLM_MODEL
VISION_BASE_URL=http://localhost:11434/v1   # default — same Ollama instance
```

At startup, inspect model capability metadata where possible and run a tiny image smoke test:

```text
Ask model: "Reply with exactly: vision-ok"
Attach a simple generated image.
Pass only if response is coherent.
```

If local vision fails:

1. Continue deterministic validation.
2. Mark vision critique as skipped.
3. Log a warning so the user knows vision is unavailable.
4. Optionally allow cloud vision only when the user enables it.

### Vision Prompt Contract

Ask for compact JSON:

```json
{
  "matches_user_intent": true,
  "printability_score": 0.82,
  "issues": [
    {
      "type": "unsupported_overhang",
      "severity": "warning",
      "view": "front",
      "description": "Long horizontal lip appears unsupported",
      "suggested_fix": "Add chamfer or support rib"
    }
  ],
  "recommended_repair_prompt": "Add two triangular ribs under the horizontal lip."
}
```

Do not let the vision model directly edit CAD. It should produce critique that the CAD repair step consumes.

## 6. Assemblies and Multi-Part Workflows

### Goal

Support functional multi-part designs without jumping into full mechanical mates.

### MVP Assembly Model

Represent assemblies as named parts with transforms:

```python
class AssemblyPart(BaseModel):
    part_id: str
    name: str
    source_model_id: str | None = None
    cad_name: str
    transform: list[list[float]]  # 4x4 matrix
    material: str = "PLA"
    color: str = "#6f9fd8"


class AssemblyManifest(BaseModel):
    assembly_id: str
    parts: list[AssemblyPart]
```

CadQuery export:

```python
assy = cq.Assembly()
assy.add(base, name="base")
assy.add(lid, name="lid", loc=cq.Location(cq.Vector(0, 0, 22)))
assy.save("model.step")
assy.export("model.glb")
```

### UI

- Assembly tree
- Toggle part visibility
- Select part
- Download whole assembly
- Download selected part
- Exploded-view slider

### Agent Behavior

When user requests a multi-part design:

- Generate explicit named parts.
- Explain print orientation assumptions per part.
- Export whole assembly plus per-part STL files.

Directory layout:

```text
model-001/
    model.step
    model.glb
    parts/
        base.stl
        lid.stl
```

## 7. Import and Conversion Workflows

### Goal

Allow users to bring in existing CAD and ask the agent to inspect, modify, or derive from it.

### MVP Supported Imports

- STEP: preferred editable CAD input
- STL: view/measure/printability only, not truly parametric
- GLB: view/measure only

### Endpoints

```text
POST /api/projects/{id}/imports
GET  /api/projects/{id}/imports
POST /api/projects/{id}/imports/{import_id}/analyze
POST /api/projects/{id}/imports/{import_id}/derive
```

### Import Pipeline

```text
Upload file
    -> Detect format
    -> Store original
    -> Convert preview to GLB
    -> Run geometry stats
    -> Run manufacturability checks where possible
    -> Make import available as agent context
```

### STEP Modification Strategy

True parametric editing of arbitrary imported STEP is hard because STEP usually lacks original feature history.

For MVP:

- Treat imported STEP as reference geometry.
- Allow measurements, inspection, and derived designs.
- Allow simple boolean modifications where reliable.
- For complex edits, ask the agent to recreate a parametric CadQuery version from the imported geometry and user intent.

## 8. Internet-Aware Local-First Research

### Goal

Give the local model controlled internet access for standards, hardware dimensions, material properties, vendor specs, and design references.

The local model should remain the main reasoning and CAD-generation model. Web search is a tool called by the orchestrator, not a replacement model.

### Free / Low-Cost Options

Preferred options:

1. **Ollama web search API**
   - Official Ollama capability.
   - Requires a free Ollama account and API key.
   - Good integration path with Ollama Python/JS libraries.
   - Not fully offline, but the reasoning model can remain local.

2. **SearXNG self-hosted metasearch**
   - Free and self-hostable.
   - Can query multiple public search engines.
   - Best privacy/control option.
   - Requires running a local Docker service.

3. **DuckDuckGo search through a Python package**
   - Easy MVP.
   - Free but unofficial and may be rate-limited or brittle.
   - Good fallback for light usage.

4. **Brave Search API**
   - Has a free tier at times, but quotas and terms can change.
   - Good structured API if the user is willing to configure a key.

### Recommended Architecture

Create `backend/tools/web_research.py` with a provider interface:

```python
class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str
    source: str


class WebSearchProvider(Protocol):
    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        ...

    async def fetch(self, url: str, max_chars: int = 12000) -> str:
        ...
```

Providers:

```text
OllamaSearchProvider
SearxngSearchProvider
DuckDuckGoSearchProvider
BraveSearchProvider
```

Configuration:

```env
WEB_RESEARCH_ENABLED=false
WEB_SEARCH_PROVIDER=ollama
OLLAMA_API_KEY=
SEARXNG_BASE_URL=http://localhost:8080
BRAVE_SEARCH_API_KEY=
WEB_FETCH_MAX_CHARS=12000
WEB_SEARCH_MAX_RESULTS=5
```

### Agent Policy

The agent may search only when:

- User explicitly asks for current information.
- User asks for standards, dimensions, material properties, or vendor specs not in local memory.
- A generated design depends on hardware dimensions not provided by the user.

The agent must cite URLs in the chat response when web results influenced the design.

### Research Flow

```text
User prompt
    -> Planner decides if web research is needed
    -> Search query generated
    -> Top results fetched/summarized
    -> Source snippets injected into CAD prompt
    -> Generated model metadata stores citations
```

Persist:

```text
model-001/research.json
```

### Guardrails

- Never execute code from fetched pages.
- Strip scripts/styles from fetched HTML.
- Limit fetched content length.
- Prefer official vendor/docs pages over forum content.
- Store citations for reproducibility.
- If web access fails, ask the user for the missing dimensions or proceed with clearly stated assumptions.

## 9. Implementation Order

Recommended sequence:

1. Geometry stats and manufacturability report.
2. Server-side renders and local vision critique.
3. Feature manifest and parameter panel.
4. Viewport selection and selection-aware chat.
5. Import STEP/STL/GLB workflow.
6. Assembly manifest and multi-part exports.
7. Web research tool with provider interface.

This order improves model quality and validation before adding more editing surface area.

---

# Future Extensions

Possible future additions:

- Multi-agent specialization
- Manufacturability agent
- Support optimization agent
- Assembly reasoning
- FEM/simulation integration
- Hardware standards lookup
- Geometry graph memory
- Topology transformers
- Automatic slicing feedback
- Direct 3MF generation (multi-color, multi-plate, build plate placement)
- Per-face topology mapping for geometry selection (Phase 2)
- Docker-based execution sandboxing for multi-user
- On-demand re-tessellation at different quality levels

---

# Important Initial Non-Goals

Avoid initially:

- Blender
- Diffusion mesh generation
- Artistic workflows
- Complex multi-agent systems
- Cloud-native infra
- Enterprise auth systems
- Custom geometry kernels
- Concurrent multi-user support
- Authentication / authorization
- 3MF export (single-color STL is sufficient)
- Per-face topology mapping (use Assembly-level selection first)

Focus first on:

- Stable CAD generation
- Reliable geometry workflows
- Manufacturable outputs
- Repair iteration loops
- Strong browser UX
- AI-assisted engineering workflows
