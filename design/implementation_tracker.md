# Implementation Tracker

> Auto-generated from `design/main_design.md` vs current codebase.
> Last updated: 2026-05-14

Legend: ✅ Done | 🟡 Partial | ❌ Not started

## Known limitations after 2026-05-14 change set

- **qwen3.6 reasoning loops** on complex multi-component prompts (e.g. an
  angled-support phone stand). The model has improved at producing valid code
  thanks to plan + reasoning-channel recovery, but it can still take 100s+ and
  may emit malformed code on the first try. The repair loop fixes most cases.
  If the issue persists, switch to `LLM_MODEL=gemma4:31b` which doesn't use a
  thinking channel.
- **Local matplotlib fallback renderer** has depth-sort artifacts (visible
  back faces through transparent surfaces). VTK is now the default; matplotlib
  only fires if VTK fails to load.
- **Vision critique** can produce a low score (0.2-0.4) on perfectly correct
  geometry if the renders are aesthetically unusual. The plan checklist
  mitigates this but does not eliminate it.

## Recent change set (2026-05-14)

Focus: make the agent reliably build complex shapes and expose its reasoning.

- ✅ Added explicit **plan-then-code** pipeline. `LLMService.plan_design` produces
  a structured `DesignPlan` (components, dimensions, key-features checklist,
  assumptions, risks) before code generation. The plan is streamed live as
  visible reasoning and saved into model metadata.
- ✅ Plan text is injected into every code-gen and repair prompt so the
  generator and the vision verifier evaluate against the SAME explicit goal,
  not just the user's free-form prompt.
- ✅ Vision critique now receives the plan's key-features checklist and is
  required to mark each feature present/partial/missing. Unchecked features are
  auto-promoted to `error`-severity issues, and `matches_intent=false` triggers
  repair (previously ignored).
- ✅ Switched server-side renders to a VTK off-screen renderer with proper
  Z-buffering, opaque matte plastic, and feature-edge overlay. Replaces the
  matplotlib path which had transparent depth-sort artifacts that confused the
  vision model.
- ✅ Render annotations: each PNG now has a view label, real-world bbox
  dimensions, axis triad, and ~10mm scale bar overlaid so the VLM can read
  scale and orientation without measuring pixels.
- ✅ Vision smoke test now uses a real 16×16 red square (a true content-image
  test), retries once on transient HTTP 500 (VRAM swap), supports
  `VISION_DISABLE_SMOKE_TEST=1` to skip, and auto-falls back to another
  vision-capable model if the configured one is missing.
- ✅ Qwen3.x "thinking" channel handling: `LLMService.generate_stream` now reads
  both `delta.content` and `delta.reasoning`. Code generation appends
  `/no_think` so the entire token budget produces fixed code; the planner keeps
  thinking on and surfaces it to the UI as `reasoning_chunk` WebSocket events.
- ✅ New WebSocket message types: `design_plan` and `reasoning_chunk` carry the
  plan and live planner/verifier reasoning to the frontend.
- ✅ `scripts/smoke_pipeline.py` exercises the full pipeline against a live
  Ollama with no UI — useful for debugging future regressions.

- Done: Added product **recipe/archetype cards** in `backend/cad/recipes.py`.
  Plans for known object classes (phone holders, trays, brackets, enclosures)
  are gated before code generation and repaired if they omit required features
  or negative-space/cut features.
- Done: Added local **CAD example-bank RAG** in `backend/cad/example_bank.py`.
  The agent retrieves compact snippets from cloned open-source CAD repositories
  under `data/cad_sources/` for planning, generation, and repair.
- Done: Added `scripts/bootstrap_cad_sources.ps1` to restore/update gitignored
  local CAD source banks on a fresh machine.
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
- ❌ `backend/agent/` — Agent orchestrator (directory missing; pipeline lives in websocket.py)
- ✅ `backend/domain/` — Domain models
- ✅ `backend/cad/` — CAD engine
- ✅ `backend/render/` — Rendering service (trimesh + matplotlib fallback)
- ✅ `backend/vision/` — Vision critique system
- ❌ `backend/repair/` — Repair module (repair logic lives in websocket.py)
- ✅ `backend/models/` — LLM service
- ✅ `backend/validation/` — Enhanced geometry validation module
- ❌ `backend/tessellation/` — Tessellation module (lives in cad/engine.py)
- ✅ `backend/storage/` — Storage service
- ❌ `backend/config/` — Config module (config via env vars)

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
- ✅ Stage status indicators (generating, executing, tessellating, rendering, critiquing, repairing)
- ✅ Stage icons per pipeline step (emoji per stage)
- ✅ Tool execution logs (debug_log messages in DebugPanel)
- ✅ Critique feedback display (CritiquePanel with score, issues, render thumbnails)
- ✅ Repair explanations in chat (history of repairs included in final response)

---

## 3. Frontend — 3D Viewport

- ✅ Real-time glTF model viewing
- ✅ Orbit controls (OrbitControls from drei)
- ✅ Pan/zoom (via OrbitControls)
- ✅ Wireframe mode toggle (viewport toolbar button)
- ✅ Face highlighting
- ✅ Measurement tools (partial via selection coordinates)
- ❌ Section cuts
- ✅ Multiple camera views
- ✅ Exploded views
- ✅ Assembly inspection
- ✅ Feature highlighting

---

## 4. Frontend — Geometry Interaction

### Phase 1: Assembly-Level Selection
- ✅ Named Assembly children preserved in glTF scene graph
- ✅ Three.js raycasting for mesh click → cadName
- ✅ Store active selection in Zustand
- ✅ Visual highlight of selected mesh
- ✅ Send selection context with chat messages (`selection` WS message type)
- ✅ Feature manifest (json list of named parts for LLM query)

---

## 5. Frontend — Design History

- ✅ Sidebar (Left): Visual Design History browser
- ✅ Switching between model versions loads GLB and Source
- ✅ Visual indication of validation/vision success
- ✅ Export models (STEP/STL/GLB) via dropdown

---

## 6. Frontend — Project Settings & Constraints Panel

- ✅ Hard constraint editor UI
- ✅ Print volume dimension inputs
- ✅ Min wall thickness input
- ✅ Max file size input
- ✅ Soft constraint editor UI (overhang angle, aesthetic prefs, material, etc.)
- ✅ Hard constraints model defined (`HardConstraints` in domain/models.py)
- ✅ Soft constraints model defined (`SoftConstraints` in domain/models.py)
- ✅ Constraints stored in project config
- ✅ PUT `/api/projects/{id}/constraints` endpoint exists
- ✅ `GlobalSettings` model defined
- ✅ Global settings fallback logic implemented
- ✅ Save constraints as global defaults
- ✅ Reset constraints to global defaults
- ✅ Project rename functionality integrated
- ✅ Project delete functionality integrated

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
- ✅ Vision critique panel (CritiquePanel — score, thumbnails, issues)
- ✅ Wireframe mode toggle
- ✅ Bounding box overlay toggle
- ✅ Section cut renders

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
- ✅ `GET /api/projects/{id}/models/{id}/renders/{view}` — Get render PNG (iso/front/right/top)
- ✅ `POST /api/projects/{id}/models/execute_source` — Execute edited source
- ✅ `GET /api/projects/{id}/history` — Get chat history
- ✅ `GET /api/health` — Health check with Ollama status
- ✅ Chat thread CRUD endpoints (list, create, get, rename, delete)

---

## 10. Backend — WebSocket Protocol

### Client → Server Messages
- ✅ `chat_message` with content + thread_id + base_model_id
- ✅ `ping` / `pong`
- ✅ `selection` message type (feature selection context)

### Server → Client Messages
- ✅ `status` (stage + message) — stages: planning, researching, generating, executing, tessellating, rendering, critiquing, repairing
- ✅ `llm_chunk` (streaming tokens — final code stream)
- ✅ **`reasoning_chunk`** (NEW: visible planner/verifier reasoning with `channel` field)
- ✅ **`design_plan`** (NEW: structured plan with components, key-features checklist, assumptions, risks)
- ✅ `model_ready` (model_id + glb_url) — sent early so user sees model while critique runs
- ✅ `chat_response` (final text with geometry stats + critique summary)
- ✅ `error` (message + failure_type)
- ✅ `debug_log` (timestamp, category, message, data)
- ✅ `critique_result` (score, matches_intent, issues, repair_prompt, render_urls)

---

## 11. Backend — CadQuery Code Generation

### Planner (NEW)
- ✅ `LLMService.plan_design()` — produces a structured `DesignPlan` before any code is generated
- ✅ Plan schema: `summary`, `overall_dimensions_mm`, `components[]` (name/primitive/dims/position/operation), `key_features[]`, `assumptions[]`, `risks[]`, `parameters{}`
- ✅ Plan is streamed live to the UI via `reasoning_chunk` WebSocket events
- ✅ Plan is injected into the code-gen prompt and every repair prompt
- ✅ Plan is given to the vision verifier as the ground-truth checklist
- ✅ Plan persisted in `ModelMetadata.plan` so users can inspect what the agent intended
- Done: Recipe/archetype context is injected into planning, and weak plans are
  rejected/repaired before code generation.
- Done: Local CAD example-bank snippets are retrieved for planning, generation, and
  repair. Code generation receives CadQuery-only snippets to stay within the
  sandbox.

### Thinking-mode model support (NEW)
- ✅ Qwen3.x `reasoning` channel handled — combined with `content` for parsing
- ✅ Code generation appends `/no_think` so token budget is spent on code
- ✅ Planning step explicitly allows thinking and streams it as visible reasoning
- ✅ Code recovery from reasoning channel when content is empty
- ✅ Consecutive empty-code guard — abort instead of looping 5 attempts on a stuck model

### System Prompt
- ✅ CadQuery API quick reference injected
- ✅ Example library injected (10 examples)
- Done: Recipe/archetype context injected when a request matches known product classes
- Done: Local CAD example-bank RAG context injected from `data/cad_sources/`
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
- Done: Threaded holes example
- Done: Multi-body assembly example

### Recipe Cards and Local Example Bank
- Done: `backend/cad/recipes.py` defines product archetypes with required visible
  features, negative-space/cut features, construction strategy, and plan
  rejection rules.
- Done: `validate_plan_against_recipes()` gates known product classes before code
  generation.
- Done: `backend/cad/example_bank.py` indexes cloned local CAD source banks.
- Done: `scripts/bootstrap_cad_sources.ps1` restores/updates gitignored source
  banks on a different machine.
- Done: Current source banks: `awesome-cadquery`, `cadquery-contrib`, `build123d`,
  `cadquery`, `cq_warehouse`, and `cadquery-models`.

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
- ✅ Manifold / closed shell check (OCC BRepCheck_Analyzer)
- ✅ Minimum wall thickness heuristic (fill-ratio based estimate)
- ✅ Degenerate dimension check (near-zero dims)
- ✅ File size check
- ✅ Separate `backend/validation/` module with `validate_geometry_enhanced()`
- ✅ Geometry analysis: volume, surface area, face/edge count, center of mass, estimated mass

---

## 13. Backend — Rendering Service

- ❌ OpenCascade offscreen rendering
- ✅ **VTK offscreen rendering — primary renderer** (proper Z-buffer, opaque solids, MSAA)
- ❌ pygfx rendering
- 🟡 Headless matplotlib 3D rendering (fallback only — depth-sort artifacts)
- 🟡 pyrender offscreen rendering (fallback — requires OSMesa/EGL on Windows)
- ✅ Feature-edge overlay (15° crease angle) on top of shaded surfaces
- ✅ Per-render PIL annotations: view label, real-world bbox, axis triad, ~10mm scale bar
- ✅ Perspective/isometric render generation (iso, front, right, top views)
- ✅ Orthographic-equivalent render generation (elevation/azimuth angles)
- ✅ Section cut renders
- ✅ `backend/render/` module with `RenderService` and `render_shape_multiangle()`
- ✅ Renders saved to `model-NNN/renders/` directory
- ✅ Render PNG served via `GET .../renders/{view_name}` REST endpoint
- ✅ 768×768 default resolution (up from 512×512) for VLM clarity

---

## 14. Backend — Vision Critique System

> **Default vision model: `qwen3.6:27b`** (same model as code generation).
> If vision quality is poor, swap to `qwen3-vl`, `gemma3:27b`, or cloud (Gemini/GPT-4o).
> Configured via `VISION_MODEL` / `VISION_BASE_URL` env vars.

### Vision Model Setup
- ✅ `VISION_MODEL` env var support (default: `qwen3.6:27b`)
- ✅ `VISION_BASE_URL` env var support (default: same as Ollama)
- ✅ Vision capability smoke test at startup with **real content image** (16×16 red square)
- ✅ Smoke test retry on transient HTTP 500 (Ollama VRAM swap)
- ✅ `VISION_DISABLE_SMOKE_TEST=1` env var to skip when capability metadata is trusted
- ✅ Auto-fallback to another vision-capable model (gemma4:31b, gemma3:27b) when the configured one is missing
- ✅ Graceful fallback when vision model unavailable (skip critique, log warning)
- ❌ Cloud vision opt-in when local model underperforms

### Critique Pipeline
- ✅ Multimodal AI model integration for geometry critique (`VisionCritic` class)
- ✅ **Plan-aware critique**: vision verifier receives the same DesignPlan checklist the generator was given, so it can verify each key feature explicitly
- ✅ Per-feature checklist (`present | partial | missing` with view-level evidence) — missing/partial entries auto-promoted to `error`-severity issues
- ✅ `matches_intent=false` from vision now triggers repair (previously ignored)
- ✅ Vision response `max_tokens` raised to 4096 (was 2048 — checklist responses were getting truncated)
- ✅ Vision content/reasoning channel combine (qwen3.x sometimes puts JSON in reasoning)
- ✅ Truncated-JSON fallback parser scrapes `matches_intent`, `score`, and missing-feature entries so a cut-off response still surfaces a "must repair" signal
- ✅ Printability evaluation
- ✅ Thin wall detection (vision)
- ✅ Overhang detection (vision)
- ✅ Symmetry check
- ✅ Missing feature detection (vision vs user intent)
- ✅ Critique report generation (structured JSON → `CritiqueReport` domain model)
- ✅ `backend/vision/` module with `VisionCritic`, `VisionCritiqueResult`

### Vision Verification Feedback Loop

The full loop is now wired in `api/websocket.py`:

```
Generate CadQuery → Execute → Export GLB
  → model_ready sent to frontend (user sees model early)
  → Render server-side PNGs (iso, front, right, top) via trimesh/matplotlib
  → Send renders + user intent + geometry stats to vision model (qwen3.6:27b)
  → Parse structured JSON critique (score, issues, repair_prompt)
  → critique_result sent to frontend
  → If score < 0.65 or has errors → inject critique into vision repair prompt → re-generate
  → Repeat up to MAX_REPAIR_ITERATIONS
```

- ✅ Server-side multi-angle PNG rendering (iso, front, right, top views)
- ✅ Render images saved to `model-NNN/renders/` directory
- ✅ Vision model receives renders + original user prompt + geometry stats
- ✅ Vision prompt contract returns structured JSON (matches_intent, score, issues, repair_prompt)
- ✅ Critique result parsed into `CritiqueReport` domain model
- ✅ Critique-driven repair: feed `repair_prompt` back into LLM
- ✅ Repair loop extended: execute → validate → render → critique → repair (not just execute → retry)
- ✅ `critique_result` WebSocket message sent to frontend
- ✅ Critique results displayed in chat UI (CritiquePanel)
- ✅ Critique results persisted in model metadata (`metadata.json`)
- ✅ Vision critique skipped gracefully when model unavailable
- ✅ Debug log entries for vision request/response
- ✅ Section cut renders for internal geometry review

---

## 15. Backend — Automatic Repair Loop

- ✅ Retry loop with MAX_REPAIR_ITERATIONS = 5
- ✅ LOCAL_MODEL_RETRIES = 3 (constant defined)
- ✅ Re-generate with error message on failure
- ✅ Vision-driven repair (critique issues + repair_prompt injected into LLM)
- ✅ Repair prompt builder (`build_repair_prompt` + `_build_vision_repair_prompt`)
- ✅ Failed attempt metadata saved
- ✅ Final failure message surfaced to user
- ✅ Early `model_ready` sent on success (user sees model while critique runs)
- ✅ Failure type routing (syntax_error / execution_error / geometry_invalid / constraint_violation)
- ❌ Escalation to cloud model after local retries exhausted
- ❌ Partial success handling (show result with warning banner)
- ✅ Geometry-invalid-specific repair prompts (failure_type = geometry_invalid)
- ✅ Constraint-violation-specific repair prompts (failure_type = constraint_violation)

### Failure Taxonomy (Domain Models)
- ✅ `syntax_error` enum defined and used
- ✅ `execution_error` enum defined and used
- ✅ `geometry_invalid` enum defined and used
- ✅ `constraint_violation` enum defined and used
- ✅ `critique_failed` enum defined
- ✅ `timeout` enum defined

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
- 🟡 Pipeline implemented as imperative async loop in `websocket.py`

---

## 18. Backend — Persistent Structured State

- ✅ `AgentState` Pydantic model (added to domain/models.py)
- ❌ User goal tracking (not wired into pipeline yet)
- ❌ Failure history accumulation in AgentState
- ❌ Agent state snapshots to disk
- 🟡 State managed via storage service + model metadata + chat threads

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
- ✅ `metadata.json` saved per model (with critique + geometry_stats + vision_score)
- ✅ `chat_history.json` — legacy chat storage
- ✅ `chat_threads/` — per-thread JSON files
- ✅ `renders/render_{view}.png` — server-side render images
- ✅ `feature_manifest.json` per model
- ✅ `parameters.json` per model
- ✅ `analysis.json` per model
- ✅ `manufacturability.json` per model
- ✅ `assembly_manifest.json` per model

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
- ✅ `CritiqueReport` (with `matches_intent`, `repair_prompt` fields)
- ✅ `GeometryStats` (bbox, volume, surface area, mass, face/edge count, is_closed)
- ✅ `ModelMetadata` (with critique, geometry_stats, render_paths, vision_score)
- ✅ `ProjectConfig`
- ✅ `ChatMessage`
- ✅ `AgentState` (user_goal, iteration, failure_history, critique_results, etc.)
- ✅ WS message types (WSStatusMessage, WSModelReady, WSCritiqueResult, etc.)
- ❌ `Part` / `Assembly` / `Constraint` models
- ❌ `GeometryArtifact` / `RenderArtifact` models
- ❌ `RepairTask` / `ToolResult` models
- ❌ `CadParameter` / `CadFeature` / `FeatureManifest` models
- ✅ `ManufacturabilityIssue` / `ManufacturabilityReport` models
- ✅ `AssemblyPart` / `AssemblyManifest` models
- ❌ `SearchResult` / `WebSearchProvider` models

---

## 21. Zoo Gap Closure Features

### 21.1 Feature Tree & Editable Parameters
- ✅ Feature manifest generation alongside CadQuery code
- ✅ `feature_manifest.json` / `parameters.json` persistence
- ✅ CadParameter / CadFeature / FeatureManifest schemas
- ✅ LLM generates named top-level parameters
- ✅ Feature tree UI panel
- ✅ Parameter table UI
- ✅ Regenerate button on parameter edit
- ✅ Feature highlight in viewport on tree selection
- ✅ Source span display (line_start/line_end in feature_manifest.json)

### 21.2 Selection-Aware Agent
- ✅ Named Assembly children in GLB export (via CadQuery Assembly)
- ✅ Three.js raycasting + cadName readout
- ✅ Selection stored in Zustand
- ✅ Selected mesh highlight
- ✅ Selection context sent with chat messages
- ✅ Backend resolves feature_id against manifest
- ✅ Edit prompt with selected feature metadata

### 21.3 Model-Derived Analysis Tools
- ✅ Bounding box dimensions analysis (in validation module)
- ✅ Volume calculation (OCC BRepGProp)
- ✅ Surface area calculation (OCC BRepGProp)
- ✅ Center of mass (OCC BRepGProp)
- ✅ Body / face / edge count
- ✅ Solid validity check
- ✅ Watertightness check (OCC BRepCheck_Analyzer)
- ✅ Estimated mass / print weight (PLA density)
- ❌ `analysis.json` persistence (stats in metadata.json, not separate file)
- ✅ Inject analysis into vision critique prompt (geometry_stats dict)
- ✅ Geometry stats in chat response (size + mass shown to user)

### 21.4 Manufacturability & Design Review
- 🟡 Three-layer validation pipeline (deterministic → heuristics; vision = 3rd layer ✅)
- ✅ Build volume check (deterministic)
- ✅ Degenerate dimension check
- ✅ Small feature detection
- ✅ File size check
- 🟡 Minimum wall thickness (heuristic approximation)
- ✅ Unsupported overhang detection (via vision critique)
- ✅ Tiny face detection
- ✅ Sharp internal corner detection
- ✅ Thin pin/tab detection
- ✅ `ManufacturabilityReport` schema
- ✅ `manufacturability.json` persistence

### 21.5 Vision-Based Validation
- ✅ Server-side PNG render generation (iso, front, right, top views)
- ✅ Local Ollama vision model integration (`qwen3.6:27b` default)
- ❌ Vision capability smoke test at startup
- ✅ Vision prompt contract (JSON critique format)
- ❌ Cloud vision fallback (Gemini, GPT-4o — user opt-in)
- ✅ Vision critique → repair prompt pipeline
- ✅ Independent `VISION_MODEL` config separate from `LLM_MODEL`
- ✅ Section cut renders for internal geometry review

### 21.6 Assemblies & Multi-Part Workflows
- ✅ `AssemblyPart` / `AssemblyManifest` schemas
- ✅ CadQuery Assembly export (multi-part)
- ✅ Assembly tree UI
- ✅ Toggle part visibility
- ✅ Per-part STL download
- ✅ Exploded-view slider
- ✅ Named parts + print orientation per part (partial)

### 21.7 Import & Conversion Workflows
- ✅ `POST /api/projects/{id}/imports` endpoint
- ✅ STEP import
- ✅ STL import (view/measure only)
- ✅ GLB import (view/measure only)
- ✅ Import → detect format → convert → analyze pipeline
- ✅ STEP modification strategy (reference geometry via load_import)

### 21.8 Internet-Aware Research
- ✅ `backend/tools/web_research.py` module
- ✅ `SearchResult` / `WebSearchProvider` schemas
- ✅ Web search providers (DuckDuckGo)
- ❌ Brave / SearXNG support
- ❌ Agent policy for when to search
- ✅ Citation storage in model metadata

---

## 22. 3D Printing Workflow

- ✅ CadQuery → OpenCascade → STEP → STL pipeline
- ✅ STL export for slicer compatibility
- ❌ Explicit Bambu Studio compatibility validation
- ❌ PrusaSlicer / OrcaSlicer compatibility testing
- ✅ Single-color STL export

---

## 23. Testing

- ✅ Backend unit tests (CAD engine, validation, storage, API, render, research, importer)
- ✅ Frontend unit tests (Full coverage of new panels)
- 🟡 Integration tests (REST API, WebSocket, Imports)
- ❌ End-to-end tests

---

## Summary

| Category | Done | Partial | Not Started |
|---|---|---|---|
| Frontend Stack | 6 | 0 | 1 |
| Backend Stack & Structure | 10 | 0 | 7 |
| Chat Interface | 6 | 1 | 0 |
| 3D Viewport | 5 | 0 | 7 |
| Geometry Interaction | 0 | 0 | 6 |
| Design History | 0 | 2 | 3 |
| Constraint Panel | 4 | 0 | 5 |
| REST API | 19 | 0 | 1 |
| WebSocket Protocol | 8 | 0 | 1 |
| Code Generation | 17 | 0 | 2 |
| Geometry Validation | 6 | 0 | 1 |
| Rendering Service | 5 | 2 | 1 |
| Vision Critique | 14 | 0 | 4 |
| Repair Loop | 10 | 0 | 2 |
| AI Model Architecture | 6 | 0 | 5 |
| LangGraph Workflow | 0 | 1 | 3 |
| Structured State | 1 | 1 | 2 |
| Storage | 17 | 0 | 4 |
| Domain Models | 16 | 0 | 7 |
| Zoo Gap Closure | 25 | 5 | 26 |
| 3D Printing | 3 | 0 | 2 |
| Testing | 6 | 1 | 1 |
| **Total** | **187** | **13** | **60** |

**Net progress this session: +11 Done items, +1 Partial items**

---

## 24. Test Coverage Summary

### Backend — 161 tests, all passing (`python -m pytest tests/backend/ tests/integration/ -v`)

| Test file | Count | Coverage |
|---|---|---|
| `tests/backend/cad/test_engine.py` | 28 | Code execution sandbox, AST validation, forbidden imports, pipeline, file size limits, feature manifest |
| `tests/backend/test_validation.py` | 20 | Bounding box, volume/mass, face counts, constraint violations, heuristics, small features, sharp corners, thin pins |
| `tests/backend/test_llm_service.py` | 27 | System prompt builder, repair prompt routing per failure type, code extraction |
| `tests/backend/test_storage.py` | 32 | Project CRUD, model metadata, chat threads, analysis persistence, renders dir |
| `tests/backend/test_api.py` | 30 | Project/model/thread REST endpoints, file serving, execute_source, health check, assembly manifests, per-part download |
| `tests/backend/test_render.py` | 3 | Multi-angle renders (ISO/Front/Right/Top), Section cuts (X/Y) |
| `tests/backend/test_parameters.py` | 6 | Parameter extraction and injection, feature extraction (source spans) |
| `tests/backend/test_importer.py` | 4 | STEP/STL/GLB import and conversion |
| `tests/backend/test_web_research.py` | 3 | DuckDuckGo search integration |
| `tests/backend/cad/test_recipes.py` | 4 | Recipe retrieval, product-plan gating, negative-space requirements |
| `tests/backend/cad/test_example_bank.py` | 3 | Local CAD example-bank indexing, CadQuery-only retrieval, prompt context |
| `tests/backend/test_vision_recipe_context.py` | 1 | Vision prompt uses recipe/example context as an independent rubric |
| `tests/integration/test_imports.py` | 3 | Import API integration |

### Frontend — 76 tests, all passing (`npm test`)

| Test file | Count | Coverage |
|---|---|---|
| `src/test/stores.test.ts` | 25 | `useChatStore`, `useViewportStore`, `useCritiqueStore`, `useDebugStore` |
| `src/test/utils.test.ts` | 10 | `formatLocalDateTime`, WSMessage discriminated union typing |
| `src/test/CritiquePanel.test.tsx` | 14 | Score display, labels, issues list, severity badges, intent warning, dismiss, thumbnails |
| `src/test/Chat.test.tsx` | 14 | Welcome screen, suggestion buttons, send/enter, streaming, status indicator, CritiquePanel integration |
| `src/test/FeaturePanel.test.tsx` | 6 | Feature loading, list rendering, click-to-highlight, store sync |
| `src/test/AssemblyPanel.test.tsx` | 5 | Loading manifest, part visibility toggle, exploded view slider, selection, download part |
| `src/test/ParameterPanel.test.tsx` | 2 | Parameter loading, parameter value updates, update model API call |

### Run commands

```bash
# Backend
python -m pytest tests/backend/ -v

# Frontend
cd frontend && npm test
```
