"""Debug the vision-critique parser using model-004's renders as input.

The chat log
data/projects/20260516-063707-f6745082/chat_threads/20260516-063707-f314583f.json
ended with "Failed to parse vision model response (no usable signal recovered)"
on model-004. This script re-runs the vision critic against those renders,
captures the raw model response, attempts to parse it through both the strict
and fallback paths, and prints exactly where parsing breaks. Use it as a
regression target while hardening the parser.

Usage:
    python scripts/debug_vision_parse.py
    python scripts/debug_vision_parse.py --no-call    # skip the LLM call; parse a saved fixture
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.domain.models import DesignPlan, DesignComponent  # noqa: E402
from backend.vision.critic import (  # noqa: E402
    VisionCritic,
    _fallback_parse_critique,
    _parse_json_response,
)

# Re-create the design plan from the chat thread so the vision critique gets
# the same context it had in the failed run.
PLAN = DesignPlan(
    summary=(
        "Flat-printable desk stand for iPhone 14 Pro Max with angled back "
        "support, side guides, front retention lip, cable slot, corner "
        "mounting holes, and reinforced gussets."
    ),
    overall_dimensions_mm=(180.0, 110.0, 160.0),
    components=[
        DesignComponent(
            name="base_plate", description="Primary load-bearing foundation",
            primitive="box", dimensions={"length": 180.0, "width": 110.0, "height": 4.0},
            position=(0.0, 0.0, 2.0), operation="base",
        ),
        DesignComponent(
            name="back_support_wall", description="Angled rear wall",
            primitive="box", dimensions={"length": 90.0, "width": 3.0, "height": 160.0},
            position=(0.0, -53.5, 4.0), operation="union",
        ),
    ],
    key_features=[
        "Flat 180x110x4 mm base plate for stable desk placement",
        "Two 85 mm tall side walls with 80 mm inner spacing for 77.6 mm phone width",
        "160 mm tall back wall angled 15° from vertical for ergonomic viewing",
        "3 mm high front retention lip preventing forward slide",
        "Open top cavity explicitly cut to guarantee phone insertion clearance",
        "12x10 mm rear cable management slot",
        "Four 4.5 mm corner mounting holes",
        "Triangular gussets reinforcing all wall-to-base junctions",
        "2 mm fillets on all sharp edges for strength and print reliability",
    ],
)

GEOMETRY_STATS = {
    "bounding_box": "201.1 × 111.8 × 215.2 mm",
    "solid_count": 5,
    "face_count": 88,
    "edge_count": 246,
    "is_closed_shell": True,
    "small_feature_count": 1,
    "sharp_corner_count": 3,
}

MODEL_DIR = (
    ROOT
    / "data" / "projects" / "20260516-063707-f6745082"
    / "models" / "model-004"
)
RENDER_DIR = MODEL_DIR / "renders"
FIXTURE_PATH = ROOT / "scratch" / "vision_raw_response.txt"


def load_render_paths() -> dict[str, str]:
    return {
        view: str(RENDER_DIR / f"render_{view}.png")
        for view in ("iso", "front", "right", "top", "section_x", "section_y")
        if (RENDER_DIR / f"render_{view}.png").exists()
    }


async def call_vision_and_capture() -> str:
    critic = VisionCritic()
    render_paths = load_render_paths()
    if not render_paths:
        raise SystemExit(f"No render images found under {RENDER_DIR}")
    print(f"[debug] Calling vision critic ({critic.model}) "
          f"with {len(render_paths)} render(s)...")
    result = await critic.critique(
        render_paths,
        "Design a iphone 14 pro max holder so I can watch movie",
        GEOMETRY_STATS,
        plan=PLAN,
    )
    print(f"[debug] critic.success={result.success}")
    print(f"[debug] critic.message={result.message}")
    raw = result.raw_response or ""
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(raw, encoding="utf-8")
    print(f"[debug] saved raw response ({len(raw)} chars) -> {FIXTURE_PATH}")
    return raw


def diagnose(raw: str) -> None:
    print("\n=== raw vision response (first 800 chars) ===")
    print(raw[:800])
    print("... (truncated)" if len(raw) > 800 else "")
    print("\n=== last 400 chars ===")
    print(raw[-400:] if len(raw) > 400 else raw)

    print("\n=== strict parse attempt ===")
    try:
        parsed = _parse_json_response(raw)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"strict parse FAILED: {type(e).__name__}: {e}")
        parsed = None
    if parsed is not None:
        print("strict parse OK — keys:", sorted(parsed.keys()))
        print(json.dumps(parsed, indent=2)[:1000])

    print("\n=== fallback parse attempt ===")
    fb = _fallback_parse_critique(raw)
    if not fb:
        print("fallback parse RECOVERED NOTHING")
    else:
        print("fallback recovered keys:", sorted(fb.keys()))
        print(json.dumps(fb, indent=2)[:600])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--no-call", action="store_true",
        help="Don't call Ollama; parse the saved fixture from scratch/.",
    )
    args = ap.parse_args()

    if args.no_call:
        if not FIXTURE_PATH.exists():
            raise SystemExit(
                f"No fixture at {FIXTURE_PATH}. "
                "Run without --no-call first to capture a real response."
            )
        raw = FIXTURE_PATH.read_text(encoding="utf-8")
    else:
        raw = asyncio.run(call_vision_and_capture())

    if not raw:
        print("[debug] empty response — vision model produced no content "
              "(likely a text-only model that can't see images).")
        return

    diagnose(raw)


if __name__ == "__main__":
    main()
