"""
Agent Orchestrator — manages the CAD generation and repair pipeline.

This module extracts the core logic from the API layer to provide a 
reusable, structured generation workflow.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import time
import traceback
from typing import Any, Callable, Dict, List, Optional

import httpx


def _extract_failing_source_line(error_message: str, code: str) -> Optional[str]:
    """Pull the source line referenced by ``File "<string>", line N`` from a
    CadQuery execution traceback.

    Returns ``"line N: <stripped source>"`` (truncated) or ``None`` if the
    traceback doesn't pin a single line in the user's code. Used to label
    each entry in the repair-attempt history so the next prompt can show
    *which* line the previous attempt touched.
    """
    if not error_message or not code:
        return None
    m = re.search(r'File "<string>", line (\d+)', error_message)
    if not m:
        # Syntax error path: ``Syntax error at line N: msg``
        m = re.search(r"line (\d+)", error_message)
        if not m:
            return None
    try:
        lineno = int(m.group(1))
    except ValueError:
        return None
    lines = code.splitlines()
    if not (1 <= lineno <= len(lines)):
        return None
    snippet = lines[lineno - 1].strip()
    if len(snippet) > 140:
        snippet = snippet[:140] + "…"
    return f"line {lineno}: {snippet}"


def _collect_recent_turn_errors(turn_error_events) -> list[str]:
    """Return the de-duplicated first-line text of failures that already
    happened on this turn, capped at 5. The repair system prompt uses this
    to highlight "errors hit THIS turn — must avoid" as a higher-priority
    signal than the long-running learned-pitfalls aggregate.

    Order-preserving dedup so the most recent failures appear first when
    the cap kicks in. Empty strings are dropped silently.
    """
    seen: list[str] = []
    for ev in reversed(turn_error_events or []):
        line = (getattr(ev, "error_first_line", "") or "").strip()
        if not line:
            continue
        # Truncate to keep the prompt block compact.
        line = line[:200]
        if line not in seen:
            seen.append(line)
        if len(seen) >= 5:
            break
    return seen


def _error_signature(error_message: str) -> str:
    """Return the most-informative single line of a traceback.

    Tracebacks put the exception type/message on the LAST non-empty line
    (``OCP.OCP.StdFail.StdFail_NotDone: BRep_API: command not done``). The
    first line is just ``Execution error:`` — useless for deduping. We use
    this signature to detect "same error recurred" across repair attempts.
    """
    if not error_message:
        return ""
    for ln in reversed(error_message.strip().splitlines()):
        if ln.strip():
            return ln.strip()[:200]
    return ""


def _build_regen_with_critique_user_message(
    *,
    user_message: str,
    current_code: str,
    critique,
    iteration: int,
    escalation_reason: str,
) -> str:
    """LLM-agent escalation: assemble a code-generation prompt that includes
    the previous attempt's source and the vision verifier's complaints.

    This is the fallback when patch-style repair stalls. We bypass the lean
    repair LLM call and route the prompt through ``_generate_code_streaming``
    instead — which uses the full generation system prompt (CadQuery API
    reference, examples, anti-stub guardrails). The model sees:

    - the original user intent (so it can rebuild from scratch if needed),
    - the previous code (so it can keep what works and replace what doesn't),
    - the vision issues + repair instructions (so it knows *what* to change),
    - an explicit "the patch path stalled" note (so it doesn't try the same
      micro-edit the patch LLM was already failing on).
    """
    issue_lines: list[str] = []
    for it in (critique.issues if critique else [])[:10]:
        severity = (it.severity or "info").upper()
        loc = it.location_hint or "unknown location"
        issue_lines.append(f"- [{severity}] {it.issue_type} ({loc}): {it.description}")
    issues_text = "\n".join(issue_lines) or "- (no specific issues listed)"

    repair_block = ""
    if critique and (critique.repair_prompt or "").strip():
        repair_block = (
            "## Verifier-supplied repair instructions\n"
            f"{critique.repair_prompt.strip()}\n\n"
        )

    score_text = f"{critique.overall_printability:.2f}" if critique else "?"
    matches_text = "yes" if (critique and critique.matches_intent) else "NO"

    return (
        "## Original user request\n"
        f"{user_message}\n\n"
        "## What happened so far\n"
        f"The patch-style repair path stalled on attempt {iteration} — "
        f"{escalation_reason}. This is a full code-generation retry; ignore "
        "the failed patches and rebuild a complete program that passes vision "
        "verification this time.\n\n"
        "## Previous CadQuery source (the program that the verifier rejected)\n"
        "```python\n"
        f"{current_code}\n"
        "```\n\n"
        "## Vision verifier findings on the previous source\n"
        f"- Overall score: {score_text} (threshold ~0.65)\n"
        f"- Matches user intent: {matches_text}\n\n"
        "### Specific issues\n"
        f"{issues_text}\n\n"
        f"{repair_block}"
        "## How to think about this rewrite\n"
        "The previous code already runs cleanly — the geometry it produces is what's wrong. "
        "If the verifier complaint is structural (e.g. \"X is oriented wrong\", \"Y is not "
        "vertical\", \"Z should be tilted\"), do NOT just add another `.rotate(...)` or "
        "`.translate(...)` on top of the assembled result. That style of fix usually rotates "
        "every component including the ones the verifier said WERE correct, breaking another "
        "constraint. Instead, build each component in its FINAL orientation:\n"
        "- If a panel must stay vertical, build it on a vertical workplane (`cq.Workplane(\"XZ\")` "
        "or `cq.Workplane(\"YZ\")`) so it's vertical by construction.\n"
        "- If a tray/plate must be tilted N degrees, rotate THAT plate (and only it) before "
        "translating it into place; leave the other components alone.\n"
        "- Don't rotate the unioned assembly at the end as a shortcut — every other "
        "component gets dragged along with it.\n\n"
        "## Required output\n"
        "Write a SINGLE complete CadQuery program inside one ```python fenced "
        "block. The program must address every vision issue above while still "
        "honoring the Design Plan. Treat this as a fresh write — keep the "
        "parameter block and useful components from the previous code, but do "
        "NOT preserve geometry that the verifier called out as wrong. The "
        "first line must be `import cadquery as cq`; the last meaningful line "
        "must assign the final shape to `result`. No prose outside the code block."
    )


_PYTHON_BLOCK_RE = re.compile(r"```\s*python[^\n]*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def _extract_best_cadquery_block(blob: str) -> str:
    """Pick the most-complete CadQuery program out of a blob that may contain
    several fenced ``​```python​`` drafts.

    Used by the LLM-agent regeneration path. Thinking-mode models (qwen3.x in
    particular) ignore ``/no_think`` on complex prompts and emit a long
    monologue with multiple partial code drafts before the final program.
    The default extractor returns the FIRST block that parses, which is
    usually one of those drafts (e.g. a 10-line sketch of just the VESA
    plate). This scorer picks the block that actually looks like a finished
    CadQuery program — typically the last one.

    Scoring (higher = more likely to be the finished program):

    - Must parse with ``ast.parse`` → otherwise score 0
    - +20 if it contains ``import cadquery``
    - +15 if it contains ``result =`` (or ``result=`` ignoring whitespace)
    - +1 per meaningful non-comment line, up to a cap
    - +5 if it contains ``.union(`` or ``.cut(`` (sign of an assembled model)

    Returns the best-scoring block, or an empty string if no block scores
    above zero. The caller can then fall back to ``extract_code_from_response``
    if needed.
    """
    if not blob:
        return ""
    candidates = _PYTHON_BLOCK_RE.findall(blob)
    if not candidates:
        return ""

    import ast as _ast

    best_score = 0
    best_code = ""
    for raw_block in candidates:
        block = raw_block.strip()
        if not block:
            continue
        try:
            _ast.parse(block)
        except SyntaxError:
            continue
        score = 0
        if "import cadquery" in block:
            score += 20
        if re.search(r"^\s*result\s*=", block, re.MULTILINE):
            score += 15
        meaningful = [
            ln for ln in block.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        score += min(len(meaningful), 80)
        if ".union(" in block or ".cut(" in block:
            score += 5
        if score > best_score:
            best_score = score
            best_code = block
    return best_code if best_score >= 20 else ""


def _format_agent_policy_for_prompt(policy: Optional[AgentTurnPolicy]) -> str:
    if policy is None:
        return ""
    lines = [
        "## LLM Agent Turn Policy",
        f"- Strategy: {policy.strategy}",
    ]
    if policy.rationale:
        lines.append(f"- Rationale: {policy.rationale}")
    if policy.planning_directives:
        lines.append("- Planner directives:")
        lines.extend(f"  - {item}" for item in policy.planning_directives)
    if policy.generation_directives:
        lines.append("- Code-generation directives:")
        lines.extend(f"  - {item}" for item in policy.generation_directives)
    if policy.verification_focus:
        lines.append("- Verification focus:")
        lines.extend(f"  - {item}" for item in policy.verification_focus)
    if policy.risk_notes:
        lines.append("- Risks to avoid:")
        lines.extend(f"  - {item}" for item in policy.risk_notes)
    return "\n".join(lines)


def _agent_policy_log_dict(policy: Optional[AgentTurnPolicy], *, raw_preview_chars: int = 0) -> Optional[dict]:
    if policy is None:
        return None
    data = policy.model_dump()
    raw = data.pop("raw_text", "") or ""
    if raw_preview_chars > 0 and raw:
        data["raw_text_preview"] = raw[:raw_preview_chars]
        data["raw_text_length"] = len(raw)
    return data


from ..cad.engine import process_cadquery_code
from ..cad.example_bank import build_example_bank_prompt_context, retrieve_example_snippets
from ..cad.static_lint import lint_cadquery_source
from ..cad.recipes import (
    build_combined_recipe_context,
    merge_plan_quality_reports,
    retrieve_recipe_cards,
    validate_plan_against_constraints,
    validate_plan_against_recipes,
)
from ..domain.models import (
    ChatMessage,
    Connection,
    CritiqueReport,
    DesignComponent,
    FeatureDecision,
    AgentTurnPolicy,
    DesignPlan,
    FailureType,
    GeometryStats,
    ModelMetadata,
    PhysicalUse,
    SelectionContext,
    PipelineStep,
    Rotation,
)
from ..models.llm_service import (
    LLMBackendUnavailable,
    LLMService,
    build_repair_prompt,
    build_repair_system_prompt,
    build_system_prompt,
    build_vision_repair_prompt,
    detect_repair_deletion,
    extract_code_from_response,
    plan_to_prompt_text,
)
from ..knowledge import LocalKnowledgeService
from ..knowledge import error_patterns as _error_patterns
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
        # Separate budgets for syntax/execution repairs vs. vision-driven
        # repairs. Syntax bugs (LLM emits invalid Python, a wrong CadQuery
        # call, etc.) burn cheaply through retries without producing
        # meaningful design progress, so they get the larger budget. Vision
        # repairs are slower (full render + LLM critique each round) but
        # each one is supposed to move the design closer to the user's
        # intent, so a smaller budget keeps the turn from running forever.
        self.MAX_SYNTAX_REPAIR_ITERATIONS = 8
        self.MAX_VISION_REPAIR_ITERATIONS = 5
        # Absolute cap so a pathological loop can never exceed budget+1
        # iterations even if both counters somehow misfire. Also the value
        # the catch-all `except` uses to decide when to surface a fatal.
        self.MAX_REPAIR_ITERATIONS = (
            self.MAX_SYNTAX_REPAIR_ITERATIONS + self.MAX_VISION_REPAIR_ITERATIONS
        )
        self.VISION_SCORE_THRESHOLD = 0.65

        # Per-run context
        self._current_project_id: Optional[str] = None
        self._current_thread_id: Optional[str] = None
        self._current_agent_logic: str = "orchestrator"

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
                        steps=self.current_steps,
                        agent_logic=self._current_agent_logic,
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
        from ..config import VISION_FALLBACK_MODELS, resolve_llm_model, resolve_vision_model
        from ..vision.critic import VisionCritic

        configured = resolve_vision_model()
        if configured == resolve_llm_model() and configured not in VISION_FALLBACK_MODELS:
            candidates = list(VISION_FALLBACK_MODELS) + [configured]
        else:
            candidates = [configured] + [m for m in VISION_FALLBACK_MODELS if m != configured]
        first_failure = ""

        for idx, candidate in enumerate(candidates):
            critic = VisionCritic(model=candidate)
            await self._emit_debug("vision", f"Checking vision model availability: {candidate}")
            available, error = await critic.is_available()
            if not available:
                first_failure = first_failure or f"registry check failed - {error}"
                await self._emit_debug("vision_warning", f"Vision model not available ({candidate}): {error}")
                continue

            if idx > 0:
                await self._emit_status(
                    "preflight",
                    f"Trying fallback vision model `{candidate}`.",
                    details=f"Configured vision model `{configured}` did not pass preflight.",
                    data={
                        "sub_stage": "vision_fallback",
                        "rationale": "A vision-capable verifier is required, so the agent tries installed fallback models before blocking generation.",
                        "model": candidate,
                    },
                )

            await self._emit_debug("vision", f"Performing vision smoke test: {candidate}")
            await self._emit_status(
                "preflight",
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
            if ok:
                os.environ["VISION_MODEL"] = candidate
                await self._emit_debug("vision", f"Vision model is fully operational: {candidate}")
                return True, msg or "ok"

            first_failure = first_failure or f"smoke test failed - {msg}"
            await self._emit_debug("vision_warning", f"Vision smoke test failed ({candidate}): {msg}")

        return False, first_failure or "no vision model passed preflight"

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
            "preflight",
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
        agent_logic: str = "orchestrator",
    ) -> Optional[str]:
        """
        Runs the full agentic generation pipeline.
        Returns the final model_id on success, or None on failure.
        """
        config = self.storage.get_project(project_id)
        if not config:
            await self._emit_error("Project not found")
            return None

        agent_logic = (agent_logic or "orchestrator").strip().lower()
        if agent_logic not in {"orchestrator", "llm_agent"}:
            agent_logic = "orchestrator"

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
        self._current_agent_logic = agent_logic
        
        # Add placeholder assistant message so we can update it incrementally
        self.storage.append_chat_thread_message(
            project_id, thread_id,
            ChatMessage(role="assistant", content="Starting generation...", steps=[], agent_logic=agent_logic)
        )

        # 2. Prepare Context
        system_prompt = build_system_prompt(config.hard_constraints, config.soft_constraints)
        history = self.storage.get_chat_thread_messages(project_id, thread_id)
        chat_ctx = [{"role": m.role, "content": m.content} for m in history[-10:]]
        latest_model_for_policy = self.storage.latest_successful_model(project_id)
        agent_policy: Optional[AgentTurnPolicy] = None
        agent_policy_context = ""

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

        if agent_logic == "llm_agent":
            policy_system_prompt, policy_user_prompt = LLMService.build_agent_policy_prompt(
                user_message,
                base_model_id=base_model_id,
                latest_model_id=latest_model_for_policy.model_id if latest_model_for_policy else None,
                has_selection=selection is not None,
                selection_name=selection.feature_name if selection else None,
                hard_constraints=config.hard_constraints,
                soft_constraints=config.soft_constraints,
            )
            await self._emit_status(
                "planning",
                "LLM agent is choosing the turn policy.",
                details=(
                    "This mode asks the model to decide how the CAD pipeline "
                    "should gather context and whether this turn should start "
                    "fresh or edit an existing checkpoint."
                ),
                data={
                    "sub_stage": "agent_policy",
                    "agent_logic": agent_logic,
                    "rationale": "The LLM agent path lets the model author the turn policy before the shared CAD tools run.",
                    "rationale_source": "agent",
                    "inputs": ["user prompt", "active checkpoint", "latest successful checkpoint", "active selection"],
                    "model": self.llm.model,
                    "system_prompt": policy_system_prompt,
                    "prompt": policy_user_prompt,
                    "in_progress": True,
                },
            )
            try:
                agent_policy = await self.llm.decide_agent_policy(
                    user_message,
                    chat_history=chat_ctx,
                    base_model_id=base_model_id,
                    latest_model_id=latest_model_for_policy.model_id if latest_model_for_policy else None,
                    has_selection=selection is not None,
                    selection_name=selection.feature_name if selection else None,
                    hard_constraints=config.hard_constraints,
                    soft_constraints=config.soft_constraints,
                )
            except LLMBackendUnavailable as e:
                await self._handle_backend_unavailable(e, stage="agent_policy")
                raise
            except Exception as e:
                await self._emit_debug(
                    "agent_policy_error",
                    f"LLM agent policy failed; falling back to conservative defaults: {e}",
                    {"traceback": traceback.format_exc()},
                )
                agent_policy = AgentTurnPolicy(
                    strategy="auto",
                    rationale="Policy call failed; using default CAD pipeline behavior.",
                )
            agent_policy_context = _format_agent_policy_for_prompt(agent_policy)
            await self._emit_debug(
                "agent_policy",
                "LLM agent turn policy selected",
                _agent_policy_log_dict(agent_policy, raw_preview_chars=1200) or {},
            )
            await self._emit_status(
                "planning",
                f"LLM agent policy ready · {agent_policy.strategy if agent_policy else 'auto'}",
                details=None,
                data={
                    "sub_stage": "agent_policy_ready",
                    "agent_logic": agent_logic,
                    "outcome": agent_policy.rationale if agent_policy else "Using default policy.",
                    "outcome_source": "planner",
                    "policy": _agent_policy_log_dict(agent_policy),
                },
            )
        else:
            await self._emit_status(
                "planning",
                "Using deterministic orchestrator logic.",
                details=None,
                data={
                    "sub_stage": "agent_policy_ready",
                    "agent_logic": agent_logic,
                    "outcome": "The backend will follow the hardcoded plan-retrieve-generate-execute-verify-repair sequence.",
                    "outcome_source": "agent",
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
        recall_disabled_by_policy = bool(agent_policy and not agent_policy.use_local_recall)
        subject_detection_prompt = self.local_knowledge.build_subject_detection_prompt(user_message)
        await self._emit_status(
            "recalling",
            (
                "Local-LLM recall skipped by LLM agent policy."
                if recall_disabled_by_policy
                else "Detecting real-world references that may need exact specs."
            ),
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
            if recall_disabled_by_policy:
                recall_subjects = []
            else:
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
                else (
                    "Local-LLM recall skipped by policy."
                    if recall_disabled_by_policy
                    else "No external reference specs needed."
                )
            ),
            details=None,
            data={
                "sub_stage": "subject_detection_done",
                "outcome": (
                    ", ".join(subj.subject for subj in recall_subjects)
                    if recall_subjects
                    else (
                        "The LLM agent policy disabled local recall for this turn."
                        if recall_disabled_by_policy
                        else "The request appears fully specified or purely parametric."
                    )
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
        policy_strategy = agent_policy.strategy if agent_policy else "auto"
        # The strategy decision is purely "do we have a prior checkpoint to
        # edit?" — it does NOT consult the user prompt. Track the two probes
        # so the UI's Inputs row shows what the agent actually looked at.
        strategy_probes: list[str] = []
        if agent_policy:
            strategy_probes.append(f"LLM agent strategy: `{policy_strategy}`")
        if policy_strategy == "create_new":
            strategy_probes.append("LLM agent requested a fresh model; existing checkpoints ignored")
            current_model_id = None
        elif base_model_id and policy_strategy in ("auto", "edit_requested"):
            strategy_probes.append(f"base model id from request: `{base_model_id}`")
            current_source = self.storage.get_model_source_text(project_id, current_model_id or "")
            strategy_probes.append(
                f"source for `{base_model_id}`: {'found' if current_source else 'missing'}"
            )
        else:
            strategy_probes.append("base model id from request: none")
        if not current_source and policy_strategy not in ("create_new", "edit_requested"):
            latest_model = latest_model_for_policy
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
        if agent_policy:
            context_used.append("LLM agent turn policy")
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
                "rationale": (
                    "The LLM agent policy chose this source strategy."
                    if agent_policy
                    else "Reuses existing geometry when relevant; otherwise starts fresh."
                ),
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
        use_recipes = agent_policy.use_recipes if agent_policy else True
        use_example_bank = agent_policy.use_example_bank if agent_policy else True
        recipe_cards = retrieve_recipe_cards(user_message) if use_recipes else []
        recipe_context = build_combined_recipe_context(user_message, recipe_cards) if use_recipes else ""
        planning_example_context = (
            build_example_bank_prompt_context(
                user_message,
                max_snippets=2,
                cadquery_only=False,
                max_chars=2200,
            )
            if use_example_bank
            else ""
        )
        code_example_context = (
            build_example_bank_prompt_context(
                user_message,
                max_snippets=3,
                cadquery_only=True,
                max_chars=3600,
            )
            if use_example_bank
            else ""
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
        example_hits = (
            retrieve_example_snippets(user_message, max_snippets=5, cadquery_only=False)
            if use_example_bank
            else []
        )
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
        merged_external_context = _merge_external_context(agent_policy_context, recall_context, research_context)
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

        quality_report = merge_plan_quality_reports(
            validate_plan_against_recipes(plan, recipe_cards, user_message=user_message),
            validate_plan_against_constraints(plan, config.hard_constraints),
        )
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
                "Plan needs revision before code generation.",
                details=(
                    "A rule-based quality gate compared the planner's output "
                    "against the retrieved CAD recipe checklist and hard print-volume constraints. "
                    "The planner LLM is now asked to rewrite the plan with the "
                    "missing features/constraint fixes filled in. The issue list "
                    "below comes from the rule check; the rewritten plan in the "
                    "next step is LLM output."
                ),
                data={
                    "sub_stage": "plan_repair",
                    "rationale": "A weak or impossible plan leads to bad geometry, so the plan is repaired before CadQuery code is generated.",
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
                    research_context=_merge_external_context(agent_policy_context, recall_context, research_context),
                    recipe_context=planning_reference_context,
                    hard_constraints=config.hard_constraints,
                    soft_constraints=config.soft_constraints,
                    on_chunk=_plan_chunk,
                )
                quality_report = merge_plan_quality_reports(
                    validate_plan_against_recipes(plan, recipe_cards, user_message=user_message),
                    validate_plan_against_constraints(plan, config.hard_constraints),
                )
            except LLMBackendUnavailable as e:
                await self._handle_backend_unavailable(e, stage="plan_repair")
                raise
            except Exception as e:
                await self._emit_debug("plan_repair_error", f"Plan repair failed: {e}", {"traceback": traceback.format_exc()})

            if not quality_report.is_sufficient:
                failure_message = (
                    "Plan repair still failed the quality gate, so code generation was stopped. "
                    "The revised plan is missing required dimensions/features and would likely "
                    "produce invalid or placeholder CAD."
                )
                await self._emit_status(
                    "planning",
                    "Plan repair still failed the quality gate.",
                    details=failure_message,
                    data={
                        "sub_stage": "plan_repair_partial",
                        "rationale": "Generating CadQuery from an incomplete plan produces placeholder geometry and wastes repair cycles, so this turn stops before code generation.",
                        "missing_features": list(quality_report.missing_features),
                        "missing_negative_space": list(quality_report.missing_negative_space),
                        "feedback": quality_report.feedback,
                        # The LLM did try to repair; surface its post-repair
                        # summary so the user can see what it returned.
                        "llm_revised_summary": plan.summary,
                    },
                )
                await self._emit_error(failure_message, "plan_quality_failed")
                self._save_failure_chat(project_id, thread_id, current_model_id, failure_message)
                self._schedule_summarization_safely([], turn_succeeded=False)
                return None
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
        # Best-so-far across the repair loop. A repair attempt that scores
        # WORSE than an earlier iteration is a regression — the next pass
        # should rebase on the prior best rather than compound the worse code,
        # and when the budget runs out we should ship the best, not the last.
        # Only updated when a real vision critique was produced (None vs 1.0
        # default would otherwise mask "vision was unavailable" as a perfect
        # score).
        best_code: str = ""
        best_vision_score: Optional[float] = None
        best_model_id: Optional[str] = None
        best_critique: Optional[CritiqueReport] = None
        VISION_REGRESSION_MARGIN = 0.05
        # Per-turn history of failed execution attempts. Each entry captures
        # the failing source line (extracted from the traceback) and the
        # first significant line of the error. Passed into the next repair
        # prompt so the LLM can see when it's repeating itself — the previous
        # behaviour was to call repair_cadquery with only the latest failed
        # code + error, so the model had no idea it had already tried the
        # same micro-edit twice.
        repair_attempt_history: List[Dict] = []
        consecutive_empty = 0
        # Separate per-kind counters. ``iteration`` is the absolute attempt
        # index (used for model IDs / metadata); these track how many of each
        # repair flavour we've actually spent, so syntax stumbles don't eat
        # into the vision budget and vice versa.
        syntax_repairs_used = 0
        vision_repairs_used = 0
        # LLM-agent escalation tracking. The patch-style repair LLM frequently
        # returns truncated code (drops `import cadquery`); the deletion guard
        # then falls back to the prior source, so two consecutive vision
        # repairs can produce byte-identical code while still burning budget.
        # When that happens in `llm_agent` mode we abandon the patch path and
        # do a full code regeneration with the vision feedback embedded — the
        # generation LLM has the full system prompt and produces a complete
        # program from scratch, which the patch LLM cannot.
        prev_vision_repair_code: str = ""
        consecutive_identical_vision_repairs = 0
        # Track the persistent failure mode: if the same primary issue_type
        # recurs across iterations the model is structurally wrong, not just
        # imperfect. The escalation prompt surfaces this signal so the
        # generator can apply a structurally different approach.
        prev_primary_issue: str = ""
        consecutive_same_issue = 0
        # Error-pattern events recorded across this turn. Each repair attempt
        # appends one event so the post-turn summarizer can see the full
        # failure → fix → next-result trajectory.
        turn_error_events: List[_error_patterns.FailureEvent] = []
        # Identifier used as ``turn_id`` in the error log so events from a
        # single turn cluster together when humans audit pitfalls.
        error_turn_id = f"{project_id}:{thread_id}:{turn_index}"
        agent_policy_dump = _agent_policy_log_dict(agent_policy, raw_preview_chars=1200)

        # 3. Iterative Loop
        iteration = 0
        while iteration < self.MAX_REPAIR_ITERATIONS:
            iteration += 1
            try:
                # Generate the model ID for this iteration early
                model_id = self.storage.next_model_id(project_id)
                current_model_id = model_id

                # ── Step A: Generate or Repair code ──────────────────────────
                if iteration == 1:
                    code_generation_context = _merge_external_context(agent_policy_context, recall_context, research_context)
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
                    if not last_code.strip():
                        await self._emit_status(
                            "generating",
                            f"Initial response contained no usable CadQuery source; retrying code-only (`{model_id}`)",
                            details=(
                                "The extractor rejected the model response as prose/markdown rather "
                                "than Python source, so this retry asks for a single fenced CadQuery "
                                "program instead of entering the repair loop with non-code."
                            ),
                            data={
                                "iteration": iteration,
                                "rationale": "A format retry keeps prose leakage out of syntax repair.",
                                "inputs": ["code extraction result: empty"],
                                "model_id": model_id,
                                "model": self.llm.model,
                            },
                        )
                        last_code = await self._generate_code_streaming(
                            user_message,
                            system_prompt,
                            chat_ctx,
                            current_source,
                            base_model_id,
                            selection,
                            research_context=code_generation_context,
                            recipe_context=generation_reference_context,
                            project_id=project_id,
                            plan_text=plan_text,
                            format_retry=True,
                        )
                elif last_critique and last_critique.issues:
                    # Vision-driven repair. Bail out if we've already spent
                    # the per-turn vision budget — better to accept the
                    # current model and let the user iterate from there
                    # than to spin forever trying to chase the verifier.
                    if vision_repairs_used >= self.MAX_VISION_REPAIR_ITERATIONS:
                        first_issue = last_critique.issues[0].description if last_critique.issues else "verifier rejected the model"
                        failure_message = (
                            f"Could not produce a model that passes visual/plan verification after "
                            f"{self.MAX_VISION_REPAIR_ITERATIONS} vision repair attempts. "
                            f"Last issue: {first_issue[:220]}"
                        )
                        await self._emit_debug(
                            "vision_budget_exhausted",
                            f"Vision repair budget spent ({vision_repairs_used}/"
                            f"{self.MAX_VISION_REPAIR_ITERATIONS}); failing turn instead of "
                            f"shipping an invalid model.",
                        )
                        await self._emit_status(
                            "failed",
                            "Vision/plan verification budget exhausted.",
                            details=failure_message,
                            data={
                                "iteration": iteration,
                                "model_id": current_model_id,
                                "failure_type": "vision_quality_failed",
                                "vision_repairs_used": vision_repairs_used,
                                "max_vision_repairs": self.MAX_VISION_REPAIR_ITERATIONS,
                                "last_issue": first_issue,
                            },
                        )
                        await self._emit_error(failure_message, "vision_quality_failed")
                        self._save_failure_chat(project_id, thread_id, current_model_id, failure_message)
                        self._schedule_summarization_safely(turn_error_events, turn_succeeded=False)
                        return None
                    vision_repairs_used += 1
                    recent_turn_errors = _collect_recent_turn_errors(turn_error_events)

                    # Stall detection (LLM-agent only):
                    #   The patch-style repair LLM frequently returns truncated
                    #   code; ``_accept_or_recover_repair`` then falls back to
                    #   the previous source so we don't ship a 2-line stub. The
                    #   side effect: two consecutive vision repairs can produce
                    #   byte-identical `last_code`, and the same vision issue
                    #   recurs forever. When that pattern shows up under the
                    #   LLM-agent strategy, we abandon the patch path and run a
                    #   full code-generation pass with the vision critique
                    #   embedded — the generator has the full system prompt
                    #   (API reference, examples, anti-stub guardrails) so it
                    #   can produce a complete fresh program. The hardcoded
                    #   orchestrator never gets this escalation; this is the
                    #   primary improvement of `llm_agent` over `orchestrator`.
                    code_unchanged = (
                        prev_vision_repair_code != ""
                        and prev_vision_repair_code == last_code
                    )
                    if code_unchanged:
                        consecutive_identical_vision_repairs += 1
                    else:
                        consecutive_identical_vision_repairs = 0

                    primary_issue_signature = ""
                    if last_critique.issues:
                        top = last_critique.issues[0]
                        primary_issue_signature = f"{top.issue_type}::{(top.description or '')[:80]}"
                    if primary_issue_signature and primary_issue_signature == prev_primary_issue:
                        consecutive_same_issue += 1
                    else:
                        consecutive_same_issue = 1 if primary_issue_signature else 0
                    prev_primary_issue = primary_issue_signature

                    # Escalation thresholds:
                    #   - Code unchanged across two vision repairs ⇒ patch path
                    #     is structurally broken for this prompt, regenerate.
                    #   - Same primary issue for 3+ vision iterations ⇒ the
                    #     model isn't converging via patches even if code is
                    #     changing slightly, regenerate.
                    escalate_to_regen = (
                        agent_logic == "llm_agent"
                        and (
                            consecutive_identical_vision_repairs >= 1
                            or consecutive_same_issue >= 3
                        )
                    )

                    if escalate_to_regen:
                        escalation_reason = (
                            "patch-repair output identical to previous iteration"
                            if consecutive_identical_vision_repairs >= 1
                            else f"same primary vision issue recurred {consecutive_same_issue}× in a row"
                        )
                        await self._emit_status(
                            "repairing",
                            f"LLM agent escalating: full code regeneration (vision attempt {vision_repairs_used}/{self.MAX_VISION_REPAIR_ITERATIONS}) · `{model_id}`",
                            details=(
                                "The patch-style repair path is stuck — the model keeps "
                                "returning truncated code that the deletion guard rejects, "
                                "or the same vision issue keeps surviving each patch. "
                                "Escalating to a full code-generation pass with the vision "
                                "critique embedded in the user message. The generator has "
                                "the full system prompt (API reference + examples) and "
                                "produces a complete program from scratch, which the patch "
                                "LLM cannot."
                            ),
                            data={
                                "iteration": iteration,
                                "repair_kind": "regenerate_with_critique",
                                "rationale": (
                                    "LLM-agent supervises the repair loop. When the patch "
                                    "path stalls, switch to a fresh full generation grounded "
                                    "in the plan + vision feedback."
                                ),
                                "outcome": f"Escalating because {escalation_reason}.",
                                "inputs": [
                                    f"vision score {last_critique.overall_printability:.2f}",
                                    f"{len(last_critique.issues)} critique issue(s)",
                                    escalation_reason,
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
                            },
                        )
                        # Use the geometry-repair system prompt for the regen
                        # call: it's smaller than the full generation prompt
                        # (no example bank), so qwen3.x has more headroom to
                        # produce the final program instead of burning tokens
                        # on partial drafts. The plan + recipes + research
                        # already travel in the user message via
                        # `external_context` / `recipe_context`.
                        regen_system_prompt = build_repair_system_prompt(
                            config.hard_constraints,
                            config.soft_constraints,
                            recent_turn_errors=recent_turn_errors,
                            geometry_repair=True,
                        )
                        last_code = await self._llm_agent_regenerate_with_critique(
                            user_message=user_message,
                            current_code=last_code,
                            critique=last_critique,
                            iteration=iteration,
                            escalation_reason=escalation_reason,
                            system_prompt=regen_system_prompt,
                            plan_text=plan_text,
                            external_context=_merge_external_context(
                                agent_policy_context, recall_context, research_context
                            ),
                            recipe_context=generation_reference_context,
                        )
                        prev_vision_repair_code = last_code
                        repair_notes.append(
                            f"LLM-agent escalation (iter {iteration}): "
                            f"regenerated from scratch because {escalation_reason}."
                        )
                        vision_event = _error_patterns.record_failure(
                            failure_type="vision_critique",
                            error_text=(last_critique.repair_prompt or "vision critique below threshold"),
                            fix_kind="regenerate",
                            succeeded=False,
                            iteration=iteration,
                            turn_id=error_turn_id,
                            model=self.llm.model,
                        )
                        if vision_event is not None:
                            turn_error_events.append(vision_event)
                        # Drop into the post-repair common path. Skip the
                        # patch-LLM call below by guarding on this flag.
                        last_critique = None  # consumed
                        # Continue execution below the patch-style block.
                        repair_done_via_escalation = True
                    else:
                        repair_done_via_escalation = False

                    if not repair_done_via_escalation:
                        repair_system_prompt = build_repair_system_prompt(
                            config.hard_constraints, config.soft_constraints,
                            recent_turn_errors=recent_turn_errors,
                        )
                        repair_user_prompt = build_vision_repair_prompt(
                            last_code,
                            intent=user_message,
                            iteration=iteration,
                            issues=[
                                {
                                    "severity": i.severity,
                                    "issue_type": i.issue_type,
                                    "description": i.description,
                                    "location_hint": i.location_hint,
                                }
                                for i in last_critique.issues
                            ],
                            repair_instructions=last_critique.repair_prompt or "",
                            matches_intent=bool(last_critique.matches_intent),
                            overall_score=last_critique.overall_printability,
                            confidence=last_critique.confidence,
                            plan_summary=plan.summary or "",
                            key_features=list(plan.key_features) if plan.key_features else None,
                            plan_components=list(plan.components) if plan.components else None,
                        )
                        await self._emit_status("repairing",
                            f"Repairing for vision feedback (vision attempt {vision_repairs_used}/{self.MAX_VISION_REPAIR_ITERATIONS}) · `{model_id}`",
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
                            "user_prompt_chars": len(repair_user_prompt),
                        })
                        repair_response = await self.llm.repair_cadquery_vision(
                            user_prompt=repair_user_prompt,
                            hard_constraints=config.hard_constraints,
                            soft_constraints=config.soft_constraints,
                            recent_turn_errors=recent_turn_errors,
                        )
                        repair_notes.append(f"Vision critique identified {len(last_critique.issues)} issues (score: {last_critique.overall_printability:.2f})")
                        candidate_code = extract_code_from_response(repair_response)
                        last_code = await self._accept_or_recover_repair(
                            original_code=last_code,
                            candidate_code=candidate_code,
                            iteration=iteration,
                            repair_kind="vision",
                            error_message=repair_user_prompt,
                            failure_type=None,
                            geometry_stats=last_geometry_stats,
                            hard_constraints=config.hard_constraints,
                            soft_constraints=config.soft_constraints,
                            repair_notes=repair_notes,
                        )
                        prev_vision_repair_code = last_code
                        # Vision-driven repairs don't have a prior failed-execution
                        # event to update — they fire after a *successful* render
                        # whose critique flagged issues. Record an event so the
                        # learner sees vision-repair journeys too. (The escalation
                        # branch already records its own event upstream.)
                        vision_event = _error_patterns.record_failure(
                            failure_type="vision_critique",
                            error_text=(last_critique.repair_prompt or "vision critique below threshold"),
                            fix_kind="vision",
                            succeeded=False,
                            iteration=iteration,
                            turn_id=error_turn_id,
                            model=self.llm.model,
                        )
                        if vision_event is not None:
                            turn_error_events.append(vision_event)
                else:
                    # Execution / syntax repair. Same idea as the vision
                    # branch: if syntax bugs already burned through their
                    # budget, give up rather than loop indefinitely.
                    if syntax_repairs_used >= self.MAX_SYNTAX_REPAIR_ITERATIONS:
                        from ..cad.engine import try_patch_standalone_workplane_hole, try_patch_workplane_bounding_box, try_patch_workplane_clone, try_remove_failing_fillet
                        emergency_patch = (
                            try_remove_failing_fillet(last_code, last_error)
                            or try_patch_workplane_bounding_box(last_code, last_error)
                            or try_patch_standalone_workplane_hole(last_code, last_error)
                            or try_patch_workplane_clone(last_code, last_error)
                        )
                        if emergency_patch is not None:
                            await self._emit_status(
                                "repairing",
                                f"Mechanical fillet recovery after syntax budget · `{model_id}`",
                                details=(
                                    "The remaining failure is a known OCCT fillet-construction error. "
                                    "Fillets are cosmetic/print-quality features, so the agent removes only "
                                    "the failing fillet line and keeps the geometry otherwise unchanged."
                                ),
                                data={
                                    "iteration": iteration,
                                    "repair_kind": "mechanical",
                                    "rationale": "A deterministic fillet-line removal is safer than aborting after the LLM repair budget is exhausted.",
                                    "outcome": "Applied one deterministic execution fix.",
                                    "model_id": model_id,
                                },
                            )
                            last_code = emergency_patch
                            repair_notes.append(
                                "Mechanical fix (no LLM call): Applied one deterministic execution fix after repair budget was exhausted."
                            )
                            continue
                        await self._emit_debug(
                            "syntax_budget_exhausted",
                            f"Syntax repair budget spent ({syntax_repairs_used}/"
                            f"{self.MAX_SYNTAX_REPAIR_ITERATIONS}); aborting turn.",
                        )
                        await self._emit_error(
                            f"Could not produce executable CadQuery after "
                            f"{self.MAX_SYNTAX_REPAIR_ITERATIONS} syntax/execution "
                            f"repair attempts. Last error: "
                            f"{(last_error.splitlines()[0][:200] if last_error else 'unknown')}",
                            "execution_error",
                        )
                        self._save_failure_chat(project_id, thread_id, current_model_id)
                        self._schedule_summarization_safely(turn_error_events, turn_succeeded=False)
                        return None
                    syntax_repairs_used += 1
                    # Execution-error repair
                    failure_label = (last_failure_type or "execution_error").replace("_", " ")
                    err_first = last_error.splitlines()[0][:120] if last_error else "previous attempt failed"

                    # Mechanical pre-repair: try cheap deterministic fixes
                    # before burning a 90-second LLM repair cycle.
                    #   1. Missing `result =` → AST-patch alias to the last var.
                    #   2. Syntax error from reasoning prose leaking into the
                    #      code block (qwen3.x failure mode) → strip the prose.
                    #   3. Constraint violation from oversize AABB → append a
                    #      uniform `.scale(factor)` step that brings the model
                    #      under the print-volume cap.
                    # If none apply, fall through to the LLM.
                    mechanical_patch: Optional[str] = None
                    mechanical_note: str = ""
                    if last_failure_type == "syntax_error" and last_error and "must assign" in last_error.lower():
                        from ..cad.engine import looks_like_parameter_only_stub
                        if looks_like_parameter_only_stub(last_code):
                            await self._emit_status(
                                "repairing",
                                f"Regenerating full CadQuery source (syntax attempt {syntax_repairs_used}/{self.MAX_SYNTAX_REPAIR_ITERATIONS}) · `{model_id}`",
                                details=(
                                    "The previous response was only parameter assignments, "
                                    "not a CadQuery model. A minimal repair would preserve "
                                    "the stub, so this retry asks for a complete program."
                                ),
                                data={
                                    "iteration": iteration,
                                    "repair_kind": "format_regeneration",
                                    "rationale": "Parameter-only stubs need full code generation, not a local missing-result patch.",
                                    "inputs": ["parameter-only source stub", last_failure_type],
                                    "model_id": model_id,
                                },
                            )
                            last_code = await self._generate_code_streaming(
                                user_message,
                                system_prompt,
                                chat_ctx,
                                current_source,
                                current_model_id,
                                selection,
                                research_context=merged_external_context,
                                recipe_context=generation_reference_context,
                                project_id=project_id,
                                plan_text=plan_to_prompt_text(plan),
                                format_retry=True,
                            )
                            repair_notes.append(
                                "Regenerated full code after parameter-only stub."
                            )
                            continue
                        from ..cad.engine import try_patch_missing_result
                        mechanical_patch = try_patch_missing_result(last_code)
                        if mechanical_patch is not None:
                            mechanical_note = (
                                "Appended `result = <last_shape_var>` to the source."
                            )
                    elif last_failure_type == "syntax_error" and last_error:
                        from ..cad.engine import strip_reasoning_leakage
                        stripped = strip_reasoning_leakage(last_code)
                        if stripped is not None:
                            mechanical_patch = stripped
                            mechanical_note = (
                                "Stripped LLM reasoning prose that leaked into "
                                "the code block (no LLM call)."
                            )
                    elif last_failure_type == "constraint_violation" and last_geometry_stats:
                        from ..cad.engine import try_auto_scale_for_fit
                        scaled = try_auto_scale_for_fit(
                            last_code,
                            last_geometry_stats,
                            max_x_mm=config.hard_constraints.max_x_mm,
                            max_y_mm=config.hard_constraints.max_y_mm,
                            max_z_mm=config.hard_constraints.max_z_mm,
                        )
                        if scaled is not None:
                            mechanical_patch = scaled
                            mechanical_note = (
                                "Appended a uniform `result.val().scale(...)` step "
                                "to bring the geometry under the print-volume cap "
                                "(deterministic — no LLM repair call needed)."
                            )

                    elif last_failure_type == "execution_error" and last_error:
                        from ..cad.engine import try_patch_standalone_workplane_hole, try_patch_workplane_bounding_box, try_patch_workplane_clone, try_remove_failing_fillet
                        exec_patch = (
                            try_remove_failing_fillet(last_code, last_error)
                            or try_patch_workplane_bounding_box(last_code, last_error)
                            or try_patch_standalone_workplane_hole(last_code, last_error)
                            or try_patch_workplane_clone(last_code, last_error)
                        )
                        if exec_patch is not None:
                            mechanical_patch = exec_patch
                            mechanical_note = (
                                "Applied a deterministic execution fix for a known "
                                "CadQuery/OCC failure mode (no LLM call)."
                            )

                    if mechanical_patch is not None:
                        await self._emit_status("repairing",
                            f"Mechanical fix (syntax attempt {syntax_repairs_used}/{self.MAX_SYNTAX_REPAIR_ITERATIONS}) · `{model_id}`",
                            details=None,
                            data={
                                "iteration": iteration,
                                "repair_kind": "mechanical",
                                "rationale": "Deterministic fix avoids a ~90s LLM repair call when the bug is a known textual artifact (missing `result` alias, LLM reasoning prose leaking into the code block, or oversize geometry that just needs a uniform scale-down).",
                                "outcome": mechanical_note,
                                "inputs": ["AST analysis"],
                                "model_id": model_id,
                            })
                        await self._emit_debug("mechanical_repair", mechanical_note, {
                            "original_tail": last_code[-200:],
                            "patched_tail": mechanical_patch[-200:],
                            "original_len": len(last_code),
                            "patched_len": len(mechanical_patch),
                        })
                        last_code = mechanical_patch
                        repair_notes.append(
                            f"Mechanical fix (no LLM call): {mechanical_note}"
                        )
                        # Record the fix on the most-recent pending failure so
                        # the post-turn summarizer can see which strategy was
                        # used. We won't know if it succeeded until step B
                        # runs at the top of the next iteration.
                        if turn_error_events:
                            turn_error_events[-1].fix_kind = "mechanical"
                            _error_patterns.update_failure_outcome(
                                turn_id=turn_error_events[-1].turn_id,
                                iteration=turn_error_events[-1].iteration,
                                fix_kind="mechanical",
                            )
                    else:
                        recent_turn_errors = _collect_recent_turn_errors(turn_error_events)
                        repair_system_prompt = build_repair_system_prompt(
                            config.hard_constraints, config.soft_constraints,
                            recent_turn_errors=recent_turn_errors,
                        )
                        # Pass the prior attempts EXCEPT the most recent one
                        # (which corresponds to the failure we're repairing
                        # right now — that's already in ``last_error`` /
                        # ``last_code``). When the same error signature
                        # recurs across multiple entries, the prompt builder
                        # switches to a "structural fix required" framing.
                        prior_for_prompt = repair_attempt_history[:-1] if repair_attempt_history else []
                        repair_user_prompt = build_repair_prompt(
                            last_code,
                            last_error,
                            iteration,
                            failure_type=last_failure_type,
                            geometry_stats=last_geometry_stats,
                            prior_attempts=prior_for_prompt,
                        )
                        await self._emit_status("repairing",
                            f"Repairing {failure_label} (syntax attempt {syntax_repairs_used}/{self.MAX_SYNTAX_REPAIR_ITERATIONS}) · `{model_id}`",
                            details=None,
                            data={
                                "iteration": iteration,
                                "repair_kind": "execution",
                                "rationale": "The previous generated source did not produce valid geometry, so the next LLM call is constrained by the failure.",
                                "outcome": f"Trying to fix: {err_first}",
                                "inputs": [last_failure_type or "execution_error", err_first],
                                "error_excerpt": (last_error[:1500] if last_error else None),
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
                            prior_attempts=prior_for_prompt,
                            recent_turn_errors=recent_turn_errors,
                        )
                        error_summary = last_error.splitlines()[0][:60] if last_error else "previous attempt failed"
                        repair_notes.append(f"Fixed {failure_label}: {error_summary}...")
                        candidate_code = extract_code_from_response(repair_response)
                        last_code = await self._accept_or_recover_repair(
                            original_code=last_code,
                            candidate_code=candidate_code,
                            iteration=iteration,
                            repair_kind="execution",
                            error_message=last_error,
                            failure_type=last_failure_type,
                            geometry_stats=last_geometry_stats,
                            hard_constraints=config.hard_constraints,
                            soft_constraints=config.soft_constraints,
                            repair_notes=repair_notes,
                        )
                        # Tag the pending failure event with the actual fix
                        # strategy (LLM repair). Outcome (succeeded/failed)
                        # will be set when step B runs next iteration.
                        if turn_error_events:
                            turn_error_events[-1].fix_kind = "llm"
                            _error_patterns.update_failure_outcome(
                                turn_id=turn_error_events[-1].turn_id,
                                iteration=turn_error_events[-1].iteration,
                                fix_kind="llm",
                            )

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
                        self._schedule_summarization_safely(turn_error_events, turn_succeeded=False)
                        return None
                    continue
                consecutive_empty = 0

                # ── Step B0: Static lint (pre-execution AST checks) ───────────
                # Fast, deterministic check for source-level bugs the AST can
                # prove are wrong before we pay the cost of running the geometry
                # engine, the renderer, and the vision critic. The canonical
                # case is `.rotate(p1, p2, angle)` misuse where (p2 - p1) is not
                # axis-aligned — a part that "looks" right in code but spins
                # around an oblique axis at execution time. When the plan locks
                # a rotation, the lint auto-corrects by snapping to the plan's
                # axis; otherwise it raises a `static_lint` failure that feeds
                # the existing repair loop.
                try:
                    lint_report = lint_cadquery_source(last_code, plan=plan)
                except Exception as _lint_exc:
                    # Linting must never crash the pipeline. Fall back to the
                    # unlinted source.
                    await self._emit_debug("static_lint_error", str(_lint_exc))
                    lint_report = None

                if lint_report is not None:
                    if lint_report.autofix_summary:
                        await self._emit_status(
                            "validating",
                            f"Static lint auto-corrected {len(lint_report.autofix_summary)} "
                            f"issue(s) before execution.",
                            details=(
                                "Detected source-level bugs the AST can prove are wrong "
                                "(e.g. an oblique-axis `.rotate(...)` that disagrees with the plan) "
                                "and rewrote them to the canonical form. The corrected source "
                                "is what runs next."
                            ),
                            data={
                                "iteration": iteration,
                                "rationale": "Deterministic pre-execution lint catches LLM rotation/argument bugs.",
                                "outcome": "Auto-corrected and continuing to execution.",
                                "fixes": lint_report.autofix_summary,
                            },
                        )
                        last_code = lint_report.rewritten_source

                    if lint_report.has_blocking:
                        # The lint found something it cannot safely auto-fix.
                        # Route it through the same repair branch as an
                        # execution error — saves the full execute/render/vision
                        # cycle on a bug we can already describe.
                        blocking = "\n".join(lint_report.blocking_messages)
                        last_error = (
                            "Static lint detected source-level bugs before execution:\n"
                            f"{blocking}"
                        )
                        last_failure_type = "static_lint"
                        last_geometry_stats = {}
                        last_critique = None

                        repair_attempt_history.append({
                            "iteration": iteration,
                            "error_first_line": _error_signature(last_error),
                            "failing_source_line": _extract_failing_source_line(last_error, last_code),
                        })
                        if len(repair_attempt_history) > 5:
                            del repair_attempt_history[:-5]

                        event = _error_patterns.record_failure(
                            failure_type=last_failure_type,
                            error_text=last_error,
                            fix_kind="pending",
                            succeeded=False,
                            iteration=iteration,
                            turn_id=error_turn_id,
                            model=self.llm.model,
                        )
                        if event is not None:
                            turn_error_events.append(event)

                        metadata = ModelMetadata(
                            model_id=model_id,
                            parent_model_id=base_model_id,
                            prompt=user_message,
                            cad_source=last_code,
                            failure_type=FailureType.STATIC_LINT,
                            failure_message=last_error,
                            iteration=iteration,
                            citations=citations,
                            is_final=False,
                            thread_id=thread_id,
                            turn_index=turn_index,
                            agent_logic=agent_logic,
                            agent_policy=agent_policy_dump,
                        )
                        self.storage.save_model_metadata(project_id, metadata)
                        current_model_id = model_id

                        await self._emit_status(
                            "failed",
                            f"Static lint failed: {lint_report.blocking_messages[0][:140]}",
                            details=None,
                            data={
                                "iteration": iteration,
                                "model_id": model_id,
                                "failure_type": last_failure_type,
                                "error_excerpt": last_error[:600],
                                "outcome": "Will retry with a code-repair pass (no execution attempted).",
                            },
                        )
                        continue

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
                _exec_coro = asyncio.get_event_loop().run_in_executor(
                    None,
                    process_cadquery_code,
                    last_code,
                    model_dir,
                    "part",
                    config.hard_constraints,
                    project_id,
                    self.storage,
                )
                _cadquery_timeout_s: int = int(
                    os.environ.get("CADQUERY_EXEC_TIMEOUT", "120")
                )
                try:
                    exec_result = await asyncio.wait_for(
                        _exec_coro, timeout=_cadquery_timeout_s
                    )
                except asyncio.TimeoutError:
                    exec_result = {
                        "success": False,
                        "message": (
                            f"CadQuery execution timed out after "
                            f"{_cadquery_timeout_s}s — the OCCT kernel did not "
                            "return. Likely cause: .edges().fillet() on a complex "
                            "multi-body union/cut assembly. Simplify or move fillets "
                            "to individual bodies before the union."
                        ),
                        "failure_type": "timeout",
                        "geometry_stats": {},
                        "violations": [],
                        "warnings": [],
                        "file_paths": {},
                    }
                t_exec_elapsed = time.time() - t_exec_start

                await self._emit_debug("cadquery_result",
                    f"CadQuery execution {'succeeded' if exec_result['success'] else 'failed'} ({t_exec_elapsed:.2f}s)", {
                        "success": exec_result["success"],
                        "message": exec_result["message"],
                        "geometry_stats": exec_result.get("geometry_stats", {}),
                        "failure_type": exec_result.get("failure_type"),
                    })

                # If a prior iteration recorded a failure and we now see a
                # successful execution, that fix worked. Update the most-recent
                # event so the summarizer sees a positive outcome.
                if exec_result["success"] and turn_error_events:
                    last_event = turn_error_events[-1]
                    if not last_event.succeeded and last_event.fix_kind not in ("", "pending"):
                        last_event.succeeded = True
                        _error_patterns.update_failure_outcome(
                            turn_id=last_event.turn_id,
                            iteration=last_event.iteration,
                            succeeded=True,
                        )

                if not exec_result["success"]:
                    last_error = exec_result["message"]
                    last_critique = None
                    last_failure_type = exec_result.get("failure_type") or "execution_error"
                    last_geometry_stats = exec_result.get("geometry_stats", {})

                    # Capture this failed attempt in the per-turn history so
                    # the next repair prompt can show prior attempts. Keeping
                    # the last few entries is enough; more would just bloat
                    # the prompt.
                    repair_attempt_history.append({
                        "iteration": iteration,
                        "error_first_line": _error_signature(last_error),
                        "failing_source_line": _extract_failing_source_line(last_error, last_code),
                    })
                    if len(repair_attempt_history) > 5:
                        del repair_attempt_history[:-5]

                    # Record the failure for the pattern-learning log. ``fix_kind``
                    # and ``succeeded`` will be updated on the next iteration when
                    # we know which repair strategy was applied and whether it
                    # actually worked.
                    event = _error_patterns.record_failure(
                        failure_type=last_failure_type,
                        error_text=last_error,
                        fix_kind="pending",
                        succeeded=False,
                        iteration=iteration,
                        turn_id=error_turn_id,
                        model=self.llm.model,
                    )
                    if event is not None:
                        turn_error_events.append(event)

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
                        agent_logic=agent_logic,
                        agent_policy=agent_policy_dump,
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
                    agent_logic=agent_logic,
                    agent_policy=agent_policy_dump,
                )
                self.storage.save_model_metadata(project_id, preliminary_metadata)

                glb_url = f"/api/projects/{project_id}/models/{model_id}/glb"
                if self.on_model_ready:
                    await self.on_model_ready(model_id, glb_url)

                # Render multi-angle PNGs
                shape = exec_result.get("_shape")
                render_paths = {}
                if shape is not None:
                    render_result = await self._run_render(shape, model_dir, iteration)
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
                        iteration=iteration,
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
                    agent_logic=agent_logic,
                    agent_policy=agent_policy_dump,
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
                only_vision_parse_failed = bool(
                    critique
                    and critique.issues
                    and all(i.issue_type == "vision_parse_failed" for i in critique.issues)
                )
                if only_vision_parse_failed:
                    failure_message = (
                        "Vision verifier did not return parseable JSON, so the model cannot be "
                        "certified as matching the user's intent. Geometry execution and "
                        "plan-conformance may have passed, but visual acceptance failed."
                    )
                    await self._emit_error(failure_message, "vision_parse_failed")
                    self._save_failure_chat(project_id, thread_id, model_id, failure_message)
                    self._schedule_summarization_safely(turn_error_events, turn_succeeded=False)
                    return None

                # Track best-so-far across the repair loop. We only count
                # iterations where a real vision critique was produced — the
                # `vision_score = 1.0 if no critique` fallback above would
                # otherwise let a vision-unavailable iteration masquerade as
                # the best.
                score_regressed = False
                if critique is not None:
                    if best_vision_score is None or vision_score > best_vision_score:
                        best_code = last_code
                        best_vision_score = vision_score
                        best_model_id = model_id
                        best_critique = critique
                    elif vision_score < best_vision_score - VISION_REGRESSION_MARGIN:
                        score_regressed = True
                        await self._emit_status(
                            "validating",
                            f"Vision score regressed to {vision_score:.2f} (best {best_vision_score:.2f} "
                            f"at `{best_model_id}`). Next repair will rebase on the prior best.",
                            details=(
                                "A repair attempt scored measurably worse than an earlier "
                                "iteration. Continuing forward from worse code compounds the "
                                "regression, so the next repair pass starts from the best "
                                "baseline instead."
                            ),
                            data={
                                "iteration": iteration,
                                "rationale": "Avoids letting a worse iteration anchor the rest of the repair loop.",
                                "best_model_id": best_model_id,
                                "best_score": best_vision_score,
                                "current_score": vision_score,
                                "regression_margin": VISION_REGRESSION_MARGIN,
                            },
                        )
                        repair_notes.append(
                            f"Iteration {iteration} regressed (score {vision_score:.2f} < "
                            f"best {best_vision_score:.2f} at `{best_model_id}`); rebased "
                            f"next repair on the prior best."
                        )

                if needs_repair and vision_repairs_used < self.MAX_VISION_REPAIR_ITERATIONS:
                    # On regression: rebase the next repair on the best
                    # baseline, attacking its remaining issues — instead of
                    # building on the worse code we just produced.
                    if score_regressed and best_code and best_critique is not None:
                        last_code = best_code
                        last_critique = best_critique
                    else:
                        last_critique = critique
                    await self._emit_debug("vision_repair_trigger",
                        f"Vision score {vision_score:.2f} below threshold — triggering "
                        f"repair ({vision_repairs_used}/{self.MAX_VISION_REPAIR_ITERATIONS} "
                        f"vision attempts used)")
                    continue
                if needs_repair:
                    # Vision budget exhausted. Prefer the best-scoring earlier
                    # iteration when it's measurably better than the current
                    # one (or when the current iteration has no real score,
                    # which happens when vision was unavailable at the tail).
                    current_score_known = critique is not None
                    best_is_acceptable = (
                        best_critique is not None
                        and best_vision_score is not None
                        and best_vision_score >= self.VISION_SCORE_THRESHOLD
                        and best_critique.matches_intent is not False
                        and not any(i.severity == "error" for i in best_critique.issues)
                    )
                    best_is_better = (
                        best_model_id is not None
                        and best_model_id != model_id
                        and best_vision_score is not None
                        and best_is_acceptable
                        and (
                            (not current_score_known)
                            or vision_score < best_vision_score - VISION_REGRESSION_MARGIN
                        )
                    )
                    if best_is_better:
                        await self._emit_debug(
                            "shipping_best_baseline",
                            f"Budget exhausted; shipping earlier `{best_model_id}` "
                            f"(score {best_vision_score:.2f}) instead of `{model_id}` "
                            f"(score {vision_score:.2f}).",
                        )
                        repair_notes.append(
                            f"Vision budget exhausted; shipped earlier `{best_model_id}` "
                            f"(score {best_vision_score:.2f}) instead of the latest attempt "
                            f"(score {vision_score:.2f}) because it scored higher."
                        )
                        best_meta = self.storage.get_model_metadata(project_id, best_model_id)
                        if best_meta is not None:
                            best_meta.is_final = True
                            self.storage.save_model_metadata(project_id, best_meta)
                            response_text = self._build_final_response(
                                best_model_id, iteration, exec_result, best_critique,
                                best_vision_score, repair_notes,
                            )
                            self.storage.update_last_chat_thread_message(
                                project_id, thread_id,
                                ChatMessage(
                                    role="assistant",
                                    content=response_text,
                                    model_id=best_model_id,
                                    steps=self.current_steps,
                                    agent_logic=agent_logic,
                                ),
                            )
                            self._schedule_summarization_safely(turn_error_events, turn_succeeded=True)
                            return best_model_id
                    first_issue = critique.issues[0].description if critique and critique.issues else "verifier rejected the model"
                    failure_message = (
                        f"Could not produce a model that passes visual/plan verification after "
                        f"{self.MAX_VISION_REPAIR_ITERATIONS} vision repair attempts. "
                        f"Last issue: {first_issue[:220]}"
                    )
                    await self._emit_debug(
                        "vision_budget_exhausted",
                        f"Vision score {vision_score:.2f} still below threshold but "
                        f"used {vision_repairs_used}/{self.MAX_VISION_REPAIR_ITERATIONS} "
                        f"vision attempts; failing turn instead of shipping invalid geometry.",
                    )
                    await self._emit_status(
                        "failed",
                        "Vision/plan verification budget exhausted.",
                        details=failure_message,
                        data={
                            "iteration": iteration,
                            "model_id": model_id,
                            "failure_type": "vision_quality_failed",
                            "vision_score": vision_score,
                            "vision_repairs_used": vision_repairs_used,
                            "max_vision_repairs": self.MAX_VISION_REPAIR_ITERATIONS,
                            "last_issue": first_issue,
                        },
                    )
                    await self._emit_error(failure_message, "vision_quality_failed")
                    self._save_failure_chat(project_id, thread_id, model_id, failure_message)
                    self._schedule_summarization_safely(turn_error_events, turn_succeeded=False)
                    return None

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
                        steps=self.current_steps,
                        agent_logic=agent_logic,
                    ),
                )

                # Autonomous error-pattern learning: if this successful turn
                # involved at least one repair, fire-and-forget an LLM
                # summarization that distills new pitfall cards from the
                # journey. Does NOT block the response — runs in background.
                self._schedule_summarization_safely(turn_error_events, turn_succeeded=True)

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
        self._schedule_summarization_safely(turn_error_events, turn_succeeded=False)
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
        format_retry: bool = False,
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
        if format_retry:
            effective_user_message += (
                "\n\n## Format Retry (highest priority)\n"
                "Your previous response did not contain usable Python source "
                "or only listed parameter assignments without building geometry. "
                "Return exactly one ```python fenced block and nothing else. "
                "Do not write bullets, diagnosis, planning notes, or markdown prose. "
                "The first line inside the block must be `import cadquery as cq`, "
                "the program must contain real `cq.Workplane(...)` or `cq.Assembly(...)` "
                "geometry construction, and it must assign the final CadQuery shape "
                "or assembly to `result`."
            )

        await self._emit_debug("llm_request", "Sending request to LLM", {
            "model": self.llm.model,
            "user_message": user_message,
            "base_model_id": current_model_id,
            "format_retry": format_retry,
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
        #
        # When the model emits multiple partial drafts inside reasoning before
        # the final program (e.g. "Step 1: ... ```python ... ``` Step 2: ...
        # ```python ... ``` Final: ```python complete program ```"), the
        # first-block extractor picks one of the early sketches and returns
        # an incomplete program. ``_extract_best_cadquery_block`` scores all
        # candidate blocks and prefers the one that looks like a finished
        # CadQuery program (has `import cadquery` + `result =`).
        extracted = extract_code_from_response(full_response).strip()

        # If content extraction yielded only a fragment (no `import cadquery`
        # or no `result =`), but the reasoning buffer holds a more complete
        # program, prefer the reasoning's best block. Without this, a 115-char
        # fragment like the one observed in 20260519-062300-74b35034 / run1+run2
        # model-001 propagates into the syntax-repair loop and burns multiple
        # iterations on something that the reasoning channel already had a
        # complete answer for.
        looks_incomplete = bool(extracted) and (
            "import cadquery" not in extracted
            or not re.search(r"^\s*result\s*=", extracted, re.MULTILINE)
        )
        if looks_incomplete and reasoning_buffer:
            best_from_reasoning = _extract_best_cadquery_block(reasoning_buffer)
            if best_from_reasoning and len(best_from_reasoning) > len(extracted):
                await self._emit_debug(
                    "code_recovered_from_reasoning",
                    "Content extract was a fragment; replaced with best CadQuery block from reasoning.",
                    {
                        "content_extract_chars": len(extracted),
                        "reasoning_extract_chars": len(best_from_reasoning),
                        "candidate_block_count": len(_PYTHON_BLOCK_RE.findall(reasoning_buffer)),
                        "extractor": "smart_best_block_replacement",
                    },
                )
                full_response = "```python\n" + best_from_reasoning + "\n```"
                extracted = best_from_reasoning

        if not extracted and reasoning_buffer:
            # Try the smart extractor first — it handles multi-draft traces.
            best_from_reasoning = _extract_best_cadquery_block(reasoning_buffer)
            if best_from_reasoning:
                await self._emit_debug(
                    "code_recovered_from_reasoning",
                    "Picked best CadQuery block from reasoning channel (multi-draft trace).",
                    {
                        "reasoning_length": len(reasoning_buffer),
                        "code_length": len(best_from_reasoning),
                        "candidate_block_count": len(_PYTHON_BLOCK_RE.findall(reasoning_buffer)),
                        "extractor": "smart_best_block",
                    },
                )
                full_response = "```python\n" + best_from_reasoning + "\n```"
            else:
                extracted_from_reasoning = extract_code_from_response(reasoning_buffer).strip()
                if extracted_from_reasoning:
                    await self._emit_debug(
                        "code_recovered_from_reasoning",
                        "Code block was in the reasoning channel; recovered via first-block extractor.",
                        {
                            "reasoning_length": len(reasoning_buffer),
                            "code_length": len(extracted_from_reasoning),
                            "extractor": "first_block",
                        },
                    )
                    # Synthesize the full_response so the rest of the pipeline
                    # (parse, save, etc.) sees the code as if it had arrived
                    # normally.
                    full_response = "```python\n" + extracted_from_reasoning + "\n```"

        elapsed = time.time() - t_start
        await self._emit_debug("llm_response", f"LLM response complete ({elapsed:.1f}s)")

        return extract_code_from_response(full_response)

    async def _llm_agent_regenerate_with_critique(
        self,
        *,
        user_message: str,
        current_code: str,
        critique,
        iteration: int,
        escalation_reason: str,
        system_prompt: str,
        plan_text: str,
        external_context: str,
        recipe_context: str,
    ) -> str:
        """Full code regeneration grounded in the previous attempt's vision
        critique. Used by the LLM-agent escalation when patch-style repair has
        stalled.

        This bypasses the streaming generator (which is qwen3.x-friendly but
        burns the token budget on partial reasoning drafts) and instead uses
        a single non-streaming `generate()` call with thinking disabled and a
        higher max_tokens budget. The response goes through a scoring extractor
        that prefers blocks looking like a finished CadQuery program (has
        `import cadquery` + `result =` + union/cut), not the first partial
        draft a thinking model emitted while warming up.
        """
        regen_user_message = _build_regen_with_critique_user_message(
            user_message=user_message,
            current_code=current_code,
            critique=critique,
            iteration=iteration,
            escalation_reason=escalation_reason,
        )
        # Wrap with the same plan + recipe + research context the generation
        # path normally injects, but skip the `## Requested Change`
        # boilerplate — the regen body already states the task clearly.
        prefix_parts = [p for p in (external_context, recipe_context, plan_text) if p]
        prefix = "\n\n".join(prefix_parts)
        full_prompt = (
            f"{prefix}\n\n{regen_user_message}" if prefix else regen_user_message
        )

        await self._emit_debug("llm_request", "Regen-with-critique request", {
            "model": self.llm.model,
            "iteration": iteration,
            "prompt_chars": len(full_prompt),
            "current_code_chars": len(current_code),
        })

        t_start = time.time()
        try:
            response = await self.llm.generate(
                full_prompt,
                system_prompt,
                allow_thinking=False,
                max_tokens=10240,
            )
        except Exception as exc:
            await self._emit_debug(
                "regen_with_critique_failed",
                f"LLM regen call failed: {exc}",
                {"traceback": traceback.format_exc()},
            )
            return current_code  # Keep the previous code so the next iter still has something
        elapsed = time.time() - t_start

        # Prefer the best-scoring complete program from the response. Falls
        # back to the standard first-block extractor when only one candidate
        # exists (or when none scored above the cadquery threshold).
        best = _extract_best_cadquery_block(response)
        fallback_used = ""
        if not best:
            best = extract_code_from_response(response).strip()
            fallback_used = "extract_code_from_response"
        if not best:
            # Both extractors failed. The model produced no usable program.
            # Returning the previous code keeps the loop alive — the next
            # iteration's patch attempt has something to work from — instead
            # of poisoning Step B with empty source.
            best = current_code
            fallback_used = "kept_previous_code"
        await self._emit_debug(
            "llm_response",
            f"Regen-with-critique response ({elapsed:.1f}s)",
            {
                "response_chars": len(response or ""),
                "extracted_chars": len(best),
                "candidate_block_count": len(_PYTHON_BLOCK_RE.findall(response or "")),
                "fallback_used": fallback_used or "smart_extractor",
            },
        )
        return best

    async def _run_render(self, shape, model_dir, iteration: int) -> Dict[str, str]:
        await self._emit_status("rendering", "Rendering ISO / Top / Front / Side views…",
                              details=None,
                              data={
                                  "iteration": iteration,
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
        iteration: int,
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
                                  "iteration": iteration,
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
                # Vision parse failure used to be a quiet info-level message
                # ("returned no usable feedback — continuing.") that gave no
                # hint the verifier had effectively been skipped for this
                # iteration. Surface it as a visible warning with the raw
                # response preview so the user knows the deterministic
                # plan-conformance check is the only remaining gate. We still
                # return None — that preserves the existing routing where
                # plan-conformance decides repair-vs-ship instead of the vision
                # repair branch burning its budget on noise it can't act on.
                await self._emit_status(
                    "critiquing",
                    "⚠️ Vision verifier returned no parseable JSON — plan-conformance "
                    "so this iteration cannot be vision-certified.",
                    details=(
                        "The vision model produced prose or malformed JSON that the "
                        "parser couldn't recover. The model will not be accepted as "
                        "final from geometry checks alone. To diagnose: see "
                        "scratch/vision_raw_response.txt or switch VISION_MODEL to one "
                        "with better JSON discipline."
                    ),
                    data={
                        "iteration": iteration,
                        "rationale": "A silent vision skip would let visual bugs ship if plan-conformance also passes; treating it as a verifier failure keeps final artifacts honest.",
                        "outcome": critique_result.message or "Verifier output could not be parsed.",
                        "skipped": ["vision-based acceptance"],
                        "raw_response_preview": (critique_result.raw_response or "")[:600],
                    },
                )
                return CritiqueReport(
                    issues=[
                        GeometryIssue(
                            issue_type="vision_parse_failed",
                            severity="error",
                            description=critique_result.message or "Vision verifier output could not be parsed.",
                            location_hint="vision verifier response",
                        )
                    ],
                    overall_printability=0.0,
                    suggested_repairs=[
                        "Vision verifier did not return parseable JSON; rerun with a JSON-disciplined vision model or retry the verifier."
                    ],
                    confidence=0.0,
                    matches_intent=False,
                    repair_prompt=(
                        "Vision verifier did not return parseable JSON, so this model cannot be certified "
                        "as matching the user's intent from vision evidence."
                    ),
                )

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
                    "iteration": iteration,
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
                    # Render URLs travel with the critiquing step so the UI
                    # timeline can show the same thumbnails the verifier
                    # actually scored, alongside the issue list — no need
                    # to keep a separate live-critique store in sync.
                    "render_urls": render_urls,
                },
            )

            return report
        except Exception as e:
            await self._emit_debug("vision_error", str(e))
            return None

    async def _accept_or_recover_repair(
        self,
        *,
        original_code: str,
        candidate_code: str,
        iteration: int,
        repair_kind: str,
        error_message: str,
        failure_type: Optional[str],
        geometry_stats: Optional[Dict],
        hard_constraints,
        soft_constraints,
        repair_notes: List[str],
    ) -> str:
        """Anti-deletion guard for LLM repair output.

        Repair LLMs frequently "fix" errors by deleting most of the program —
        turning a 70-line iPhone holder into a 2-line stub. This wrapper
        detects that, retries once with an explicit "you deleted too much"
        warning, and falls back to the prior code if the model can't recover.

        Returning `original_code` rather than the truncated repair keeps the
        next iteration's repair pass anchored to the real design, so a single
        bad LLM response doesn't poison the rest of the loop.
        """
        reason = detect_repair_deletion(original_code, candidate_code)
        if reason is None:
            return candidate_code

        await self._emit_debug(
            "repair_deletion_detected",
            f"Iteration {iteration} {repair_kind} repair rejected: {reason}",
            {
                "iteration": iteration,
                "repair_kind": repair_kind,
                "reason": reason,
                "original_lines": original_code.count("\n") + 1,
                "candidate_lines": candidate_code.count("\n") + 1 if candidate_code else 0,
                "candidate_preview": (candidate_code or "")[:400],
            },
        )
        await self._emit_status(
            "repairing",
            "Repair output deleted too much code — retrying with a stronger preserve-geometry instruction.",
            details=(
                "The repair LLM responded with a much shorter program than the "
                "input, which usually means it removed components instead of "
                "patching the bug. Re-asking with an explicit \"do not delete\" "
                "warning before falling back to the prior code."
            ),
            data={
                "iteration": iteration,
                "repair_kind": f"{repair_kind}_retry",
                "rationale": reason,
                "outcome": "Retrying repair with preservation warning.",
            },
        )

        try:
            if repair_kind == "vision":
                # Vision retries: the `error_message` argument is already the
                # full vision-repair user prompt. Re-send it (with a stronger
                # "don't delete" preface) via the vision path so it isn't
                # truncated by ``build_repair_prompt``'s 2000-char slice.
                retry_user_prompt = (
                    "⚠️ The previous repair attempt deleted most of the program. "
                    "That was wrong. Restore the original components and apply "
                    "only the minimum geometry changes needed to address the "
                    "vision issues below.\n\n"
                    + error_message
                )
                retry_response = await self.llm.repair_cadquery_vision(
                    user_prompt=retry_user_prompt,
                    hard_constraints=hard_constraints,
                    soft_constraints=soft_constraints,
                )
            else:
                retry_response = await self.llm.repair_cadquery(
                    original_code=original_code,
                    error_message=error_message,
                    iteration=iteration,
                    hard_constraints=hard_constraints,
                    soft_constraints=soft_constraints,
                    failure_type=failure_type,
                    geometry_stats=geometry_stats,
                    extra_preservation_warning=True,
                )
        except Exception as exc:
            await self._emit_debug(
                "repair_retry_error",
                f"Retry after deletion-detect failed: {exc}",
            )
            repair_notes.append(
                "Repair LLM kept deleting code — kept the prior source and continued."
            )
            return original_code

        retry_code = extract_code_from_response(retry_response)
        retry_reason = detect_repair_deletion(original_code, retry_code)
        if retry_reason is None:
            repair_notes.append(
                f"Repair LLM initially deleted code ({reason}); retry preserved the program."
            )
            return retry_code

        await self._emit_debug(
            "repair_deletion_persists",
            f"Retry still deleted code: {retry_reason}",
            {
                "iteration": iteration,
                "first_reason": reason,
                "retry_reason": retry_reason,
                "retry_preview": (retry_code or "")[:400],
            },
        )
        repair_notes.append(
            f"Repair LLM kept deleting code ({reason}); kept the prior source so the next "
            f"iteration still has the full design to work from."
        )
        # Returning the original lets the next iteration see the real program
        # plus a new repair attempt — instead of inheriting a 2-line stub.
        return original_code

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

    def _save_failure_chat(self, pid, tid, mid=None, message: str = "Failed to generate valid model after retries."):
        # Use update instead of append because we already have a placeholder
        self.storage.update_last_chat_thread_message(
            pid, tid,
            ChatMessage(
                role="assistant",
                content=message,
                model_id=mid,
                steps=self.current_steps,
                agent_logic=self._current_agent_logic,
            ),
        )

    def _schedule_summarization_safely(
        self,
        turn_error_events: List[_error_patterns.FailureEvent],
        *,
        turn_succeeded: bool,
    ) -> None:
        """Fire-and-forget pitfall summarization. Never raises; logs to debug
        if scheduling fails. Both success and failure paths feed this — a
        budget-exhausted turn carries different but equally useful signal."""
        try:
            _error_patterns.schedule_summarization(
                self.llm,
                turn_error_events,
                turn_succeeded=turn_succeeded,
            )
        except Exception as exc:
            # _emit_debug is async; we can't await here because the failure
            # paths call this synchronously after `await` is no longer
            # appropriate. Fall back to module logger.
            import logging
            logging.getLogger(__name__).warning(
                "Pitfall summarization could not be scheduled: %s", exc
            )
