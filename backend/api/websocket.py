"""
WebSocket handler for real-time chat and CAD generation.

Generation runs are owned by the backend process, not by an individual
browser socket. A page reload can disconnect and reconnect without cancelling
the active pipeline; explicit cancellation is still supported over WS or REST.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..agent.orchestrator import AgentOrchestrator
from ..domain.models import ChatMessage, DesignPlan, SelectionContext
from ..models.llm_service import LLMService
from ..storage import StorageService

ws_router = APIRouter()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ActiveChatRun:
    project_id: str
    thread_id: str
    task: asyncio.Task
    orchestrator: AgentOrchestrator
    agent_logic: str = "orchestrator"
    started_at: str = field(default_factory=_now_iso)
    events: list[dict[str, Any]] = field(default_factory=list)
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    cancel_requested: bool = False
    cancel_requested_at: Optional[str] = None

    @property
    def running(self) -> bool:
        return not self.task.done()

    def state_payload(self) -> dict[str, Any]:
        return {
            "type": "run_state",
            "running": self.running,
            "project_id": self.project_id,
            "thread_id": self.thread_id,
            "agent_logic": self.agent_logic,
            "started_at": self.started_at,
            "steps": [
                step.model_dump(mode="json")
                for step in self.orchestrator.current_steps
            ],
        }

    def publish(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        if len(self.events) > 500:
            self.events = self.events[-500:]
        for queue in list(self.subscribers):
            queue.put_nowait(event)


class ChatRunManager:
    def __init__(self, storage: StorageService):
        self.storage = storage
        self._runs: dict[tuple[str, str], ActiveChatRun] = {}
        self._idle_subscribers: dict[tuple[str, str], set[asyncio.Queue]] = {}

    def _key(self, project_id: str, thread_id: str) -> tuple[str, str]:
        return (project_id, thread_id)

    def get_run(self, project_id: str, thread_id: str) -> ActiveChatRun | None:
        run = self._runs.get(self._key(project_id, thread_id))
        if run and run.task.done():
            self._runs.pop(self._key(project_id, thread_id), None)
            return None
        return run

    def subscribe(self, project_id: str, thread_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        run = self.get_run(project_id, thread_id)
        if run:
            run.subscribers.add(queue)
            queue.put_nowait(run.state_payload())
            for event in run.events:
                queue.put_nowait(event)
        else:
            self._idle_subscribers.setdefault(self._key(project_id, thread_id), set()).add(queue)
            queue.put_nowait({
                "type": "run_state",
                "running": False,
                "project_id": project_id,
                "thread_id": thread_id,
                "steps": [],
            })
        return queue

    def unsubscribe(self, project_id: str, thread_id: str, queue: asyncio.Queue) -> None:
        run = self.get_run(project_id, thread_id)
        if run:
            run.subscribers.discard(queue)
        idle = self._idle_subscribers.get(self._key(project_id, thread_id))
        if idle:
            idle.discard(queue)
            if not idle:
                self._idle_subscribers.pop(self._key(project_id, thread_id), None)

    def start_run(
        self,
        project_id: str,
        thread_id: str,
        content: str,
        base_model_id: Optional[str],
        selection: Optional[SelectionContext],
        agent_logic: str = "orchestrator",
    ) -> ActiveChatRun:
        existing = self.get_run(project_id, thread_id)
        if existing:
            raise RuntimeError("A chat turn is already running - stop it before sending a new one.")

        llm = LLMService()
        run_ref: dict[str, ActiveChatRun] = {}

        async def publish(event: dict[str, Any]) -> None:
            run_ref["run"].publish(event)

        async def on_status(stage: str, message: str, details: Optional[str] = None, data: Optional[dict] = None):
            payload: dict[str, Any] = {"type": "status", "stage": stage, "message": message}
            if details:
                payload["details"] = details
            if data:
                payload["data"] = data
            await publish(payload)

        async def on_chunk(chunk: str):
            await publish({"type": "llm_chunk", "content": chunk})

        async def on_debug(category: str, message: str, data: Optional[dict] = None):
            payload: dict[str, Any] = {
                "type": "debug_log",
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "category": category,
                "message": message,
            }
            if data:
                payload["data"] = data
            await publish(payload)

        async def on_model_ready(model_id: str, glb_url: str):
            await publish({
                "type": "model_ready",
                "model_id": model_id,
                "glb_url": glb_url,
            })

        async def on_critique(report, render_urls):
            await publish({
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
            })

        async def on_error(message: str, failure_type: Optional[str] = None):
            payload: dict[str, Any] = {"type": "error", "message": message}
            if failure_type:
                payload["failure_type"] = failure_type
            await publish(payload)

        async def on_plan(plan: DesignPlan):
            await publish({
                "type": "design_plan",
                "summary": plan.summary,
                "overall_dimensions_mm": plan.overall_dimensions_mm,
                "components": [c.model_dump() for c in plan.components],
                "key_features": plan.key_features,
                "assumptions": plan.assumptions,
                "risks": plan.risks,
                "parameters": plan.parameters,
                "raw_reasoning": plan.raw_reasoning,
            })

        async def on_reasoning(channel: str, text: str):
            await publish({
                "type": "reasoning_chunk",
                "channel": channel,
                "content": text,
            })

        orchestrator = AgentOrchestrator(
            storage=self.storage,
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

        agent_logic = (agent_logic or "orchestrator").strip().lower()
        if agent_logic not in {"orchestrator", "llm_agent"}:
            agent_logic = "orchestrator"

        self.storage.append_chat_thread_message(
            project_id,
            thread_id,
            ChatMessage(role="user", content=content, agent_logic=agent_logic),
        )

        async def run_turn():
            try:
                await orchestrator.run_pipeline(
                    project_id=project_id,
                    thread_id=thread_id,
                    user_message=content,
                    base_model_id=base_model_id,
                    selection=selection,
                    agent_logic=agent_logic,
                )
                await self._publish_latest_assistant(run_ref["run"])
            except asyncio.CancelledError as exc:
                run = run_ref["run"]
                if run.cancel_requested:
                    await self._persist_cancelled(run)
                else:
                    await self._persist_failed(
                        run,
                        "Generation stopped unexpectedly: the backend task was cancelled "
                        "without a user cancel request. This usually means the backend "
                        "process restarted, the event loop shut down, or an upstream "
                        "dependency cancelled the request.",
                        failure_type="unexpected_cancel",
                        exc=exc,
                    )
                raise
            except Exception as exc:
                await self._persist_failed(
                    run_ref["run"],
                    f"{type(exc).__name__}: {exc}",
                    failure_type="backend_exception",
                    exc=exc,
                )
            finally:
                current = self._runs.get(self._key(project_id, thread_id))
                if current is run_ref.get("run"):
                    self._runs.pop(self._key(project_id, thread_id), None)

        task = asyncio.create_task(run_turn())
        run = ActiveChatRun(
            project_id=project_id,
            thread_id=thread_id,
            task=task,
            orchestrator=orchestrator,
            agent_logic=agent_logic,
        )
        run.subscribers = self._idle_subscribers.pop(self._key(project_id, thread_id), set())
        run_ref["run"] = run
        self._runs[self._key(project_id, thread_id)] = run
        run.publish(run.state_payload())
        run.publish({
            "type": "debug_log",
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "category": "init",
            "message": "Chat run started",
            "data": {
                "project_id": project_id,
                "thread_id": thread_id,
                "llm_base_url": llm.base_url,
                "llm_model": llm.model,
                "agent_logic": agent_logic,
            },
        })
        return run

    async def _publish_latest_assistant(self, run: ActiveChatRun) -> None:
        messages = self.storage.get_chat_thread_messages(run.project_id, run.thread_id)
        if messages and messages[-1].role == "assistant":
            assistant = messages[-1]
            run.publish({
                "type": "chat_response",
                "content": assistant.content,
                "model_id": assistant.model_id,
                "steps": [
                    step.model_dump(mode="json")
                    for step in assistant.steps
                ],
            })

    async def _persist_cancelled(self, run: ActiveChatRun) -> None:
        message = ChatMessage(
            role="assistant",
            content="Stopped by user.",
            steps=run.orchestrator.current_steps,
            agent_logic=run.agent_logic,
        )
        messages = self.storage.get_chat_thread_messages(run.project_id, run.thread_id)
        if messages and messages[-1].role == "assistant":
            self.storage.update_last_chat_thread_message(run.project_id, run.thread_id, message)
        else:
            self.storage.append_chat_thread_message(run.project_id, run.thread_id, message)
        run.publish({
            "type": "chat_response",
            "content": message.content,
            "model_id": None,
            "steps": [
                step.model_dump(mode="json")
                for step in message.steps
            ],
        })

    async def _persist_failed(
        self,
        run: ActiveChatRun,
        error_message: str,
        failure_type: str = "backend_exception",
        exc: BaseException | None = None,
    ) -> None:
        content = f"Generation failed: {error_message}"
        message = ChatMessage(
            role="assistant",
            content=content,
            steps=run.orchestrator.current_steps,
            agent_logic=run.agent_logic,
        )
        messages = self.storage.get_chat_thread_messages(run.project_id, run.thread_id)
        if messages and messages[-1].role == "assistant":
            self.storage.update_last_chat_thread_message(run.project_id, run.thread_id, message)
        else:
            self.storage.append_chat_thread_message(run.project_id, run.thread_id, message)

        debug_data: dict[str, Any] = {"failure_type": failure_type}
        if exc is not None:
            debug_data.update({
                "exception_type": type(exc).__name__,
                "exception_message": str(exc),
                "traceback": "".join(
                    traceback.format_exception(type(exc), exc, exc.__traceback__)
                ),
            })

        run.publish({
            "type": "debug_log",
            "timestamp": _now_iso(),
            "category": failure_type,
            "message": content,
            "data": debug_data,
        })
        run.publish({
            "type": "chat_response",
            "content": message.content,
            "model_id": None,
            "steps": [
                step.model_dump(mode="json")
                for step in message.steps
            ],
        })

    def cancel(self, project_id: str, thread_id: str) -> bool:
        run = self.get_run(project_id, thread_id)
        if not run:
            return False
        run.cancel_requested = True
        run.cancel_requested_at = _now_iso()
        run.task.cancel()
        run.publish({
            "type": "debug_log",
            "timestamp": _now_iso(),
            "category": "cancel",
            "message": "Chat cancellation requested by user",
        })
        return True

    def status(self, project_id: str, thread_id: str) -> dict[str, Any]:
        run = self.get_run(project_id, thread_id)
        if not run:
            return {
                "running": False,
                "project_id": project_id,
                "thread_id": thread_id,
                "steps": [],
            }
        payload = run.state_payload()
        payload.pop("type", None)
        return payload


def get_chat_run_manager(app) -> ChatRunManager:
    manager = getattr(app.state, "chat_runs", None)
    if manager is None:
        manager = ChatRunManager(app.state.storage)
        app.state.chat_runs = manager
    return manager


@ws_router.websocket("/ws/{project_id}")
async def websocket_endpoint(ws: WebSocket, project_id: str):
    await ws.accept()

    storage: StorageService = ws.app.state.storage
    config = storage.get_project(project_id)
    if not config:
        await ws.send_text(json.dumps({"type": "error", "message": "Project not found"}))
        await ws.close()
        return

    thread_id = ws.query_params.get("thread_id") or "legacy"
    manager = get_chat_run_manager(ws.app)
    active_selection: Optional[SelectionContext] = None
    outbound = manager.subscribe(project_id, thread_id)

    async def send_loop():
        while True:
            event = await outbound.get()
            await ws.send_text(json.dumps(event))

    async def receive_loop():
        nonlocal active_selection
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                outbound.put_nowait({"type": "error", "message": "Invalid JSON"})
                continue

            msg_type = msg.get("type")

            if msg_type == "chat_message":
                content = msg.get("content", "").strip()
                incoming_thread_id = msg.get("thread_id") or thread_id
                base_model_id = msg.get("base_model_id")
                agent_logic = msg.get("agent_logic") or "orchestrator"

                if incoming_thread_id != thread_id:
                    outbound.put_nowait({"type": "error", "message": "Thread changed; reconnecting is required."})
                    continue
                if not content:
                    outbound.put_nowait({"type": "error", "message": "Empty message"})
                    continue

                try:
                    manager.start_run(project_id, thread_id, content, base_model_id, active_selection, agent_logic)
                except RuntimeError as exc:
                    outbound.put_nowait({"type": "error", "message": str(exc)})

            elif msg_type == "cancel_chat":
                if not manager.cancel(project_id, thread_id):
                    outbound.put_nowait({
                        "type": "debug_log",
                        "timestamp": datetime.utcnow().isoformat() + "Z",
                        "category": "cancel",
                        "message": "Cancel requested but no active chat turn",
                    })

            elif msg_type == "selection":
                feature_name = msg.get("feature_name")
                point = msg.get("point")
                if feature_name:
                    active_selection = SelectionContext(feature_name=feature_name, point=point)
                    outbound.put_nowait({
                        "type": "debug_log",
                        "timestamp": datetime.utcnow().isoformat() + "Z",
                        "category": "selection",
                        "message": f"Feature selected: {feature_name}",
                        "data": {"feature_name": feature_name, "point": point},
                    })
                else:
                    active_selection = None
                    outbound.put_nowait({
                        "type": "debug_log",
                        "timestamp": datetime.utcnow().isoformat() + "Z",
                        "category": "selection",
                        "message": "Selection cleared",
                    })

            elif msg_type == "ping":
                outbound.put_nowait({"type": "pong"})
            else:
                outbound.put_nowait({"type": "error", "message": f"Unknown message type: {msg_type}"})

    sender = asyncio.create_task(send_loop())
    receiver = asyncio.create_task(receive_loop())

    try:
        done, pending = await asyncio.wait(
            {sender, receiver},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            with contextlib.suppress(WebSocketDisconnect):
                task.result()
        for task in pending:
            task.cancel()
    except WebSocketDisconnect:
        pass
    finally:
        manager.unsubscribe(project_id, thread_id, outbound)
        sender.cancel()
        receiver.cancel()
