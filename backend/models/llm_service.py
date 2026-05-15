"""
LLM service — handles communication with local and cloud AI models.

Supports OpenAI-compatible APIs (Ollama, vLLM, OpenAI, etc.).
"""

from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET
from typing import Any, AsyncIterator, Callable, Optional

from openai import AsyncOpenAI

from ..cad.examples import get_api_reference, get_examples_text
from ..domain.models import (
    DesignComponent,
    DesignPlan,
    HardConstraints,
    SoftConstraints,
)


# ---------------------------------------------------------------------------
# System prompt builder
# ---------------------------------------------------------------------------

def build_system_prompt(
    hard_constraints: Optional[HardConstraints] = None,
    soft_constraints: Optional[SoftConstraints] = None,
) -> str:
    """Build the full system prompt for CadQuery code generation."""
    if hard_constraints is None:
        hard_constraints = HardConstraints()
    if soft_constraints is None:
        soft_constraints = SoftConstraints()

    return f"""\
You are an expert mechanical CAD engineer specializing in FDM 3D-printable parts.
Generate production-quality CadQuery Python code for the user's request.

## Output Rules (CRITICAL)
- Output ONLY a single ```python code block — no prose before or after
- Always `import cadquery as cq` at the top
- Assign the final shape to a variable named `result`
- Use metric units (millimeters)
- Do NOT use os, subprocess, open(), pathlib, or any file/system operations
- Do NOT import anything other than `cadquery as cq` and `math`
- For multi-part designs, use `cq.Assembly()` and add parts with descriptive names (e.g., `assy.add(part, name="base")`) to enable selection. Assign the assembly to `result`.

## Engineering Quality Standards
- **Watertight**: All geometry must be closed / manifold (no open shells unless intentional)
- **Wall thickness**: Minimum {hard_constraints.min_wall_thickness_mm}mm for all walls and features
- **Fillets/chamfers**: Apply 1-3mm fillets to sharp external corners for strength and printability
- **Overhangs**: Avoid overhangs > {soft_constraints.overhang_angle_max}° without support structures
- **Print orientation**: Design assuming the model prints flat on the XY build plate (Z = up)
- **No thin pins**: Standalone pins/posts < 2mm diameter will break — make them thicker
- **Boolean correctness**: After cut/union operations verify the result is a solid

## Hard Constraints (validation will fail if violated)
- Maximum part size: {hard_constraints.max_x_mm} × {hard_constraints.max_y_mm} × {hard_constraints.max_z_mm} mm
- Minimum wall thickness: {hard_constraints.min_wall_thickness_mm} mm

## Soft Constraints (design guidelines)
- Material: {soft_constraints.material}
- Max overhang angle: {soft_constraints.overhang_angle_max}°
- Prefer fillets: {soft_constraints.prefer_fillets}
- Prefer chamfers: {soft_constraints.prefer_chamfers}
{f'- Additional notes: {soft_constraints.notes}' if soft_constraints.notes else ''}

## Design Best Practices for FDM
1. Round all external sharp edges with fillet(1.5) or chamfer(1) minimum
2. Snap-fit features need 0.2-0.4mm clearance for assembly
3. Holes should be 0.2mm larger than nominal (FDM shrinkage)
4. Add a 0.5mm chamfer to hole entries to guide screws
5. Bridging spans > 50mm need support or redesign
6. For enclosures, use .shell(-wall) on the top face for open-top boxes

{get_api_reference()}

{get_examples_text()}
"""


# ---------------------------------------------------------------------------
# Failure-type-specific repair prompts
# ---------------------------------------------------------------------------

def build_repair_prompt(
    original_code: str,
    error_message: str,
    iteration: int,
    failure_type: Optional[str] = None,
    geometry_stats: Optional[dict] = None,
) -> str:
    """
    Build a targeted repair prompt based on the failure type.

    Failure types: syntax_error, execution_error, geometry_invalid, constraint_violation
    """
    stats_text = ""
    if geometry_stats:
        stats_text = "\n## Current Geometry Stats\n"
        for k, v in geometry_stats.items():
            if v is not None:
                stats_text += f"- {k}: {v}\n"

    # Failure-type-specific guidance
    if failure_type == "syntax_error":
        guidance = """\
## Fix Required: Syntax Error
- Fix the Python syntax error shown above
- Make sure all parentheses, brackets, and quotes are balanced
- Check for missing colons, incorrect indentation, or typos in method names
- Verify all CadQuery method names are spelled correctly (e.g., `.fillet()` not `.filleted()`)"""

    elif failure_type == "geometry_invalid":
        guidance = """\
## Fix Required: Invalid Geometry
The geometry produced an error or is not a valid solid. Common causes and fixes:

**Non-manifold geometry:**
- Avoid zero-thickness faces — ensure all walls have thickness >= 1.5mm
- After `.shell()`, verify the result is a valid solid
- When using `.cut()`, ensure the cutting body fully intersects the target

**Empty or null result:**
- The shape may have evaluated to nothing — check that extrude/revolve produces solid volume
- Verify selectors like `.faces(">Z")` actually select a face (try simpler selectors)
- After boolean operations, verify the result is non-empty

**Boolean failures:**
- Separate complex shapes into simpler steps
- Try building each part independently, then `.union()` at the end
- Avoid exact face-to-face contacts that can cause numerical issues

**General approach:**
- Start with a simpler, more conservative geometry
- Build from primitives and add detail incrementally"""

    elif failure_type == "constraint_violation":
        guidance = """\
## Fix Required: Constraint Violation
The geometry exceeds one or more hard constraints:

**If too large:**
- Scale all dimensions proportionally to fit within the allowed print volume
- Re-check all hardcoded dimension values in the code

**If wall too thin:**
- Increase wall thickness to at least 1.5mm
- Avoid shell operations that produce < 1.5mm walls
- Thin ribs/fins need to be at least 1.5mm wide"""

    else:
        # Generic execution error
        guidance = """\
## Fix Required: Execution Error
Analyze the error traceback carefully:

- If it's an attribute error: check the method name spelling in CadQuery docs
- If it's a selector error (`.faces()`, `.edges()`): simplify the selector string
- If it's a geometry kernel error from OCC: the operation is geometrically invalid
  - Try breaking the operation into smaller steps
  - Avoid operations on degenerate geometry
- If `result` is None/empty: make sure the final shape is assigned to `result`"""

    return f"""\
The CadQuery code failed on attempt {iteration}. Fix the issue and regenerate.

## Failed Code
```python
{original_code}
```

## Error
```
{error_message[:1500]}
```
{stats_text}
{guidance}

## Output
- Output ONLY the corrected CadQuery Python code in a ```python block
- Keep the same design intent
- Assign the final shape to `result`
"""


# ---------------------------------------------------------------------------
# LLM Client
# ---------------------------------------------------------------------------

class LLMService:
    """
    Async LLM client using OpenAI-compatible API.

    Works with:
    - Ollama (default, localhost:11434)
    - vLLM
    - OpenAI
    - Any OpenAI-compatible endpoint
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
    ):
        self.base_url = base_url or os.environ.get(
            "LLM_BASE_URL", "http://localhost:11434/v1"
        )
        self.api_key = api_key or os.environ.get("LLM_API_KEY", "ollama")
        self.model = model or os.environ.get("LLM_MODEL", "qwen3.6:27b")

        self.client = AsyncOpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
        )

    def _is_thinking_model(self) -> bool:
        """Return True for models that emit a separate `reasoning` stream channel.

        Qwen3.x (including qwen3.6) and some Gemma variants put internal thinking
        in a `reasoning` field rather than `content`. We need to either disable
        that mode (for code generation, where thinking eats max_tokens) or
        surface it as visible reasoning (for planning).
        """
        name = (self.model or "").lower()
        return ("qwen3" in name) or ("qwen-3" in name)

    def _suffix_no_think(self, text: str) -> str:
        """Append the conventional `/no_think` directive for Qwen3 models.

        For non-thinking models the suffix is harmless filler so we always add it.
        """
        if not self._is_thinking_model():
            return text
        if "/no_think" in text or "/no-think" in text:
            return text
        return text.rstrip() + "\n\n/no_think"

    async def generate(
        self,
        user_message: str,
        system_prompt: str,
        chat_history: Optional[list[dict]] = None,
        allow_thinking: bool = False,
        max_tokens: int = 4096,
    ) -> str:
        """Generate a complete response (non-streaming).

        For thinking-mode models we disable thinking by default — `generate` is
        used for repair/research where we want the final answer immediately and
        the model would otherwise burn the whole token budget on internal monolog.
        Pass `allow_thinking=True` if you do want to keep thinking on.
        """
        user_msg = user_message if allow_thinking else self._suffix_no_think(user_message)
        messages = [{"role": "system", "content": system_prompt}]
        if chat_history:
            messages.extend(chat_history)
        messages.append({"role": "user", "content": user_msg})

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=0.2,
            max_tokens=max_tokens,
        )
        msg = response.choices[0].message
        content = msg.content or ""
        # qwen3.x sometimes places the answer in `reasoning` when `/no_think`
        # doesn't fully suppress thinking. Combine both so downstream parsers
        # can find the code block regardless of which channel the model used.
        reasoning = getattr(msg, "reasoning", None) or getattr(msg, "reasoning_content", None) or ""
        if reasoning and not content:
            return reasoning
        if reasoning:
            return content + "\n" + reasoning
        return content

    async def generate_stream(
        self,
        user_message: str,
        system_prompt: str,
        chat_history: Optional[list[dict]] = None,
        allow_thinking: bool = False,
        max_tokens: int = 4096,
        on_reasoning: Optional[Callable[[str], Any]] = None,
    ) -> AsyncIterator[str]:
        """Stream the model response, yielding content chunks.

        - Thinking-mode models (qwen3.x) emit internal thinking in `delta.reasoning`
          instead of `delta.content`. By default we disable thinking via `/no_think`
          for code generation so the token budget goes to the actual answer.
        - If `on_reasoning` is provided, reasoning chunks are forwarded to it.
        """
        user_msg = user_message if allow_thinking else self._suffix_no_think(user_message)
        messages = [{"role": "system", "content": system_prompt}]
        if chat_history:
            messages.extend(chat_history)
        messages.append({"role": "user", "content": user_msg})

        stream = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=0.2,
            max_tokens=max_tokens,
            stream=True,
        )

        async for chunk in stream:
            delta = chunk.choices[0].delta
            content = getattr(delta, "content", None)
            reasoning = getattr(delta, "reasoning", None) or getattr(delta, "reasoning_content", None)
            if reasoning and on_reasoning:
                await on_reasoning(reasoning)
            if content:
                yield content

    async def generate_cadquery(
        self,
        user_message: str,
        hard_constraints: Optional[HardConstraints] = None,
        soft_constraints: Optional[SoftConstraints] = None,
        chat_history: Optional[list[dict]] = None,
    ) -> str:
        """Generate CadQuery code for a user request."""
        system_prompt = build_system_prompt(hard_constraints, soft_constraints)
        return await self.generate(user_message, system_prompt, chat_history)

    async def repair_cadquery(
        self,
        original_code: str,
        error_message: str,
        iteration: int,
        hard_constraints: Optional[HardConstraints] = None,
        soft_constraints: Optional[SoftConstraints] = None,
        failure_type: Optional[str] = None,
        geometry_stats: Optional[dict] = None,
    ) -> str:
        """Generate repaired CadQuery code after a failure."""
        system_prompt = build_system_prompt(hard_constraints, soft_constraints)
        repair_prompt = build_repair_prompt(
            original_code, error_message, iteration,
            failure_type=failure_type,
            geometry_stats=geometry_stats,
        )
        # Disable thinking so the whole token budget produces fixed code, and
        # give the model enough headroom for a complete rewrite if needed.
        return await self.generate(repair_prompt, system_prompt, max_tokens=6144)

    @staticmethod
    def build_planning_prompt(
        user_message: str,
        current_source: str = "",
        current_model_id: Optional[str] = None,
        research_context: str = "",
        recipe_context: str = "",
        plan_feedback: str = "",
        hard_constraints: Optional[HardConstraints] = None,
        soft_constraints: Optional[SoftConstraints] = None,
    ) -> tuple[str, str]:
        """Build the (system_prompt, user_message) pair that ``plan_design``
        will send to the LLM. Exposed as a static method so the orchestrator
        can show the user the exact prompt without re-running the LLM.
        """
        hc = hard_constraints or HardConstraints()
        sc = soft_constraints or SoftConstraints()

        system_prompt = (
            "You are an expert mechanical CAD engineer planning an FDM 3D-printable part "
            "before writing any code.\n\n"
            "## Your task\n"
            "Decompose the request into concrete sub-shapes with explicit dimensions. "
            "Think carefully about geometry, orientation, joinery, and printability. "
            "Be specific — never say things like 'reasonable size' — pick numbers in mm.\n\n"
            "## Output format (STRICT)\n"
            "1. First, a short `<thinking>` section with your free-form reasoning (1-3 paragraphs). "
            "   Use this to consider proportions, references, ambiguities, and risks.\n"
            "2. Then output a single `<design_plan>` XML block with this exact schema. DO NOT output JSON:\n"
            "```xml\n"
            "<design_plan>\n"
            "  <summary>one-sentence description of the final part</summary>\n"
            "  <overall_dimensions_mm>\n"
            "    <x>10</x><y>20</y><z>30</z>\n"
            "  </overall_dimensions_mm>\n"
            "  <components>\n"
            "    <component>\n"
            "      <name>unique_snake_case_name</name>\n"
            "      <description>what this sub-shape is and why</description>\n"
            "      <primitive>box|cylinder|sphere|extrude|revolve|polygon|cone|torus|custom</primitive>\n"
            "      <dimensions>\n"
            "        <length>50</length><width>30</width><height>10</height>\n"
            "      </dimensions>\n"
            "      <position><x>0</x><y>0</y><z>0</z></position>\n"
            "      <orientation>axis=Z|free-form description</orientation>\n"
            "      <operation>base|union|cut|intersect|fillet|chamfer|shell|pattern</operation>\n"
            "    </component>\n"
            "  </components>\n"
            "  <key_features>\n"
            "    <feature>feature 1 that must be visible in the result</feature>\n"
            "  </key_features>\n"
            "  <assumptions>\n"
            "    <assumption>specific assumption with chosen value</assumption>\n"
            "  </assumptions>\n"
            "  <risks>\n"
            "    <risk>a thing that can go wrong in CadQuery, with mitigation</risk>\n"
            "  </risks>\n"
            "  <parameters>\n"
            "    <parameter name=\"param_name\">10.0</parameter>\n"
            "  </parameters>\n"
            "</design_plan>\n"
            "```\n\n"
            "## Constraints\n"
            f"- Max print volume: {hc.max_x_mm} × {hc.max_y_mm} × {hc.max_z_mm} mm\n"
            f"- Minimum wall thickness: {hc.min_wall_thickness_mm} mm\n"
            f"- Material: {sc.material}; max overhang {sc.overhang_angle_max}°\n"
            "- Z is the print direction (up); design parts to print flat on XY.\n\n"
            "## Quality rules\n"
            "- The component list MUST be enough that an experienced engineer could implement it "
            "  in CadQuery from the plan alone, without seeing the original prompt.\n"
            "- For each cut/hole, list it as a separate component with operation=cut and its own dimensions.\n"
            "- Treat negative space as real design content: slots, notches, cavities, cable pass-throughs, and clearance reliefs "
            "  MUST appear as explicit cut components when they are functionally expected.\n"
            "- If recipe context is provided, every required feature from that recipe must appear in components or key_features.\n"
            "- Avoid under-modeled placeholder designs. A functional product should not degrade to a few primitive boxes when "
            "  a known archetype calls for lips, ribs, clearances, holes, slots, or cutouts.\n"
            "- For multi-part assemblies, name parts so the names match the final cq.Assembly children.\n"
            "- `key_features` is a checklist used by the vision verifier — list every distinct visible feature.\n"
        )

        user_parts = []
        if research_context:
            user_parts.append(research_context)
        if recipe_context:
            user_parts.append(recipe_context)
        if plan_feedback:
            user_parts.append(
                "## Previous Plan Quality Feedback\n"
                "The previous plan was rejected as too incomplete for this product archetype. "
                "Address every item below in the revised plan:\n"
                f"{plan_feedback}"
            )
        if current_source:
            user_parts.append(
                f"## Current CadQuery source (checkpoint `{current_model_id}`)\n"
                "Use this as the base; describe the *change* needed.\n"
                f"```python\n{current_source}\n```\n"
            )
        user_parts.append(f"## Request\n{user_message}")
        user_msg = "\n\n".join(user_parts)
        return system_prompt, user_msg

    async def plan_design(
        self,
        user_message: str,
        chat_history: Optional[list[dict]] = None,
        current_source: str = "",
        current_model_id: Optional[str] = None,
        research_context: str = "",
        recipe_context: str = "",
        plan_feedback: str = "",
        hard_constraints: Optional[HardConstraints] = None,
        soft_constraints: Optional[SoftConstraints] = None,
        on_chunk: Optional[Callable[[str], Any]] = None,
    ) -> DesignPlan:
        """Generate a structured design plan for the request *before* writing code.

        The plan is streamed (if `on_chunk` is provided) so the user can see the
        agent's reasoning. The result is a parsed `DesignPlan`; the raw text is
        preserved in `plan.raw_text` for debugging.
        """
        system_prompt, user_msg = self.build_planning_prompt(
            user_message=user_message,
            current_source=current_source,
            current_model_id=current_model_id,
            research_context=research_context,
            recipe_context=recipe_context,
            plan_feedback=plan_feedback,
            hard_constraints=hard_constraints,
            soft_constraints=soft_constraints,
        )

        messages = [{"role": "system", "content": system_prompt}]
        if chat_history:
            messages.extend(chat_history)
        messages.append({"role": "user", "content": user_msg})

        # Thinking-mode models (qwen3.x) stream their reasoning in a separate
        # `reasoning` field. We forward that to the UI as visible thinking and
        # combine reasoning + content for parsing in case the model put the JSON
        # in the reasoning stream (some Qwen3 servers do that).
        full_content = ""
        full_reasoning = ""
        try:
            stream = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.3,
                max_tokens=8192,
                stream=True,
            )
            async for chunk in stream:
                delta = chunk.choices[0].delta
                content = getattr(delta, "content", None)
                reasoning = getattr(delta, "reasoning", None) or getattr(delta, "reasoning_content", None)
                if reasoning:
                    full_reasoning += reasoning
                    if on_chunk:
                        await on_chunk(reasoning)
                if content:
                    full_content += content
                    if on_chunk:
                        await on_chunk(content)
        except Exception:
            # If streaming fails, fall back to a non-stream call
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.3,
                max_tokens=8192,
            )
            full_content = resp.choices[0].message.content or ""

        # Combine for parsing — both reasoning and content may contain useful
        # signal (the <thinking> section vs the json block).
        combined = full_reasoning
        if full_content:
            combined = (combined + "\n" + full_content) if combined else full_content
        plan = parse_design_plan(combined)
        if not plan.raw_reasoning and full_reasoning:
            plan.raw_reasoning = full_reasoning.strip()
        plan.raw_text = combined
        return plan

    async def repair_design_plan(
        self,
        user_message: str,
        rejected_plan: DesignPlan,
        quality_feedback: str,
        chat_history: Optional[list[dict]] = None,
        current_source: str = "",
        current_model_id: Optional[str] = None,
        research_context: str = "",
        recipe_context: str = "",
        hard_constraints: Optional[HardConstraints] = None,
        soft_constraints: Optional[SoftConstraints] = None,
        on_chunk: Optional[Callable[[str], Any]] = None,
    ) -> DesignPlan:
        """Regenerate a plan after the deterministic recipe gate rejects it."""
        rejected_text = plan_to_prompt_text(rejected_plan) or rejected_plan.raw_text
        feedback = (
            f"{quality_feedback}\n\n"
            "Rejected plan summary for reference:\n"
            f"{rejected_text[:3000]}"
        )
        return await self.plan_design(
            user_message=user_message,
            chat_history=chat_history,
            current_source=current_source,
            current_model_id=current_model_id,
            research_context=research_context,
            recipe_context=recipe_context,
            plan_feedback=feedback,
            hard_constraints=hard_constraints,
            soft_constraints=soft_constraints,
            on_chunk=on_chunk,
        )

    async def decide_research(self, user_message: str, chat_history: Optional[list[dict]] = None) -> tuple[Optional[str], str]:
        """
        Decide if web research is needed for the user's request.
        Returns (search_query, reasoning). query is None if no research needed.
        """
        prompt = f"""\
You are an expert mechanical engineer. Analyze the following user request and decide if you need to search the web for technical specifications, dimensions, or standards (e.g., bolt sizes, motor mounting patterns, material properties, standard connector dimensions).

User Request: {user_message}

Output your response in this exact XML format:
<research_decision>
  <reasoning>Detailed explanation of why research is or is not needed, focusing on what specific technical facts are missing or present.</reasoning>
  <query>The search query, or "NONE" if no search is needed</query>
</research_decision>
"""
        
        response = await self.generate(prompt, "You are a technical research assistant.", allow_thinking=True)
        
        reasoning = "The agent is evaluating the request against its internal knowledge base."
        query = None
        
        try:
            # Simple regex extraction for reliability
            reason_match = re.search(r"<reasoning>(.*?)</reasoning>", response, re.DOTALL | re.IGNORECASE)
            if reason_match:
                reasoning = reason_match.group(1).strip()
            
            query_match = re.search(r"<query>(.*?)</query>", response, re.DOTALL | re.IGNORECASE)
            if query_match:
                q_text = query_match.group(1).strip().strip('"').strip("'")
                if q_text.upper() != "NONE" and q_text:
                    query = q_text
        except Exception:
            # Fallback for non-compliant models
            if "NONE" not in response.upper() and len(response.splitlines()) > 0:
                query = response.splitlines()[-1].strip().strip('"').strip("'")
                reasoning = "Model provided a query directly."

        return query, reasoning


def parse_design_plan(raw_text: str) -> DesignPlan:
    """Parse a planner LLM response into a structured `DesignPlan`.

    The planner is asked to emit `<thinking>...</thinking>` followed by a 
    `<design_plan>...</design_plan>` XML block.
    """
    plan = DesignPlan(raw_text=raw_text)

    # 1. Pull out <thinking> ... </thinking>
    think_match = re.search(r"<thinking>\s*(.*?)\s*</thinking>", raw_text, re.DOTALL | re.IGNORECASE)
    if think_match:
        plan.raw_reasoning = think_match.group(1).strip()
    else:
        # If no tags, try to find text before the XML block
        xml_start = raw_text.find("<design_plan>")
        if xml_start > 0:
            plan.raw_reasoning = raw_text[:xml_start].strip()

    # 2. Extract <design_plan> block content
    plan_match = re.search(r"<design_plan>(.*?)</design_plan>", raw_text, re.DOTALL | re.IGNORECASE)
    if plan_match:
        content = plan_match.group(1)
        # Attempt XML parsing
        try:
            # Wrap in root to handle potentially malformed XML if the model missed the outer tag
            root = ET.fromstring(f"<root>{content}</root>")
            plan.summary = (root.findtext("summary") or "").strip()
            
            dims = root.find("overall_dimensions_mm")
            if dims is not None:
                try:
                    plan.overall_dimensions_mm = [
                        float(dims.findtext("x") or 0),
                        float(dims.findtext("y") or 0),
                        float(dims.findtext("z") or 0)
                    ]
                except Exception:
                    pass

            for comp in root.findall(".//component"):
                c_name = (comp.findtext("name") or "").strip()
                if not c_name:
                    continue
                
                dims_elem = comp.find("dimensions")
                dimensions = {}
                if dims_elem is not None:
                    for d in dims_elem:
                        try:
                            dimensions[d.tag] = float(d.text)
                        except Exception:
                            pass
                
                pos_elem = comp.find("position")
                position = None
                if pos_elem is not None:
                    try:
                        position = [
                            float(pos_elem.findtext("x") or 0),
                            float(pos_elem.findtext("y") or 0),
                            float(pos_elem.findtext("z") or 0)
                        ]
                    except Exception:
                        pass

                plan.components.append(DesignComponent(
                    name=c_name,
                    description=(comp.findtext("description") or "").strip(),
                    primitive=(comp.findtext("primitive") or "").strip(),
                    dimensions=dimensions,
                    position=position,
                    orientation=(comp.findtext("orientation") or "").strip(),
                    operation=(comp.findtext("operation") or "").strip(),
                ))
            
            plan.key_features = [f.text.strip() for f in root.findall(".//key_features/feature") if f.text and f.text.strip()]
            plan.assumptions = [a.text.strip() for a in root.findall(".//assumptions/assumption") if a.text and a.text.strip()]
            plan.risks = [r.text.strip() for r in root.findall(".//risks/risk") if r.text and r.text.strip()]
            
            for p in root.findall(".//parameters/parameter"):
                p_name = p.get("name")
                if p_name and p.text:
                    try:
                        plan.parameters[p_name] = float(p.text)
                    except ValueError:
                        pass
        except Exception:
            # Robust Fallback: Regex extraction if XML parsing fails
            if not plan.summary:
                sum_match = re.search(r"<summary>(.*?)</summary>", content, re.DOTALL | re.IGNORECASE)
                if sum_match:
                    plan.summary = sum_match.group(1).strip()
            
            if not plan.components:
                comp_matches = re.finditer(r"<component>(.*?)</component>", content, re.DOTALL | re.IGNORECASE)
                for m in comp_matches:
                    c_inner = m.group(1)
                    name_m = re.search(r"<name>(.*?)</name>", c_inner, re.IGNORECASE)
                    desc_m = re.search(r"<description>(.*?)</description>", c_inner, re.IGNORECASE)
                    if name_m:
                        plan.components.append(DesignComponent(
                            name=name_m.group(1).strip(),
                            description=desc_m.group(1).strip() if desc_m else ""
                        ))
            
            if not plan.key_features:
                plan.key_features = re.findall(r"<feature>(.*?)</feature>", content, re.IGNORECASE)

    # If nothing was parsed, treat whole response as reasoning so it stays visible
    if not plan.summary and not plan.components and not plan.raw_reasoning:
        plan.raw_reasoning = raw_text.strip()

    return plan


def plan_to_prompt_text(plan: DesignPlan) -> str:
    """Render a `DesignPlan` as a compact prompt block for the code generator."""
    if not plan or (not plan.summary and not plan.components and not plan.key_features):
        return ""

    lines: list[str] = ["## Design Plan (follow this exactly)"]
    if plan.summary:
        lines.append(f"**Goal:** {plan.summary}")
    if plan.overall_dimensions_mm and len(plan.overall_dimensions_mm) == 3:
        x, y, z = plan.overall_dimensions_mm
        lines.append(f"**Overall size target:** {x:.1f} × {y:.1f} × {z:.1f} mm")

    if plan.parameters:
        lines.append("**Named parameters (declare these at the top of the source):**")
        for k, v in plan.parameters.items():
            lines.append(f"- `{k} = {v}`  # mm")

    if plan.components:
        lines.append("**Components (each must be built and combined as the plan says):**")
        for i, c in enumerate(plan.components, 1):
            dim_text = ", ".join(f"{k}={v}" for k, v in c.dimensions.items())
            pos_text = f", position={c.position}" if c.position else ""
            op_text = f", operation={c.operation}" if c.operation else ""
            prim = c.primitive or "shape"
            lines.append(
                f"{i}. `{c.name}` — {prim} ({dim_text}){pos_text}{op_text}: {c.description}"
            )

    if plan.key_features:
        lines.append("**Key features that MUST be visible in the result:**")
        for f in plan.key_features:
            lines.append(f"- {f}")

    if plan.assumptions:
        lines.append("**Assumptions:**")
        for a in plan.assumptions:
            lines.append(f"- {a}")

    if plan.risks:
        lines.append("**Known pitfalls — handle these explicitly:**")
        for r in plan.risks:
            lines.append(f"- {r}")

    return "\n".join(lines)


def extract_code_from_response(response: str) -> str:
    """Extract Python code from an LLM response that may contain markdown."""
    # Look for ```python ... ``` blocks
    if "```python" in response:
        parts = response.split("```python")
        if len(parts) > 1:
            code_block = parts[1].split("```")[0]
            return code_block.strip()

    # Look for ``` ... ``` blocks
    if "```" in response:
        parts = response.split("```")
        if len(parts) >= 3:
            code = parts[1].strip()
            # Strip a leading "python" line if present
            if code.startswith("python\n"):
                code = code[7:]
            return code.strip()

    # Assume the entire response is code
    return response.strip()
