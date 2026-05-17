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

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
)


class LLMBackendUnavailable(RuntimeError):
    """The LLM backend (Ollama, vLLM, etc.) is unreachable, crashed, or returned
    nothing usable. Distinct from a logic-level failure where the model returned
    a parseable-but-bad response. The orchestrator should re-check connectivity
    and abort the pipeline rather than retry against a dead endpoint.
    """

    def __init__(self, message: str, *, cause: Optional[BaseException] = None):
        super().__init__(message)
        self.cause = cause

from ..cad.examples import get_api_reference, get_examples_text
from ..domain.models import (
    Connection,
    DesignComponent,
    DesignPlan,
    FeatureDecision,
    HardConstraints,
    PhysicalUse,
    SoftConstraints,
)
from ..knowledge import error_patterns as _error_patterns


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

    learned_pitfalls = _error_patterns.format_pitfalls_for_prompt(
        _error_patterns.get_active_pitfalls()
    )
    learned_section = f"\n{learned_pitfalls}\n" if learned_pitfalls else ""

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

## Code Block Discipline (CRITICAL — prevents wasted retries)
- The ```python block must contain ONLY Python statements, comments, and blank lines.
- Do NOT write "Wait, ...", "Actually, ...", "Let me try ...", "The error is ...", or any free-form English sentences inside the code block. That is reasoning, not code, and it breaks `ast.parse`.
- Do NOT include markdown back-ticked references (`like.this`) inside the code block.
- If you need to think, do it BEFORE the ```python block — but the final response must contain only the code block.
- Every statement you start, finish on the same line or with explicit `\\` continuation. Never leave a trailing `,` or open paren that you intend to "come back to."

## Common Pitfalls (avoid these — they cause 1-3 wasted repair cycles each)
{learned_section}- `cq.Workplane("XY").pushPoints(pts).hole(d)` is INVALID — `.hole()` needs a solid already in the chain. Either:
  - Chain off the existing solid: `body = body.faces(">Z").workplane().pushPoints(pts).hole(d)`, or
  - Build cylinders separately and cut: `holes = cq.Workplane("XY").pushPoints(pts).circle(d/2).extrude(depth); body = body.cut(holes)`
- Same for `.faces()`, `.edges()`, `.workplane(offset=...)` — these need a solid already in the chain.
- Hoist all parameters above any `def` that uses them. A nested function closing over an outer variable runs at call time, so the variable must already exist.
- Avoid `.rotate()` immediately after `.translate()` with overlapping pivots — recompute the pivot or reverse the order.
- `.fillet(r)` can fail on edges shorter than `2*r`. For mixed sizes, fillet vertical edges only: `body.edges("|Z").fillet(r)`.

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

def _format_code_with_line_numbers(code: str) -> str:
    """Prefix each source line with `NNN: ` so the LLM can locate the failing
    line directly from a traceback (`File "<string>", line 77`) instead of
    counting from the top. Keeps the model's repairs targeted and discourages
    full-rewrites that drop geometry.
    """
    if not code:
        return code
    lines = code.splitlines()
    width = max(2, len(str(len(lines))))
    return "\n".join(f"{i + 1:>{width}}: {ln}" for i, ln in enumerate(lines))


def build_repair_prompt(
    original_code: str,
    error_message: str,
    iteration: int,
    failure_type: Optional[str] = None,
    geometry_stats: Optional[dict] = None,
    *,
    extra_preservation_warning: bool = False,
    prior_attempts: Optional[list[dict]] = None,
) -> str:
    """
    Build a targeted repair prompt based on the failure type.

    Failure types: syntax_error, execution_error, geometry_invalid, constraint_violation

    The prompt is deliberately anti-deletion: repair LLMs have a habit of
    "fixing" an error by stripping out the offending lines (and everything
    that touched them), turning a 70-line holder into a 2-line stub. The
    instructions and the line-numbered failing code together push the model
    to patch the specific offending line, not rewrite from scratch.

    ``prior_attempts`` is an optional list of prior failed repair attempts on
    the same turn, each a dict with keys ``iteration``, ``error_first_line``,
    and ``failing_source_line``. When the current error's first line matches
    a previous attempt's, the prompt switches tone: the "apply the minimum
    fix" framing is replaced with explicit "your previous fixes did not change
    the error — try a structurally different approach" guidance. This breaks
    the loop where the LLM wiggles the same selector across 4 iterations and
    keeps getting the same OCC failure.
    """
    stats_text = ""
    if geometry_stats:
        stats_text = "\n## Current Geometry Stats\n"
        for k, v in geometry_stats.items():
            if v is not None:
                stats_text += f"- {k}: {v}\n"

    # Detect the "same error repeating" case. If the most recent prior
    # attempt's error first line matches the current one, the previous
    # "minimum fix" advice clearly is not working — surface that to the model
    # explicitly and ask for a structural change instead of another wiggle.
    current_err_first = (error_message or "").strip().splitlines()
    current_err_first = current_err_first[-1] if current_err_first else ""
    # Use the last non-empty line — OCC errors put the exception type there,
    # whereas the first line is just "Execution error:".
    for ln in reversed((error_message or "").strip().splitlines()):
        if ln.strip():
            current_err_first = ln.strip()
            break
    same_error_repeated = False
    prior_block = ""
    if prior_attempts:
        prior_lines = []
        for a in prior_attempts[-3:]:  # last 3 attempts is enough context
            it = a.get("iteration", "?")
            err = (a.get("error_first_line") or "").strip()
            src = (a.get("failing_source_line") or "").strip()
            if err and err == current_err_first:
                same_error_repeated = True
            bullet = f"- Attempt {it}: changed `{src or '(unknown line)'}` — still got `{err[:160]}`"
            prior_lines.append(bullet)
        if prior_lines:
            prior_block = (
                "\n## Prior repair attempts on this turn\n"
                + "\n".join(prior_lines)
                + "\n"
            )
            if same_error_repeated:
                prior_block += (
                    "\n**The same error has now recurred across multiple attempts.** "
                    "Your previous fixes did not change the failure mode. STOP tweaking "
                    "the same line — apply a structurally different fix this time "
                    "(reorder operations, swap the offending call for an equivalent, "
                    "reduce a problematic parameter by 2-3x, or drop a non-essential "
                    "feature like a fillet whose radius is incompatible with the "
                    "underlying edges).\n"
                )

    # Failure-type-specific guidance
    if failure_type == "syntax_error":
        guidance = """\
## Fix Required: Syntax Error
- Fix the Python syntax error shown above
- Make sure all parentheses, brackets, and quotes are balanced
- Check for missing colons, incorrect indentation, or typos in method names
- Verify all CadQuery method names are spelled correctly (e.g., `.fillet()` not `.filleted()`)
- If the validator says `result` is missing, ADD `result = <final_shape_var>` at the end. Do not delete code to make the check pass."""

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
Use the error traceback line number to locate the offending line in the numbered source above, then change ONLY what is necessary.

Common root causes:
- **`Cannot find a solid on the stack or in the parent chain`**: a method like `.hole()`, `.faces()`, `.workplane()` was called on a `cq.Workplane("XY")` chain that has no base solid yet. Fix by chaining the operation onto an existing solid (`base.faces(">Z").workplane().hole(d)`), or by `.cut()`-ing a separate cylinder built as its own solid.
- **`NameError: name 'X' is not defined`**: the variable was used before assignment, used outside the function/scope where it was defined, or accidentally referenced before a function `def` closes over it. Hoist parameters above any `def` that uses them, or pass them in as arguments.
- **AttributeError on a Workplane method**: check the method name spelling against CadQuery docs.
- **Selector returned empty**: `.faces(">Z")` / `.edges("|Z")` did not match anything — usually because the chain doesn't have geometry yet, or you applied the selector after a `.cut()` that consumed the face you wanted. Move the selector earlier or simplify it.
- **`StdFail_NotDone: BRep_API: command not done` from `.fillet()` or `.chamfer()`**: the OCC kernel could not blend the selected edges. This is NOT a Python error and tweaking the `.edges(...)` selector rarely helps. Real causes: (a) the radius is larger than the shortest selected edge (try halving or thirding `fillet_radius`), (b) the fillet runs over edges that were partly consumed by a later `.cut()` / `.union()` and now have slivers, or (c) two filleted edges meet at a corner OCC can't resolve. Fixes, in order of preference: **(1) move the fillet earlier** — apply it to each component before the union/cut, **(2) cut the radius by 2-3x**, **(3) restrict the selector to one face** (e.g. `.faces(">Z").edges()`), or **(4) drop this single fillet line** — sharp edges still print fine.
- **Other OCC kernel error from a boolean**: split the boolean into smaller steps and union at the end."""

    if same_error_repeated:
        # The minimum-fix framing is what got us stuck. Loosen it: the model
        # is allowed (encouraged, even) to restructure the failing region or
        # drop a non-essential feature, as long as the design's named
        # parameters and primary components are still defined.
        preservation_block = """\
## Preserve the design (the error keeps recurring — structural fix is allowed)
- Keep all named PARAMETERS defined at the top of the file.
- Keep the primary COMPONENTS (the main solids that make up the design) defined.
- You ARE allowed to: reorder operations, replace the offending call with an equivalent, halve/third a problematic numeric parameter, or DROP one non-essential feature (e.g. a single fillet line) if it's the source of the OCC failure.
- You ARE NOT allowed to: delete the parameter block, delete primary components, or shorten the program to a 5-line stub.
- DO NOT drop the `import cadquery as cq` line.
"""
    else:
        preservation_block = """\
## Preserve the design (CRITICAL)
- The error above is a localized bug. Apply the smallest possible fix.
- DO NOT delete components, parameters, helper functions, or geometry to make the error go away. Every named variable defined in the failed code MUST still be defined in your output (unless it is the direct cause of the failure, in which case it must be replaced with an equivalent definition).
- DO NOT shorten the program. Your output should have at least as many functional statements as the input.
- DO NOT drop the `import cadquery as cq` line.
- If you are tempted to skip a part because it is "complicated", keep it — fix the bug instead.
"""
    if extra_preservation_warning:
        preservation_block += (
            "- ⚠️ The previous repair attempt deleted most of the program. "
            "That was wrong. Restore the original components (parameters, helper "
            "shapes, booleans, fillets) and apply only a minimal fix to the line "
            "that caused the error.\n"
        )

    intro = (
        f"The CadQuery code failed on attempt {iteration}. The same error keeps recurring — "
        "STOP applying the same micro-edit and try a structurally different fix."
        if same_error_repeated
        else f"The CadQuery code failed on attempt {iteration}. Apply a minimal fix to the failing line(s) — do NOT rewrite the program."
    )
    return f"""\
{intro}

## Failed Code (with line numbers — match these to the traceback)
```python
{_format_code_with_line_numbers(original_code)}
```

## Error
```
{error_message[:2000]}
```
{prior_block}{stats_text}
{guidance}

{preservation_block}
## Output

FIRST, emit a short `<diagnosis>` block — 2-5 lines — explaining your understanding
of the root cause and the smallest fix you plan to apply. This is the only place
prose is allowed; put it BEFORE the python code block. Example:

```
<diagnosis>
Root cause: line 47 calls `.hole(d)` on a fresh Workplane that has no base solid yet.
Fix: chain the hole onto the existing `body` (body.faces('>Z').workplane().hole(d)).
Preserve all 12 parameters and the 7 booleans below — only the one line changes.
</diagnosis>
```

THEN, output the corrected program in a single ```python block:
- Output the FULL program (all parameters, helper shapes, booleans, fillets, and the `result = ...` assignment), not just the diff
- Keep variable names and structure of the failed code wherever possible
- Assign the final shape to `result`
- The python block must contain ONLY Python statements and comments — no English sentences.
"""


def build_vision_repair_prompt(
    code: str,
    *,
    intent: str,
    iteration: int,
    issues: list[dict],
    repair_instructions: str,
    matches_intent: bool,
    overall_score: float,
    confidence: float,
    plan_summary: str = "",
    key_features: Optional[list[str]] = None,
) -> str:
    """Build the user prompt for a vision-driven repair.

    Vision repair is fundamentally different from execution repair: the code
    *ran*, but the rendered geometry is wrong. So this prompt deliberately
    avoids ``build_repair_prompt``'s execution-error guidance and its strong
    "preserve every line" wording — both of which push the model into appending
    inert boolean ops instead of editing the offending component.

    The prompt is full-strength: the caller must pass it to a method that does
    not truncate. The previous setup ran the vision body through
    ``build_repair_prompt``'s ``error_message[:2000]`` slice, which silently
    chopped off the issues list and the repair instructions — leaving the LLM
    to "fix" the model with no idea what was actually wrong.
    """
    issues_text = "\n".join(
        f"- [{(it.get('severity') or 'info').upper()}] "
        f"{it.get('issue_type', 'issue')} "
        f"({it.get('location_hint') or 'unknown location'}): "
        f"{it.get('description', '')}"
        for it in issues
    ) or "- (no specific issues listed — overall score below threshold)"

    intent_block = ""
    if not matches_intent:
        intent_block = (
            "\n## CRITICAL\n"
            "The vision verifier reports the model does NOT match the user's intent. "
            "The current code produces the wrong shape, not just an imperfect one — "
            "rework the geometry rather than tweaking dimensions.\n"
        )

    plan_block = ""
    if plan_summary.strip():
        plan_block = f"\n## Design plan (ground truth)\n{plan_summary.strip()}\n"

    features_block = ""
    if key_features:
        bullets = "\n".join(f"- {f}" for f in key_features)
        features_block = f"\n## Key features that must appear in the result\n{bullets}\n"

    return f"""\
The CAD code below executed successfully, but the rendered model failed visual verification on attempt {iteration}. This is NOT a Python error — the code runs, but the geometry is wrong. Modify the geometry to make every listed issue go away.

## User intent
{intent}
{plan_block}{features_block}
## Current code (the program ran without error)
```python
{code}
```

## Vision verifier findings
- Overall score: {overall_score:.2f} (threshold ~0.65)
- Matches intent: {matches_intent}
- Verifier confidence: {confidence:.2f}

### Issues to address
{issues_text}
{intent_block}
## Required fixes (from the verifier)
{repair_instructions or '(no explicit instructions — address every issue listed above)'}

## How to repair geometry (CRITICAL — read before writing code)
1. **Modify in place, do not just append.** If a feature is wrong (e.g. "the lip is solid, it needs a notch"), find the component's definition near the top of the code and REPLACE it with a version that includes the feature. Subtracting a cavity from the final `result` is almost never the right answer — appending a `.cut()` after the final union is the failure mode of the previous attempt.
2. **Cuts must intersect material.** Any new `cq.Workplane(...).box(...).translate(...)` used as a cutter must geometrically overlap the part you intend to cut. Compute the target component's X/Y/Z extents from the parameters above and verify the cutter's extents overlap on ALL three axes. A `.cut(cavity)` against empty space silently returns the unchanged solid.
3. **Prefer feature-on-target over global ops.** To add a notch to `lip`, build `lip = lip.cut(notch)` immediately after `lip` is defined — not at the end. To fillet external edges, chain `.edges(...).fillet(r)` onto each component (or onto the union, AFTER all components are joined).
4. **You ARE allowed to restructure.** Keeping variable names is good; preserving wrong geometry is not. If the current `lip = box(...)` cannot have a notch, replace it with a more elaborate definition that does.
5. **Address EVERY issue.** Partial repairs come back for another round; a full pass costs the user less time even if it touches more code.

## Output rules
- Output a single ```python``` block with the complete, corrected program. No prose, no diff.
- Keep `import cadquery as cq` and assign the final shape to `result`.
- Keep the named parameter block at the top so the design stays editable.
- The python block must contain ONLY Python statements and comments — no English sentences inside it.
"""


def build_repair_system_prompt(
    hard_constraints: Optional[HardConstraints] = None,
    soft_constraints: Optional[SoftConstraints] = None,
) -> str:
    """Lean system prompt for the repair pass.

    The full generation system prompt ships ~12KB of API reference + every
    canonical example. During a repair the model already has a working program
    in the user message; what it needs is the rules and the API skeleton, not
    another copy of the example bank. Trimming the system prompt frees the
    token budget for the actual fix and reduces the temptation to substitute
    a canonical example for the user's design.
    """
    hc = hard_constraints or HardConstraints()
    sc = soft_constraints or SoftConstraints()
    learned_pitfalls = _error_patterns.format_pitfalls_for_prompt(
        _error_patterns.get_active_pitfalls()
    )
    learned_section = f"\n{learned_pitfalls}\n" if learned_pitfalls else ""
    return f"""\
You are an expert mechanical CAD engineer repairing a CadQuery program.
{learned_section}

## Output Rules (CRITICAL)
- FIRST, emit a single `<diagnosis>...</diagnosis>` block (2-5 lines) explaining your
  understanding of the root cause and the smallest fix you plan to apply. This is the
  ONLY place prose is allowed. Diagnosing the failure mode before patching produces
  tighter, less destructive repairs than jumping straight to code.
- THEN, output a single ```python code block with the complete fixed program.
- The python block MUST contain ONLY Python statements, comments, and blank lines. NO free-form English ("Wait,...", "Actually,...", "The error is...") inside the code block — that breaks `ast.parse` and forces another repair cycle.
- Always keep `import cadquery as cq` at the top
- Assign the final shape to a variable named `result`
- Use metric units (millimeters)
- Do NOT import anything other than `cadquery as cq` and `math`

## Repair Rules (CRITICAL)
- Apply the MINIMUM change needed to fix the reported error.
- Preserve every parameter, helper, sub-shape, and boolean operation from the failed code unless it is the direct cause of the error.
- DO NOT shorten the program to make the error vanish. Shorter output than input is a regression.
- If the same variable name was used before, keep using it — don't rename.

## Hard Constraints (must still be satisfied)
- Maximum part size: {hc.max_x_mm} × {hc.max_y_mm} × {hc.max_z_mm} mm
- Minimum wall thickness: {hc.min_wall_thickness_mm} mm
- Material: {sc.material}; max overhang {sc.overhang_angle_max}°

{get_api_reference()}
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
        from ..config import resolve_llm_model
        self.model = model or resolve_llm_model()

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
        *,
        extra_preservation_warning: bool = False,
        prior_attempts: Optional[list[dict]] = None,
    ) -> str:
        """Generate repaired CadQuery code after a failure.

        Uses a lean repair-specific system prompt (no full example bank) so
        the token budget goes to the actual fix rather than restating the
        ~10KB of canonical CadQuery snippets the generation pass already used.

        The model is instructed to emit a short ``<diagnosis>`` block before
        the code. We keep ``allow_thinking=False`` (thinking-mode reasoning
        burns the budget without surfacing it) — the diagnosis lives in the
        visible content stream where downstream code can extract it.
        """
        system_prompt = build_repair_system_prompt(hard_constraints, soft_constraints)
        repair_prompt = build_repair_prompt(
            original_code, error_message, iteration,
            failure_type=failure_type,
            geometry_stats=geometry_stats,
            extra_preservation_warning=extra_preservation_warning,
            prior_attempts=prior_attempts,
        )
        # Token budget bumped to accommodate the new <diagnosis> block plus the
        # full fixed program. ~6KB for diagnosis + code is comfortable headroom
        # for designs up to ~120 lines of CadQuery.
        return await self.generate(repair_prompt, system_prompt, max_tokens=7168)

    async def repair_cadquery_vision(
        self,
        user_prompt: str,
        hard_constraints: Optional[HardConstraints] = None,
        soft_constraints: Optional[SoftConstraints] = None,
    ) -> str:
        """Run a vision-driven repair.

        Distinct from ``repair_cadquery`` because vision repair is a geometry
        modification task, not a bug fix. The caller passes the full vision
        repair body (via ``build_vision_repair_prompt``) and we send it
        verbatim — no truncation, no execution-error guidance wrapper.
        """
        system_prompt = build_repair_system_prompt(hard_constraints, soft_constraints)
        return await self.generate(user_prompt, system_prompt, max_tokens=7168)

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
            "2. Then a `<physical_use>` block that grounds the design in the real world (see schema below). "
            "   This is where you reason about gravity, contact surfaces, applied forces, and how the object is actually used.\n"
            "3. Then a `<feature_decisions>` block where you decide which OPTIONAL feature families this design needs, "
            "   with a one-line rationale per decision. Skip features the request does not actually require.\n"
            "4. Finally, output a single `<design_plan>` XML block with this exact schema. DO NOT output JSON:\n"
            "```xml\n"
            "<physical_use>\n"
            "  <orientation>How the part sits in normal use — which face is the bottom under gravity?</orientation>\n"
            "  <contact_surfaces>What the part touches in use (table, wall, hand, the object it holds)</contact_surfaces>\n"
            "  <applied_forces>Where loads come from and roughly how big (e.g. 200g phone pulling forward on the holder lip)</applied_forces>\n"
            "  <use_cycle>How a user interacts with it (place once, insert/remove repeatedly, screw down, etc.)</use_cycle>\n"
            "  <ergonomic_notes>Any human-scale considerations: graspable, finger clearance, visibility</ergonomic_notes>\n"
            "  <mating_object>If holding/mounting/joining something, describe its key dimensions and clearances</mating_object>\n"
            "</physical_use>\n"
            "<feature_decisions>\n"
            "  <decision feature=\"fasteners_or_mounting_holes\" needed=\"true|false\">why this design does/does not need fastener holes</decision>\n"
            "  <decision feature=\"internal_cavity_or_shell\" needed=\"true|false\">why this design does/does not need a hollow cavity</decision>\n"
            "  <decision feature=\"retention_geometry\" needed=\"true|false\">why retention (lips, clips, hooks) is or is not required</decision>\n"
            "  <decision feature=\"load_bearing_reinforcement\" needed=\"true|false\">whether ribs/gussets are needed for the expected loads</decision>\n"
            "  <decision feature=\"clearance_or_port_cutouts\" needed=\"true|false\">whether cable paths/ports/access cutouts are required</decision>\n"
            "  <decision feature=\"moving_or_mating_interface\" needed=\"true|false\">whether the design has hinges/threads/sliding parts</decision>\n"
            "</feature_decisions>\n"
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
            "      <spec_source>explicit|inferred|default</spec_source>\n"
            "    </component>\n"
            "  </components>\n"
            "  <connections>\n"
            "    <connection from=\"part_a\" to=\"part_b\" kind=\"union|cut|press_fit|screw|hinge|slide|contact\">\n"
            "      how the two parts join, including clearances/tolerances if applicable\n"
            "    </connection>\n"
            "  </connections>\n"
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
            "  MUST appear as explicit cut components WHEN THEY ARE FUNCTIONALLY EXPECTED. Do NOT add fastener holes or cavities "
            "  just because the recipe mentions them — only add them if `<feature_decisions>` says they are needed.\n"
            "- For each component, tag `<spec_source>` honestly: `explicit` (the user requested it verbatim), `inferred` "
            "  (derived from the request — e.g. user said \"phone holder\", you inferred phone width), `default` "
            "  (your engineering choice with no strong signal from the user). The vision critic will weight `explicit` highest.\n"
            "- Use `<connections>` to record how parts join: a press-fit screw boss, a hinge axis, a sliding rail, or simply "
            "  \"both parts share face X via union\". This makes assembly relationships explicit so the code generator and the "
            "  vision verifier agree on intent.\n"
            "- If recipe context is provided, every required feature from that recipe must EITHER appear in components/key_features "
            "  OR be explicitly opted out in `<feature_decisions>` with a clear rationale.\n"
            "- Avoid under-modeled placeholder designs. A functional product should not degrade to a few primitive boxes when "
            "  a known archetype calls for lips, ribs, clearances, holes, slots, or cutouts AND the feature_decisions block says they are needed.\n"
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
        except (APIConnectionError, APITimeoutError, APIStatusError) as exc:
            # Backend dropped, timed out, or returned a 5xx mid-stream — almost
            # always a crashed/OOM Ollama. Don't retry against the dead endpoint;
            # surface it so the orchestrator can re-check connectivity and abort.
            raise LLMBackendUnavailable(
                f"LLM backend failed during plan streaming: {type(exc).__name__}: {exc}",
                cause=exc,
            ) from exc
        except Exception as exc:
            # Unknown streaming error (e.g. malformed SSE chunk). Try once
            # non-streaming as a best effort. If that also fails with an
            # infrastructure error, surface it; otherwise let the original
            # exception propagate.
            try:
                resp = await self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=0.3,
                    max_tokens=8192,
                )
            except (APIConnectionError, APITimeoutError, APIStatusError) as fallback_exc:
                raise LLMBackendUnavailable(
                    f"LLM backend failed on non-stream fallback: "
                    f"{type(fallback_exc).__name__}: {fallback_exc}",
                    cause=fallback_exc,
                ) from fallback_exc
            full_content = resp.choices[0].message.content or ""

        # Detect silent truncation: an Ollama worker that crashed mid-stream
        # often closes the SSE cleanly with zero tokens emitted. The 8k budget
        # makes a legitimate empty response vanishingly unlikely, so treat it
        # as a backend failure rather than letting the parser produce a blank
        # DesignPlan.
        if not full_content and not full_reasoning:
            raise LLMBackendUnavailable(
                "LLM backend returned an empty stream (0 content + 0 reasoning tokens). "
                "This usually means the backend crashed mid-generation — check Ollama logs "
                "for OOM or worker termination."
            )

        # Parse content first, fall back to reasoning. Thinking-mode models
        # (qwen3 etc.) often rehearse the schema in their reasoning stream
        # ("I need to emit <design_plan>..."), which the parser would
        # otherwise pick up as the first <design_plan> match and treat as
        # the answer — producing an empty/skeleton plan even though the real
        # one was waiting in the content channel.
        plan = parse_design_plan(full_content) if full_content else DesignPlan()
        if not _plan_has_structured_content(plan) and full_reasoning:
            plan = parse_design_plan(full_reasoning)

        if not plan.raw_reasoning and full_reasoning:
            plan.raw_reasoning = full_reasoning.strip()
        # raw_text keeps everything so the debug surface still shows the full
        # response (both channels) regardless of which one we parsed from.
        combined = full_reasoning
        if full_content:
            combined = (combined + "\n" + full_content) if combined else full_content
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


def extract_diagnosis_from_response(response: str) -> str:
    """Pull the optional ``<diagnosis>`` block out of a repair LLM response.

    Returns the inner text (trimmed) or an empty string when no block is
    present. Safe to call on any response.
    """
    if not response:
        return ""
    m = re.search(r"<diagnosis\b[^>]*>(.*?)</diagnosis>", response, re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _plan_has_structured_content(plan: DesignPlan) -> bool:
    """True if the parsed plan carries anything beyond an empty skeleton.

    Used by ``plan_design`` to decide whether to retry parsing against a
    different source (e.g. fall back from content to reasoning).
    """
    return bool(
        plan.summary
        or plan.components
        or plan.key_features
        or plan.parameters
        or plan.assumptions
        or plan.risks
    )


def _parse_physical_use_block(content: str) -> Optional[PhysicalUse]:
    """Parse the inner XML of a <physical_use> block into a PhysicalUse model.

    Resilient to namespace prefixes, mixed casing, and missing tags — any
    missing field stays empty rather than tripping a parse error.
    """
    if not content or not content.strip():
        return None

    def _grab(tag: str) -> str:
        m = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", content, re.DOTALL | re.IGNORECASE)
        return m.group(1).strip() if m else ""

    pu = PhysicalUse(
        orientation=_grab("orientation"),
        contact_surfaces=_grab("contact_surfaces"),
        applied_forces=_grab("applied_forces"),
        use_cycle=_grab("use_cycle"),
        ergonomic_notes=_grab("ergonomic_notes"),
        mating_object=_grab("mating_object"),
    )
    if not any([pu.orientation, pu.contact_surfaces, pu.applied_forces,
                pu.use_cycle, pu.ergonomic_notes, pu.mating_object]):
        return None
    return pu


_TRUE_TOKENS = {"true", "yes", "y", "1", "needed", "required"}
_FALSE_TOKENS = {"false", "no", "n", "0", "not_needed", "skip", "omit", "none"}


def _parse_bool_flag(value: str) -> bool:
    """Parse a yes/no-ish string into a boolean. Defaults to False for
    ambiguous values so the recipe gate doesn't accidentally force features
    the planner wasn't sure about.
    """
    v = (value or "").strip().lower()
    if v in _TRUE_TOKENS:
        return True
    if v in _FALSE_TOKENS:
        return False
    return False


def _parse_feature_decisions_block(content: str) -> list[FeatureDecision]:
    """Parse the inner XML of a <feature_decisions> block.

    Tolerates two shapes the LLM tends to produce:
    1. ``<decision feature="X" needed="true">rationale</decision>``
    2. ``<decision><feature>X</feature><needed>true</needed><rationale>...</rationale></decision>``
    """
    if not content or not content.strip():
        return []
    decisions: list[FeatureDecision] = []
    for m in re.finditer(r"<decision\b([^>]*)>(.*?)</decision>", content, re.DOTALL | re.IGNORECASE):
        attrs = m.group(1) or ""
        body = m.group(2) or ""
        # Attribute-style
        feature_attr = re.search(r'feature\s*=\s*"([^"]+)"', attrs, re.IGNORECASE)
        needed_attr = re.search(r'needed\s*=\s*"([^"]+)"', attrs, re.IGNORECASE)
        # Tag-style fallbacks
        feature_tag = re.search(r"<feature\b[^>]*>(.*?)</feature>", body, re.DOTALL | re.IGNORECASE)
        needed_tag = re.search(r"<needed\b[^>]*>(.*?)</needed>", body, re.DOTALL | re.IGNORECASE)
        rationale_tag = re.search(r"<rationale\b[^>]*>(.*?)</rationale>", body, re.DOTALL | re.IGNORECASE)

        feature = (feature_attr.group(1).strip() if feature_attr else
                   (feature_tag.group(1).strip() if feature_tag else "")).lower()
        needed = _parse_bool_flag(
            needed_attr.group(1) if needed_attr else
            (needed_tag.group(1) if needed_tag else "")
        )
        rationale = (rationale_tag.group(1).strip() if rationale_tag else body.strip())
        # Strip nested tags from rationale (when it inherited the whole body).
        rationale = re.sub(r"<[^>]+>", " ", rationale)
        rationale = re.sub(r"\s+", " ", rationale).strip()

        if feature:
            decisions.append(FeatureDecision(
                feature=feature,
                needed=needed,
                rationale=rationale[:400],
            ))
    return decisions


def parse_design_plan(raw_text: str) -> DesignPlan:
    """Parse a planner LLM response into a structured `DesignPlan`.

    The planner is asked to emit `<thinking>...</thinking>`, optional
    `<physical_use>...</physical_use>` and `<feature_decisions>...</feature_decisions>`
    blocks, then a `<design_plan>...</design_plan>` XML block. Older planner
    outputs may skip the new blocks — those still parse, just with empty
    physical_use and feature_decisions fields.
    """
    plan = DesignPlan(raw_text=raw_text)

    # 1. Pull out <thinking> ... </thinking>
    think_match = re.search(r"<thinking\b[^>]*>\s*(.*?)\s*</thinking>", raw_text, re.DOTALL | re.IGNORECASE)
    if think_match:
        plan.raw_reasoning = think_match.group(1).strip()
    else:
        # If no tags, try to find text before the first known XML block
        xml_start = re.search(
            r"<(?:design_plan|physical_use|feature_decisions)\b[^>]*>",
            raw_text,
            re.IGNORECASE,
        )
        if xml_start and xml_start.start() > 0:
            plan.raw_reasoning = raw_text[:xml_start.start()].strip()

    # 1b. <physical_use> — wraps the planner's real-world reasoning. Optional.
    pu_match = re.search(r"<physical_use\b[^>]*>(.*?)</physical_use>", raw_text, re.DOTALL | re.IGNORECASE)
    if pu_match:
        plan.physical_use = _parse_physical_use_block(pu_match.group(1))

    # 1c. <feature_decisions> — yes/no decisions on optional feature families.
    fd_match = re.search(r"<feature_decisions\b[^>]*>(.*?)</feature_decisions>", raw_text, re.DOTALL | re.IGNORECASE)
    if fd_match:
        plan.feature_decisions = _parse_feature_decisions_block(fd_match.group(1))

    # 2. Extract <design_plan> block content
    plan_match = re.search(r"<design_plan\b[^>]*>(.*?)</design_plan>", raw_text, re.DOTALL | re.IGNORECASE)
    if plan_match:
        content = plan_match.group(1)
    elif re.search(r"<summary\b[^>]*>|<component\b[^>]*>|<key_features\b[^>]*>", raw_text, re.IGNORECASE):
        # Some local models omit the outer wrapper while still emitting the
        # requested inner XML. Treat that as usable planner output instead of
        # hiding it behind an empty "(no summary)" draft.
        content = re.sub(r"<thinking\b[^>]*>.*?</thinking>", "", raw_text, flags=re.DOTALL | re.IGNORECASE)
    else:
        content = ""

    if content:
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
                    spec_source=(comp.findtext("spec_source") or "").strip().lower(),
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

            # Connections — optional, single-part designs may omit.
            for conn in root.findall(".//connections/connection"):
                kind = (conn.get("kind") or "").strip()
                from_part = (conn.get("from") or "").strip()
                to_part = (conn.get("to") or "").strip()
                description = (conn.text or "").strip()
                if from_part or to_part or kind or description:
                    plan.connections.append(Connection(
                        from_part=from_part,
                        to_part=to_part,
                        kind=kind,
                        description=description,
                    ))
        except Exception:
            # Robust Fallback: Regex extraction if XML parsing fails
            if not plan.summary:
                sum_match = re.search(r"<summary\b[^>]*>(.*?)</summary>", content, re.DOTALL | re.IGNORECASE)
                if sum_match:
                    plan.summary = sum_match.group(1).strip()
            
            if not plan.components:
                comp_matches = re.finditer(r"<component\b[^>]*>(.*?)</component>", content, re.DOTALL | re.IGNORECASE)
                for m in comp_matches:
                    c_inner = m.group(1)
                    name_m = re.search(r"<name\b[^>]*>(.*?)</name>", c_inner, re.DOTALL | re.IGNORECASE)
                    desc_m = re.search(r"<description\b[^>]*>(.*?)</description>", c_inner, re.DOTALL | re.IGNORECASE)
                    if name_m:
                        plan.components.append(DesignComponent(
                            name=name_m.group(1).strip(),
                            description=desc_m.group(1).strip() if desc_m else ""
                        ))
            
            if not plan.key_features:
                plan.key_features = [
                    f.strip()
                    for f in re.findall(r"<feature\b[^>]*>(.*?)</feature>", content, re.DOTALL | re.IGNORECASE)
                    if f.strip()
                ]

        # If XML parsing succeeded but the summary was missing due to a
        # malformed/namespaced tag, still try the same cheap fallback.
        if not plan.summary:
            sum_match = re.search(r"<summary\b[^>]*>(.*?)</summary>", content, re.DOTALL | re.IGNORECASE)
            if sum_match:
                plan.summary = sum_match.group(1).strip()

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

    pu = plan.physical_use
    if pu and any([pu.orientation, pu.contact_surfaces, pu.applied_forces,
                   pu.use_cycle, pu.ergonomic_notes, pu.mating_object]):
        lines.append("**Real-world use (design must satisfy these):**")
        if pu.orientation:
            lines.append(f"- Orientation under gravity: {pu.orientation}")
        if pu.contact_surfaces:
            lines.append(f"- Contact surfaces: {pu.contact_surfaces}")
        if pu.applied_forces:
            lines.append(f"- Applied forces: {pu.applied_forces}")
        if pu.use_cycle:
            lines.append(f"- Use cycle: {pu.use_cycle}")
        if pu.ergonomic_notes:
            lines.append(f"- Ergonomics: {pu.ergonomic_notes}")
        if pu.mating_object:
            lines.append(f"- Mating object: {pu.mating_object}")

    if plan.feature_decisions:
        included = [d for d in plan.feature_decisions if d.needed]
        excluded = [d for d in plan.feature_decisions if not d.needed]
        if included:
            lines.append("**Optional feature families the design DOES need (model them):**")
            for d in included:
                rationale = f" — {d.rationale}" if d.rationale else ""
                lines.append(f"- {d.feature}{rationale}")
        if excluded:
            lines.append("**Optional feature families the design does NOT need (skip them):**")
            for d in excluded:
                rationale = f" — {d.rationale}" if d.rationale else ""
                lines.append(f"- {d.feature}{rationale}")

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
            src_text = f" [{c.spec_source}]" if c.spec_source else ""
            prim = c.primitive or "shape"
            lines.append(
                f"{i}. `{c.name}`{src_text} — {prim} ({dim_text}){pos_text}{op_text}: {c.description}"
            )

    if plan.connections:
        lines.append("**Connections (how the components join):**")
        for c in plan.connections:
            kind = c.kind or "contact"
            edges = f"{c.from_part} ↔ {c.to_part}" if (c.from_part or c.to_part) else ""
            desc = f": {c.description}" if c.description else ""
            head = f"`{edges}` [{kind}]" if edges else f"[{kind}]"
            lines.append(f"- {head}{desc}")

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
    """Extract Python code from an LLM response that may contain markdown.

    A leading ``<diagnosis>...</diagnosis>`` block (emitted by the repair
    prompt) is stripped first so that, in the rare case the model omits the
    triple-backtick fence, the diagnosis prose doesn't leak into the code.
    """
    if not response:
        return ""
    # Strip diagnosis block if present — keeps it out of the code path when
    # the model forgot to fence the python block.
    response = re.sub(
        r"<diagnosis\b[^>]*>.*?</diagnosis>",
        "",
        response,
        flags=re.DOTALL | re.IGNORECASE,
    )

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


def _meaningful_lines(code: str) -> list[str]:
    """Return non-blank, non-comment lines of `code` for size comparison."""
    if not code:
        return []
    out: list[str] = []
    for raw in code.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append(stripped)
    return out


def detect_repair_deletion(
    original_code: str,
    repaired_code: str,
    *,
    shrink_threshold: float = 0.4,
    min_floor_lines: int = 5,
) -> Optional[str]:
    """Detect when a repair LLM "fixed" an error by deleting most of the code.

    Returns a human-readable reason string if `repaired_code` looks like a
    degenerate truncation of `original_code`, or None if the repair appears
    legitimate.

    Heuristics, applied in order:

    1. **Missing `import cadquery`** — every CadQuery program needs it. If the
       original had it and the repair doesn't, the repair has been gutted.
    2. **Below floor** — fewer than `min_floor_lines` meaningful lines is not
       a credible repair of a real CAD program.
    3. **Excessive shrinkage** — if the original had more than ~10 meaningful
       lines and the repair retained less than `shrink_threshold` of them,
       the model likely dropped components rather than patched the bug.

    Tuned to be conservative: a real fix usually changes a few lines but keeps
    the program intact. The iPhone-holder log shows a 70-line program reduced
    to 1 line by the repair pass; that case is well above any reasonable
    threshold.
    """
    repaired = repaired_code.strip() if repaired_code else ""
    if not repaired:
        return "repair output was empty"

    orig_lines = _meaningful_lines(original_code)
    new_lines = _meaningful_lines(repaired)

    # 1. cadquery import preserved?
    orig_has_cq = any("import cadquery" in ln for ln in orig_lines)
    new_has_cq = any("import cadquery" in ln for ln in new_lines)
    if orig_has_cq and not new_has_cq:
        return "repair dropped the `import cadquery` line"

    # 2. Floor on absolute size.
    if len(new_lines) < min_floor_lines and len(orig_lines) >= min_floor_lines * 2:
        return (
            f"repair shrank from {len(orig_lines)} meaningful line(s) to "
            f"{len(new_lines)} — below sanity floor"
        )

    # 3. Excessive shrinkage.
    if len(orig_lines) >= 10:
        ratio = len(new_lines) / max(1, len(orig_lines))
        if ratio < shrink_threshold:
            return (
                f"repair shrank from {len(orig_lines)} to {len(new_lines)} "
                f"meaningful line(s) ({ratio:.0%} retained, threshold "
                f"{shrink_threshold:.0%}) — likely a deletion-style fix"
            )

    return None
