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
- Intermediate execution updates
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

Examples:

- Select a face
- Select an edge
- Select a hole
- Ask AI to modify selected geometry

Example workflow:

```text
User selects hole
    ↓
User prompt:
"Increase this hole diameter by 2mm"
    ↓
Agent receives:
- selected topology
- feature metadata
- geometry context
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

Editable engineering constraints:

- Dimensions
- Wall thickness
- Tolerances
- Material assumptions
- Print volume
- Overhang restrictions
- Strength requirements

---

# Frontend Rendering Pipeline

## DO NOT Render STEP Directly in Browser

Recommended pipeline:

```text
STEP
    ↓
Backend tessellation
    ↓
glTF
    ↓
Three.js viewport
```

---

# Recommended Frontend Geometry Formats

| Format | Purpose |
|---|---|
| glTF | Frontend rendering |
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

### Responsibilities

- REST APIs
- WebSocket streaming
- Session management
- Artifact serving
- Job management

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

### Directory

```text
/backend/cad
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
Render
    ↓
Vision Critique
    ↓
Repair Geometry
    ↓
Repeat
```

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
- Failure escalation

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
Renderer
    ↓
Vision Critic
    ↓
Repair Loop
```

---

# Persistent Structured State

Do NOT rely on raw chat history.

Use structured state objects.

Example:

```python
class AgentState(BaseModel):
    user_goal: str
    constraints: list
    cad_source: str
    geometry_artifacts: list
    render_artifacts: list
    critique_results: list
    failure_history: list
    current_iteration: int
```

---

# Storage

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
│   ├── components/
│   ├── viewport/
│   ├── chat/
│   ├── constraints/
│   └── history/
│
├── backend/
│   ├── api/
│   ├── agent/
│   ├── domain/
│   ├── cad/
│   ├── render/
│   ├── vision/
│   ├── repair/
│   ├── models/
│   └── storage/
│
├── artifacts/
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
- Direct 3MF generation

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

Focus first on:

- Stable CAD generation
- Reliable geometry workflows
- Manufacturable outputs
- Repair iteration loops
- Strong browser UX
- AI-assisted engineering workflows
