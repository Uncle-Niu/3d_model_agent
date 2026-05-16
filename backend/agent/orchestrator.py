"""
Agent Orchestrator — manages the CAD generation and repair pipeline.

This module extracts the core logic from the API layer to provide a 
reusable, structured generation workflow.
"""

import asyncio
import inspect
import os
import time
import traceback
from typing import Any, Callable, Dict, List, Optional

import httpx

from ..cad.engine import process_cadquery_code
from ..cad.example_bank import build_example_bank_prompt_context, retrieve_example_snippets
from ..cad.recipes import (
    build_combined_recipe_context,
    retrieve_recipe_cards,
    validate_plan_against_recipes,
)
from ..domain.models import (
    ChatMessage,
    CritiqueReport,
    DesignPlan,
    FailureType,
    GeometryStats,
    ModelMetadata,
    SelectionContext,
    PipelineStep,
)
from ..models.llm_service import (
    LLMBackendUnavailable,
    LLMService,
    build_repair_prompt,
    build_system_prompt,
    extract_code_from_response,
    plan_to_prompt_text,
)
from ..knowledge import LocalKnowledgeService
from ..knowledge.local_recall import format_recall_for_prompt
from ..storage import StorageService
from ..tools.web_research import search_web, get_research_prompt_extension
from ..vision.critic import VISION_SYSTEM_PROMPT, VisionCritic, _build_vision_user_prompt


class AgentOrchestrator:
    """
    Orchestrates the full CAD generation loop:
    LLM -> CadQuery -> Validation -> Rendering -> Vision Critique -> Repair
    """

    def __init__(
        self,
        storage: StorageService,
        llm: Optional[LLMService] = None,
        local_knowledge: Optional[LocalKnowledgeService] = None,
        on_status: Optional[Callable[[str, str, Optional[str], Optional[Dict]], Any]] = None,
        on_chunk: Optional[Callable[[str], Any]] = None,
        on_debug: Optional[Callable[[str, str, Optional[Dict]], Any]] = None,
        on_model_ready: Optional[Callable[[str, str], Any]] = None,
        on_critique: Optional[Callable[[CritiqueReport, Dict[str, str]], Any]] = None,
        on_error: Optional[Callable[[str, Optional[str]], Any]] = None,
        on_plan: Optional[Callable[[DesignPlan], Any]] = None,
        on_reasoning: Optional[Callable[[str, str], Any]] = None,
    ):
        self.storage = storage
        self.llm = llm or LLMService()
        self.local_knowledge = local_knowledge or LocalKnowledgeService(
            base_url=self.llm.base_url,
            api_key=self.llm.api_key,
        )

        # Callbacks for real-time updates
        self.on_status = on_status
        self.on_chunk = on_chunk
        self.on_debug = on_debug
        self.on_model_ready = on_model_ready
        self.on_critique = on_critique
        self.on_error = on_error
        # New: reasoning channels (planning step, vision reasoning, etc.)
        self.on_plan = on_plan
        self.on_reasoning = on_reasoning

        self.current_steps: List[PipelineStep] = []

        # Constants
        self.MAX_REPAIR_ITERATIONS = 5
        self.VISION_SCORE_THRESHOLD = 0.65

        # Per-run context
        self._current_project_id: Optional[str] = None
        self._current_thread_id: Optional[str] = None

    async def _emit_status(self, stage: str, message: str, details: Optional[str] = None, data: Optional[Dict] = None):
        step = PipelineStep(stage=stage, message=message, details=details, data=data)
        self.current_steps.append(step)
        
        # Incremental persistence
        if self._current_project_id and self._current_thread_id:
            try:
                # Update the last message (which we ensure is the assistant's placeholder)
                # with the latest steps.
                self.storage.update_last_chat_thread_message(
                    self._current_project_id, 
                    self._current_thread_id,
                    ChatMessage(
                        role="assistant",
                        content="Generating model...", # Placeholder content
                        steps=self.current_steps
                    )
                )
            except Exception:
                pass
        
        if self.on_status:
            await self.on_status(stage, message, details, data)

    async def _emit_debug(self, category: str, message: str, data: Optional[Dict] = None):
        if self.on_debug:
            await self.on_debug(category, message, data)

    async def _emit_chunk(self, chunk: str):
        if self.on_chunk:
            await self.on_chunk(chunk)

    async def _emit_error(self, message: str, failure_type: Optional[str] = None):
        if self.on_error:
            await self.on_error(message, failure_type)

    async def _emit_reasoning(self, channel: str, text: str):
        """Stream visible reasoning text (planning, vision thinking, etc.)."""
        if self.on_reasoning:
            await self.on_reasoning(channel, text)

    async def _emit_plan(self, plan: DesignPlan):
        if self.on_plan:
            await self.on_plan(plan)

    async def _handle_backend_unavailable(
        self, exc: LLMBackendUnavailable, *, stage: str
    ) -> None:
        """Re-check Ollama after an LLM call surfaces an infrastructure failure.

        Emits a user-visible error with whatever diagnostic the connectivity
        probe can find (Ollama down vs. model unloaded vs. unknown). The caller
        is expected to re-raise so the pipeline aborts instead of cascading
        into doomed code generation.
        """
        await self._emit_debug(
            "llm_backend_unavailable",
            f"LLM backend failure during `{stage}`: {exc}",
            {
                "stage": stage,
                "exception_type": type(exc.cause).__name__ if exc.cause else "LLMBackendUnavailable",
                "exception_message": str(exc.cause) if exc.cause else str(exc),
                "traceback": traceback.format_exc(),
            },
        )
        # Probe Ollama again to distinguish "process crashed / OOM" from
        # "transient hiccup". `check_ollama_connectivity` already emits its
        # own user-visible error on failure, so we only need to add a generic
        # one when the probe still succeeds (meaning the stream died but the
        # daemon is back — almost always a worker OOM that restarted).
        ollama_ok = await self.check_ollama_connectivity()
        if ollama_ok:
            await self._emit_error(
                (
                    f"LLM backend dropped the `{stage}` stream but the Ollama "
                    f"daemon is reachable again. This usually means the model "
                    f"worker crashed mid-generation (OOM, GPU driver kill, or "
                    f"context overflow). Check Ollama logs for the underlying "
                    f"cause. Original error: {exc}"
                ),
                failure_type="llm_backend_unavailable",
            )

    async def check_ollama_connectivity(self) -> bool:
        ollama_base = self.llm.base_url.replace("/v1", "")
        await self._emit_debug("ollama", f"Checking Ollama at {ollama_base} ...")

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{ollama_base}/api/tags")
                if resp.status_code != 200:
                    await self._emit_error(f"Ollama returned HTTP {resp.status_code}. Is it running?")
                    return False

                data = resp.json()
                models = [m["name"] for m in data.get("models", [])]
                model_available = self.llm.model in models

                await self._emit_debug("ollama", "Ollama connected", {
                    "available_models": models,
                    "configured_model": self.llm.model,
                    "model_available": model_available,
                })

                if not model_available:
                    await self._emit_error((
                        f"Model '{self.llm.model}' is not pulled in Ollama. "
                        f"Available: {', '.join(models) or '(none)'}. "
                        f"Run `ollama pull {self.llm.model}` first."
                    ))
                    return False
                return True

        except httpx.ConnectError:
            await self._emit_error((
                f"Cannot connect to Ollama at {ollama_base}. "
                "Make sure Ollama is running (`ollama serve`)."
            ))
            return False
        except Exception as e:
            await self._emit_error(f"Ollama check failed: {e}")
            return False

    async def check_vision_connectivity(self) -> tuple[bool, str]:
        """Check if the vision model is available and working.

        Returns (ok, reason). When ok is False, `reason` describes exactly which
        step failed (registry lookup vs. smoke-test) so the caller can surface
        an actionable error instead of a generic "not available" message.
        """
        from ..vision.critic import VisionCritic
        critic = VisionCritic()
        await self._emit_debug("vision", "Checking vision model availability...")
        available, error = await critic.is_available()
        if not available:
            await self._emit_debug("vision_warning", f"Vision model not available: {error}")
            return False, f"registry check failed — {error}"

        # Perform smoke test to ensure image processing works
        await self._emit_debug("vision", "Performing vision smoke test...")
        await self._emit_status(
            "critiquing",
            "Checking the vision model with a smoke-test image.",
            details=None,
            data={
                "sub_stage": "vision_smoke_test",
                "rationale": "Confirms the configured multimodal model can actually read images before it critiques generated renders.",
                "inputs": ["64x64 red square image"],
                "model": critic.model,
                "system_prompt": "You are a vision assistant. Look at the image and answer the question literally in one word.",
                "prompt": "What color is this square? One word.",
                "image_views": ["smoke_test_red_square"],
                "in_progress": True,
            },
        )
        ok, msg = await critic.smoke_test()
        if not ok:
            await self._emit_debug("vision_warning", f"Vision smoke test failed: {msg}")
            return False, f"smoke test failed — {msg}"

        await self._emit_debug("vision", "Vision model is fully operational")
        return True, msg or "ok"

    async def run_pipeline(
        self,
        project_id: str,
        thread_id: str,
        user_message: str,
        base_model_id: Optional[str] = None,
        selection: Optional[SelectionContext] = None,
    ) -> Optional[str]:
        """
        Runs the full agentic generation pipeline.
        Returns the final model_id on success, or None on failure.
        """
        config = self.storage.get_project(project_id)
        if not config:
            await self._emit_error("Project not found")
            return None

        self.current_steps = []

        # Compute a 1-based turn index by counting prior user messages in the
        # thread. The user we're handling now has already been appended by the
        # WS layer, so this index points at the current turn.
        try:
            existing_messages = self.storage.get_chat_thread_messages(project_id, thread_id)
            turn_index = sum(1 for m in existing_messages if m.role == "user")
        except Exception:
            turn_index = None

        # 1. Connectivity Check
        ollama_ok = await self.check_ollama_connectivity()
        vision_ok, vision_reason = await self.check_vision_connectivity()

        if not ollama_ok:
            self.storage.append_chat_thread_message(
                project_id, thread_id,
                ChatMessage(role="assistant", content=(
                    f"❌ Cannot reach Ollama. Please make sure Ollama is running "
                    f"and the model `{self.llm.model}` is available."
                )),
            )
            return None

        # Vision is required by default. Without it the pipeline can only check
        # geometry deterministically (bbox / solid count) — a result that hits
        # the right bbox but is the wrong shape will still pass, which is the
        # exact failure mode we want to avoid. Set ALLOW_VISION_SKIP=1 to
        # explicitly bypass for development.
        allow_vision_skip = os.environ.get("ALLOW_VISION_SKIP", "").lower() in ("1", "true", "yes")
        if not vision_ok and not allow_vision_skip:
            from ..config import resolve_vision_model, VISION_FALLBACK_MODELS
            vision_model = resolve_vision_model()
            fallback_list_str = " / ".join(f"`{m}`" for m in VISION_FALLBACK_MODELS)
            reason_lower = vision_reason.lower()
            # Targeted fix lines per failure mode — only show what's actually
            # relevant to the specific reason, so the user doesn't have to guess
            # which bullet applies to them.
            if "registry check failed" in reason_lower:
                if "cannot reach ollama" in reason_lower:
                    fix_lines = [
                        "- Ollama is not reachable. Start it (`ollama serve`) and confirm "
                        "`curl http://localhost:11434/api/tags` returns a model list.",
                        f"- Check that `VISION_BASE_URL` / `LLM_BASE_URL` point at the running Ollama (current vision model: `{vision_model}`).",
                    ]
                else:
                    # "model X not found in Ollama. Available: [...]"
                    fix_lines = [
                        f"- The configured vision model `{vision_model}` is not installed in Ollama, "
                        f"and no fallback ({fallback_list_str}) was found either.",
                        "- Install a vision-capable model: `ollama pull gemma3:27b` (recommended) "
                        "or `ollama pull gemma4:31b`.",
                        f"- Then set `VISION_MODEL=gemma3:27b` (or the model you pulled) in `.env`.",
                    ]
            elif "smoke test failed" in reason_lower:
                if "cannot see images" in reason_lower or "model says it cannot see" in reason_lower:
                    fix_lines = [
                        f"- The model `{vision_model}` responded but said it can't see images — "
                        f"it is a text-only model, not a vision model.",
                        "- Install and configure a vision-capable model: `ollama pull gemma3:27b`, "
                        "then set `VISION_MODEL=gemma3:27b` in `.env`.",
                    ]
                elif "http 500" in reason_lower or "http 4" in reason_lower or "http 5" in reason_lower:
                    fix_lines = [
                        f"- The vision model `{vision_model}` returned an HTTP error during the smoke test. "
                        f"This usually means VRAM pressure or a transient Ollama crash.",
                        "- Try `ollama ps` to see what's loaded; restart Ollama if needed.",
                        "- Re-run; the smoke test retries twice. If it keeps failing, "
                        "set `VISION_DISABLE_SMOKE_TEST=1` only if you're sure the model is vision-capable.",
                    ]
                else:
                    fix_lines = [
                        f"- The smoke test got an unexpected reply from `{vision_model}`. "
                        f"Either the model is not vision-capable or it misread the test image.",
                        "- Confirm with: `ollama show {model} --modelfile` should list image support.".replace("{model}", vision_model),
                        "- If you trust the model, set `VISION_DISABLE_SMOKE_TEST=1` to skip the probe.",
                    ]
            else:
                fix_lines = [
                    "- Make sure Ollama has a vision-capable model installed "
                    "(`ollama pull gemma3:27b` or `ollama pull gemma4:31b`).",
                    f"- Set `VISION_MODEL` in `.env` to that model name (currently `{vision_model}`).",
                    "- If the model is vision-capable but the smoke test is flaky, "
                    "set `VISION_DISABLE_SMOKE_TEST=1`.",
                ]
            fix_lines.append(
                "- To bypass this gate entirely (not recommended), set `ALLOW_VISION_SKIP=1`."
            )

            self.storage.append_chat_thread_message(
                project_id, thread_id,
                ChatMessage(role="assistant", content=(
                    f"❌ Vision verifier is not available, so generation is blocked.\n\n"
                    f"**Reason:** {vision_reason}\n\n"
                    f"Without a vision check the agent can only confirm that the model has the "
                    f"right bounding box — not that it actually resembles what you asked for. "
                    f"Recent runs without it produced shapes that looked nothing like the prompt.\n\n"
                    f"**To fix:**\n" + "\n".join(fix_lines)
                ), steps=self.current_steps),
            )
            return None

        # 1.5 Prepare Storage and Placeholder
        self._current_project_id = project_id
        self._current_thread_id = thread_id
        
        # Add placeholder assistant message so we can update it incrementally
        self.storage.append_chat_thread_message(
            project_id, thread_id,
            ChatMessage(role="assistant", content="Starting generation...", steps=[])
        )

        # 2. Prepare Context
        system_prompt = build_system_prompt(config.hard_constraints, config.soft_constraints)
        history = self.storage.get_chat_thread_messages(project_id, thread_id)
        chat_ctx = [{"role": m.role, "content": m.content} for m in history[-10:]]

        await self._emit_status(
            "planning",
            "Gathered project context.",
            details=None,
            data={
                "sub_stage": "context",
                "rationale": "Grounds the design in this project's constraints, recent chat, and any active selection.",
                "inputs": [
                    f"{len(chat_ctx)} prior chat message(s)",
                    "hard constraints (bounding box, wall thickness)",
                    "soft constraints (material, finishing preferences)",
                ],
            },
        )

        # 2.0 Local-LLM knowledge recall.
        #
        # Before considering any web search, ask multiple local LLMs (different
        # providers, different training corpora) for structured facts about
        # any real-world subject in the user's prompt. The chain stops as soon
        # as 2+ models agree on enough fields. Anything no two models agreed
        # on falls through to the web-search step as the explicit residual
        # gap — turning "did we search?" into "what gaps remain?".
        recall_consensuses: list = []
        recall_context = ""
        subject_detection_prompt = self.local_knowledge.build_subject_detection_prompt(user_message)
        await self._emit_status(
            "recalling",
            "Detecting real-world references that may need exact specs.",
            details=(
                "This asks the main local LLM whether the request mentions a "
                "real product, part, or standard whose dimensions would improve "
                "the CAD plan. If none are found, the heavier multi-model recall "
                "step is skipped."
            ),
            data={
                "sub_stage": "subject_detection",
                "rationale": "Separates project-context setup from the LLM call that decides whether external specs are needed.",
                "rationale_source": "agent",
                "inputs": ["user prompt", f"main model `{self.llm.model}`"],
                "model": self.llm.model,
                "system_prompt": self.local_knowledge.SUBJECT_DETECTION_SYSTEM_PROMPT,
                "prompt": subject_detection_prompt + "\n\n/no_think",
                "in_progress": True,
            },
        )
        try:
            recall_subjects = await self.local_knowledge.detect_subjects(
                user_message, main_model=self.llm.model,
            )
        except Exception:
            recall_subjects = []
        await self._emit_status(
            "recalling",
            (
                f"Found {len(recall_subjects)} reference subject(s) to cross-check."
                if recall_subjects
                else "No external reference specs needed."
            ),
            details=None,
            data={
                "sub_stage": "subject_detection_done",
                "outcome": (
                    ", ".join(subj.subject for subj in recall_subjects)
                    if recall_subjects
                    else "The request appears fully specified or purely parametric."
                ),
                "outcome_source": "agent",
            },
        )

        if recall_subjects:
            for subj in recall_subjects:
                await self._emit_status(
                    "recalling",
                    f"Cross-checking local LLMs: {subj.subject}",
                    details=(
                        "Queries up to 5 local Ollama models in priority order "
                        "and stops as soon as two of them agree on enough "
                        "fields. Each model is asked the same structured-JSON "
                        "question; the consensus is what gets fed to the "
                        "planner. Fields no two models agreed on are listed "
                        "as 'uncertain' and the agent decides whether to fall "
                        "back to a web search for them."
                    ),
                    data={
                        "sub_stage": "recall_start",
                        "rationale": subj.reasoning,
                        "rationale_source": "planner",
                        "subject": subj.subject,
                        "requested_fields": subj.fields,
                        "model_chain": list(self.local_knowledge.model_chain),
                        "in_progress": True,
                    },
                )
                async def _recall_step(event: str, payload: Dict):
                    await self._emit_debug(f"recall_{event}", f"{payload.get('model','?')}: {event}", payload)
                    model_name = payload.get("model", "?")
                    if event == "model_start":
                        await self._emit_status(
                            "recalling",
                            f"Asking local recall model `{model_name}`.",
                            details=None,
                            data={
                                "sub_stage": "recall_model",
                                "subject": payload.get("subject"),
                                "model": model_name,
                                "system_prompt": payload.get("system_prompt"),
                                "prompt": payload.get("prompt"),
                                "in_progress": True,
                            },
                        )
                    elif event == "model_done":
                        latency = payload.get("latency_s")
                        try:
                            latency_text = f"{float(latency):.1f}s"
                        except (TypeError, ValueError):
                            latency_text = "?s"
                        await self._emit_status(
                            "recalling",
                            f"Local recall model `{model_name}` returned.",
                            details=None,
                            data={
                                "sub_stage": "recall_model_done",
                                "subject": payload.get("subject"),
                                "model": model_name,
                                "outcome": (
                                    payload.get("error")
                                    or f"{payload.get('field_count', 0)} field(s) in {latency_text}"
                                ),
                                "outcome_source": "agent",
                            },
                        )
                consensus = await self.local_knowledge.extract_knowledge(
                    subject=subj.subject,
                    fields=subj.fields,
                    on_step=_recall_step,
                )
                recall_consensuses.append(consensus)
                # Build the exact prompt sent to each model so the UI's
                # Arguments section can show what the LLM actually saw.
                recall_prompt_text = self.local_knowledge.build_recall_prompt(
                    subj.subject, subj.fields,
                )
                await self._emit_status(
                    "recalling",
                    (
                        f"{subj.subject}: {len(consensus.fields)} fact(s) "
                        f"agreed by {len(consensus.contributing_models)} model(s)."
                        if consensus.fields else
                        f"{subj.subject}: no two models agreed."
                    ),
                    details=None,
                    data={
                        "sub_stage": "recall_done",
                        "subject": consensus.subject,
                        "requested_fields": subj.fields,
                        "model_chain": list(self.local_knowledge.model_chain),
                        "system_prompt": self.local_knowledge.RECALL_SYSTEM_PROMPT,
                        "prompt": recall_prompt_text,
                        "outcome": (
                            "All required fields covered by local recall — web search not needed."
                            if consensus.is_complete(subj.fields, self.local_knowledge.min_agreement_ratio)
                            else f"Recalled {len(consensus.fields)} field(s); {len(consensus.uncertain_fields)} remain uncertain."
                        ),
                        "outcome_source": "agent",
                        "agreed_fields": {k: v.model_dump() for k, v in consensus.fields.items()},
                        "uncertain_fields": consensus.uncertain_fields,
                        "contributing_models": consensus.contributing_models,
                        "per_model_responses": [
                            {
                                "model": r.model,
                                "latency_s": r.latency_s,
                                "field_count": sum(1 for v in r.fields.values() if v.value is not None),
                                "error": r.error,
                                "raw_preview": r.raw_response[:400],
                                # Each model's actual field → {value, confidence,
                                # note} map, so the UI can show what each one
                                # answered (not just a count).
                                "fields": {
                                    fname: {
                                        "value": fv.value,
                                        "confidence": fv.confidence,
                                        "note": fv.note,
                                    }
                                    for fname, fv in r.fields.items()
                                },
                            }
                            for r in consensus.all_responses
                        ],
                    },
                )

            recall_context = format_recall_for_prompt(recall_consensuses)

        # Local-recall knowledge feeds the same downstream slot as web research
        # — both are external-fact context for the planner. We prepend recall
        # because it's higher-trust (cross-checked across providers).
        def _merge_external_context(*parts: str) -> str:
            return "\n\n".join(p for p in parts if p)

        # 2.1 Research step — DISABLED.
        #
        # DuckDuckGo's free text endpoint returns empty / rate-limited results
        # too often to be useful. We now rely entirely on local-LLM recall
        # consensus for external knowledge. Re-enable by flipping
        # WEB_SEARCH_ENABLED to True and the original research path will run.
        WEB_SEARCH_ENABLED = False
        citations: list = []
        research_context = ""
        await self._emit_status(
            "researching",
            "Web search disabled — using local-LLM recall only.",
            details=(
                "Web research is currently turned off. The agent relies on the "
                "local-LLM recall consensus above for any real-world facts the "
                "planner needs. To re-enable, set WEB_SEARCH_ENABLED=True in "
                "backend/agent/orchestrator.py."
            ),
            data={
                "sub_stage": "research_skipped",
                "rationale": "Web search disabled in orchestrator config.",
                "rationale_source": "agent",
                "outcome": "No external lookup performed.",
                "skipped": ["web search"],
            },
        )

        current_source = ""
        current_model_id = base_model_id
        # The strategy decision is purely "do we have a prior checkpoint to
        # edit?" — it does NOT consult the user prompt. Track the two probes
        # so the UI's Inputs row shows what the agent actually looked at.
        strategy_probes: list[str] = []
        if base_model_id:
            strategy_probes.append(f"base model id from request: `{base_model_id}`")
            current_source = self.storage.get_model_source_text(project_id, current_model_id or "")
            strategy_probes.append(
                f"source for `{base_model_id}`: {'found' if current_source else 'missing'}"
            )
        else:
            strategy_probes.append("base model id from request: none")
        if not current_source:
            latest_model = self.storage.latest_successful_model(project_id)
            if latest_model:
                current_model_id = latest_model.model_id
                current_source = self.storage.get_model_source_text(project_id, latest_model.model_id)
                strategy_probes.append(
                    f"latest successful checkpoint in project: `{latest_model.model_id}`"
                )
            else:
                strategy_probes.append("latest successful checkpoint in project: none")

        # `context_used` is the list of CONTEXT pieces the planner / generator
        # will receive (used downstream — distinct from the strategy probes).
        context_used = []
        if current_source:
            context_used.append(f"base model `{current_model_id}` source")
        if selection:
            context_used.append(f"active selection `{selection.feature_name}`")
        if citations:
            context_used.append(f"{len(citations)} research citation(s)")
        await self._emit_status(
            "planning",
            (
                "Strategy: edit existing checkpoint."
                if current_source
                else "Strategy: build a new model from scratch."
            ),
            details=None,
            data={
                "sub_stage": "strategy",
                "rationale": "Reuses existing geometry when relevant; otherwise starts fresh.",
                "outcome": (
                    f"Modify checkpoint `{current_model_id}`."
                    if current_source
                    else "No suitable base model found — start fresh."
                ),
                # `inputs` here lists the project-state probes the strategy
                # decision actually consulted (NOT the user prompt — strategy
                # is a function of project history only).
                "inputs": strategy_probes,
            },
        )

        # 2.5 Planning step — decompose the request into a structured plan BEFORE
        # writing any CadQuery. The plan is the contract carried through every
        # repair iteration AND given to the vision verifier, so the generator and
        # the critic evaluate against the same explicit goal.
        recipe_cards = retrieve_recipe_cards(user_message)
        recipe_context = build_combined_recipe_context(user_message, recipe_cards)
        planning_example_context = build_example_bank_prompt_context(
            user_message,
            max_snippets=5,
            cadquery_only=False,
        )
        code_example_context = build_example_bank_prompt_context(
            user_message,
            max_snippets=5,
            cadquery_only=True,
        )
        planning_reference_context = "\n\n".join(
            part for part in [recipe_context, planning_example_context] if part
        )
        generation_reference_context = "\n\n".join(
            part for part in [recipe_context, code_example_context] if part
        )
        if recipe_cards:
            await self._emit_status(
                "planning",
                f"Matched {len(recipe_cards)} CAD recipe pattern(s).",
                details=(
                    "Recipes are pre-authored product archetypes shipped with "
                    "the agent (defined in backend/cad/recipes.py). The retriever "
                    "scores them against the user prompt and feeds the matches "
                    "into the planner so it knows what features a typical "
                    "product class or mechanical archetype needs to include. They are "
                    "local data, not LLM output."
                ),
                data={
                    "sub_stage": "recipes",
                    "rationale": "Grounds the plan in known CAD/product patterns before code generation.",
                    "found": [f"{card.title} (`{card.recipe_id}`)" for card in recipe_cards],
                    "recipes": [
                        {
                            "recipe_id": card.recipe_id,
                            "title": card.title,
                            "required_features": list(card.required_features),
                            "negative_space_features": list(card.negative_space_features),
                        }
                        for card in recipe_cards
                    ],
                },
            )
        example_hits = retrieve_example_snippets(user_message, max_snippets=5, cadquery_only=False)
        if example_hits:
            await self._emit_status(
                "planning",
                f"Retrieved {len(example_hits)} local CAD example(s).",
                details=(
                    "Examples are real open-source CadQuery / build123d files "
                    "cloned under data/cad_sources/. The retriever picks the "
                    "ones whose path/title best matches the prompt so the "
                    "planner has concrete code patterns to anchor on."
                ),
                data={
                    "sub_stage": "example_bank",
                    "rationale": "Uses local open-source CAD patterns as retrieval context instead of relying only on a small hand-written prompt library.",
                    "found": [f"{hit.source_kind}: data/cad_sources/{hit.path}" for hit in example_hits],
                    "examples": [
                        {
                            "path": hit.path,
                            "title": hit.title,
                            "source_kind": hit.source_kind,
                            "score": hit.score,
                        }
                        for hit in example_hits
                    ],
                },
            )

        # Build the exact system + user prompt that will be sent to the planner
        # LLM, so the UI can show the user what the model actually saw.
        merged_external_context = _merge_external_context(recall_context, research_context)
        plan_system_prompt, plan_user_prompt = LLMService.build_planning_prompt(
            user_message=user_message,
            current_source=current_source,
            current_model_id=current_model_id,
            research_context=merged_external_context,
            recipe_context=planning_reference_context,
            hard_constraints=config.hard_constraints,
            soft_constraints=config.soft_constraints,
        )

        await self._emit_status(
            "planning",
            "Developing the design plan…",
            details=None,
            data={
                "sub_stage": "plan_drafting",
                "rationale": "Catches dimensional errors early before writing complex CadQuery code.",
                "inputs": context_used + (["research results"] if research_context else []) + ["user prompt", "retrieved recipes / examples"],
                "system_prompt": plan_system_prompt,
                "prompt": plan_user_prompt,
                # Marks this step as the slot under which streaming planner
                # reasoning should be shown live.
                "reasoning_channel": "planning",
                "in_progress": True,
            },
        )

        async def _plan_chunk(chunk: str):
            await self._emit_reasoning("planning", chunk)

        try:
            plan = await self.llm.plan_design(
                user_message=user_message,
                chat_history=chat_ctx,
                current_source=current_source,
                current_model_id=current_model_id,
                research_context=merged_external_context,
                recipe_context=planning_reference_context,
                hard_constraints=config.hard_constraints,
                soft_constraints=config.soft_constraints,
                on_chunk=_plan_chunk,
            )
        except LLMBackendUnavailable as e:
            await self._handle_backend_unavailable(e, stage="planning")
            raise
        except Exception as e:
            await self._emit_debug("planning_error", f"Planner failed: {e}", {"traceback": traceback.format_exc()})
            plan = DesignPlan(raw_text="", summary="(planner failed — proceeding without a structured plan)")

        # Surface the raw planner output so an empty-looking first draft can
        # be diagnosed without re-running the LLM.
        await self._emit_debug(
            "planner_raw",
            f"Planner returned {len(plan.components)} component(s), "
            f"{len(plan.key_features)} feature(s); summary={'yes' if plan.summary else 'no'}",
            {"raw_text": plan.raw_text[:8000] if plan.raw_text else ""},
        )

        # Emit the first-draft plan as its own step so the user can see what
        # the planner produced BEFORE the quality-gate decides whether to
        # repair. Carries the same structured fields as the eventual
        # `plan_ready` step so the frontend can render the same plan card
        # inline. We use sub_stage='plan_draft' so the above-timeline final
        # plan card (which only matches `plan_ready`) is not shadowed.
        draft_label = plan.summary
        if not draft_label:
            draft_bits = []
            if plan.components:
                draft_bits.append(f"{len(plan.components)} component{'s' if len(plan.components) != 1 else ''}")
            if plan.key_features:
                draft_bits.append(f"{len(plan.key_features)} feature{'s' if len(plan.key_features) != 1 else ''}")
            draft_label = ", ".join(draft_bits) if draft_bits else "(no summary)"

        await self._emit_status(
            "planning",
            f"First-draft plan · {draft_label}",
            details=None,
            data={
                "sub_stage": "plan_draft",
                "outcome": plan.summary,
                "outcome_source": "planner",
                "summary": plan.summary,
                "overall_dimensions_mm": plan.overall_dimensions_mm,
                "raw_reasoning": plan.raw_reasoning,
                "components": [c.model_dump() for c in plan.components],
                "key_features": plan.key_features,
                "assumptions": plan.assumptions,
                "risks": plan.risks,
                "parameters": plan.parameters,
            },
        )

        quality_report = validate_plan_against_recipes(plan, recipe_cards)
        if not quality_report.is_sufficient:
            rejected_plan_text = plan_to_prompt_text(plan) or plan.raw_text
            plan_repair_feedback = (
                f"{quality_report.feedback}\n\n"
                "Rejected plan summary for reference:\n"
                f"{rejected_plan_text[:3000]}"
            )
            plan_repair_system_prompt, plan_repair_user_prompt = LLMService.build_planning_prompt(
                user_message=user_message,
                current_source=current_source,
                current_model_id=current_model_id,
                research_context=merged_external_context,
                recipe_context=planning_reference_context,
                plan_feedback=plan_repair_feedback,
                hard_constraints=config.hard_constraints,
                soft_constraints=config.soft_constraints,
            )
            await self._emit_status(
                "planning",
                "Plan is missing required product details — repairing.",
                details=(
                    "A rule-based quality gate compared the planner's output "
                    "against the retrieved CAD recipe checklist and found gaps. "
                    "The planner LLM is now asked to rewrite the plan with the "
                    "missing features filled in. The missing-features list "
                    "below comes from the rule check; the rewritten plan in the "
                    "next step is LLM output."
                ),
                data={
                    "sub_stage": "plan_repair",
                    "rationale": "A weak plan leads to simplistic geometry, so the plan is repaired before CadQuery code is generated.",
                    "missing_features": list(quality_report.missing_features),
                    "missing_negative_space": list(quality_report.missing_negative_space),
                    "feedback": quality_report.feedback,
                    "system_prompt": plan_repair_system_prompt,
                    "prompt": plan_repair_user_prompt,
                    "reasoning_channel": "planning",
                    "in_progress": True,
                },
            )
            try:
                plan = await self.llm.repair_design_plan(
                    user_message=user_message,
                    rejected_plan=plan,
                    quality_feedback=quality_report.feedback,
                    chat_history=chat_ctx,
                    current_source=current_source,
                    current_model_id=current_model_id,
                    research_context=_merge_external_context(recall_context, research_context),
                    recipe_context=planning_reference_context,
                    hard_constraints=config.hard_constraints,
                    soft_constraints=config.soft_constraints,
                    on_chunk=_plan_chunk,
                )
                quality_report = validate_plan_against_recipes(plan, recipe_cards)
            except LLMBackendUnavailable as e:
                await self._handle_backend_unavailable(e, stage="plan_repair")
                raise
            except Exception as e:
                await self._emit_debug("plan_repair_error", f"Plan repair failed: {e}", {"traceback": traceback.format_exc()})

            if not quality_report.is_sufficient:
                await self._emit_status(
                    "planning",
                    "Plan still has gaps — proceeding with explicit recipe constraints.",
                    details=None,
                    data={
                        "sub_stage": "plan_repair_partial",
                        "rationale": "Proceeding keeps the pipeline usable, but code generation and vision critique will still receive the recipe checklist.",
                        "missing_features": list(quality_report.missing_features),
                        "missing_negative_space": list(quality_report.missing_negative_space),
                        "feedback": quality_report.feedback,
                        # The LLM did try to repair; surface its post-repair
                        # summary so the user can see what it returned.
                        "llm_revised_summary": plan.summary,
                    },
                )
            else:
                await self._emit_status(
                    "planning",
                    "Plan repair passed quality gate.",
                    details=None,
                    data={
                        "sub_stage": "plan_repair_ok",
                        "outcome": plan.summary or "The revised plan now satisfies the retrieved CAD recipe checklist.",
                        "outcome_source": "planner",
                    },
                )

        await self._emit_plan(plan)
        await self._emit_debug("plan_ready", "Design plan generated", {
            "summary": plan.summary,
            "overall_dimensions_mm": plan.overall_dimensions_mm,
            "component_count": len(plan.components),
            "key_feature_count": len(plan.key_features),
            "components": [c.model_dump() for c in plan.components],
            "key_features": plan.key_features,
            "assumptions": plan.assumptions,
            "risks": plan.risks,
        })

        plan_text = plan_to_prompt_text(plan)

        # Headline: prefer the planner's own one-line summary so the user can
        # read the plan goal directly without expanding the row. All structured
        # fields go into `data` so the frontend's PlanArtifacts card can render
        # them properly (with newlines preserved, etc.).
        goal_text = plan.summary or "Plan ready for code generation."
        await self._emit_status(
            "planning",
            f"Design plan ready · {goal_text}",
            details=None,
            data={
                "sub_stage": "plan_ready",
                # Match the frontend PlanArtifacts contract — `summary` is the
                # canonical key for the design goal. (`plan_summary` is kept as
                # an alias for any legacy debug consumers.)
                "summary": plan.summary,
                "plan_summary": plan.summary,
                "overall_dimensions_mm": plan.overall_dimensions_mm,
                "raw_reasoning": plan.raw_reasoning,
                "components": [c.model_dump() for c in plan.components],
                "key_features": plan.key_features,
                "assumptions": plan.assumptions,
                "risks": plan.risks,
                "parameters": plan.parameters,
            },
        )

        last_code = ""
        last_error = ""
        last_critique: Optional[CritiqueReport] = None
        last_failure_type: Optional[str] = None
        last_geometry_stats: Dict = {}
        repair_notes: List[str] = []
        consecutive_empty = 0

        # 3. Iterative Loop
        for iteration in range(1, self.MAX_REPAIR_ITERATIONS + 1):
            try:
                # Generate the model ID for this iteration early
                model_id = self.storage.next_model_id(project_id)
                current_model_id = model_id

                # ── Step A: Generate or Repair code ──────────────────────────
                if iteration == 1:
                    code_generation_context = _merge_external_context(recall_context, research_context)
                    code_generation_prompt = self._build_code_generation_user_prompt(
                        user_message=user_message,
                        current_source=current_source,
                        current_model_id=base_model_id,
                        selection=selection,
                        research_context=code_generation_context,
                        recipe_context=generation_reference_context,
                        project_id=project_id,
                        plan_text=plan_text,
                    )
                    await self._emit_status("generating", f"Writing CadQuery code (`{model_id}`)…",
                                          details=None,
                                          data={
                                              "iteration": iteration,
                                              "rationale": "CadQuery lets us control mechanical geometry parametrically.",
                                              "inputs": context_used + ["CadQuery API", "design plan"],
                                              "model_id": model_id,
                                              "model": self.llm.model,
                                              "system_prompt": system_prompt,
                                              "prompt": code_generation_prompt,
                                              "reasoning_channel": "generating",
                                              "in_progress": True,
                                          })
                    last_code = await self._generate_code_streaming(
                        user_message, system_prompt, chat_ctx,
                        current_source, base_model_id, selection,
                        research_context=code_generation_context,
                        recipe_context=generation_reference_context,
                        project_id=project_id,
                        plan_text=plan_text,
                    )
                elif last_critique and last_critique.issues:
                    # Vision-driven repair
                    repair_prompt = self._build_vision_repair_prompt(
                        last_code,
                        last_critique,
                        user_message,
                        iteration,
                        plan_text=plan_text,
                        recipe_context=generation_reference_context,
                    )
                    repair_system_prompt = build_system_prompt(config.hard_constraints, config.soft_constraints)
                    repair_user_prompt = build_repair_prompt(last_code, repair_prompt, iteration)
                    await self._emit_status("repairing",
                        f"Repairing for vision feedback (attempt {iteration}/{self.MAX_REPAIR_ITERATIONS}) · `{model_id}`",
                        details=None,
                        data={
                            "iteration": iteration,
                            "repair_kind": "vision",
                            "rationale": "The rendered model passed execution but did not meet the visual/printability quality threshold.",
                            "outcome": f"Fixing {len(last_critique.issues)} vision issue(s).",
                            "inputs": [
                                f"vision score {last_critique.overall_printability:.2f}",
                                f"{len(last_critique.issues)} critique issue(s)",
                            ],
                            "vision_issues": [
                                {
                                    "severity": i.severity,
                                    "issue_type": i.issue_type,
                                    "description": i.description,
                                    "location_hint": i.location_hint,
                                }
                                for i in last_critique.issues
                            ],
                            "model_id": model_id,
                            "model": self.llm.model,
                            "system_prompt": repair_system_prompt,
                            "prompt": repair_user_prompt,
                        })
                    await self._emit_debug("repair_request", f"Vision repair attempt {iteration}", {
                        "score": last_critique.overall_printability,
                        "issues_count": len(last_critique.issues),
                        "repair_prompt_preview": repair_prompt[:400],
                    })
                    repair_response = await self.llm.repair_cadquery(
                        original_code=last_code,
                        error_message=repair_prompt,
                        iteration=iteration,
                        hard_constraints=config.hard_constraints,
                        soft_constraints=config.soft_constraints,
                    )
                    repair_notes.append(f"Vision critique identified {len(last_critique.issues)} issues (score: {last_critique.overall_printability:.2f})")
                    last_code = extract_code_from_response(repair_response)
                else:
                    # Execution-error repair
                    failure_label = (last_failure_type or "execution_error").replace("_", " ")
                    err_first = last_error.splitlines()[0][:120] if last_error else "previous attempt failed"

                    # Mechanical pre-repair: when the validator complained that
                    # `result` is unassigned but the rest of the source parses
                    # fine, patch it in code instead of asking the LLM. The
                    # LLM-driven repair has a habit of "fixing" the missing
                    # assignment by also dropping most of the planned geometry.
                    mechanical_patch: Optional[str] = None
                    if last_failure_type == "syntax_error" and last_error and "must assign" in last_error.lower():
                        from ..cad.engine import try_patch_missing_result
                        mechanical_patch = try_patch_missing_result(last_code)

                    if mechanical_patch is not None:
                        await self._emit_status("repairing",
                            f"Patching missing `result` assignment (attempt {iteration}/{self.MAX_REPAIR_ITERATIONS}) · `{model_id}`",
                            details=None,
                            data={
                                "iteration": iteration,
                                "repair_kind": "mechanical",
                                "rationale": "The previous source parsed cleanly but did not assign the final shape to `result`. Patching deterministically so the LLM does not also drop geometry.",
                                "outcome": "Appended `result = <last_shape_var>` to the source.",
                                "inputs": ["AST patch"],
                                "model_id": model_id,
                            })
                        await self._emit_debug("mechanical_repair", "Applied AST-based result alias", {
                            "original_tail": last_code[-200:],
                            "patched_tail": mechanical_patch[-200:],
                        })
                        last_code = mechanical_patch
                        repair_notes.append("Mechanically aliased the final shape to `result` (no LLM call)")
                    else:
                        repair_system_prompt = build_system_prompt(config.hard_constraints, config.soft_constraints)
                        repair_user_prompt = build_repair_prompt(
                            last_code,
                            last_error,
                            iteration,
                            failure_type=last_failure_type,
                            geometry_stats=last_geometry_stats,
                        )
                        await self._emit_status("repairing",
                            f"Repairing {failure_label} (attempt {iteration}/{self.MAX_REPAIR_ITERATIONS}) · `{model_id}`",
                            details=None,
                            data={
                                "iteration": iteration,
                                "repair_kind": "execution",
                                "rationale": "The previous generated source did not produce valid geometry, so the next LLM call is constrained by the failure.",
                                "outcome": f"Trying to fix: {err_first}",
                                "inputs": [last_failure_type or "execution_error", err_first],
                                "error_excerpt": (last_error[:600] if last_error else None),
                                "model_id": model_id,
                                "model": self.llm.model,
                                "system_prompt": repair_system_prompt,
                                "prompt": repair_user_prompt,
                            })
                        await self._emit_debug("repair_request", f"Error repair attempt {iteration}", {
                            "original_code": last_code, "error_message": last_error[:500],
                        })
                        repair_response = await self.llm.repair_cadquery(
                            original_code=last_code,
                            error_message=last_error,
                            iteration=iteration,
                            hard_constraints=config.hard_constraints,
                            soft_constraints=config.soft_constraints,
                            failure_type=last_failure_type,
                            geometry_stats=last_geometry_stats,
                        )
                        error_summary = last_error.splitlines()[0][:60] if last_error else "previous attempt failed"
                        repair_notes.append(f"Fixed {failure_label}: {error_summary}...")
                        last_code = extract_code_from_response(repair_response)

                if not last_code.strip():
                    last_error = "LLM returned empty code"
                    consecutive_empty += 1
                    await self._emit_debug(
                        "empty_code",
                        f"LLM returned empty code on attempt {iteration} (consecutive={consecutive_empty}).",
                    )
                    # Two consecutive empty responses → the model is stuck (usually
                    # qwen3.x looping in reasoning). Abort early instead of wasting
                    # the remaining iterations on the same failure mode.
                    if consecutive_empty >= 2:
                        await self._emit_error(
                            "LLM returned empty code two attempts in a row; aborting. "
                            "This usually means the model is looping in reasoning. "
                            "Try a simpler/shorter prompt or switch to gemma4:31b.",
                            "execution_error",
                        )
                        self._save_failure_chat(project_id, thread_id, None)
                        return None
                    continue
                consecutive_empty = 0

                # ── Step B: Execute CadQuery ──────────────────────────────────
                await self._emit_status("executing", "Running the geometry engine…",
                                      details=None,
                                      data={
                                          "iteration": iteration,
                                          "rationale": "Builds the 3D B-Rep solids and checks they are manifold.",
                                          "inputs": ["CadQuery source"],
                                          "in_progress": True,
                                      })
                
                model_dir = self.storage.create_model_dir(project_id, model_id)

                t_exec_start = time.time()
                exec_result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    process_cadquery_code,
                    last_code,
                    model_dir,
                    "part",
                    config.hard_constraints,
                    project_id,
                    self.storage,
                )
                t_exec_elapsed = time.time() - t_exec_start

                await self._emit_debug("cadquery_result", 
                    f"CadQuery execution {'succeeded' if exec_result['success'] else 'failed'} ({t_exec_elapsed:.2f}s)", {
                        "success": exec_result["success"],
                        "message": exec_result["message"],
                        "geometry_stats": exec_result.get("geometry_stats", {}),
                        "failure_type": exec_result.get("failure_type"),
                    })

                if not exec_result["success"]:
                    last_error = exec_result["message"]
                    last_critique = None
                    last_failure_type = exec_result.get("failure_type") or "execution_error"
                    last_geometry_stats = exec_result.get("geometry_stats", {})

                    metadata = ModelMetadata(
                        model_id=model_id,
                        parent_model_id=base_model_id,
                        prompt=user_message,
                        cad_source=last_code,
                        failure_type=FailureType(last_failure_type),
                        failure_message=last_error,
                        iteration=iteration,
                        citations=citations,
                        is_final=False,
                        thread_id=thread_id,
                        turn_index=turn_index,
                    )
                    self.storage.save_model_metadata(project_id, metadata)
                    current_model_id = model_id # Track latest WIP
                    
                    # Notify UI of WIP model even if it failed, so source is visible
                    msg_first = (exec_result.get("message") or "unknown error").splitlines()[0][:160]
                    await self._emit_status("failed", f"Execution failed: {msg_first}",
                                          details=None,
                                          data={
                                              "iteration": iteration,
                                              "model_id": model_id,
                                              "failure_type": last_failure_type,
                                              "error_excerpt": (last_error[:600] if last_error else None),
                                              "outcome": "Will retry with a code-repair pass.",
                                          })
                    continue

                # ── Step C: Success! Render and Critique ──────────────────────
                await self._emit_status("tessellating", "Building 3D preview mesh…",
                                      details=None,
                                      data={
                                          "iteration": iteration,
                                          "rationale": "Convert B-Rep solids to a GLB mesh so the browser can render the model.",
                                          "inputs": ["B-Rep solids"],
                                          "in_progress": True,
                                      })
                
                preliminary_metadata = ModelMetadata(
                    model_id=model_id,
                    parent_model_id=base_model_id,
                    prompt=user_message,
                    cad_source=last_code,
                    has_step="step" in exec_result["files"],
                    has_stl="stl" in exec_result["files"],
                    has_glb="glb" in exec_result["files"],
                    iteration=iteration,
                    citations=citations,
                    is_final=False,
                    thread_id=thread_id,
                    turn_index=turn_index,
                )
                self.storage.save_model_metadata(project_id, preliminary_metadata)

                glb_url = f"/api/projects/{project_id}/models/{model_id}/glb"
                if self.on_model_ready:
                    await self.on_model_ready(model_id, glb_url)

                # Render multi-angle PNGs
                shape = exec_result.get("_shape")
                render_paths = {}
                if shape is not None:
                    render_result = await self._run_render(shape, model_dir)
                    if isinstance(render_result, dict):
                        render_paths = render_result
                    elif render_result:
                        await self._emit_debug(
                            "render_warning",
                            "Render step returned a non-dictionary result; using placeholder paths for downstream handling.",
                            {"result_type": type(render_result).__name__},
                        )
                        render_paths = {"unknown": str(render_result)}

                # Vision Critique
                geometry_stats = exec_result.get("geometry_stats", {})
                manufacturability = exec_result.get("manufacturability")
                critique = None
                if render_paths and vision_ok:
                    critique = await self._run_vision_critique(
                        render_paths, user_message, geometry_stats, project_id, model_id,
                        plan=plan,
                        recipe_context=planning_reference_context,
                    )
                elif render_paths:
                    await self._emit_status(
                        "critiquing",
                        "Skipped vision critique — verifier unavailable.",
                        details=None,
                        data={
                            "iteration": iteration,
                            "rationale": "Proceeding with geometric validation only so the run isn't blocked.",
                            "outcome": "Vision model (Ollama) is disconnected or unavailable.",
                            "skipped": ["vision-based verification"],
                        },
                    )

                # Deterministic plan-conformance check — runs whether or not
                # vision was available. Compares the measured bbox + solid count
                # against the plan so a "valid plate" can't masquerade as a
                # successful iPhone-holder when the vision critic is offline.
                from .plan_conformance import check_plan_conformance
                conformance = check_plan_conformance(plan, geometry_stats)
                if conformance is not None:
                    await self._emit_status(
                        "validating",
                        ("Plan-conformance check passed."
                         if conformance.passed
                         else f"Plan-conformance check failed: {conformance.reasons[0][:120]}"),
                        details=None,
                        data={
                            "iteration": iteration,
                            "rationale": "Deterministic bbox + solid-count comparison against the design plan.",
                            "outcome": ("Geometry matches plan dimensions."
                                        if conformance.passed
                                        else "Geometry does not match plan; triggering repair."),
                            "expected_bbox_mm": list(conformance.expected_bbox) if conformance.expected_bbox else None,
                            "measured_bbox_mm": list(conformance.measured_bbox) if conformance.measured_bbox else None,
                            "expected_solids": conformance.expected_solids,
                            "measured_solids": conformance.measured_solids,
                            "score": conformance.score,
                            "reasons": conformance.reasons,
                        },
                    )
                    if not conformance.passed:
                        # Merge into the vision critique so the repair branch
                        # treats it as a single set of issues. If vision didn't
                        # produce a critique (skipped or no signal), synthesize
                        # one from the conformance report.
                        conformance_report = conformance.as_critique()
                        if critique is None:
                            critique = conformance_report
                        else:
                            critique.issues.extend(conformance_report.issues)
                            critique.matches_intent = False
                            critique.overall_printability = min(
                                critique.overall_printability, conformance_report.overall_printability
                            )
                            if conformance_report.repair_prompt:
                                joiner = "\n\n" if critique.repair_prompt else ""
                                critique.repair_prompt = (
                                    f"{critique.repair_prompt}{joiner}{conformance_report.repair_prompt}"
                                )

                # Save metadata
                geo_stats_model = None
                if geometry_stats:
                    geo_stats_model = GeometryStats(**{
                        k: v for k, v in geometry_stats.items()
                        if k in GeometryStats.model_fields
                    })

                metadata = ModelMetadata(
                    model_id=model_id,
                    parent_model_id=base_model_id,
                    prompt=user_message,
                    cad_source=last_code,
                    has_step="step" in exec_result["files"],
                    has_stl="stl" in exec_result["files"],
                    has_glb="glb" in exec_result["files"],
                    has_render=bool(render_paths),
                    render_paths=render_paths,
                    critique=critique,
                    geometry_stats=geo_stats_model,
                    manufacturability=manufacturability,
                    assembly=exec_result.get("assembly"),
                    iteration=iteration,
                    vision_score=critique.overall_printability if critique else None,
                    citations=citations,
                    plan=plan,
                    is_final=False,  # may be promoted below if we accept this iteration
                    thread_id=thread_id,
                    turn_index=turn_index,
                )
                self.storage.save_model_metadata(project_id, metadata)

                if geometry_stats:
                    self.storage.save_geometry_analysis(project_id, model_id, geometry_stats)

                # Decide on repair — repair when:
                #   - vision score below threshold
                #   - any error-level issue
                #   - vision explicitly says the model does not match user intent
                vision_score = critique.overall_printability if critique else 1.0
                has_errors = critique and any(i.severity == "error" for i in critique.issues)
                intent_mismatch = bool(critique and critique.matches_intent is False)
                needs_repair = critique and (
                    vision_score < self.VISION_SCORE_THRESHOLD or has_errors or intent_mismatch
                )

                if needs_repair and iteration < self.MAX_REPAIR_ITERATIONS:
                    last_critique = critique
                    await self._emit_debug("vision_repair_trigger", 
                        f"Vision score {vision_score:.2f} below threshold — triggering repair")
                    continue

                # Promote this metadata to final and persist again. We re-save
                # the same metadata object with is_final=True so the version
                # sidebar can label it distinctly from the WIP iterations that
                # preceded it.
                metadata.is_final = True
                self.storage.save_model_metadata(project_id, metadata)

                # Final Success Response
                response_text = self._build_final_response(model_id, iteration, exec_result, critique, vision_score, repair_notes)
                
                # Use update instead of append because we already have a placeholder
                self.storage.update_last_chat_thread_message(
                    project_id, thread_id,
                    ChatMessage(
                        role="assistant",
                        content=response_text,
                        model_id=model_id,
                        steps=self.current_steps
                    ),
                )
                return model_id

            except Exception as e:
                await self._emit_debug("pipeline_error", f"Pipeline error at iteration {iteration}", {
                    "error": str(e), "traceback": traceback.format_exc(),
                })
                if iteration >= self.MAX_REPAIR_ITERATIONS:
                    await self._emit_error(f"Pipeline error: {str(e)}", "unexpected_error")
                    return None

        # If loop finished without success
        self._save_failure_chat(project_id, thread_id, current_model_id)
        return None

    def _build_code_generation_user_prompt(
        self,
        user_message: str,
        current_source: str,
        current_model_id: Optional[str],
        selection: Optional[SelectionContext] = None,
        research_context: str = "",
        recipe_context: str = "",
        project_id: str = "",
        plan_text: str = "",
    ) -> str:
        """Build the exact user prompt sent to the code-generation LLM."""
        plan_block = f"{plan_text}\n\n" if plan_text else ""

        selection_context = ""
        if selection:
            feature_meta = {}
            if current_model_id:
                manifest = self.storage.get_model_features(project_id, current_model_id)
                for f in manifest:
                    if f.get("name") == selection.feature_name:
                        feature_meta = f
                        break

            selection_context = (
                f"## Active Selection\n"
                f"The user has selected the following feature in the 3D viewport:\n"
                f"- Feature Name: `{selection.feature_name}`\n"
            )
            if feature_meta.get("type"):
                selection_context += f"- Feature Type: {feature_meta['type']}\n"
            if feature_meta.get("center"):
                selection_context += f"- Feature Center: {feature_meta['center']} (X, Y, Z in mm)\n"
            elif selection.point:
                selection_context += f"- Click Coordinates: {selection.point} (X, Y, Z in mm)\n"

            selection_context += "\nYour changes should prioritize or relate to this selected feature if relevant to the request.\n\n"

        if current_source:
            return (
                f"{selection_context}{research_context}{recipe_context}\n\n{plan_block}"
                "The project has one model with versioned checkpoints. "
                f"Use checkpoint `{current_model_id}` as the current base model and edit it for this request.\n\n"
                "## Current CadQuery Source\n"
                "```python\n"
                f"{current_source}\n"
                "```\n\n"
                "## Requested Change\n"
                f"{user_message}\n\n"
                "Follow the Design Plan above. If the plan and the request conflict, prefer the plan but make the conflict explicit in a brief inline comment."
            )

        return (
            f"{selection_context}{research_context}{recipe_context}\n\n{plan_block}"
            f"## Requested Change\n{user_message}\n\n"
            "Follow the Design Plan above. Declare the named parameters at the top of the source so they are editable later."
        )

    async def _generate_code_streaming(
        self,
        user_message: str,
        system_prompt: str,
        chat_ctx: List[Dict],
        current_source: str,
        current_model_id: Optional[str],
        selection: Optional[SelectionContext] = None,
        research_context: str = "",
        recipe_context: str = "",
        project_id: str = "",
        plan_text: str = "",
    ) -> str:
        effective_user_message = user_message

        plan_block = f"{plan_text}\n\n" if plan_text else ""

        selection_context = ""
        if selection:
            # Try to find more metadata in the manifest
            feature_meta = {}
            if current_model_id:
                manifest = self.storage.get_model_features(project_id, current_model_id)
                for f in manifest:
                    if f.get("name") == selection.feature_name:
                        feature_meta = f
                        break

            selection_context = (
                f"## Active Selection\n"
                f"The user has selected the following feature in the 3D viewport:\n"
                f"- Feature Name: `{selection.feature_name}`\n"
            )
            if feature_meta.get("type"):
                selection_context += f"- Feature Type: {feature_meta['type']}\n"
            if feature_meta.get("center"):
                selection_context += f"- Feature Center: {feature_meta['center']} (X, Y, Z in mm)\n"
            elif selection.point:
                selection_context += f"- Click Coordinates: {selection.point} (X, Y, Z in mm)\n"
            
            selection_context += "\nYour changes should prioritize or relate to this selected feature if relevant to the request.\n\n"

        if current_source:
            effective_user_message = (
                f"{selection_context}{research_context}{recipe_context}\n\n{plan_block}"
                "The project has one model with versioned checkpoints. "
                f"Use checkpoint `{current_model_id}` as the current base model and edit it for this request.\n\n"
                "## Current CadQuery Source\n"
                "```python\n"
                f"{current_source}\n"
                "```\n\n"
                "## Requested Change\n"
                f"{user_message}\n\n"
                "Follow the Design Plan above. If the plan and the request conflict, prefer the plan but make the conflict explicit in a brief inline comment."
            )
        else:
            effective_user_message = (
                f"{selection_context}{research_context}{recipe_context}\n\n{plan_block}"
                f"## Requested Change\n{user_message}\n\n"
                "Follow the Design Plan above. Declare the named parameters at the top of the source so they are editable later."
            )

        effective_user_message = self._build_code_generation_user_prompt(
            user_message=user_message,
            current_source=current_source,
            current_model_id=current_model_id,
            selection=selection,
            research_context=research_context,
            recipe_context=recipe_context,
            project_id=project_id,
            plan_text=plan_text,
        )

        await self._emit_debug("llm_request", "Sending request to LLM", {
            "model": self.llm.model,
            "user_message": user_message,
            "base_model_id": current_model_id,
        })

        full_response = ""
        # We also capture reasoning because qwen3.x sometimes emits the code
        # block in the `reasoning` channel rather than `content` (especially for
        # complex prompts). If `content` ends up empty we fall back to it.
        reasoning_buffer = ""
        t_start = time.time()

        async def _code_reasoning(text: str):
            nonlocal reasoning_buffer
            reasoning_buffer += text
            await self._emit_reasoning("generating", text)

        stream = self.llm.generate_stream(
            effective_user_message, system_prompt, chat_ctx,
            on_reasoning=_code_reasoning,
            max_tokens=6144,
        )
        if inspect.isawaitable(stream):
            stream = await stream
        async for chunk in stream:
            full_response += chunk
            await self._emit_chunk(chunk)

        # If the model put the code in the reasoning channel instead of content
        # (a Qwen3.x failure mode), recover by extracting from reasoning. We
        # prefer content when both have code blocks.
        extracted = extract_code_from_response(full_response).strip()
        if not extracted and reasoning_buffer:
            extracted_from_reasoning = extract_code_from_response(reasoning_buffer).strip()
            if extracted_from_reasoning:
                await self._emit_debug(
                    "code_recovered_from_reasoning",
                    "Code block was in the reasoning channel; recovered.",
                    {"reasoning_length": len(reasoning_buffer), "code_length": len(extracted_from_reasoning)},
                )
                # Synthesize the full_response so the rest of the pipeline (parse,
                # save, etc.) sees the code as if it had arrived normally.
                full_response = "```python\n" + extracted_from_reasoning + "\n```"

        elapsed = time.time() - t_start
        await self._emit_debug("llm_response", f"LLM response complete ({elapsed:.1f}s)")
        
        return extract_code_from_response(full_response)

    async def _run_render(self, shape, model_dir) -> Dict[str, str]:
        await self._emit_status("rendering", "Rendering ISO / Top / Front / Side views…",
                              details=None,
                              data={
                                  "rationale": "Multi-angle snapshots feed the vision verifier.",
                                  "inputs": ["3D shape"],
                                  "in_progress": True,
                              })
        try:
            from ..render.renderer import render_shape_multiangle
            result = await asyncio.get_event_loop().run_in_executor(
                None, render_shape_multiangle, shape, model_dir, "part"
            )
            if result.success:
                return result.renders
            return {}
        except Exception as e:
            await self._emit_debug("render_error", str(e))
            return {}

    async def _run_vision_critique(
        self, render_paths: Dict[str, str], user_intent: str,
        geometry_stats: Dict, project_id: str, model_id: str,
        plan: Optional[DesignPlan] = None,
        recipe_context: str = "",
    ) -> Optional[CritiqueReport]:
        vision_user_prompt = _build_vision_user_prompt(
            user_intent,
            geometry_stats,
            plan=plan,
            recipe_context=recipe_context,
        )
        critic = VisionCritic()
        available, _ = await critic.is_available()
        if not available:
            return None
        vision_model = critic.model
        await self._emit_status("critiquing", "Vision verifier reviewing the renders…",
                              details=None,
                              data={
                                  "rationale": "A multimodal LLM scores each plan key-feature as present / missing.",
                                  "inputs": [f"{len(render_paths)} render(s)", "design plan checklist"],
                                  "model": vision_model,
                                  "system_prompt": VISION_SYSTEM_PROMPT,
                                  "prompt": vision_user_prompt,
                                  "image_views": list(render_paths.keys()),
                                  "in_progress": True,
                              })
        try:
            critique_result = await critic.critique(
                render_paths,
                user_intent,
                geometry_stats,
                plan=plan,
                recipe_context=recipe_context,
            )
            await self._emit_debug("vision_response", "Vision critique response received", {
                "success": critique_result.success,
                "message": critique_result.message,
                "matches_intent": critique_result.matches_intent,
                "raw_response_preview": (critique_result.raw_response or "")[:1500],
            })
            if not critique_result.success:
                await self._emit_status(
                    "critiquing",
                    "Vision verifier returned no usable feedback — continuing.",
                    details=None,
                    data={
                        "outcome": critique_result.message or "Verifier output could not be parsed.",
                        "skipped": ["vision repair"],
                    },
                )
                return None

            report = critique_result.report
            render_urls = {
                view: f"/api/projects/{project_id}/models/{model_id}/renders/{view}"
                for view in render_paths
            }

            if self.on_critique:
                await self.on_critique(report, render_urls)

            report.repair_prompt = critique_result.repair_prompt
            report.matches_intent = critique_result.matches_intent

            # Surface a follow-up step with the verifier's findings so the
            # timeline shows *why* (or whether) a repair is about to fire.
            score = report.overall_printability
            n_errors = sum(1 for i in report.issues if i.severity == "error")
            n_warnings = sum(1 for i in report.issues if i.severity == "warning")
            matches = "✓ matches intent" if critique_result.matches_intent else "✗ does NOT match intent"
            headline = f"Vision score {score:.2f} · {matches} · {n_errors} error(s), {n_warnings} warning(s)"
            await self._emit_status(
                "critiquing",
                headline,
                details=None,
                data={
                    "outcome": (report.repair_prompt or "")[:300] or None,
                    "vision_score": score,
                    "matches_intent": critique_result.matches_intent,
                    "issue_counts": {"error": n_errors, "warning": n_warnings, "info": max(0, len(report.issues) - n_errors - n_warnings)},
                    "vision_issues": [
                        {
                            "severity": i.severity,
                            "issue_type": i.issue_type,
                            "description": i.description,
                            "location_hint": i.location_hint,
                        }
                        for i in report.issues
                    ],
                },
            )

            return report
        except Exception as e:
            await self._emit_debug("vision_error", str(e))
            return None

    def _build_vision_repair_prompt(
        self,
        code: str,
        critique: CritiqueReport,
        intent: str,
        iter: int,
        plan_text: str = "",
        recipe_context: str = "",
    ) -> str:
        issues_text = "\n".join(
            f"- [{i.severity.upper()}] {i.issue_type} ({i.location_hint or 'unknown location'}): {i.description}"
            for i in critique.issues
        ) or "- (no specific issues listed — overall score below threshold or intent mismatch)"

        intent_match_text = ""
        if not critique.matches_intent:
            intent_match_text = (
                "\n## CRITICAL: The vision verifier reports the model does NOT match the user's intent. "
                "Re-read the user's request and the plan; the current code is producing the wrong shape, "
                "not just an imperfect one. Rework the geometry rather than tweaking dimensions.\n"
            )

        plan_block = f"\n{plan_text}\n" if plan_text else ""
        recipe_block = f"\n## CAD Recipe / Product Archetype Context\n{recipe_context}\n" if recipe_context else ""

        return f"""The CAD model was rendered and reviewed by a vision verifier. Repair it.

## User Intent
{intent}
{recipe_block}
{plan_block}
## Current Code
```python
{code}
```

## Vision Critique (iteration {iter})
- Overall score: {critique.overall_printability:.2f}
- Matches intent: {critique.matches_intent}
- Confidence: {critique.confidence:.2f}

### Issues
{issues_text}
{intent_match_text}
## Required fixes (from verifier)
{critique.repair_prompt or '(none provided — fix the issues listed above)'}

## Output rules
- Output ONLY a single ```python block with the corrected code.
- Keep the same named parameters at the top so the design stays editable.
- Make the fewest changes needed to address every listed issue.
- If required features are missing, rework the structure using the recipe/archetype; do not merely resize the existing boxes.
"""

    def _build_final_response(self, mid, iter, res, critique, score, repair_notes: List[str] = None) -> str:
        stats = res.get("geometry_stats", {})
        size_text = f"\n📐 Size: {stats.get('bounding_box', 'Unknown')}"
        
        manufacturability = res.get("manufacturability")
        m_text = ""
        if manufacturability:
            m_emoji = "✅" if manufacturability.score >= 0.9 else "🟡" if manufacturability.score >= 0.7 else "🔴"
            m_text = f"\n{m_emoji} Printability score: **{manufacturability.score:.2f}**/1.0"

        critique_text = ""
        if critique:
            emoji = "✅" if score >= 0.8 else "🟡" if score >= 0.65 else "🔴"
            critique_text = f"\n{emoji} Vision critique score: **{score:.2f}**/1.0"
        
        repair_text = ""
        if repair_notes:
            repair_text = "\n\n**Repair History:**\n" + "\n".join(f"- {n}" for n in repair_notes)
        
        return f"✓ Model generated (`{mid}`, attempt {iter}).{size_text}{m_text}{critique_text}{repair_text}"

    def _save_failure_chat(self, pid, tid, mid=None):
        # Use update instead of append because we already have a placeholder
        self.storage.update_last_chat_thread_message(
            pid, tid,
            ChatMessage(
                role="assistant",
                content="Failed to generate valid model after retries.",
                model_id=mid,
                steps=self.current_steps,
            ),
        )
