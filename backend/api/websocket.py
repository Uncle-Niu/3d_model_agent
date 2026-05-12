"""
WebSocket handler for real-time chat and CAD generation.

Handles the full pipeline: user message → LLM → CadQuery → export → notify frontend.
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
    FailureType,
    ModelMetadata,
)
from ..models.llm_service import LLMService, build_system_prompt, extract_code_from_response
from ..storage import StorageService

ws_router = APIRouter()

# Generation constants
MAX_REPAIR_ITERATIONS = 5
LOCAL_MODEL_RETRIES = 3


async def _send(ws: WebSocket, msg: dict) -> None:
    """Send a JSON message over WebSocket."""
    await ws.send_text(json.dumps(msg))


async def _send_status(ws: WebSocket, stage: str, message: str) -> None:
    await _send(ws, {"type": "status", "stage": stage, "message": message})


async def _send_debug(ws: WebSocket, category: str, message: str, data: dict | None = None) -> None:
    """Send a debug_log message to the frontend for the debug panel."""
    payload: dict = {
        "type": "debug_log",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "category": category,
        "message": message,
    }
    if data:
        payload["data"] = data
    await _send(ws, payload)


async def _check_ollama_connectivity(ws: WebSocket, llm: LLMService) -> bool:
    """
    Check that Ollama is reachable and the configured model is available.
    Sends debug_log messages with the result.
    Returns True if OK, False if there's a problem.
    """
    ollama_base = llm.base_url.replace("/v1", "")

    await _send_debug(ws, "ollama", f"Checking Ollama at {ollama_base} ...")

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{ollama_base}/api/tags")
            if resp.status_code != 200:
                await _send_debug(ws, "ollama", f"Ollama returned HTTP {resp.status_code}", {
                    "url": f"{ollama_base}/api/tags",
                    "status_code": resp.status_code,
                })
                await _send(ws, {
                    "type": "error",
                    "message": f"Ollama returned HTTP {resp.status_code}. Is it running?",
                })
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
                await _send_debug(ws, "ollama",
                    f"⚠️ Model '{llm.model}' not found. Available: {models}. "
                    f"Run: ollama pull {llm.model}",
                )
                await _send(ws, {
                    "type": "error",
                    "message": (
                        f"Model '{llm.model}' is not pulled in Ollama. "
                        f"Available models: {', '.join(models) or '(none)'}. "
                        f"Run `ollama pull {llm.model}` first."
                    ),
                })
                return False

            return True

    except httpx.ConnectError:
        await _send_debug(ws, "ollama", f"❌ Cannot connect to Ollama at {ollama_base}", {
            "url": ollama_base,
            "error": "Connection refused",
        })
        await _send(ws, {
            "type": "error",
            "message": (
                f"Cannot connect to Ollama at {ollama_base}. "
                "Make sure Ollama is running (start it with `ollama serve` or launch the Ollama app)."
            ),
        })
        return False
    except Exception as e:
        await _send_debug(ws, "ollama", f"❌ Ollama check failed: {e}", {
            "error": str(e),
            "traceback": traceback.format_exc(),
        })
        await _send(ws, {
            "type": "error",
            "message": f"Ollama check failed: {e}",
        })
        return False


@ws_router.websocket("/ws/{project_id}")
async def websocket_endpoint(ws: WebSocket, project_id: str):
    """
    WebSocket endpoint for a project.

    Protocol:
      Client sends: { "type": "chat_message", "content": "..." }
      Server sends: status updates, llm_chunks, debug_log, model_ready, errors
    """
    await ws.accept()

    storage: StorageService = ws.app.state.storage

    # Verify project exists
    config = storage.get_project(project_id)
    if not config:
        await _send(ws, {"type": "error", "message": "Project not found"})
        await ws.close()
        return

    # Initialize LLM service (lazy)
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
                if not content:
                    await _send(ws, {"type": "error", "message": "Empty message"})
                    continue

                # Save user message
                storage.append_chat_message(
                    project_id,
                    ChatMessage(role="user", content=content),
                )

                # Run the CAD generation pipeline
                await _run_generation_pipeline(
                    ws=ws,
                    llm=llm,
                    storage=storage,
                    project_id=project_id,
                    user_message=content,
                )
            elif msg_type == "ping":
                await _send(ws, {"type": "pong"})
            else:
                await _send(ws, {
                    "type": "error",
                    "message": f"Unknown message type: {msg_type}",
                })

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await _send(ws, {"type": "error", "message": str(e)})
        except Exception:
            pass


async def _run_generation_pipeline(
    ws: WebSocket,
    llm: LLMService,
    storage: StorageService,
    project_id: str,
    user_message: str,
):
    """
    Full generation pipeline with repair loop.

    1. Check Ollama connectivity
    2. Generate CadQuery code via LLM (with debug streaming)
    3. Execute and export
    4. If failure, retry up to MAX_REPAIR_ITERATIONS
    5. Send model_ready when done
    """
    config = storage.get_project(project_id)
    if not config:
        await _send(ws, {"type": "error", "message": "Project not found"})
        return

    # --- Step 0: Check Ollama before doing anything ---
    ollama_ok = await _check_ollama_connectivity(ws, llm)
    if not ollama_ok:
        useChatStore_msg = (
            "❌ Cannot reach Ollama. Please make sure Ollama is running "
            f"and the model `{llm.model}` is available."
        )
        storage.append_chat_message(
            project_id,
            ChatMessage(role="assistant", content=useChatStore_msg),
        )
        return

    last_code = ""
    last_error = ""

    for iteration in range(1, MAX_REPAIR_ITERATIONS + 1):
        try:
            # --- Step 1: Generate / Repair code ---
            if iteration == 1:
                await _send_status(ws, "generating", "Generating CadQuery code...")

                # Build chat history context (last few messages)
                history = storage.get_chat_history(project_id)
                chat_ctx = []
                for msg in history[-10:]:  # last 10 messages for context
                    chat_ctx.append({"role": msg.role, "content": msg.content})

                system_prompt = build_system_prompt(
                    config.hard_constraints, config.soft_constraints
                )

                # --- Debug: send the raw LLM request ---
                messages_payload = [{"role": "system", "content": system_prompt}]
                messages_payload.extend(chat_ctx)
                messages_payload.append({"role": "user", "content": user_message})

                await _send_debug(ws, "llm_request", "Sending request to LLM", {
                    "model": llm.model,
                    "base_url": llm.base_url,
                    "temperature": 0.3,
                    "max_tokens": 4096,
                    "stream": True,
                    "messages_count": len(messages_payload),
                    "system_prompt_length": len(system_prompt),
                    "system_prompt_preview": system_prompt[:500] + ("..." if len(system_prompt) > 500 else ""),
                    "user_message": user_message,
                    "chat_context_messages": len(chat_ctx),
                })

                # Stream the LLM response
                full_response = ""
                token_count = 0
                t_start = time.time()

                try:
                    async for chunk in llm.generate_stream(
                        user_message, system_prompt, chat_ctx
                    ):
                        full_response += chunk
                        token_count += 1
                        await _send(ws, {"type": "llm_chunk", "content": chunk})
                except Exception as e:
                    elapsed = time.time() - t_start
                    await _send_debug(ws, "llm_error", f"LLM streaming failed after {elapsed:.1f}s", {
                        "error": str(e),
                        "traceback": traceback.format_exc(),
                        "tokens_received": token_count,
                        "partial_response_length": len(full_response),
                    })
                    await _send(ws, {
                        "type": "error",
                        "message": f"LLM call failed: {e}",
                        "failure_type": "llm_error",
                    })
                    storage.append_chat_message(
                        project_id,
                        ChatMessage(
                            role="assistant",
                            content=f"❌ LLM call failed: {e}. Is Ollama running with model `{llm.model}`?",
                        ),
                    )
                    return

                elapsed = time.time() - t_start
                await _send_debug(ws, "llm_response", f"LLM response complete ({elapsed:.1f}s)", {
                    "total_tokens": token_count,
                    "response_length": len(full_response),
                    "elapsed_seconds": round(elapsed, 2),
                    "tokens_per_second": round(token_count / elapsed, 1) if elapsed > 0 else 0,
                    "response_preview": full_response[:300] + ("..." if len(full_response) > 300 else ""),
                })

                # Extract code
                last_code = extract_code_from_response(full_response)

                await _send_debug(ws, "code_extraction", "Code extracted from LLM response", {
                    "code_length": len(last_code),
                    "code": last_code,
                    "had_python_block": "```python" in full_response,
                    "had_any_block": "```" in full_response,
                })

            else:
                await _send_status(
                    ws, "repairing",
                    f"Repairing code (attempt {iteration}/{MAX_REPAIR_ITERATIONS})..."
                )

                await _send_debug(ws, "repair_request", f"Repair attempt {iteration}", {
                    "original_code": last_code,
                    "error_message": last_error[:500],
                })

                repair_response = await llm.repair_cadquery(
                    original_code=last_code,
                    error_message=last_error,
                    iteration=iteration,
                    hard_constraints=config.hard_constraints,
                    soft_constraints=config.soft_constraints,
                )

                await _send_debug(ws, "repair_response", "Repair response received", {
                    "response_length": len(repair_response),
                    "response_preview": repair_response[:300],
                })

                last_code = extract_code_from_response(repair_response)

                await _send_debug(ws, "code_extraction", "Repaired code extracted", {
                    "code_length": len(last_code),
                    "code": last_code,
                })

            if not last_code.strip():
                last_error = "LLM returned empty code"
                await _send_debug(ws, "error", "Empty code from LLM", {
                    "iteration": iteration,
                })
                continue

            # --- Step 2: Execute CadQuery ---
            await _send_status(ws, "executing", "Running CadQuery code...")
            await _send_debug(ws, "cadquery_exec", "Executing CadQuery code...", {
                "code": last_code,
                "iteration": iteration,
            })

            model_id = storage.next_model_id(project_id)
            model_dir = storage.create_model_dir(project_id, model_id)

            t_exec_start = time.time()

            # Run in thread pool to avoid blocking the event loop
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                process_cadquery_code,
                last_code,
                model_dir,
                "part",
                config.hard_constraints,
            )

            t_exec_elapsed = time.time() - t_exec_start

            await _send_debug(ws, "cadquery_result", f"CadQuery execution {'succeeded' if result['success'] else 'failed'} ({t_exec_elapsed:.2f}s)", {
                "success": result["success"],
                "message": result["message"],
                "files": result.get("files", {}),
                "violations": result.get("violations", []),
                "elapsed_seconds": round(t_exec_elapsed, 2),
            })

            if not result["success"]:
                last_error = result["message"]
                # Save failed attempt metadata
                metadata = ModelMetadata(
                    model_id=model_id,
                    prompt=user_message,
                    cad_source=last_code,
                    failure_type=FailureType.EXECUTION_ERROR,
                    failure_message=last_error,
                    iteration=iteration,
                )
                storage.save_model_metadata(project_id, metadata)

                if iteration < MAX_REPAIR_ITERATIONS:
                    await _send_status(
                        ws, "failed",
                        f"Attempt {iteration} failed: {last_error[:200]}"
                    )
                    continue
                else:
                    # Final failure
                    await _send(ws, {
                        "type": "error",
                        "message": f"Failed after {MAX_REPAIR_ITERATIONS} attempts: {last_error[:300]}",
                        "failure_type": "max_retries_exceeded",
                    })
                    # Save assistant failure message
                    storage.append_chat_message(
                        project_id,
                        ChatMessage(
                            role="assistant",
                            content=f"I wasn't able to generate a valid model after {MAX_REPAIR_ITERATIONS} attempts. Last error: {last_error[:200]}. Could you try rephrasing your request or simplifying the design?",
                            model_id=model_id,
                        ),
                    )
                    return

            # --- Step 3: Success! ---
            await _send_status(ws, "tessellating", "Preparing 3D preview...")

            # Save metadata
            metadata = ModelMetadata(
                model_id=model_id,
                prompt=user_message,
                cad_source=last_code,
                has_step="step" in result["files"],
                has_stl="stl" in result["files"],
                has_glb="glb" in result["files"],
                iteration=iteration,
            )
            storage.save_model_metadata(project_id, metadata)

            # Notify frontend
            glb_url = f"/api/projects/{project_id}/models/{model_id}/glb"

            await _send_debug(ws, "model_ready", "Model exported successfully", {
                "model_id": model_id,
                "glb_url": glb_url,
                "files": result["files"],
                "iteration": iteration,
            })

            await _send(ws, {
                "type": "model_ready",
                "model_id": model_id,
                "glb_url": glb_url,
            })

            # Send success response
            violations_text = ""
            if result.get("violations"):
                violations_text = "\n\n⚠️ Warnings:\n" + "\n".join(
                    f"- {v}" for v in result["violations"]
                )

            response_text = f"✓ Model generated successfully (`{model_id}`, attempt {iteration}).{violations_text}"

            await _send(ws, {"type": "chat_response", "content": response_text})

            # Save assistant message
            storage.append_chat_message(
                project_id,
                ChatMessage(
                    role="assistant",
                    content=response_text,
                    model_id=model_id,
                ),
            )
            return

        except Exception as e:
            last_error = f"Unexpected error: {traceback.format_exc()}"
            await _send_debug(ws, "pipeline_error", f"Pipeline error at iteration {iteration}", {
                "error": str(e),
                "traceback": traceback.format_exc(),
            })
            if iteration >= MAX_REPAIR_ITERATIONS:
                await _send(ws, {
                    "type": "error",
                    "message": f"Pipeline error: {str(e)}",
                    "failure_type": "unexpected_error",
                })
                return
