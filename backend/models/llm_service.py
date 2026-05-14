"""
LLM service — handles communication with local and cloud AI models.

Supports OpenAI-compatible APIs (Ollama, vLLM, OpenAI, etc.).
"""

from __future__ import annotations

import os
from typing import AsyncIterator, Optional

from openai import AsyncOpenAI

from ..cad.examples import get_api_reference, get_examples_text
from ..domain.models import HardConstraints, SoftConstraints


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

    async def generate(
        self,
        user_message: str,
        system_prompt: str,
        chat_history: Optional[list[dict]] = None,
    ) -> str:
        """Generate a complete response (non-streaming)."""
        messages = [{"role": "system", "content": system_prompt}]
        if chat_history:
            messages.extend(chat_history)
        messages.append({"role": "user", "content": user_message})

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=0.2,
            max_tokens=4096,
        )
        return response.choices[0].message.content or ""

    async def generate_stream(
        self,
        user_message: str,
        system_prompt: str,
        chat_history: Optional[list[dict]] = None,
    ) -> AsyncIterator[str]:
        """Generate a streaming response, yielding chunks."""
        messages = [{"role": "system", "content": system_prompt}]
        if chat_history:
            messages.extend(chat_history)
        messages.append({"role": "user", "content": user_message})

        stream = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=0.2,
            max_tokens=4096,
            stream=True,
        )

        async for chunk in stream:
            delta = chunk.choices[0].delta
            if delta.content:
                yield delta.content

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
        return await self.generate(repair_prompt, system_prompt)

    async def decide_research(self, user_message: str, chat_history: Optional[list[dict]] = None) -> Optional[str]:
        """
        Decide if web research is needed for the user's request.
        Returns a search query if research is needed, otherwise None.
        """
        prompt = f"""\
You are an expert mechanical engineer. Analyze the following user request and decide if you need to search the web for technical specifications, dimensions, or standards (e.g., bolt sizes, motor mounting patterns, material properties, standard connector dimensions).

User Request: {user_message}

If you need to search, output ONLY the search query in a single line. 
If no search is needed, output ONLY "NONE".

Search Query:"""
        
        response = await self.generate(prompt, "You are a technical research assistant.")
        query = response.strip().strip('"').strip("'")
        if query.upper() == "NONE":
            return None
        return query


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
