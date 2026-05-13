# Implementation Tracker

> Auto-generated from `design/main_design.md` vs current codebase.
> Last updated: 2026-05-12

Legend: ✅ Done | 🟡 Partial | ❌ Not started

---

## 1. System Architecture

### 1.1 Frontend Stack
- ✅ React
- ✅ TypeScript
- ✅ Vite (dev server + proxy configured)
- ✅ Three.js
- ✅ React Three Fiber (`@react-three/fiber`)
- ❌ TailwindCSS (vanilla CSS used instead — acceptable divergence)
- ✅ Zustand (state management)

### 1.2 Backend Stack
- ✅ Python 3.12+
- ✅ FastAPI
- ❌ LangGraph (listed in requirements.txt but not used in any code)
- ✅ Pydantic (domain models)
- ✅ CadQuery
- ✅ OpenCascade (via CadQuery)
- ✅ uvicorn

### 1.3 Backend Directory Structure
- ✅ `backend/api/` — API layer
- ❌ `backend/agent/` — Agent orchestrator (directory missing)
- ✅ `backend/domain/` — Domain models
- ✅ `backend/cad/` — CAD engine
- ❌ `backend/render/` — Rendering service (directory missing)
- ❌ `backend/vision/` — Vision critique (directory missing)
- ❌ `backend/repair/` — Repair module (directory missing; repair logic lives in websocket.py)
- ✅ `backend/models/` — LLM service
- ❌ `backend/validation/` — Geometry validation (directory missing; validation lives in cad/engine.py)
- ❌ `backend/tessellation/` — Tessellation module (directory missing; tessellation lives in cad/engine.py)
- ✅ `backend/storage/` — Storage service
- ❌ `backend/config/` — Config module (directory missing; config via env vars)

### 1.4 Frontend Directory Structure
- ✅ `frontend/src/components/` — UI components
- ❌ `frontend/src/viewport/` — Viewport module (lives in components/)
- ❌ `frontend/src/chat/` — Chat module (lives in components/)
- ❌ `frontend/src/constraints/` — Constraints UI (missing)
- ❌ `frontend/src/history/` — History UI (missing)
- ✅ `frontend/src/hooks/` — React hooks
- ✅ `frontend/src/stores/` — Zustand stores (stores.ts)
- ✅ `frontend/src/types/` — TypeScript types (types.ts)

---

## 2. Frontend — AI Chat Interface

- ✅ Natural language prompt input
- ✅ Streaming LLM responses (llm_chunk via WebSocket)
- ✅ Stage status indicators (generating, executing, tessellating, etc.)
- ✅ Tool execution logs (debug_log messages in DebugPanel)
- ❌ Critique feedback display (no critique UI)
- ❌ Repair explanations in chat (only generic failure messages shown)

---

## 3. Frontend — 3D Viewport

- ✅ Real-time glTF model viewing
- ✅ Orbit controls (OrbitControls from drei)
- ✅ Pan/zoom (via OrbitControls)
- ❌ Wireframe mode toggle
- ❌ Face highlighting
- ❌ Bounding boxes overlay
- ❌ Measurement tools
- ❌ Section cuts
- ❌ Multiple camera views
- ❌ Exploded views (future)
- ❌ Assembly inspection (future)
- ❌ Feature highlighting (future)

---

## 4. Frontend — Geometry Interaction

### Phase 1: Assembly-Level Selection
- ❌ Named Assembly children preserved in glTF scene graph
- ❌ Three.js raycasting for mesh click → cadName
- ❌ Store active selection in Zustand
- ❌ Visual highlight of selected mesh
- ❌ Send selection context with chat messages (`selection` WS message type)

### Phase 2: Per-Face Topology Mapping
- ❌ Custom tessellation pipeline with face IDs (deferred)

---

## 5. Frontend — Design History

- 🟡 Prompt history (stored in chat threads, viewable in chat)
- 🟡 CAD source revisions (viewable in SourcePanel with diff view)
- ❌ STEP revision browser
- ❌ Render snapshots
- ❌ Critique reports display

---

## 6. Frontend — Constraint Panel

- ❌ Hard constraint editor UI
- ❌ Print volume dimension inputs
- ❌ Min wall thickness input
- ❌ Max file size input
- ❌ Soft constraint editor UI (overhang angle, aesthetic prefs, material, etc.)
- ✅ Hard constraints model defined (`HardConstraints` in domain/models.py)
- ✅ Soft constraints model defined (`SoftConstraints` in domain/models.py)
- ✅ Constraints stored in project config
- ✅ PUT `/api/projects/{id}/constraints` endpoint exists

---

## 7. Frontend — Rendering Pipeline

- ✅ CadQuery Assembly → glTF export (`export_glb` in engine.py)
- ✅ Serve .glb via REST endpoint (`GET .../glb`)
- ✅ Three.js GLTFLoader renders in viewport
- ✅ Tessellation parameters (tolerance/angularTolerance) configurable in code
- ❌ On-demand re-tessellation at different quality levels
- ❌ Configurable tessellation from frontend

---

## 8. Frontend — Additional UI Features

- ✅ Project selector (dropdown)
- ✅ Create / rename / delete projects
- ✅ Open project folder on local machine
- ✅ Chat thread management (create, rename, delete, switch)
- ✅ Model version selector dropdown
- ✅ Source code editor with execute capability (SourcePanel)
- ✅ Side-by-side diff view for source versions
- ✅ File download (STL, STEP, GLB) with dropdown menu
- ✅ Debug log panel (DebugPanel)
- ✅ Connection status indicator

---

## 9. Backend — REST API Endpoints

- ✅ `POST /api/projects` — Create project
- ✅ `GET /api/projects` — List projects
- ✅ `GET /api/projects/{id}` — Get project details
- ✅ `PUT /api/projects/{id}` — Update project
- ✅ `DELETE /api/projects/{id}` — Delete project
- ✅ `PUT /api/projects/{id}/constraints` — Update constraints
- ❌ `POST /api/projects/{id}/chat` — Send chat (WebSocket used instead)
- ✅ `GET /api/projects/{id}/models` — List models
- ✅ `GET /api/projects/{id}/models/{id}/glb` — Download glTF
- ✅ `GET /api/projects/{id}/models/{id}/step` — Download STEP
- ✅ `GET /api/projects/{id}/models/{id}/stl` — Download STL
- ✅ `GET /api/projects/{id}/models/{id}/source` — Get CadQuery source
- ✅ `POST /api/projects/{id}/models/execute_source` — Execute edited source
- ✅ `GET /api/projects/{id}/history` — Get chat history
- ✅ `GET /api/health` — Health check with Ollama status
- ✅ Chat thread CRUD endpoints (list, create, get, rename, delete)

---

## 10. Backend — WebSocket Protocol

### Client → Server Messages
- ✅ `chat_message` with content + thread_id + base_model_id
- ✅ `ping` / `pong`
- ❌ `selection` message type (feature selection context)

### Server → Client Messages
- ✅ `status` (stage + message)
- ✅ `llm_chunk` (streaming tokens)
- ✅ `model_ready` (model_id + glb_url)
- ✅ `chat_response` (final text)
- ✅ `error` (message + failure_type)
- ✅ `debug_log` (timestamp, category, message, data)
- ❌ `critique_result` (defined in domain models, never sent)

---

## 11. Backend — CadQuery Code Generation

### System Prompt
- ✅ CadQuery API quick reference injected
- ✅ Example library injected (10 examples)
- ✅ Output format spec (`result` variable requirement)
- ✅ Hard constraints injected into prompt
- ✅ Soft constraints injected into prompt
- ✅ Rules for safe imports

### Example Library (`backend/cad/examples.py`)
- ✅ Basic primitives (simple_box)
- ✅ Box with hole
- ✅ Cylinder with chamfer
- ✅ Enclosure with wall thickness
- ✅ Bracket with holes
- ✅ Rounded plate with bolt pattern
- ✅ Cylindrical container
- ✅ Hex nut
- ✅ Phone stand
- ✅ Cable clip (snap-fit)
- ❌ Threaded holes example
- ❌ Multi-body assembly example

### Code Validation (AST)
- ✅ Syntax check (ast.parse)
- ✅ Forbidden imports check
- ✅ `result` variable assignment check

### Execution Sandboxing
- ✅ Restricted `__builtins__` (SAFE_BUILTINS)
- ✅ Only `cq`, `cadquery`, `math` in globals
- ✅ Restricted `__import__` function
- ✅ Forbidden module list (subprocess, os, sys, etc.)

---

## 12. Backend — Geometry Validation

- ✅ Bounding box dimension check against hard constraints
- ✅ Solid body count check (non-zero)
- ❌ Manifold / closed shell (watertight) check
- ❌ Minimum wall thickness check
- ❌ Degenerate dimension check
- ❌ File size check
- ❌ Separate `backend/validation/` module (logic in `cad/engine.py`)

---

## 13. Backend — Rendering Service

- ❌ OpenCascade offscreen rendering
- ❌ VTK rendering
- ❌ pygfx rendering
- ❌ Headless Three.js rendering
- ❌ Perspective render generation
- ❌ Orthographic render generation
- ❌ Section cut renders
- ❌ `backend/render/` module (directory missing)

---

## 14. Backend — Vision Critique System

- ❌ Multimodal AI model integration for geometry critique
- ❌ Printability evaluation
- ❌ Thin wall detection (vision)
- ❌ Overhang detection (vision)
- ❌ Symmetry check
- ❌ Critique report generation (schema defined but never populated)
- ❌ `backend/vision/` module (directory missing)

---

## 15. Backend — Automatic Repair Loop

- ✅ Retry loop with MAX_REPAIR_ITERATIONS = 5
- ✅ LOCAL_MODEL_RETRIES = 3 (constant defined)
- ✅ Re-generate with error message on failure
- ✅ Repair prompt builder (`build_repair_prompt`)
- ✅ Failed attempt metadata saved
- ✅ Final failure message surfaced to user
- ❌ Escalation to cloud model after local retries exhausted
- ❌ Partial success handling (show result with warning banner)
- ❌ Critique-driven repair (no vision critique to trigger it)
- ❌ Geometry-invalid-specific repair prompts
- ❌ Constraint-violation-specific repair prompts

### Failure Taxonomy (Domain Models)
- ✅ `syntax_error` enum defined
- ✅ `execution_error` enum defined
- ✅ `geometry_invalid` enum defined
- ✅ `constraint_violation` enum defined
- ✅ `critique_failed` enum defined
- ✅ `timeout` enum defined
- 🟡 Failure types used: only `execution_error` is actually set in pipeline

---

## 16. Backend — AI Model Architecture

### LLM Service
- ✅ OpenAI-compatible API client (AsyncOpenAI)
- ✅ Ollama support (default localhost:11434)
- ✅ Streaming response generation
- ✅ Non-streaming response generation
- ✅ Configurable base_url, api_key, model via env vars
- ✅ Ollama connectivity check before generation
- ❌ vLLM support (not tested / no specific code)
- ❌ Cloud model support (OpenAI, Claude, Gemini — no routing)

### Model Routing
- ❌ Dynamic model routing layer
- ❌ Task → model routing table
- ❌ `backend/models/` routing config (only llm_service.py exists)

---

## 17. Backend — LangGraph Workflow

- ❌ LangGraph integration (dependency installed but not used)
- ❌ Single-agent workflow graph (Planner → Generator → Executor → …)
- ❌ State machine / checkpointing
- 🟡 Pipeline implemented as imperative loop in `websocket.py` instead

---

## 18. Backend — Persistent Structured State

- ❌ `AgentState` Pydantic model (not implemented)
- ❌ User goal tracking
- ❌ Failure history accumulation
- ❌ Agent state snapshots
- 🟡 State managed implicitly via storage service (project config, model metadata, chat)

---

## 19. Backend — Storage

### Filesystem Layout
- ✅ `data/projects/{id}/` directory structure
- ✅ `project.json` — project metadata + constraints
- ✅ `models/model-NNN/` — sequential model directories
- ✅ `source.py` saved per model
- ✅ `model.step` saved per model
- ✅ `model.stl` saved per model
- ✅ `model.glb` saved per model
- ✅ `metadata.json` saved per model
- ✅ `chat_history.json` — legacy chat storage
- ✅ `chat_threads/` — per-thread JSON files
- ❌ `render.png` — server-side render (no render service)
- ❌ `feature_manifest.json` per model
- ❌ `parameters.json` per model
- ❌ `analysis.json` per model
- ❌ `manufacturability.json` per model

### Storage Service
- ✅ Create / get / list / update / delete projects
- ✅ Sequential model ID generation
- ✅ Model metadata CRUD
- ✅ Latest successful model lookup
- ✅ Model source text retrieval
- ✅ Chat thread CRUD (create, list, get, append, rename, delete)
- ✅ Legacy chat_history.json support
- ✅ Binary and text file save helpers

---

## 20. Domain Models

- ✅ `FailureType` enum
- ✅ `RepairStage` enum
- ✅ `HardConstraints`
- ✅ `SoftConstraints`
- ✅ `GeometryIssue`
- ✅ `CritiqueReport`
- ✅ `ModelMetadata`
- ✅ `ProjectConfig`
- ✅ `ChatMessage`
- ✅ WS message types (WSStatusMessage, WSModelReady, etc.)
- ❌ `Part` / `Assembly` / `Constraint` models
- ❌ `GeometryArtifact` / `RenderArtifact` models
- ❌ `RepairTask` / `ToolResult` models
- ❌ `CadParameter` / `CadFeature` / `FeatureManifest` models
- ❌ `GeometryStats` model
- ❌ `ManufacturabilityIssue` / `ManufacturabilityReport` models
- ❌ `AssemblyPart` / `AssemblyManifest` models
- ❌ `SearchResult` / `WebSearchProvider` models

---

## 21. Zoo Gap Closure Features

### 21.1 Feature Tree & Editable Parameters
- ❌ Feature manifest generation alongside CadQuery code
- ❌ `feature_manifest.json` / `parameters.json` persistence
- ❌ CadParameter / CadFeature / FeatureManifest schemas
- ❌ LLM generates named top-level parameters
- ❌ Feature tree UI panel
- ❌ Parameter table UI
- ❌ Regenerate button on parameter edit
- ❌ Feature highlight in viewport on tree selection
- ❌ Source span display

### 21.2 Selection-Aware Agent
- ❌ Named Assembly children in GLB export
- ❌ Three.js raycasting + cadName readout
- ❌ Selection stored in Zustand
- ❌ Selected mesh highlight
- ❌ Selection context sent with chat messages
- ❌ Backend resolves feature_id against manifest
- ❌ Edit prompt with selected feature metadata

### 21.3 Model-Derived Analysis Tools
- ❌ Bounding box dimensions analysis
- ❌ Volume calculation
- ❌ Surface area calculation
- ❌ Center of mass
- ❌ Body / face / edge count
- ❌ Solid validity check
- ❌ Watertightness check
- ❌ Estimated mass / print weight
- ❌ `analysis.json` persistence
- ❌ Inject analysis into repair/critique prompts

### 21.4 Manufacturability & Design Review
- ❌ Three-layer validation pipeline (deterministic → heuristics → vision)
- ❌ Build volume check (deterministic) — exists in geometry validation but not as separate layer
- ❌ Degenerate dimension check
- ❌ Small feature detection
- ❌ File size check
- ❌ Minimum wall thickness (approximate)
- ❌ Unsupported overhang detection
- ❌ Tiny face detection
- ❌ Sharp internal corner detection
- ❌ Thin pin/tab detection
- ❌ `ManufacturabilityReport` schema
- ❌ `manufacturability.json` persistence

### 21.5 Vision-Based Validation
- ❌ Server-side PNG render generation (iso, front, right, top views)
- ❌ Local Ollama vision model integration
- ❌ Vision capability smoke test at startup
- ❌ Vision prompt contract (JSON critique format)
- ❌ Cloud vision fallback
- ❌ Vision critique → repair prompt pipeline

### 21.6 Assemblies & Multi-Part Workflows
- ❌ `AssemblyPart` / `AssemblyManifest` schemas
- ❌ CadQuery Assembly export (multi-part)
- ❌ Assembly tree UI
- ❌ Toggle part visibility
- ❌ Per-part STL download
- ❌ Exploded-view slider
- ❌ Named parts + print orientation per part

### 21.7 Import & Conversion Workflows
- ❌ `POST /api/projects/{id}/imports` endpoint
- ❌ STEP import
- ❌ STL import (view/measure only)
- ❌ GLB import (view/measure only)
- ❌ Import → detect format → convert → analyze pipeline
- ❌ STEP modification strategy (reference geometry)

### 21.8 Internet-Aware Research
- ❌ `backend/tools/web_research.py` module
- ❌ `SearchResult` / `WebSearchProvider` schemas
- ❌ OllamaSearchProvider
- ❌ SearxngSearchProvider
- ❌ DuckDuckGoSearchProvider
- ❌ BraveSearchProvider
- ❌ Web research env var config
- ❌ Agent policy for when to search
- ❌ Citation storage in model metadata
- ❌ `research.json` persistence

---

## 22. 3D Printing Workflow

- ✅ CadQuery → OpenCascade → STEP → STL pipeline
- ✅ STL export for slicer compatibility
- ❌ Explicit Bambu Studio compatibility validation
- ❌ PrusaSlicer / OrcaSlicer compatibility testing
- ✅ Single-color STL export

---

## 23. Testing

- ❌ Backend unit tests (tests/backend/cad/ exists but content unknown)
- ❌ Frontend unit tests
- ❌ Integration tests
- ❌ End-to-end tests

---

## Summary

| Category | Done | Partial | Not Started |
|---|---|---|---|
| Frontend Stack | 6 | 0 | 1 |
| Backend Stack & Structure | 8 | 0 | 9 |
| Chat Interface | 4 | 0 | 2 |
| 3D Viewport | 3 | 0 | 9 |
| Geometry Interaction | 0 | 0 | 6 |
| Design History | 0 | 2 | 3 |
| Constraint Panel | 4 | 0 | 5 |
| REST API | 16 | 0 | 1 |
| WebSocket Protocol | 7 | 0 | 2 |
| Code Generation | 17 | 0 | 2 |
| Geometry Validation | 2 | 0 | 4 |
| Rendering Service | 0 | 0 | 8 |
| Vision Critique | 0 | 0 | 7 |
| Repair Loop | 6 | 1 | 5 |
| AI Model Architecture | 6 | 0 | 5 |
| LangGraph Workflow | 0 | 1 | 3 |
| Structured State | 0 | 1 | 3 |
| Storage | 16 | 0 | 5 |
| Domain Models | 13 | 0 | 8 |
| Zoo Gap Closure | 0 | 0 | 49 |
| 3D Printing | 3 | 0 | 2 |
| Testing | 0 | 0 | 4 |
| **Total** | **111** | **5** | **143** |
