"""
WebSocket handler for real-time chat and CAD generation.

Handles the full pipeline: user message → LLM → CadQuery → export → notify frontend.
"""

from __future__ import annotations

import asyncio
import json
import traceback
from datetime import datetime

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


@ws_router.websocket("/ws/{project_id}")
async def websocket_endpoint(ws: WebSocket, project_id: str):
    """
    WebSocket endpoint for a project.

    Protocol:
      Client sends: { "type": "chat_message", "content": "..." }
      Server sends: status updates, llm_chunks, model_ready, errors
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

    1. Generate CadQuery code via LLM
    2. Execute and export
    3. If failure, retry up to MAX_REPAIR_ITERATIONS
    4. Send model_ready when done
    """
    config = storage.get_project(project_id)
    if not config:
        await _send(ws, {"type": "error", "message": "Project not found"})
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

                # Stream the LLM response
                full_response = ""
                system_prompt = build_system_prompt(
                    config.hard_constraints, config.soft_constraints
                )

                async for chunk in llm.generate_stream(
                    user_message, system_prompt, chat_ctx
                ):
                    full_response += chunk
                    await _send(ws, {"type": "llm_chunk", "content": chunk})

                last_code = extract_code_from_response(full_response)

            else:
                await _send_status(
                    ws, "repairing",
                    f"Repairing code (attempt {iteration}/{MAX_REPAIR_ITERATIONS})..."
                )

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

            # --- Step 2: Execute CadQuery ---
            await _send_status(ws, "executing", "Running CadQuery code...")

            model_id = storage.next_model_id(project_id)
            model_dir = storage.create_model_dir(project_id, model_id)

            # Run in thread pool to avoid blocking the event loop
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                process_cadquery_code,
                last_code,
                model_dir,
                "part",
                config.hard_constraints,
            )

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
            if iteration >= MAX_REPAIR_ITERATIONS:
                await _send(ws, {
                    "type": "error",
                    "message": f"Pipeline error: {str(e)}",
                    "failure_type": "unexpected_error",
                })
                return
