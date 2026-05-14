"""
WebSocket handler for real-time chat and CAD generation.

Delegates to AgentOrchestrator for the core generation pipeline.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..agent.orchestrator import AgentOrchestrator
from ..domain.models import ChatMessage, SelectionContext
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

                storage.append_chat_thread_message(
                    project_id, thread_id,
                    ChatMessage(role="user", content=content),
                )

                # Run generation
                await orchestrator.run_pipeline(
                    project_id=project_id,
                    thread_id=thread_id,
                    user_message=content,
                    base_model_id=base_model_id,
                    selection=active_selection,
                )

                messages = storage.get_chat_thread_messages(project_id, thread_id)
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
