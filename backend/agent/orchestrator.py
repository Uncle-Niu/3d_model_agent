"""
Agent Orchestrator — manages the CAD generation and repair pipeline.

This module extracts the core logic from the API layer to provide a 
reusable, structured generation workflow.
"""

import asyncio
import inspect
import time
import traceback
from typing import Any, Callable, Dict, List, Optional

import httpx

from ..cad.engine import process_cadquery_code
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
    LLMService,
    build_system_prompt,
    extract_code_from_response,
    plan_to_prompt_text,
)
from ..storage import StorageService
from ..tools.web_research import search_web, get_research_prompt_extension
from ..vision.critic import VisionCritic


class AgentOrchestrator:
    """
    Orchestrates the full CAD generation loop:
    LLM -> CadQuery -> Validation -> Rendering -> Vision Critique -> Repair
    """

    def __init__(
        self,
        storage: StorageService,
        llm: Optional[LLMService] = None,
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

    async def _emit_status(self, stage: str, message: str, details: Optional[str] = None, data: Optional[Dict] = None):
        step = PipelineStep(stage=stage, message=message, details=details, data=data)
        self.current_steps.append(step)
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

    async def check_vision_connectivity(self) -> bool:
        """Check if the vision model is available and working."""
        from ..vision.critic import VisionCritic
        critic = VisionCritic()
        await self._emit_debug("vision", "Checking vision model availability...")
        available, error = await critic.is_available()
        if not available:
            await self._emit_debug("vision_warning", f"Vision model not available: {error}")
            return False
            
        # Perform smoke test to ensure image processing works
        await self._emit_debug("vision", "Performing vision smoke test...")
        ok, msg = await critic.smoke_test()
        if not ok:
            await self._emit_debug("vision_warning", f"Vision smoke test failed: {msg}")
            return False

        await self._emit_debug("vision", "Vision model is fully operational")
        return True

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
        vision_ok = await self.check_vision_connectivity()

        if not ollama_ok:
            self.storage.append_chat_thread_message(
                project_id, thread_id,
                ChatMessage(role="assistant", content=(
                    f"❌ Cannot reach Ollama. Please make sure Ollama is running "
                    f"and the model `{self.llm.model}` is available."
                )),
            )
            return None

        # 2. Prepare Context
        system_prompt = build_system_prompt(config.hard_constraints, config.soft_constraints)
        history = self.storage.get_chat_thread_messages(project_id, thread_id)
        chat_ctx = [{"role": m.role, "content": m.content} for m in history[-10:]]

        await self._emit_status(
            "planning",
            "Preparing turn context...",
            details=(
                "The agent is gathering the request, recent chat history, project constraints, "
                "and any selected or existing model context before choosing the next action."
            ),
            data={
                "why": "Keep generation grounded in the current project instead of treating this as an isolated prompt.",
                "used": [
                    f"{len(chat_ctx)} recent chat message(s)",
                    "project hard constraints",
                    "project soft constraints",
                ],
                "skipped": [],
            },
        )

        # 2.1 Research step
        citations = []
        research_context = ""
        await self._emit_status(
            "planning",
            "Deciding whether web research is needed...",
            details=(
                "A lightweight LLM planning call checks if the request depends on current "
                "standards, vendor dimensions, material data, or hardware specifications."
            ),
            data={
                "why": "Search only when external facts would materially affect the CAD dimensions or design.",
                "used": ["user request", "recent chat context"],
            },
        )
        search_query = await self.llm.decide_research(user_message, chat_ctx)
        if search_query:
            await self._emit_status("researching", f"Searching the web for: {search_query}...", 
                                  details=f"The agent determined that external technical standards or dimensions are required for '{user_message}'.",
                                  data={
                                      "why": "External reference data may be needed for correct sizing or standards compliance.",
                                      "used": [search_query],
                                  })
            citations = await search_web(search_query)
            if citations:
                research_context = get_research_prompt_extension(citations)
                await self._emit_debug("research_result", f"Found {len(citations)} results", {
                    "query": search_query,
                    "citations": [c.model_dump() for c in citations]
                })
        else:
            await self._emit_status(
                "planning",
                "Skipping web research.",
                details=(
                    "The planner did not find a need for outside specifications; the request can be handled "
                    "from the prompt, chat context, and project constraints."
                ),
                data={
                    "why": "Avoid adding unrelated or stale web facts when the design does not require them.",
                    "skipped": ["web search", "page fetching"],
                },
            )

        current_source = ""
        current_model_id = base_model_id
        if current_model_id:
            current_source = self.storage.get_model_source_text(project_id, current_model_id)
        if not current_source:
            latest_model = self.storage.latest_successful_model(project_id)
            if latest_model:
                current_model_id = latest_model.model_id
                current_source = self.storage.get_model_source_text(project_id, latest_model.model_id)

        context_used = []
        if current_source:
            context_used.append(f"base model `{current_model_id}` source")
        else:
            context_used.append("new model from scratch")
        if selection:
            context_used.append(f"active selection `{selection.feature_name}`")
        if citations:
            context_used.append(f"{len(citations)} research citation(s)")
        await self._emit_status(
            "planning",
            "Selected modeling context.",
            details=(
                "The agent chose whether to edit an existing checkpoint or generate a new model, "
                "then assembled the prompt inputs for code generation."
            ),
            data={
                "why": "Use the most relevant geometry context while keeping unrelated state out of the CAD prompt.",
                "used": context_used,
                "skipped": [] if current_source else ["source-code edit path"],
            },
        )

        # 2.5 Planning step — decompose the request into a structured plan BEFORE
        # writing any CadQuery. The plan is the contract carried through every
        # repair iteration AND given to the vision verifier, so the generator and
        # the critic evaluate against the same explicit goal.
        await self._emit_status(
            "planning",
            "Decomposing the request into a design plan...",
            details=(
                "The agent thinks step-by-step about the geometry, picks specific dimensions, "
                "lists every sub-shape, and records the key visible features the result must have. "
                "You will see this reasoning stream live."
            ),
            data={
                "why": "Planning before coding catches dimensional and decomposition mistakes early and gives the vision verifier an explicit checklist.",
                "used": context_used + (["research context"] if research_context else []),
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
                research_context=research_context,
                hard_constraints=config.hard_constraints,
                soft_constraints=config.soft_constraints,
                on_chunk=_plan_chunk,
            )
        except Exception as e:
            await self._emit_debug("planning_error", f"Planner failed: {e}", {"traceback": traceback.format_exc()})
            plan = DesignPlan(raw_text="", summary="(planner failed — proceeding without a structured plan)")

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
        plan_summary_for_status = plan.summary or (plan_text.splitlines()[0] if plan_text else "")
        await self._emit_status(
            "planning",
            "Plan ready.",
            details=plan_summary_for_status or "Proceeding without a structured plan.",
            data={
                "plan_summary": plan.summary,
                "components": [c.model_dump() for c in plan.components],
                "key_features": plan.key_features,
                "assumptions": plan.assumptions,
                "risks": plan.risks,
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
                # ── Step A: Generate or Repair code ──────────────────────────
                if iteration == 1:
                    await self._emit_status("generating", "Generating CadQuery code...", 
                                          details="Synthesizing Python code using CadQuery API based on your description and project constraints.",
                                          data={
                                              "why": "CadQuery source is the editable canonical representation for this CAD-first workflow.",
                                              "used": context_used + ["CadQuery examples", "constraint prompt"],
                                          })
                    last_code = await self._generate_code_streaming(
                        user_message, system_prompt, chat_ctx,
                        current_source, current_model_id, selection,
                        research_context=research_context,
                        project_id=project_id,
                        plan_text=plan_text,
                    )
                elif last_critique and last_critique.issues:
                    # Vision-driven repair
                    await self._emit_status("repairing", 
                        f"Vision-driven repair (attempt {iteration}/{self.MAX_REPAIR_ITERATIONS})...",
                        details=f"The vision AI identified {len(last_critique.issues)} issues in the previous rendering. Attempting to fix geometry or printability faults.",
                        data={
                            "why": "The rendered model passed execution but did not meet the visual/printability quality threshold.",
                            "used": [
                                f"vision score {last_critique.overall_printability:.2f}",
                                f"{len(last_critique.issues)} critique issue(s)",
                            ],
                        })
                    repair_prompt = self._build_vision_repair_prompt(
                        last_code, last_critique, user_message, iteration, plan_text=plan_text
                    )
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
                    await self._emit_status("repairing", 
                        f"Repairing code (attempt {iteration}/{self.MAX_REPAIR_ITERATIONS})...",
                        details="The previous code failed with an execution error. Analyzing traceback to correct the logic.",
                        data={
                            "why": "The previous generated source did not produce valid geometry, so the next LLM call is constrained by the failure.",
                            "used": [last_failure_type or "execution_error", last_error.splitlines()[0] if last_error else "previous failure"],
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
                    failure_label = (last_failure_type or "execution_error").replace("_", " ")
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
                await self._emit_status("executing", "Running CadQuery code...",
                                      details="Executing the generated Python script in a sandboxed CadQuery environment to produce 3D geometry.",
                                      data={
                                          "why": "Only actual OpenCascade geometry can confirm whether the generated source is usable.",
                                          "used": ["generated CadQuery source", "project constraints"],
                                      })
                
                model_id = self.storage.next_model_id(project_id)
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
                    continue

                # ── Step C: Success! Render and Critique ──────────────────────
                await self._emit_status("tessellating", "Preparing 3D preview...",
                                      details="Tessellating the B-Rep geometry into a GLB mesh for real-time 3D viewing in the browser.",
                                      data={
                                          "why": "The browser displays GLB meshes while STEP remains the canonical CAD export.",
                                          "used": ["validated geometry", "generated model files"],
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
                    )
                elif render_paths:
                    await self._emit_status(
                        "critiquing",
                        "Skipping vision critique.",
                        details="The vision model was unavailable during the preflight check, so deterministic validation results are used for this turn.",
                        data={
                            "why": "Avoid blocking a successful CAD result on an unavailable optional reviewer.",
                            "skipped": ["vision critique"],
                            "used": ["deterministic execution and geometry checks"],
                        },
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
                self.storage.append_chat_thread_message(
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
        self._save_failure_chat(project_id, thread_id, model_id)
        return None

    async def _generate_code_streaming(
        self,
        user_message: str,
        system_prompt: str,
        chat_ctx: List[Dict],
        current_source: str,
        current_model_id: Optional[str],
        selection: Optional[SelectionContext] = None,
        research_context: str = "",
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
                f"{selection_context}{research_context}{plan_block}"
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
                f"{selection_context}{research_context}{plan_block}"
                f"## Requested Change\n{user_message}\n\n"
                "Follow the Design Plan above. Declare the named parameters at the top of the source so they are editable later."
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
        await self._emit_status("rendering", "Generating multi-angle renders...",
                              details="Capturing high-resolution snapshots from multiple angles (ISO, Top, Front, Side) for vision analysis.")
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
    ) -> Optional[CritiqueReport]:
        await self._emit_status("critiquing", "Analyzing geometry with vision AI...",
                              details="Running a multi-modal LLM over the rendered images. The verifier is given the plan's key-features checklist and must explicitly mark each present/missing.")
        try:
            critic = VisionCritic()
            available, _ = await critic.is_available()
            if not available:
                return None

            critique_result = await critic.critique(render_paths, user_intent, geometry_stats, plan=plan)
            await self._emit_debug("vision_response", "Vision critique response received", {
                "success": critique_result.success,
                "message": critique_result.message,
                "matches_intent": critique_result.matches_intent,
                "raw_response_preview": (critique_result.raw_response or "")[:1500],
            })
            if not critique_result.success:
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

        return f"""The CAD model was rendered and reviewed by a vision verifier. Repair it.

## User Intent
{intent}
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
        self.storage.append_chat_thread_message(
            pid, tid,
            ChatMessage(
                role="assistant",
                content="Failed to generate valid model after retries.",
                model_id=mid,
                steps=self.current_steps,
            ),
        )
