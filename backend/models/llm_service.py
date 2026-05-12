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

    return f"""You are an expert CAD engineer. Generate CadQuery Python code for the user's request.

## Rules
- Output ONLY valid CadQuery Python code inside a single ```python code block
- Always `import cadquery as cq` at the top
- Assign the final shape to a variable called `result`
- Use metric units (millimeters)
- Apply fillets or chamfers where appropriate for 3D printing
- Make parts solid and manifold (watertight)
- Do NOT use os, subprocess, open(), or any file/system operations
- Do NOT import anything other than `cadquery`, `cq`, and `math`

## Hard Constraints (must satisfy)
- Maximum dimensions: {hard_constraints.max_x_mm} x {hard_constraints.max_y_mm} x {hard_constraints.max_z_mm} mm
- Minimum wall thickness: {hard_constraints.min_wall_thickness_mm} mm (for FDM 3D printing)

## Soft Constraints (guidelines)
- Material: {soft_constraints.material}
- Maximum overhang angle: {soft_constraints.overhang_angle_max}°
- Prefer fillets: {soft_constraints.prefer_fillets}
- Prefer chamfers: {soft_constraints.prefer_chamfers}
{f'- Notes: {soft_constraints.notes}' if soft_constraints.notes else ''}

{get_api_reference()}

{get_examples_text()}
"""


def build_repair_prompt(
    original_code: str,
    error_message: str,
    iteration: int,
) -> str:
    """Build a repair prompt when code generation fails."""
    return f"""The previous CadQuery code failed. Please fix it.

## Previous Code
```python
{original_code}
```

## Error (attempt {iteration})
{error_message}

## Instructions
- Fix the error and output corrected CadQuery Python code
- Keep the same design intent
- Output ONLY the corrected code inside a ```python code block
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
        self.model = model or os.environ.get("LLM_MODEL", "qwen3:32b")

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
            temperature=0.3,
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
            temperature=0.3,
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
    ) -> str:
        """Generate repaired CadQuery code after a failure."""
        system_prompt = build_system_prompt(hard_constraints, soft_constraints)
        repair_prompt = build_repair_prompt(original_code, error_message, iteration)
        return await self.generate(repair_prompt, system_prompt)


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
            return parts[1].strip()

    # Assume the entire response is code
    return response.strip()
