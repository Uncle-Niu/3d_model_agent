# Mission Crafter — Agent improvements implementation plan

Hand-off doc for a fresh thread. Diagnostic + design work done in earlier
sessions (May 16-17, 2026). All findings are reproducible from
`data/projects/20260517-052822-cbf57006` (Turn 1) and
`data/projects/20260517-054740-d4627e23` (Turn 2).

## What's already on disk (ready to use, no more downloads needed)

```
data/recipe_sources/
  google_product_taxonomy/      5,595 product categories  (CC-BY)
  wikipedia_mechanical_components/  17 article summaries  (CC-BY-SA)
  printables_categories_seed/      80 functional 3D-print categories
  cadquery_example_index/         429 example .py files indexed (already-cloned repos)
  README.md                       full provenance, license, mining playbook

data/diffusion_models/
  sdxl_base_1.0/      ~6.5 GB    quality tier text->image
  sdxl_turbo/         ~6.5 GB    1-4 step text->image
  stable_zero123/     ~16 GB     single-view -> novel-view (two 8 GB fp32 ckpts; use `_c.ckpt`, load at fp16 for ~4 GB VRAM)
  README.md                      VRAM budget + smoke-test snippets

scripts/download_diffusion_models.py   # idempotent
scripts/download_recipe_sources.py     # idempotent
```

`pip` adds installed: `diffusers 0.38.0`, `transformers 5.8.1`,
`accelerate 1.13.0`, `safetensors`, `hf-xet`.

## Findings from the iPhone-holder test runs

1. **No exec timeout** — `process_cadquery_code` runs in
   `run_in_executor` with no timeout. Turn 1's generated source hit
   `.edges().fillet(2.0)` on a complex union-and-cut assembly, OCCT
   never returned, the FastAPI event loop froze for hours, every
   subsequent API call timed out, backend had to be killed manually.
   *Highest-priority operational fix.*

2. **Driver WS pings drop the connection during long generations** —
   `websockets.connect` default `ping_interval=20s`. qwen3.6:27b
   streaming blocks the server's event loop past 20s, ping fails,
   connection dies. Pipeline keeps running backend-side but the
   driver can't observe it. Mitigation: `ping_interval=None`
   in `scripts/ws_drive_turn.py`.

3. **qwen3.6:27b returns 0 fields in recall** — emits valid JSON but
   prefixes it with a multi-paragraph "thinking" block despite
   `/no_think`, so the 2048-token cap truncates the JSON mid-value.
   Parser correctly returns `{}`. Burns ~30s per turn for nothing.
   Mitigation: bump `max_tokens` to 4096 for recall, or route qwen
   later in the chain.

4. **Planner failed silently in Turn 2** — first-draft plan had
   `components=[]` and `key_features=[]`. `plan_repair` ran twice
   against the same qwen model, ended with two placeholder components
   and no dimensions. Code-gen proceeded against an essentially empty
   plan. Mitigation: deterministic rubric + escalation to a different
   model on repair (item 5).

5. **Recipe coverage is too thin** — `RECIPES` in
   `backend/cad/recipes.py` has 3 entries: tray, bracket, enclosure.
   "phone stand" matched `bracket_or_mount` (because of "holder")
   which then demanded mounting holes the design doesn't need.

## Implementation roadmap

Two buckets. Tackle bucket A first — these block everything else.
Bucket B is the actual product work and depends on A.

### Bucket A — operational fixes (1-2 days)

| # | Fix                                                                                  | Where                                       | Test                                                                                                                      |
|---|--------------------------------------------------------------------------------------|---------------------------------------------|---------------------------------------------------------------------------------------------------------------------------|
| A1| Wrap `process_cadquery_code` in `asyncio.wait_for(..., timeout=120)` (configurable). | `backend/agent/orchestrator.py` exec block  | Feed `data/projects/20260517-052822-cbf57006/models/model-001/source.py` as fixture — must surface a `timeout` failure type, not hang. |
| A2| `ping_interval=None, close_timeout=None` on the WS driver connect.                   | `scripts/ws_drive_turn.py:41`               | Run a long turn (>5 min), confirm driver stays connected.                                                                  |
| A3| Bump recall `max_tokens` from 2048 -> 4096; *also* strip leading `<think>...</think>` blocks before JSON parse. | `backend/knowledge/local_recall.py`         | Replay Turn 2 — qwen `field_count` should be ≥ 4, not 0.                                                                   |
| A4| Re-order `DEFAULT_MODEL_CHAIN` to put gemma4 or nemotron3 first for recall, qwen last. | `backend/knowledge/local_recall.py:46`     | Recall completes ≥ 5s faster on the iPhone prompt.                                                                          |
| A5| Add the *suggest, don't ban* `.edges().fillet()` guidance into the system prompt — see findings doc, do NOT add a hard rule. | `backend/models/llm_service.py` `build_system_prompt` | Single-body fillet examples still generated; multi-body intersecting code includes per-body fillet.                         |

### Bucket B — capability work (2-4 weeks)

| # | Capability                                                                                                                     | Depends on | Why this order                                                                 |
|---|--------------------------------------------------------------------------------------------------------------------------------|------------|--------------------------------------------------------------------------------|
| B1| **Recipe expansion**. Write a mining script that consumes `data/recipe_sources/` and emits ~150 `CadRecipe` entries. Validate by attempting a code-gen run per entry and keeping only those that execute. | A1         | Highest leverage: changes what classes of designs the planner can recognize.   |
| B2| **Conditional re-planning with deterministic rubric**. After plan returns, score with 5-bullet rubric (overall_dims present, components non-empty, all dimensions filled, key_features ≥ 3, bbox-sum sanity). On fail → re-run with a *different* model (gemma4) plus the missing-field list. | A3, A4     | Catches the empty-plan silent-fail mode from Turn 2.                            |
| B3| **Concept-sketch step before planning**. Add a 1-paragraph plain-English "what is this thing, orientation, mating context, ports" call before plan_design. Pass to planner as a separate `physical_context` block. | A4         | Fixes the under-specified-prompt failure mode (Turn 1 picked portrait/15°).    |
| B4| **Closed-loop physical-use grader** (deterministic, no LLM). Computes from bounding box and `<applied_forces>`: stability test (COM inside footprint), base-aspect test, tilt-not-too-steep test. Runs after vision critique, merges issues into the repair prompt. | A1         | Catches "will tip / has the wrong proportions" independently of vision.        |
| B5| **Reference image bank via SDXL + rendered CAD exemplars**. Two-source bank: (i) per-recipe text-to-image renders via SDXL (4 angles each via prompt), (ii) renders of known-good CAD exemplars from `data/cad_sources/`. Vision critic gets both the generated renders AND a small set of references; asks "does the design resemble these in pose/proportion?". User explicitly OK with references that don't agree with each other — the goal is "ideas/suggestions," not consensus. | B1         | Needs recipes to exist first (one reference per recipe). Needs A1 so VRAM swaps don't strand the backend. |
| B6| **Use Zero123 for multi-angle reference generation from a single LLM-generated front view**. Drop-in upgrade to B5 once the base text-to-image pipeline works. | B5         | Pure additive — fall back to multi-prompt SDXL if Zero123 quality is bad.      |
| B7| **Semantic parameter tagging** (sidecar JSON). Each generated model gets a `parameters.json` mapping every `*_mm`/`*_deg` variable in the source to `{role: device_spec|tolerance|design_choice, applies_to, min, max}`. Frontend uses these to render grouped sliders + disable editing of true device specs. | A1         | Pure additive, no risk to existing flows.                                       |
| B8| **Parametric "snap-back"** (built on B7). When the user changes a `device_spec` parameter (e.g. swap iPhone 14 -> iPhone 15), all `applies_to` parameters re-derive from the new device's recalled fields. | B7         | Optional polish layer.                                                          |
| B9| **Semantic dedup in repair history**. Today repair history compares error signatures. Add a one-line "what did this attempt try, and how does it differ from the previous attempt" emitted by the LLM after each failure; surface to next prompt to catch design-level churn (not just error churn). | A1         | Quality improvement to existing repair loop.                                    |

## VRAM strategy (RTX 5090, 32 GB)

Only one of {`Ollama LLM`, `Ollama vision LLM`, `diffusion pipeline`}
resident at a time. Before loading SDXL/Zero123:

```python
# Force Ollama to unload immediately
httpx.post("http://localhost:11434/api/generate",
           json={"model": "qwen3.6:27b", "keep_alive": 0})
```

After image generation:

```python
del pipe
torch.cuda.empty_cache()
```

See `data/diffusion_models/README.md` for the full table.

## Repro fixtures (don't delete)

- `data/projects/20260517-052822-cbf57006/models/model-001/source.py` —
  the source that hangs CadQuery's fillet pass. Use as the test for A1.
- `data/projects/20260517-054740-d4627e23/` — the project whose Turn 2
  planner produced an empty plan; useful for B2 testing.

## Out of scope for this batch

- Realtime web search. Explicitly off.
- Cloud LLM calls. Explicitly off.
- Hard ban on `.edges().fillet()` — would break legitimate single-body
  recipes; we go with prompt guidance + the exec timeout instead.
