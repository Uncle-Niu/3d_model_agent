"""
Backend-wide configuration constants.

Single source of truth for cross-cutting defaults — model identifiers in
particular. Everywhere that needs a default model name should import from
here rather than hardcoding the literal, so flipping the default takes one
edit instead of seven.

Runtime overrides via env vars (`LLM_MODEL`, `VISION_MODEL`, …) still take
precedence; these constants are only the fallback when nothing is set.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Model identifiers
# ---------------------------------------------------------------------------

# Main agent model. Used for planning, code generation, repair, subject
# detection, and as the chain-head for local-LLM recall (so it's already warm
# in VRAM when recall starts).
DEFAULT_LLM_MODEL: str = "qwen3.6:27b"

# Vision-capable models the agent will auto-swap to if the configured
# `VISION_MODEL` isn't installed. Order matters: try gemma3 first (smallest
# vision-specialist), then gemma4 (multimodal), then fall back to the main
# qwen if it's installed and proven vision-capable.
VISION_FALLBACK_MODELS: tuple[str, ...] = ("gemma4:31b", "nemotron3:33b")


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------


def resolve_llm_model() -> str:
    """Return the configured LLM model, or `DEFAULT_LLM_MODEL` if unset."""
    return os.environ.get("LLM_MODEL", DEFAULT_LLM_MODEL)


def resolve_vision_model() -> str:
    """Return the configured vision model.

    Resolution order: `VISION_MODEL` → `LLM_MODEL` → `DEFAULT_LLM_MODEL`.
    The fall-through to `LLM_MODEL` is intentional: most setups use one
    model for both roles, and forcing users to set `VISION_MODEL` separately
    would be a footgun.
    """
    return os.environ.get("VISION_MODEL", resolve_llm_model())
