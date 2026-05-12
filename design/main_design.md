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

## 5. Constraint Panel

Editable engineering constraints displayed in UI.

### Hard Constraints (Deterministic, Validated Post-Generation)

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

Recommended rendering approaches:

- OpenCascade offscreen rendering
- VTK
- pygfx
- trimesh rendering
- headless Three.js rendering

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

Core AI workflow:

```text
Generate CAD
    ↓
Execute
    ↓
Validate (deterministic geometry checks)
    ↓
Tessellate → glTF
    ↓
Render
    ↓
Vision Critique
    ↓
Repair Geometry (if needed)
    ↓
Repeat
```

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

- Qwen3 32B
- Gemma 27B
- DeepSeek

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
