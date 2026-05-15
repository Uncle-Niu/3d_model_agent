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
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

from ..domain.models import CritiqueReport, DesignPlan, GeometryIssue

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
You are an expert 3D CAD and 3D printing engineer verifying a generated model
against an explicit design plan and the user's intent.

You will be shown rendered images from multiple angles (isometric, front, right, top)
plus a checklist of "key features" that must be visible. Treat the checklist as
GROUND TRUTH — every entry must be checked individually.

## Inspection procedure (do this internally before you write JSON)
1. For each "key feature" in the checklist, look across all four views and decide:
   present, partially present, or missing. Cite which view(s) show it.
2. Look for shape-level mismatches: wrong overall form, wrong proportions, missing
   sub-shapes, extra sub-shapes that the plan did not call for.
3. Look for printability problems: overhangs, thin walls, fragile pins, sharp
   internal corners, geometry that is clearly non-manifold (visible holes).
4. Only mark `matches_intent=true` if the result looks like the plan AND no required
   feature is missing.

## Output (STRICT)
Respond with ONLY a JSON object, no markdown fences, no preamble:

{
  "matches_intent": <bool>,
  "score": <float 0.0-1.0>,
  "feature_checklist": [
    {"feature": "<from the key-feature checklist>", "present": <true|false|"partial">, "evidence": "<which view + what you saw>"}
  ],
  "issues": [
    {
      "issue_type": "missing_feature|wrong_shape|wrong_proportion|thin_wall|overhang|fragile_feature|non_manifold|symmetry|intent_mismatch|geometry_error|other",
      "severity": "error|warning|info",
      "description": "<concrete, specific — name the feature and what is wrong>",
      "location_hint": "<view + region, e.g. 'front view, lower-left'>"
    }
  ],
  "repair_prompt": "<actionable CadQuery instructions, naming the components/parameters to change>",
  "confidence": <float 0.0-1.0>
}

## Scoring rules
- ANY missing key feature → severity=error, matches_intent=false, score <= 0.4.
- Wrong overall shape → matches_intent=false, score <= 0.3.
- All features present, only printability concerns → matches_intent=true, score 0.7-0.95.
- Perfect match with no concerns → matches_intent=true, score 0.95-1.0.

## Do NOT
- Do NOT invent dimensions you cannot measure from the renders.
- Do NOT mark `matches_intent=true` just because the renders look like *a* CAD model.
- Do NOT wrap your response in ```json fences.
"""


def _build_vision_user_prompt(
    user_intent: str,
    geometry_stats: Optional[dict] = None,
    plan: Optional[DesignPlan] = None,
    recipe_context: str = "",
) -> str:
    """Build the user message for the vision critique."""
    stats_text = ""
    if geometry_stats:
        stats_text = "\n## Measured Geometry Statistics (deterministic, from OCCT)\n"
        for k, v in geometry_stats.items():
            if v is not None:
                stats_text += f"- {k}: {v}\n"

    plan_text = ""
    if plan and (plan.summary or plan.key_features or plan.components):
        parts = ["\n## Design Plan (ground truth — verify the result against this)"]
        if plan.summary:
            parts.append(f"**Goal:** {plan.summary}")
        if plan.overall_dimensions_mm:
            x, y, z = plan.overall_dimensions_mm
            parts.append(f"**Target size:** {x:.1f} × {y:.1f} × {z:.1f} mm")
        if plan.components:
            parts.append("**Expected components:**")
            for c in plan.components:
                dims = ", ".join(f"{k}={v}" for k, v in c.dimensions.items())
                pos = f" at {c.position}" if c.position else ""
                op = f" [{c.operation}]" if c.operation else ""
                parts.append(f"- `{c.name}`{op}: {c.primitive} ({dims}){pos} — {c.description}")
        if plan.key_features:
            parts.append("**Key features checklist — verify EACH explicitly:**")
            for f in plan.key_features:
                parts.append(f"- {f}")
        if plan.assumptions:
            parts.append("**Assumptions made by the planner:**")
            for a in plan.assumptions:
                parts.append(f"- {a}")
        plan_text = "\n".join(parts) + "\n"

    recipe_text = ""
    if recipe_context:
        recipe_text = (
            "\n## CAD Recipe / Product Archetype Context\n"
            "Use this as an independent rubric in addition to the generated plan. "
            "If the model satisfies the plan but omits required recipe features, mark matches_intent=false.\n"
            f"{recipe_context}\n"
        )

    return f"""\
Verify this generated 3D CAD model against the user's intent and the plan below.

## User's Original Request
{user_intent}
{recipe_text}{plan_text}{stats_text}
You are looking at four rendered views of the model: isometric, front, right, and top.
Inspect each key feature in the checklist individually. Only return `matches_intent=true`
if every feature in the checklist is actually visible, every required recipe/archetype
feature is represented, and the overall shape matches. Reject simplistic placeholder
geometry such as a plain base plus slab when the recipe calls for lips, ribs, slots,
holes, guides, or cable notches.

Return only the JSON object specified in your instructions — no fences, no preamble.
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

def _fallback_parse_critique(raw_text: str) -> dict:
    """Recover a partial critique from a truncated or malformed response.

    Vision models sometimes run out of tokens mid-JSON. Rather than dropping
    the whole critique (which silently turns off the repair loop), scrape the
    raw text for the must-have signals — `matches_intent`, `score`, and the
    feature_checklist's `present:false` entries — and synthesize a minimal
    critique that still triggers repair when the verifier saw a problem.
    """
    text = raw_text or ""
    if not text:
        return {}

    out: dict = {}

    m = re.search(r'"matches_intent"\s*:\s*(true|false)', text, re.IGNORECASE)
    if m:
        out["matches_intent"] = m.group(1).lower() == "true"

    m = re.search(r'"score"\s*:\s*([0-9.]+)', text)
    if m:
        try:
            out["score"] = float(m.group(1))
        except ValueError:
            pass

    m = re.search(r'"confidence"\s*:\s*([0-9.]+)', text)
    if m:
        try:
            out["confidence"] = float(m.group(1))
        except ValueError:
            pass

    # Pull issues / missing checklist entries that we can identify by name
    issues = []
    for match in re.finditer(
        r'"feature"\s*:\s*"([^"]{1,200})"\s*,\s*"present"\s*:\s*(?:false|"false"|"missing"|"no")',
        text,
        re.IGNORECASE,
    ):
        issues.append({
            "issue_type": "missing_feature",
            "severity": "error",
            "description": f"Key feature absent: {match.group(1)}",
            "location_hint": "",
        })
    if issues:
        out["issues"] = issues

    # If nothing useful was recovered, return empty so caller can mark as failure
    if "matches_intent" not in out and "score" not in out and not issues:
        return {}

    # Defaults to ensure downstream code has something to work with
    out.setdefault("score", 0.4)
    out.setdefault("matches_intent", out["score"] >= 0.6)
    out.setdefault("issues", [])
    out.setdefault(
        "repair_prompt",
        "The vision verifier's response was truncated before completing the JSON. "
        "Treat this as a failed verification: re-check every key feature in the plan, "
        "and fix any that are missing, miss-positioned, or wrong shape.",
    )
    return out


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

    # Promote unchecked checklist items into explicit missing_feature issues so the
    # repair loop reasons about them even if the model forgot to list them in `issues`.
    for entry in data.get("feature_checklist", []) or []:
        if not isinstance(entry, dict):
            continue
        present = entry.get("present")
        if present in (False, "false", "missing", "no"):
            issues.append(GeometryIssue(
                issue_type="missing_feature",
                severity="error",
                description=f"Key feature absent: {entry.get('feature', '(unnamed)')}",
                location_hint=str(entry.get("evidence", "")),
            ))
        elif present in ("partial", "partially"):
            issues.append(GeometryIssue(
                issue_type="missing_feature",
                severity="warning",
                description=f"Key feature only partially present: {entry.get('feature', '(unnamed)')}",
                location_hint=str(entry.get("evidence", "")),
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
        Check if the configured vision model is registered with Ollama. If not,
        and at least one other vision-capable model IS, auto-swap to it (so the
        pipeline keeps working when e.g. qwen3.6 is unavailable but gemma4 is).
        """
        ollama_base = self.base_url.replace("/v1", "")
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{ollama_base}/api/tags")
                if resp.status_code != 200:
                    return False, f"Ollama returned HTTP {resp.status_code}"

                data = resp.json()
                models = [m["name"] for m in data.get("models", [])]
                if self.model in models:
                    return True, f"Vision model '{self.model}' is available"

                # Fall back to another known-vision-capable model already on the host
                preferred_fallbacks = ["gemma4:31b", "gemma3:27b", "qwen3.6:27b"]
                for candidate in preferred_fallbacks:
                    if candidate in models and candidate != self.model:
                        old = self.model
                        self.model = candidate
                        return True, f"Vision model '{old}' not found; falling back to '{candidate}'"

                return False, f"Vision model '{self.model}' not found in Ollama. Available: {models}"
        except Exception as e:
            return False, f"Cannot reach Ollama: {e}"

    async def smoke_test(self, attempts: int = 2) -> tuple[bool, str]:
        """
        Perform a smoke test with a small content image so we can tell the
        difference between a vision-capable model and a text-only one.

        Vision-capable Ollama models occasionally fail the first call after a
        long idle (model swap, VRAM contention). We retry once with a short
        wait so a transient HTTP 500 doesn't disable critique for the run.
        Override with VISION_DISABLE_SMOKE_TEST=1 to skip entirely (trust the
        capability metadata reported by Ollama).
        """
        if os.environ.get("VISION_DISABLE_SMOKE_TEST", "").lower() in ("1", "true", "yes"):
            return True, "Smoke test skipped (VISION_DISABLE_SMOKE_TEST set)"

        last_msg = ""
        for attempt in range(1, attempts + 1):
            ok, msg = await self._smoke_test_once()
            last_msg = msg
            if ok:
                if attempt > 1:
                    return True, f"{msg} (succeeded on attempt {attempt})"
                return True, msg
            # Brief wait before retry — gives Ollama time to recover from VRAM swap
            import asyncio as _asyncio
            await _asyncio.sleep(1.5)
        return False, last_msg

    async def _smoke_test_once(self) -> tuple[bool, str]:
        """Single smoke-test attempt.

        Sends a 16x16 red square and asks for the color. A model that genuinely
        sees the image will say something close to "red". A non-vision model will
        either refuse or guess randomly.
        """
        # 16x16 solid red PNG (PIL-generated, base64-encoded once)
        red_png_b64 = (
            "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAAI0lEQVR4nGO8"
            "IyfHQApgIkk1w6gG4gATkergYFQDMYDkUAIAjEsBOCOwN18AAAAASUVORK5CYII="
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a vision assistant. Look at the image and answer the question literally in one word."
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What color is this square? One word."},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{red_png_b64}"}},
                ],
            },
        ]

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "messages": messages,
                        "temperature": 0.0,
                        "max_tokens": 20,
                        "stream": False,
                    },
                )
                if response.status_code != 200:
                    return False, f"Smoke test failed: HTTP {response.status_code} - {response.text[:200]}"

                data = response.json()
                text = (data["choices"][0]["message"]["content"] or "").lower()
                # Accept anything that names red. Treat refusals / "I can't see images" / unrelated answers as failure.
                if any(token in text for token in ("red", "crimson", "scarlet", "ruby")):
                    return True, f"Smoke test passed (model identified red square): '{text.strip()[:80]}'"
                if any(token in text for token in ("can't see", "cannot see", "no image", "don't see", "not able")):
                    return False, f"Smoke test failed: model says it cannot see images — '{text.strip()[:120]}'"
                # Ambiguous response — still allow but flag
                return True, f"Smoke test ambiguous (allowing): '{text.strip()[:120]}'"
        except Exception as e:
            return False, f"Smoke test failed: {e}"

    async def critique(
        self,
        render_paths: dict[str, str],
        user_intent: str,
        geometry_stats: Optional[dict] = None,
        plan: Optional[DesignPlan] = None,
        recipe_context: str = "",
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

        # Build user message with images (plan included so the verifier checks the same checklist the generator was given)
        user_text = _build_vision_user_prompt(
            user_intent,
            geometry_stats,
            plan=plan,
            recipe_context=recipe_context,
        )
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
                        # Larger budget — the feature checklist alone can be 1k tokens
                        # for multi-component designs, and we'd rather waste a few
                        # tokens than silently drop a critique because of truncation.
                        "max_tokens": 4096,
                        "stream": False,
                    },
                )

                if response.status_code != 200:
                    return VisionCritiqueResult(
                        success=False,
                        message=f"Vision model API error: HTTP {response.status_code} — {response.text[:300]}",
                    )

                data = response.json()
                msg_obj = data["choices"][0]["message"]
                raw_text = msg_obj.get("content") or ""
                # Same channel-confusion as code generation: vision models built on
                # qwen3.x sometimes emit JSON in the reasoning channel. Fall back
                # to combining channels so the parser can recover.
                reasoning_text = msg_obj.get("reasoning") or msg_obj.get("reasoning_content") or ""
                if reasoning_text and not raw_text:
                    raw_text = reasoning_text
                elif reasoning_text:
                    raw_text = raw_text + "\n" + reasoning_text

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

        # Parse JSON response — primary path is strict JSON; fallback path scrapes
        # the (possibly truncated) text for the must-have signals so we still
        # trigger repair when the verifier saw something wrong but ran out of
        # tokens before finishing the JSON.
        try:
            parsed = _parse_json_response(raw_text)
        except (json.JSONDecodeError, ValueError):
            parsed = _fallback_parse_critique(raw_text)
            if not parsed:
                return VisionCritiqueResult(
                    success=False,
                    message="Failed to parse vision model response (no usable signal recovered)",
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
