"""
Vision critique system.

Sends multi-angle renders to a vision-capable LLM and gets back a structured
critique report with printability issues and a repair prompt.

Default model: configured via VISION_MODEL env var (defaults to LLM_MODEL).
Same Ollama endpoint; model must support vision (image inputs).

The vision critique contract returns JSON:
{
  "matches_intent": bool,
  "score": float,          // 0.0 - 1.0 overall quality/printability
  "issues": [
    {
      "issue_type": str,   // thin_wall, overhang, non_manifold, symmetry, intent_mismatch, etc.
      "severity": str,     // error | warning | info
      "description": str,
      "location_hint": str
    }
  ],
  "repair_prompt": str,    // Actionable LLM prompt to fix the issues
  "confidence": float      // 0.0 - 1.0 how confident the vision model is
}
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

from ..domain.models import CritiqueReport, GeometryIssue

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Vision critique result
# ---------------------------------------------------------------------------

@dataclass
class VisionCritiqueResult:
    success: bool
    message: str = ""
    report: Optional[CritiqueReport] = None
    repair_prompt: str = ""
    matches_intent: bool = True
    raw_response: str = ""


# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------

VISION_SYSTEM_PROMPT = """\
You are an expert 3D CAD and 3D printing engineer reviewing a CAD model.
You will be shown rendered images of the model from multiple angles (isometric, front, right, top).

Your task: Critique the model for:
1. Printability (overhangs, wall thickness, bridges)
2. Structural integrity (thin features, sharp corners, fragile pins)
3. Symmetry and aesthetic quality
4. Whether the model matches the user's intent
5. Any missing features or clear geometry errors

IMPORTANT: You must respond ONLY with a valid JSON object matching this schema:
{
  "matches_intent": <bool>,
  "score": <float 0.0-1.0>,
  "issues": [
    {
      "issue_type": "<thin_wall|overhang|non_manifold|symmetry|intent_mismatch|fragile_feature|missing_feature|geometry_error|other>",
      "severity": "<error|warning|info>",
      "description": "<clear description>",
      "location_hint": "<which part of the model, e.g. 'bottom edge', 'left side', 'top surface'>"
    }
  ],
  "repair_prompt": "<actionable CadQuery code repair instructions for the LLM>",
  "confidence": <float 0.0-1.0>
}

Rules:
- score 0.8-1.0 = good/excellent, 0.5-0.8 = acceptable with warnings, below 0.5 = needs repair
- If no issues found, return empty issues array and score >= 0.9
- repair_prompt must be specific CadQuery instructions, not vague suggestions
- Do NOT wrap your response in markdown code blocks
- Output ONLY the JSON object
"""


def _build_vision_user_prompt(
    user_intent: str,
    geometry_stats: Optional[dict] = None,
) -> str:
    """Build the user message for the vision critique."""
    stats_text = ""
    if geometry_stats:
        stats_text = "\n\n## Geometry Statistics\n"
        for k, v in geometry_stats.items():
            if v is not None:
                stats_text += f"- {k}: {v}\n"

    return f"""\
Please critique this 3D CAD model.

## User's Original Intent
{user_intent}
{stats_text}

I'm attaching rendered images of the model from 4 angles: isometric, front, right, and top views.

Evaluate the model and return your critique as a JSON object following the schema in your instructions.
"""


# ---------------------------------------------------------------------------
# Image encoding
# ---------------------------------------------------------------------------

def _encode_image_base64(image_path: str) -> str:
    """Encode an image file to base64 string."""
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def _build_image_content_items(render_paths: dict[str, str]) -> list[dict]:
    """Build the content array items for multimodal messages."""
    items = []
    view_order = ["iso", "front", "right", "top"]

    for view_name in view_order:
        if view_name not in render_paths:
            continue

        path = render_paths[view_name]
        if not Path(path).exists():
            continue

        b64 = _encode_image_base64(path)
        items.append({
            "type": "text",
            "text": f"[{view_name.upper()} VIEW]",
        })
        items.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/png;base64,{b64}",
            },
        })

    return items


# ---------------------------------------------------------------------------
# JSON parsing (robust — handle model wrapping JSON in markdown)
# ---------------------------------------------------------------------------

def _parse_json_response(response: str) -> dict:
    """Extract and parse JSON from a potentially markdown-wrapped response."""
    text = response.strip()

    # Strip markdown code blocks if present
    if "```json" in text:
        match = re.search(r"```json\s*(.*?)```", text, re.DOTALL)
        if match:
            text = match.group(1).strip()
    elif "```" in text:
        match = re.search(r"```\s*(.*?)```", text, re.DOTALL)
        if match:
            text = match.group(1).strip()

    # Find JSON object in text (handles models that add preamble text)
    brace_match = re.search(r"\{.*\}", text, re.DOTALL)
    if brace_match:
        text = brace_match.group(0)

    return json.loads(text)


def _json_to_critique_report(data: dict) -> CritiqueReport:
    """Convert parsed JSON response to CritiqueReport domain model."""
    issues = []
    for issue_data in data.get("issues", []):
        issues.append(GeometryIssue(
            issue_type=issue_data.get("issue_type", "other"),
            severity=issue_data.get("severity", "warning"),
            description=issue_data.get("description", ""),
            location_hint=issue_data.get("location_hint", ""),
        ))

    return CritiqueReport(
        issues=issues,
        overall_printability=float(data.get("score", 0.5)),
        suggested_repairs=[data.get("repair_prompt", "")] if data.get("repair_prompt") else [],
        confidence=float(data.get("confidence", 0.5)),
    )


# ---------------------------------------------------------------------------
# Vision Critic
# ---------------------------------------------------------------------------

class VisionCritic:
    """
    Sends rendered images to a vision model and parses the structured critique.

    Uses Ollama's OpenAI-compatible API with image inputs.
    Model must be a vision-capable model (e.g., qwen3.6:27b, llava, gemma3).
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = 120.0,
    ):
        self.base_url = base_url or os.environ.get(
            "VISION_BASE_URL",
            os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1"),
        )
        self.api_key = api_key or os.environ.get(
            "VISION_API_KEY",
            os.environ.get("LLM_API_KEY", "ollama"),
        )
        self.model = model or os.environ.get(
            "VISION_MODEL",
            os.environ.get("LLM_MODEL", "qwen3.6:27b"),
        )
        self.timeout = timeout

    async def is_available(self) -> tuple[bool, str]:
        """
        Check if the vision model is available.
        Returns (available, message).
        """
        ollama_base = self.base_url.replace("/v1", "")
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{ollama_base}/api/tags")
                if resp.status_code != 200:
                    return False, f"Ollama returned HTTP {resp.status_code}"

                data = resp.json()
                models = [m["name"] for m in data.get("models", [])]
                if self.model not in models:
                    return False, f"Vision model '{self.model}' not found in Ollama. Available: {models}"

                return True, f"Vision model '{self.model}' is available"
        except Exception as e:
            return False, f"Cannot reach Ollama: {e}"

    async def critique(
        self,
        render_paths: dict[str, str],
        user_intent: str,
        geometry_stats: Optional[dict] = None,
    ) -> VisionCritiqueResult:
        """
        Send renders to the vision model and get back a structured critique.

        Args:
            render_paths: dict of view_name → PNG file path
            user_intent: the original user's description/intent
            geometry_stats: optional dict of geometry measurements to include

        Returns VisionCritiqueResult with parsed CritiqueReport.
        """
        if not render_paths:
            return VisionCritiqueResult(
                success=False,
                message="No render images provided for vision critique",
            )

        # Build image content items
        image_items = _build_image_content_items(render_paths)
        if not image_items:
            return VisionCritiqueResult(
                success=False,
                message="No valid render images found on disk",
            )

        # Build user message with images
        user_text = _build_vision_user_prompt(user_intent, geometry_stats)
        user_content = [{"type": "text", "text": user_text}] + image_items

        messages = [
            {"role": "system", "content": VISION_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

        # Call the vision model
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "messages": messages,
                        "temperature": 0.1,
                        "max_tokens": 2048,
                        "stream": False,
                    },
                )

                if response.status_code != 200:
                    return VisionCritiqueResult(
                        success=False,
                        message=f"Vision model API error: HTTP {response.status_code} — {response.text[:300]}",
                    )

                data = response.json()
                raw_text = data["choices"][0]["message"]["content"]

        except httpx.TimeoutException:
            return VisionCritiqueResult(
                success=False,
                message=f"Vision model timed out after {self.timeout}s",
            )
        except Exception as e:
            return VisionCritiqueResult(
                success=False,
                message=f"Vision model call failed: {e}\n{traceback.format_exc()}",
            )

        # Parse JSON response
        try:
            parsed = _parse_json_response(raw_text)
        except (json.JSONDecodeError, ValueError) as e:
            return VisionCritiqueResult(
                success=False,
                message=f"Failed to parse vision model JSON response: {e}",
                raw_response=raw_text,
            )

        report = _json_to_critique_report(parsed)
        repair_prompt = parsed.get("repair_prompt", "")
        matches_intent = bool(parsed.get("matches_intent", True))

        return VisionCritiqueResult(
            success=True,
            message=f"Critique complete. Score: {report.overall_printability:.2f}, Issues: {len(report.issues)}",
            report=report,
            repair_prompt=repair_prompt,
            matches_intent=matches_intent,
            raw_response=raw_text,
        )


def run_vision_critique(
    render_paths: dict[str, str],
    user_intent: str,
    geometry_stats: Optional[dict] = None,
) -> VisionCritiqueResult:
    """Synchronous wrapper for use in thread pools."""
    import asyncio

    critic = VisionCritic()
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            critic.critique(render_paths, user_intent, geometry_stats)
        )
    finally:
        loop.close()
