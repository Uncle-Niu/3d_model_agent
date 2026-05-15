"""
WebSocket handler for real-time chat and CAD generation.

Delegates to AgentOrchestrator for the core generation pipeline.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..agent.orchestrator import AgentOrchestrator
from ..domain.models import ChatMessage, DesignPlan, SelectionContext
from ..models.llm_service import LLMService
from ..storage import StorageService

ws_router = APIRouter()


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------

@ws_router.websocket("/ws/{project_id}")
async def websocket_endpoint(ws: WebSocket, project_id: str):
    await ws.accept()

    storage: StorageService = ws.app.state.storage
    config = storage.get_project(project_id)
    if not config:
        await ws.send_text(json.dumps({"type": "error", "message": "Project not found"}))
        await ws.close()
        return

    llm = LLMService()
    active_selection: Optional[SelectionContext] = None
    # Holds the asyncio.Task running an active chat pipeline so we can cancel
    # it from a `cancel_chat` WS message without blocking the receive loop.
    active_pipeline_task: Optional[asyncio.Task] = None

    # Callbacks for the orchestrator
    async def on_status(stage: str, message: str, details: Optional[str] = None, data: Optional[dict] = None):
        payload = {"type": "status", "stage": stage, "message": message}
        if details: payload["details"] = details
        if data: payload["data"] = data
        await ws.send_text(json.dumps(payload))

    async def on_chunk(content: str):
        await ws.send_text(json.dumps({"type": "llm_chunk", "content": content}))

    async def on_debug(category: str, message: str, data: Optional[dict] = None):
        payload = {
            "type": "debug_log",
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "category": category,
            "message": message,
        }
        if data:
            payload["data"] = data
        await ws.send_text(json.dumps(payload))

    async def on_model_ready(model_id: str, glb_url: str):
        await ws.send_text(json.dumps({
            "type": "model_ready",
            "model_id": model_id,
            "glb_url": glb_url,
        }))

    async def on_critique(report, render_urls):
        await ws.send_text(json.dumps({
            "type": "critique_result",
            "score": report.overall_printability,
            "matches_intent": report.matches_intent,
            "issues": [
                {
                    "issue_type": i.issue_type,
                    "severity": i.severity,
                    "description": i.description,
                    "location_hint": i.location_hint,
                }
                for i in report.issues
            ],
            "repair_prompt": report.repair_prompt,
            "render_urls": render_urls,
        }))

    async def on_error(message: str, failure_type: Optional[str] = None):
        payload = {"type": "error", "message": message}
        if failure_type:
            payload["failure_type"] = failure_type
        await ws.send_text(json.dumps(payload))

    async def on_plan(plan: DesignPlan):
        await ws.send_text(json.dumps({
            "type": "design_plan",
            "summary": plan.summary,
            "overall_dimensions_mm": plan.overall_dimensions_mm,
            "components": [c.model_dump() for c in plan.components],
            "key_features": plan.key_features,
            "assumptions": plan.assumptions,
            "risks": plan.risks,
            "parameters": plan.parameters,
            "raw_reasoning": plan.raw_reasoning,
        }))

    async def on_reasoning(channel: str, text: str):
        # Stream the planner / verifier thoughts as their own channel so the UI can
        # show them next to the code stream instead of mixed in with chat tokens.
        await ws.send_text(json.dumps({
            "type": "reasoning_chunk",
            "channel": channel,
            "content": text,
        }))

    # Instantiate orchestrator
    orchestrator = AgentOrchestrator(
        storage=storage,
        llm=llm,
        on_status=on_status,
        on_chunk=on_chunk,
        on_debug=on_debug,
        on_model_ready=on_model_ready,
        on_critique=on_critique,
        on_error=on_error,
        on_plan=on_plan,
        on_reasoning=on_reasoning,
    )

    await on_debug("init", "WebSocket connected", {
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
                await on_error("Invalid JSON")
                continue

            msg_type = msg.get("type")

            if msg_type == "chat_message":
                content = msg.get("content", "").strip()
                thread_id = msg.get("thread_id") or ws.query_params.get("thread_id") or "legacy"
                base_model_id = msg.get("base_model_id")

                if not content:
                    await on_error("Empty message")
                    continue

                if active_pipeline_task and not active_pipeline_task.done():
                    await on_error("A chat turn is already running — stop it before sending a new one.")
                    continue

                storage.append_chat_thread_message(
                    project_id, thread_id,
                    ChatMessage(role="user", content=content),
                )

                async def _run_turn(_thread_id: str = thread_id, _content: str = content, _base_model_id=base_model_id):
                    try:
                        await orchestrator.run_pipeline(
                            project_id=project_id,
                            thread_id=_thread_id,
                            user_message=_content,
                            base_model_id=_base_model_id,
                            selection=active_selection,
                        )

                        messages = storage.get_chat_thread_messages(project_id, _thread_id)
                        if messages and messages[-1].role == "assistant":
                            assistant_message = messages[-1]
                            await ws.send_text(json.dumps({
                                "type": "chat_response",
                                "content": assistant_message.content,
                                "model_id": assistant_message.model_id,
                                "steps": [
                                    step.model_dump(mode="json")
                                    for step in assistant_message.steps
                                ],
                            }))
                    except asyncio.CancelledError:
                        # Cancellation is initiated by the user via `cancel_chat`.
                        # Persist a short marker message and notify the client so
                        # the chat UI can leave generating state.
                        try:
                            storage.append_chat_thread_message(
                                project_id, _thread_id,
                                ChatMessage(role="assistant", content="⏹ Stopped by user."),
                            )
                        except Exception:
                            pass
                        try:
                            await ws.send_text(json.dumps({
                                "type": "chat_response",
                                "content": "⏹ Stopped by user.",
                                "model_id": None,
                                "steps": [],
                            }))
                        except Exception:
                            pass
                        raise

                active_pipeline_task = asyncio.create_task(_run_turn())

            elif msg_type == "cancel_chat":
                if active_pipeline_task and not active_pipeline_task.done():
                    active_pipeline_task.cancel()
                    await on_debug("cancel", "Chat cancellation requested by user")
                else:
                    await on_debug("cancel", "Cancel requested but no active chat turn")

            elif msg_type == "selection":
                feature_name = msg.get("feature_name")
                point = msg.get("point")
                if feature_name:
                    active_selection = SelectionContext(feature_name=feature_name, point=point)
                    await on_debug("selection", f"Feature selected: {feature_name}", {
                        "feature_name": feature_name,
                        "point": point,
                    })
                else:
                    active_selection = None
                    await on_debug("selection", "Selection cleared")

            elif msg_type == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
            else:
                await on_error(f"Unknown message type: {msg_type}")

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await on_error(str(e))
        except Exception:
            pass
    finally:
        # Cancel any in-flight pipeline so we don't leak background work after
        # the client disconnects or the loop exits unexpectedly.
        if active_pipeline_task and not active_pipeline_task.done():
            active_pipeline_task.cancel()
