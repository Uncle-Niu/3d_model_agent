"""Replay the exact failed CadQuery source from chat thread
`20260519-015545-50458ca5` through the new floor-violation guards and confirm
the systemic fix now catches the bug that previously scored 0.95 / matches_intent.

The original run shipped a 30-degree laptop tray with the VESA plate that
ended up 77 mm below the build plate (bbox_z_min_mm = -77.51). The vision
critic returned 0 errors / 0 warnings, the plan-conformance check passed,
and the mechanical scale fix masked the AABB overflow. This script proves
that with the new checks:

  1. `try_auto_scale_for_fit` refuses to mask the layout bug.
  2. `check_plan_conformance` fires `below the build plate`.
  3. `_deterministic_pre_flags` injects a hard `weak_contact_patch` error
     into the vision-critic prompt before the LLM even sees it.

Run: python scripts/verify_floor_check_on_failed_run.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# The exact source the original run produced and shipped as model-005.
FAILED_SOURCE = (
    ROOT
    / "data/projects/20260519-015545-61d3bc05/models/model-005/source.py"
)


def main() -> int:
    print(f"[verify] replaying {FAILED_SOURCE}")
    code = FAILED_SOURCE.read_text(encoding="utf-8")

    # --- Execute the CadQuery to get a real geometry analysis -------------
    from backend.cad.engine import execute_cadquery_code
    from backend.validation.validator import validate_geometry_enhanced
    from backend.domain.models import HardConstraints

    ok, shape, err = execute_cadquery_code(code)
    if not ok:
        print(f"[verify] FAILED — could not execute source: {err}")
        return 1

    constraints = HardConstraints(
        max_x_mm=256.0, max_y_mm=256.0, max_z_mm=256.0,
        min_wall_thickness_mm=1.2, max_file_size_mb=100.0,
    )
    result = validate_geometry_enhanced(shape, constraints)
    stats = result.analysis.to_stats_dict()
    print(f"[verify] measured bbox:        {stats.get('bounding_box')}")
    print(f"[verify] measured bbox_z_min:  {stats.get('bbox_z_min_mm')}")
    print(f"[verify] measured bbox_z_max:  {stats.get('bbox_z_max_mm')}")
    print(f"[verify] measured solid_count: {stats.get('solid_count')}")

    # --- Check 1: mechanical scale-fix refuses ---------------------------
    from backend.cad.engine import try_auto_scale_for_fit
    scaled = try_auto_scale_for_fit(
        code, stats,
        max_x_mm=256.0, max_y_mm=256.0, max_z_mm=256.0,
    )
    if scaled is not None:
        print("[verify] FAILED — try_auto_scale_for_fit still masked the bug")
        return 1
    print("[verify] OK    — try_auto_scale_for_fit refused to mask the layout bug")

    # --- Check 2: plan-conformance fires --------------------------------
    # Build a minimal plan that matches what the original run produced. The
    # critical signal is `physical_use.orientation` mentioning "build plate"
    # and the bbox_z_min stat. The original metadata.json has the plan.
    plan_path = (
        ROOT
        / "data/projects/20260519-015545-61d3bc05/models/model-005/metadata.json"
    )
    metadata = json.loads(plan_path.read_text(encoding="utf-8"))
    plan_data = metadata.get("plan") or {}

    from backend.domain.models import DesignPlan
    plan = DesignPlan.model_validate(plan_data) if plan_data else None
    if plan is None:
        print("[verify] WARN — no plan in metadata, building a synthetic one")
        from backend.domain.models import PhysicalUse, DesignComponent
        plan = DesignPlan(
            summary="Laptop tray with VESA plate",
            components=[
                DesignComponent(name="plate", description="", primitive="box", operation="base"),
                DesignComponent(name="tray", description="", primitive="box", operation="union"),
            ],
            physical_use=PhysicalUse(
                orientation="Printed flat on the build plate with the VESA plate face down on Z=0.",
            ),
        )

    from backend.agent.plan_conformance import check_plan_conformance
    report = check_plan_conformance(plan, stats)
    if report is None:
        print("[verify] FAILED — plan-conformance returned None")
        return 1
    if report.passed:
        print("[verify] FAILED — plan-conformance still passed the broken model")
        print(f"[verify]   reasons: {report.reasons}")
        return 1
    print(f"[verify] OK    — plan-conformance rejected (score={report.score:.2f})")
    for r in report.reasons:
        print(f"[verify]   reason: {r[:160]}")

    floor_reason_present = any("below the build plate" in r.lower() for r in report.reasons)
    if not floor_reason_present:
        print("[verify] FAILED — floor-violation reason was not raised")
        return 1
    print("[verify] OK    — floor-violation reason was raised explicitly")

    # The repair prompt should explain the *rotation* bug, not just scaling.
    critique = report.as_critique()
    if "rotation" not in critique.repair_prompt.lower():
        print("[verify] FAILED — repair prompt does not mention rotation as the root cause")
        print(f"[verify]   repair_prompt: {critique.repair_prompt}")
        return 1
    print("[verify] OK    — repair prompt names rotation as the root cause")

    # --- Check 3: vision critic pre-flag injection ----------------------
    from backend.vision.critic import _build_vision_user_prompt, _deterministic_pre_flags

    flags = _deterministic_pre_flags(stats)
    if not flags:
        print("[verify] FAILED — _deterministic_pre_flags returned empty for a broken model")
        return 1
    if not any(f["issue_type"] == "weak_contact_patch" for f in flags):
        print("[verify] FAILED — weak_contact_patch was not among the pre-flags")
        return 1
    print(f"[verify] OK    — pre-flags raised: {[f['issue_type'] for f in flags]}")

    prompt = _build_vision_user_prompt(
        user_intent="30 degree laptop tray with vesa mount plate on the back",
        geometry_stats=stats,
        plan=plan,
    )
    if "Pre-Detected Hard Failures" not in prompt:
        print("[verify] FAILED — pre-flag block missing from vision prompt")
        return 1
    if "weak_contact_patch" not in prompt:
        print("[verify] FAILED — weak_contact_patch label missing from vision prompt")
        return 1
    print("[verify] OK    — vision prompt now carries the hard-failure pre-flag block")

    print()
    print("[verify] ALL CHECKS PASS — the systemic fix catches the original failure")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
