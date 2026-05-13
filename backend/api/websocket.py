"""
WebSocket handler for real-time chat and CAD generation.

Full agentic pipeline:
  user message → LLM → CadQuery → validate → render → vision critique → repair → repeat

Sends detailed debug_log messages so the frontend can display raw LLM request/response info.
"""

from __future__ import annotations

import asyncio
import json
import time
import traceback
from datetime import datetime

import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..cad.engine import process_cadquery_code
from ..domain.models import (
    ChatMessage,
    CritiqueReport,
    FailureType,
    GeometryStats,
    ModelMetadata,
)
from ..models.llm_service import LLMService, build_system_prompt, extract_code_from_response
from ..storage import StorageService

ws_router = APIRouter()

# Generation constants
MAX_REPAIR_ITERATIONS = 5
LOCAL_MODEL_RETRIES = 3
# Vision critique only triggers after a successful CAD execution
VISION_SCORE_THRESHOLD = 0.65  # below this score → trigger vision-driven repair


# ---------------------------------------------------------------------------
# WebSocket helpers
# ---------------------------------------------------------------------------

async def _send(ws: WebSocket, msg: dict) -> None:
    await ws.send_text(json.dumps(msg))


async def _send_status(ws: WebSocket, stage: str, message: str) -> None:
    await _send(ws, {"type": "status", "stage": stage, "message": message})


async def _send_debug(ws: WebSocket, category: str, message: str, data: dict | None = None) -> None:
    payload: dict = {
        "type": "debug_log",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "category": category,
        "message": message,
    }
    if data:
        payload["data"] = data
    await _send(ws, payload)


# ---------------------------------------------------------------------------
# Ollama connectivity check
# ---------------------------------------------------------------------------

async def _check_ollama_connectivity(ws: WebSocket, llm: LLMService) -> bool:
    ollama_base = llm.base_url.replace("/v1", "")
    await _send_debug(ws, "ollama", f"Checking Ollama at {ollama_base} ...")

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{ollama_base}/api/tags")
            if resp.status_code != 200:
                await _send(ws, {"type": "error", "message": f"Ollama returned HTTP {resp.status_code}. Is it running?"})
                return False

            data = resp.json()
            models = [m["name"] for m in data.get("models", [])]
            model_available = llm.model in models

            await _send_debug(ws, "ollama", "Ollama connected", {
                "available_models": models,
                "configured_model": llm.model,
                "model_available": model_available,
            })

            if not model_available:
                await _send(ws, {"type": "error", "message": (
                    f"Model '{llm.model}' is not pulled in Ollama. "
                    f"Available: {', '.join(models) or '(none)'}. "
                    f"Run `ollama pull {llm.model}` first."
                )})
                return False
            return True

    except httpx.ConnectError:
        await _send(ws, {"type": "error", "message": (
            f"Cannot connect to Ollama at {ollama_base}. "
            "Make sure Ollama is running (`ollama serve`)."
        )})
        return False
    except Exception as e:
        await _send(ws, {"type": "error", "message": f"Ollama check failed: {e}"})
        return False


# ---------------------------------------------------------------------------
# Rendering step (thread-pool)
# ---------------------------------------------------------------------------

async def _run_render(ws: WebSocket, shape, model_dir) -> dict[str, str]:
    """
    Render multi-angle PNGs from a CadQuery shape.
    Returns dict of view_name → file_path, or empty dict on failure.
    """
    await _send_status(ws, "rendering", "Generating multi-angle renders...")
    try:
        from ..render.renderer import render_shape_multiangle
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            render_shape_multiangle,
            shape,
            model_dir,
            "part",
        )
        if result.success:
            await _send_debug(ws, "render", f"Renders complete: {list(result.renders.keys())}", {
                "views": list(result.renders.keys()),
                "paths": result.renders,
            })
            return result.renders
        else:
            await _send_debug(ws, "render", f"Render failed: {result.message}", {"message": result.message})
            return {}
    except Exception as e:
        await _send_debug(ws, "render", f"Render error: {e}", {"traceback": traceback.format_exc()})
        return {}


# ---------------------------------------------------------------------------
# Vision critique step (async)
# ---------------------------------------------------------------------------

async def _run_vision_critique(
    ws: WebSocket,
    render_paths: dict[str, str],
    user_intent: str,
    geometry_stats: dict,
    project_id: str,
    model_id: str,
) -> CritiqueReport | None:
    """
    Run vision critique on rendered images.
    Returns CritiqueReport or None if unavailable/skipped.
    Sends critique_result WebSocket message on success.
    """
    if not render_paths:
        await _send_debug(ws, "vision", "Skipping vision critique — no renders available")
        return None

    await _send_status(ws, "critiquing", "Analyzing geometry with vision AI...")

    try:
        from ..vision.critic import VisionCritic
        critic = VisionCritic()

        # Check availability first (non-blocking)
        available, avail_msg = await critic.is_available()
        if not available:
            await _send_debug(ws, "vision", f"Vision model unavailable — skipping critique: {avail_msg}")
            return None

        await _send_debug(ws, "vision", f"Sending {len(render_paths)} renders to vision model '{critic.model}'", {
            "model": critic.model,
            "views": list(render_paths.keys()),
            "geometry_stats": geometry_stats,
        })

        critique_result = await critic.critique(
            render_paths=render_paths,
            user_intent=user_intent,
            geometry_stats=geometry_stats if geometry_stats else None,
        )

        if not critique_result.success:
            await _send_debug(ws, "vision", f"Vision critique failed: {critique_result.message}")
            return None

        report = critique_result.report

        await _send_debug(ws, "vision", critique_result.message, {
            "score": report.overall_printability,
            "issues": [{"type": i.issue_type, "severity": i.severity, "desc": i.description} for i in report.issues],
            "matches_intent": critique_result.matches_intent,
            "repair_prompt": critique_result.repair_prompt[:300] if critique_result.repair_prompt else "",
        })

        # Build render URLs for frontend
        render_urls: dict[str, str] = {}
        for view_name in render_paths:
            render_urls[view_name] = (
                f"/api/projects/{project_id}/models/{model_id}/renders/{view_name}"
            )

        # Emit critique_result to frontend
        await _send(ws, {
            "type": "critique_result",
            "score": report.overall_printability,
            "matches_intent": critique_result.matches_intent,
            "issues": [
                {
                    "issue_type": i.issue_type,
                    "severity": i.severity,
                    "description": i.description,
                    "location_hint": i.location_hint,
                }
                for i in report.issues
            ],
            "repair_prompt": critique_result.repair_prompt,
            "render_urls": render_urls,
        })

        # Update report with repair info
        report.repair_prompt = critique_result.repair_prompt
        report.matches_intent = critique_result.matches_intent
        return report

    except Exception as e:
        await _send_debug(ws, "vision", f"Vision critique exception: {e}", {
            "traceback": traceback.format_exc(),
        })
        return None


# ---------------------------------------------------------------------------
# Build vision-driven repair prompt
# ---------------------------------------------------------------------------

def _build_vision_repair_prompt(
    original_code: str,
    critique: CritiqueReport,
    user_intent: str,
    iteration: int,
) -> str:
    """Build a repair prompt that incorporates vision critique findings."""
    issues_text = "\n".join(
        f"- [{i.severity.upper()}] {i.issue_type}: {i.description}"
        + (f" (location: {i.location_hint})" if i.location_hint else "")
        for i in critique.issues
    )

    vision_repair = critique.repair_prompt or "Fix the identified issues and improve model quality."

    return f"""The CAD model was reviewed by a vision AI and needs improvement.

## Original User Intent
{user_intent}

## Current Code
```python
{original_code}
```

## Vision Critique (iteration {iteration})
Printability score: {critique.overall_printability:.2f}/1.0
Matches intent: {critique.matches_intent}

### Issues Found
{issues_text if issues_text else "No specific issues listed, but score is low."}

## Required Fixes
{vision_repair}

## Instructions
- Fix all ERROR severity issues first, then WARNING issues
- Keep the same overall design intent: "{user_intent}"
- Output ONLY corrected CadQuery Python code in a ```python block
- Assign the final shape to `result`
"""


# ---------------------------------------------------------------------------
# LLM code generation (with streaming)
# ---------------------------------------------------------------------------

async def _generate_code_streaming(
    ws: WebSocket,
    llm: LLMService,
    user_message: str,
    system_prompt: str,
    chat_ctx: list[dict],
    current_source: str,
    current_model_id: str | None,
) -> str:
    """Generate CadQuery code via streaming LLM. Returns extracted code."""
    effective_user_message = user_message
    if current_source:
        effective_user_message = (
            "The project has one model with versioned checkpoints. "
            f"Use checkpoint `{current_model_id}` as the current base model and edit it for this request.\n\n"
            "## Current CadQuery Source\n"
            "```python\n"
            f"{current_source}\n"
            "```\n\n"
            "## Requested Change\n"
            f"{user_message}"
        )

    await _send_debug(ws, "llm_request", "Sending request to LLM", {
        "model": llm.model,
        "base_url": llm.base_url,
        "messages_count": len(chat_ctx) + 2,
        "system_prompt_length": len(system_prompt),
        "user_message": user_message,
        "base_model_id": current_model_id,
        "included_current_source": bool(current_source),
    })

    full_response = ""
    token_count = 0
    t_start = time.time()

    try:
        async for chunk in llm.generate_stream(effective_user_message, system_prompt, chat_ctx):
            full_response += chunk
            token_count += 1
            await _send(ws, {"type": "llm_chunk", "content": chunk})
    except Exception as e:
        elapsed = time.time() - t_start
        await _send_debug(ws, "llm_error", f"LLM streaming failed after {elapsed:.1f}s", {
            "error": str(e), "traceback": traceback.format_exc(),
        })
        raise

    elapsed = time.time() - t_start
    await _send_debug(ws, "llm_response", f"LLM response complete ({elapsed:.1f}s)", {
        "total_tokens": token_count,
        "response_length": len(full_response),
        "elapsed_seconds": round(elapsed, 2),
        "tokens_per_second": round(token_count / elapsed, 1) if elapsed > 0 else 0,
    })

    code = extract_code_from_response(full_response)
    await _send_debug(ws, "code_extraction", "Code extracted from LLM response", {
        "code_length": len(code),
        "code": code,
        "had_python_block": "```python" in full_response,
    })
    return code


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------

@ws_router.websocket("/ws/{project_id}")
async def websocket_endpoint(ws: WebSocket, project_id: str):
    await ws.accept()

    storage: StorageService = ws.app.state.storage
    config = storage.get_project(project_id)
    if not config:
        await _send(ws, {"type": "error", "message": "Project not found"})
        await ws.close()
        return

    llm = LLMService()

    await _send_debug(ws, "init", "WebSocket connected", {
        "project_id": project_id,
        "llm_base_url": llm.base_url,
        "llm_model": llm.model,
    })

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await _send(ws, {"type": "error", "message": "Invalid JSON"})
                continue

            msg_type = msg.get("type")

            if msg_type == "chat_message":
                content = msg.get("content", "").strip()
                thread_id = msg.get("thread_id") or ws.query_params.get("thread_id") or "legacy"
                base_model_id = msg.get("base_model_id")
                if not content:
                    await _send(ws, {"type": "error", "message": "Empty message"})
                    continue

                storage.append_chat_thread_message(
                    project_id, thread_id,
                    ChatMessage(role="user", content=content),
                )

                await _run_generation_pipeline(
                    ws=ws, llm=llm, storage=storage,
                    project_id=project_id, thread_id=thread_id,
                    base_model_id=base_model_id, user_message=content,
                )

            elif msg_type == "ping":
                await _send(ws, {"type": "pong"})
            else:
                await _send(ws, {"type": "error", "message": f"Unknown message type: {msg_type}"})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await _send(ws, {"type": "error", "message": str(e)})
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Full generation pipeline (agentic loop)
# ---------------------------------------------------------------------------

async def _run_generation_pipeline(
    ws: WebSocket,
    llm: LLMService,
    storage: StorageService,
    project_id: str,
    thread_id: str,
    base_model_id: str | None,
    user_message: str,
):
    """
    Full agentic pipeline with vision-driven repair loop.

    Loop per iteration:
    1. Generate/repair CadQuery code via LLM
    2. Execute + validate geometry (deterministic)
    3. Export STEP/STL/GLB
    4. Render multi-angle PNGs (server-side)
    5. Vision critique (multimodal LLM)
    6. If score < threshold → vision-driven repair → loop back
    7. On success → send model_ready + chat_response
    """
    config = storage.get_project(project_id)
    if not config:
        await _send(ws, {"type": "error", "message": "Project not found"})
        return

    # Check Ollama
    ollama_ok = await _check_ollama_connectivity(ws, llm)
    if not ollama_ok:
        storage.append_chat_thread_message(
            project_id, thread_id,
            ChatMessage(role="assistant", content=(
                f"❌ Cannot reach Ollama. Please make sure Ollama is running "
                f"and the model `{llm.model}` is available."
            )),
        )
        return

    # Load context
    system_prompt = build_system_prompt(config.hard_constraints, config.soft_constraints)
    history = storage.get_chat_thread_messages(project_id, thread_id)
    chat_ctx = [{"role": m.role, "content": m.content} for m in history[-10:]]

    current_source = ""
    current_model_id = base_model_id
    if current_model_id:
        current_source = storage.get_model_source_text(project_id, current_model_id)
    if not current_source:
        latest_model = storage.latest_successful_model(project_id)
        if latest_model:
            current_model_id = latest_model.model_id
            current_source = storage.get_model_source_text(project_id, latest_model.model_id)

    last_code = ""
    last_error = ""
    last_critique: CritiqueReport | None = None
    best_model_id: str | None = None
    best_score: float = 0.0

    for iteration in range(1, MAX_REPAIR_ITERATIONS + 1):
        try:
            # ── Step 1: Generate or Repair code ──────────────────────────────
            if iteration == 1:
                await _send_status(ws, "generating", "Generating CadQuery code...")
                try:
                    last_code = await _generate_code_streaming(
                        ws, llm, user_message, system_prompt, chat_ctx,
                        current_source, current_model_id,
                    )
                except Exception as e:
                    storage.append_chat_thread_message(
                        project_id, thread_id,
                        ChatMessage(role="assistant", content=f"❌ LLM call failed: {e}"),
                    )
                    return
            elif last_critique and last_critique.issues:
                # Vision-driven repair
                await _send_status(ws, "repairing",
                    f"Vision-driven repair (attempt {iteration}/{MAX_REPAIR_ITERATIONS})...")
                repair_prompt = _build_vision_repair_prompt(
                    last_code, last_critique, user_message, iteration
                )
                await _send_debug(ws, "repair_request", f"Vision repair attempt {iteration}", {
                    "score": last_critique.overall_printability,
                    "issues_count": len(last_critique.issues),
                    "repair_prompt_preview": repair_prompt[:400],
                })
                repair_response = await llm.repair_cadquery(
                    original_code=last_code,
                    error_message=repair_prompt,
                    iteration=iteration,
                    hard_constraints=config.hard_constraints,
                    soft_constraints=config.soft_constraints,
                )
                last_code = extract_code_from_response(repair_response)
                await _send_debug(ws, "code_extraction", "Vision-repaired code extracted", {
                    "code_length": len(last_code), "code": last_code,
                })
            else:
                # Execution-error repair
                await _send_status(ws, "repairing",
                    f"Repairing code (attempt {iteration}/{MAX_REPAIR_ITERATIONS})...")
                await _send_debug(ws, "repair_request", f"Error repair attempt {iteration}", {
                    "original_code": last_code, "error_message": last_error[:500],
                })
                repair_response = await llm.repair_cadquery(
                    original_code=last_code,
                    error_message=last_error,
                    iteration=iteration,
                    hard_constraints=config.hard_constraints,
                    soft_constraints=config.soft_constraints,
                )
                last_code = extract_code_from_response(repair_response)

            if not last_code.strip():
                last_error = "LLM returned empty code"
                continue

            # ── Step 2: Execute CadQuery ──────────────────────────────────────
            await _send_status(ws, "executing", "Running CadQuery code...")
            await _send_debug(ws, "cadquery_exec", "Executing CadQuery code...", {
                "code": last_code, "iteration": iteration,
            })

            model_id = storage.next_model_id(project_id)
            model_dir = storage.create_model_dir(project_id, model_id)

            t_exec_start = time.time()
            exec_result = await asyncio.get_event_loop().run_in_executor(
                None,
                process_cadquery_code,
                last_code,
                model_dir,
                "part",
                config.hard_constraints,
            )
            t_exec_elapsed = time.time() - t_exec_start

            await _send_debug(ws, "cadquery_result",
                f"CadQuery execution {'succeeded' if exec_result['success'] else 'failed'} ({t_exec_elapsed:.2f}s)", {
                    "success": exec_result["success"],
                    "message": exec_result["message"],
                    "files": exec_result.get("files", {}),
                    "violations": exec_result.get("violations", []),
                    "warnings": exec_result.get("warnings", []),
                    "geometry_stats": exec_result.get("geometry_stats", {}),
                    "failure_type": exec_result.get("failure_type"),
                    "elapsed_seconds": round(t_exec_elapsed, 2),
                })

            if not exec_result["success"]:
                last_error = exec_result["message"]
                last_critique = None  # reset vision critique for error repair

                metadata = ModelMetadata(
                    model_id=model_id,
                    prompt=user_message,
                    cad_source=last_code,
                    failure_type=FailureType(exec_result.get("failure_type") or "execution_error"),
                    failure_message=last_error,
                    iteration=iteration,
                )
                storage.save_model_metadata(project_id, metadata)

                if iteration < MAX_REPAIR_ITERATIONS:
                    await _send_status(ws, "failed",
                        f"Attempt {iteration} failed: {last_error[:200]}")
                    continue
                else:
                    await _send(ws, {
                        "type": "error",
                        "message": f"Failed after {MAX_REPAIR_ITERATIONS} attempts: {last_error[:300]}",
                        "failure_type": "max_retries_exceeded",
                    })
                    _save_failure_chat(storage, project_id, thread_id, user_message, model_id, MAX_REPAIR_ITERATIONS)
                    return

            # ── Step 3: Notify frontend (model visible early) ─────────────────
            await _send_status(ws, "tessellating", "Preparing 3D preview...")
            glb_url = f"/api/projects/{project_id}/models/{model_id}/glb"

            await _send(ws, {
                "type": "model_ready",
                "model_id": model_id,
                "glb_url": glb_url,
            })

            best_model_id = model_id

            # ── Step 4: Render multi-angle PNGs ──────────────────────────────
            shape = exec_result.get("_shape")
            render_paths: dict[str, str] = {}
            if shape is not None:
                render_paths = await _run_render(ws, shape, model_dir)

            # ── Step 5: Vision Critique ───────────────────────────────────────
            geometry_stats = exec_result.get("geometry_stats", {})
            critique: CritiqueReport | None = None

            if render_paths:
                critique = await _run_vision_critique(
                    ws=ws,
                    render_paths=render_paths,
                    user_intent=user_message,
                    geometry_stats=geometry_stats,
                    project_id=project_id,
                    model_id=model_id,
                )

            # Build geometry stats domain model
            geo_stats_model: GeometryStats | None = None
            if geometry_stats:
                geo_stats_model = GeometryStats(**{
                    k: v for k, v in geometry_stats.items()
                    if k in GeometryStats.model_fields
                })

            # Save metadata (with critique if available)
            metadata = ModelMetadata(
                model_id=model_id,
                prompt=user_message,
                cad_source=last_code,
                has_step="step" in exec_result["files"],
                has_stl="stl" in exec_result["files"],
                has_glb="glb" in exec_result["files"],
                has_render=bool(render_paths),
                render_paths=render_paths,
                critique=critique,
                geometry_stats=geo_stats_model,
                iteration=iteration,
                vision_score=critique.overall_printability if critique else None,
            )
            storage.save_model_metadata(project_id, metadata)

            # ── Step 6: Decide whether to repair via vision ───────────────────
            vision_score = critique.overall_printability if critique else 1.0
            has_errors = critique and any(i.severity == "error" for i in critique.issues)
            needs_repair = critique and (vision_score < VISION_SCORE_THRESHOLD or has_errors)

            if needs_repair and iteration < MAX_REPAIR_ITERATIONS:
                last_critique = critique
                best_score = vision_score
                await _send_debug(ws, "vision_repair_trigger",
                    f"Vision score {vision_score:.2f} below threshold {VISION_SCORE_THRESHOLD} — triggering repair", {
                        "score": vision_score,
                        "error_issues": [i.description for i in critique.issues if i.severity == "error"],
                    })
                await _send_status(ws, "repairing",
                    f"Vision critique found issues (score {vision_score:.2f}) — improving model...")
                continue  # loop back to repair

            # ── Step 7: Success ───────────────────────────────────────────────
            # Build response text
            violations_text = ""
            if exec_result.get("violations"):
                violations_text = "\n\n⚠️ Constraint Violations:\n" + "\n".join(
                    f"- {v}" for v in exec_result["violations"]
                )

            warnings_text = ""
            if exec_result.get("warnings"):
                warnings_text = "\n\n💡 Warnings:\n" + "\n".join(
                    f"- {w}" for w in exec_result["warnings"]
                )

            critique_text = ""
            if critique:
                score_emoji = "✅" if vision_score >= 0.8 else "🟡" if vision_score >= 0.65 else "🔴"
                critique_text = f"\n\n{score_emoji} Vision critique score: **{vision_score:.2f}**/1.0"
                if critique.issues:
                    error_issues = [i for i in critique.issues if i.severity == "error"]
                    warn_issues = [i for i in critique.issues if i.severity == "warning"]
                    if error_issues:
                        critique_text += f"\n- {len(error_issues)} error(s): " + "; ".join(i.description[:60] for i in error_issues[:3])
                    if warn_issues:
                        critique_text += f"\n- {len(warn_issues)} warning(s): " + "; ".join(i.description[:60] for i in warn_issues[:2])
                else:
                    critique_text += "\n- No printability issues found 🎉"

            stats_text = ""
            if geometry_stats.get("bounding_box"):
                stats_text = f"\n\n📐 Size: {geometry_stats['bounding_box']}"
                if geometry_stats.get("estimated_mass_pla"):
                    stats_text += f" | Est. weight: {geometry_stats['estimated_mass_pla']}"

            response_text = (
                f"✓ Model generated (`{model_id}`, attempt {iteration})."
                f"{stats_text}{critique_text}{violations_text}{warnings_text}"
            )

            await _send(ws, {"type": "chat_response", "content": response_text})
            storage.append_chat_thread_message(
                project_id, thread_id,
                ChatMessage(role="assistant", content=response_text, model_id=model_id),
            )
            return

        except Exception as e:
            last_error = f"Unexpected error: {traceback.format_exc()}"
            last_critique = None
            await _send_debug(ws, "pipeline_error", f"Pipeline error at iteration {iteration}", {
                "error": str(e), "traceback": traceback.format_exc(),
            })
            if iteration >= MAX_REPAIR_ITERATIONS:
                await _send(ws, {
                    "type": "error",
                    "message": f"Pipeline error: {str(e)}",
                    "failure_type": "unexpected_error",
                })
                return


def _save_failure_chat(
    storage: StorageService,
    project_id: str,
    thread_id: str,
    user_message: str,
    model_id: str,
    max_iters: int,
) -> None:
    storage.append_chat_thread_message(
        project_id, thread_id,
        ChatMessage(
            role="assistant",
            content=(
                f"I wasn't able to generate a valid model after {max_iters} attempts. "
                "Could you try rephrasing your request or simplifying the design?"
            ),
            model_id=model_id,
        ),
    )
