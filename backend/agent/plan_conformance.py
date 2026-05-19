"""
Plan-conformance gate.

Deterministic, LLM-free check that the rendered geometry actually resembles the
plan the planner produced. The motivating failure: the planner emits a 9-component
iPhone holder (base + 4 walls + cutouts + holes + fillets) with a 45mm-tall back
wall, the code generator emits valid CadQuery that only builds the base plate +
mounting holes, execution succeeds, and — with the vision critic offline — the
pipeline reports success on what is effectively a drilled plate.

This module compares the measured geometry against the plan's overall dimensions
and component count, and returns a structured verdict the orchestrator can fold
into the repair loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..domain.models import CritiqueReport, DesignComponent, DesignPlan, GeometryIssue


# Bbox dimensions are *targets*, not exact specs — a few percent slack absorbs
# legitimate planner-vs-implementation rounding. But a missing tall wall in a
# 45mm-tall design will produce a bbox that's <50% of the target Z, which is
# what we actually want to catch.
BBOX_MIN_FRACTION = 0.75  # measured/target must be >= this on every axis
BBOX_MAX_FRACTION = 1.6   # and <= this (catches "extruded the whole thing 10x")
# Components that don't add new solids (edge modifiers, cuts, patterns applied in-place)
_NON_SOLID_OPS = {"fillet", "chamfer", "shell"}
_ADDITIVE_OPS = {"", "base", "union", "intersect", "pattern"}
# We need at least this many solid bodies vs the plan's count of additive parts.
# We accept fused unions as 1 solid — a base + 4 walls fused together is still
# correct geometry — so the bar is "at least one fused solid OR roughly the
# right count if the generator kept them separate".
SOLID_COUNT_MIN_FRACTION = 0.5

# A part meant to sit on a surface should carry most of its mass low. The
# center of mass should be in the lower half of bbox_z; anything above this
# fraction is top-heavy and likely to tip in real use. Tuned so a plate with
# a moderate vertical feature (typical of stands) still passes, but a vertical
# wall whose base is small or whose body extends below the floor will fail.
TOP_HEAVY_COM_Z_FRACTION = 0.62
# Words in physical_use.orientation that indicate the part is supposed to
# rest on a horizontal surface. Only then does the top-heavy check apply —
# a wall-mount bracket has its own load path and the CoM check is meaningless.
_RESTS_ON_SURFACE_WORDS = (
    "desk", "table", "floor", "ground", "shelf", "counter", "benchtop", "bench",
    "rest", "rests", "sit", "sits", "stand", "stands", "stand on", "set on",
    "place on", "placed on",
    # FDM-print-flat language: the planner often says "Printed flat on the
    # build plate with X face down on Z=0" instead of using desk/table. The
    # build plate IS a horizontal surface for the purposes of the
    # floor-alignment check (no geometry should print below Z=0), so these
    # phrasings should trigger the same check.
    "build plate", "print bed", "z=0", "flat on the",
    "face down", "face-down", "printed flat", "prints flat",
)

# Below-floor tolerance for FDM models. CadQuery placement frequently lands
# objects centered on Z=0 (so bottom = -H/2). The floor-violation check fires
# only when geometry extends measurably below z=0 — small numerical noise from
# fillets / boolean operations is ignored. Tuned so a 5mm-thick base centered
# at z=0 (bottom -2.5mm) is caught — anything inside that magnitude is real
# geometry below the build plate, not numerical jitter.
FLOOR_VIOLATION_TOL_MM = 0.5


@dataclass
class ConformanceReport:
    """Outcome of the plan-vs-render comparison."""
    passed: bool
    score: float                       # 0.0 - 1.0, soft signal for ranking
    reasons: list[str] = field(default_factory=list)   # human-readable failures
    expected_bbox: Optional[tuple[float, float, float]] = None
    measured_bbox: Optional[tuple[float, float, float]] = None
    expected_solids: int = 0
    measured_solids: int = 0

    def as_critique(self) -> CritiqueReport:
        """Wrap the verdict as a CritiqueReport so the existing repair branch
        can consume it without a new code path."""
        issues = [
            GeometryIssue(
                issue_type="plan_mismatch",
                severity="error",
                description=r,
                location_hint="deterministic plan-vs-geometry check",
            )
            for r in self.reasons
        ]
        repair_lines: list[str] = []
        if self.expected_bbox and self.measured_bbox:
            ex, mx = self.expected_bbox, self.measured_bbox
            repair_lines.append(
                f"Target overall size was {ex[0]:.0f} x {ex[1]:.0f} x {ex[2]:.0f} mm but "
                f"the rendered model measures {mx[0]:.0f} x {mx[1]:.0f} x {mx[2]:.0f} mm."
            )
        if self.expected_solids and self.measured_solids < self.expected_solids:
            repair_lines.append(
                f"The plan includes {self.expected_solids} additive component(s), while "
                f"the rendered result reports {self.measured_solids} solid bod"
                f"{'y' if self.measured_solids == 1 else 'ies'}. This is not a request "
                f"to create separate loose parts: for a fused printable assembly, each "
                f"planned body must be present and physically joined into the final part."
            )
        if any("disconnected solids" in r.lower() for r in self.reasons):
            repair_lines.append(
                "A `.union()` only fuses bodies that intersect or share a face. Move the "
                "floating plate/rib/support until its bounding box overlaps the body it "
                "joins, or add a real connector/gusset that bridges the gap. The repaired "
                "single-piece print should normally report `solid_count == 1`."
            )
        if any("below the build plate" in r.lower() for r in self.reasons):
            repair_lines.append(
                "Re-derive every `.rotate(...)` and `.translate(...)` on paper before "
                "writing them — start each body at origin, apply the rotation, then "
                "translate so its lowest point lands at z>=0. If the plan says the tray "
                "tilts UP toward the user, the +Y end must move toward +Z, which is a "
                "POSITIVE rotation around the X axis (`(0,0,0),(1,0,0),+angle`) when the "
                "tray's depth extends in +Y. A negative angle on the same axis sends the "
                "+Y end DOWN through the build plate — that is the bug to fix, not the "
                "symptom to scale away."
            )
        if repair_lines:
            repair_lines.append(
                "Modify the existing component definitions and placements so the rendered "
                "geometry matches the plan, instead of appending cosmetic cuts or scaling "
                "the final result."
            )
        repair_prompt = " ".join(repair_lines) if repair_lines else (
            "The rendered geometry does not match the plan's overall dimensions. "
            "Re-emit code that implements every planned component."
        )
        return CritiqueReport(
            issues=issues,
            overall_printability=self.score,
            suggested_repairs=[repair_prompt],
            confidence=0.95,           # this is a deterministic measurement
            matches_intent=self.passed,
            repair_prompt=repair_prompt,
        )


def _expected_solid_count(components: list[DesignComponent]) -> int:
    """Count plan components that contribute additive solid bodies.

    We exclude pure edge modifiers and subtractive features. Cuts matter, but
    they should be verified by feature/vision checks and bbox changes, not by
    comparing against the number of OCCT solids. Counting holes and cutouts as
    "expected solids" made repair prompts tell the LLM to create extra bodies
    when the actual requirement was one fused printable part.
    """
    count = 0
    for c in components:
        op = (c.operation or "").strip().lower()
        if op in _ADDITIVE_OPS and op not in _NON_SOLID_OPS:
            count += 1
    return count


def check_plan_conformance(
    plan: Optional[DesignPlan],
    geometry_stats: dict,
) -> Optional[ConformanceReport]:
    """Compare measured geometry to the plan. Returns None if we can't make a
    judgment (no plan, no measurements), otherwise a ConformanceReport.

    A None return means "no signal" — caller should not treat it as pass or
    fail. A returned report with `passed=False` means we have positive evidence
    the result is wrong and the caller should trigger a repair.
    """
    if not plan or not geometry_stats:
        return None

    bbox_x = geometry_stats.get("bbox_x_mm")
    bbox_y = geometry_stats.get("bbox_y_mm")
    bbox_z = geometry_stats.get("bbox_z_mm")
    if bbox_x is None or bbox_y is None or bbox_z is None:
        return None

    reasons: list[str] = []
    score = 1.0

    # ------------------------------------------------------------------
    # 1. Overall bbox check
    # ------------------------------------------------------------------
    expected_bbox: Optional[tuple[float, float, float]] = None
    if plan.overall_dimensions_mm and len(plan.overall_dimensions_mm) == 3:
        ex, ey, ez = (float(v) for v in plan.overall_dimensions_mm)
        expected_bbox = (ex, ey, ez)
        # Sort both expected and measured so the planner naming X/Y axes
        # differently from the generator doesn't trigger spurious failures.
        # A plate-vs-tall-design mismatch shows up on the *largest* axis
        # regardless of orientation.
        ex_sorted = sorted([ex, ey, ez])
        meas_sorted = sorted([bbox_x, bbox_y, bbox_z])
        for axis_idx, (e, m) in enumerate(zip(ex_sorted, meas_sorted)):
            if e <= 0:
                continue
            ratio = m / e
            if ratio < BBOX_MIN_FRACTION:
                reasons.append(
                    f"Measured dimension {m:.1f}mm is only {ratio*100:.0f}% of the "
                    f"planned {e:.1f}mm — the model is missing height/depth that the "
                    f"plan calls for (e.g. a tall back wall or vertical support)."
                )
                # The worst axis dominates the score.
                score = min(score, max(0.0, ratio))
            elif ratio > BBOX_MAX_FRACTION:
                reasons.append(
                    f"Measured dimension {m:.1f}mm is {ratio*100:.0f}% of the planned "
                    f"{e:.1f}mm — the model is significantly oversized vs the plan."
                )
                score = min(score, max(0.0, 1.0 / ratio))

    # ------------------------------------------------------------------
    # 2. Solid count check
    # ------------------------------------------------------------------
    expected_solids = _expected_solid_count(plan.components)
    measured_solids = int(geometry_stats.get("solid_count") or 0)
    # We are lenient: a fused single solid is fine as long as bbox passed.
    # Only flag when the plan listed many components AND the result has so few
    # solids that critical geometry (walls, ribs, supports) plausibly went
    # missing rather than being fused.
    if expected_solids >= 4 and measured_solids > 0:
        # If the bbox check already caught a problem, the solid-count signal
        # is corroborating; if not, only flag when we have BOTH a low solid
        # count AND a small bbox to avoid false positives on legitimately
        # fused designs.
        if measured_solids < max(2, int(expected_solids * SOLID_COUNT_MIN_FRACTION)) and reasons:
            reasons.append(
                f"Plan listed {expected_solids} component sub-shapes; result has only "
                f"{measured_solids} solid bod{'y' if measured_solids == 1 else 'ies'}."
            )
            score = min(score, 0.4)

    # ------------------------------------------------------------------
    # 3. Disconnected-sub-shape check
    # ------------------------------------------------------------------
    # When the plan explicitly connects its components via `union`, the intent
    # is a single fused part. Any result with >1 solid means at least one
    # sub-shape didn't actually intersect the body it was supposed to join —
    # the classic "floating backrest above the base plate" failure mode. We
    # catch this independently of the bbox check: a tilted panel can sit in
    # the right bounding box while being completely detached.
    union_connections = sum(
        1 for c in (plan.connections or [])
        if (c.kind or "").strip().lower() == "union"
    )
    if union_connections >= 1 and measured_solids >= 2:
        reasons.append(
            f"Rendered model contains {measured_solids} disconnected solids; the plan "
            f"calls for them to be fused via {union_connections} union connection(s). "
            f"At least one component is floating instead of being joined to the body — "
            f"verify each component's position so its bounding box actually overlaps "
            f"the part it should union with."
        )
        score = min(score, 0.3)

    # If the plan listed many components but emitted ZERO connection tags,
    # the planner skipped its joinery contract — the code generator then
    # routinely builds half the bodies and leaves the rest out (the
    # "12 planned components, 1 measured solid" failure). Catch it here so
    # the bbox path isn't the only safety net (bbox is silent when
    # `overall_dimensions_mm` is missing, which often co-occurs).
    if (
        expected_solids >= 4
        and measured_solids == 1
        and not (plan.connections or [])
    ):
        reasons.append(
            f"Plan describes {expected_solids} additive components but did not declare "
            f"any `<connection>` tags, and the rendered geometry collapsed to a single "
            f"solid. Either the planner forgot to declare joinery (every union/cut "
            f"between components needs a connection entry) or the code generator "
            f"emitted only one body. Re-emit the plan with explicit `<connection>` "
            f"tags AND verify each planned component appears in the source."
        )
        score = min(score, 0.4)

    # ------------------------------------------------------------------
    # 4a. Build-plate floor violation (universal — applies to every FDM
    #     print regardless of plan text). Geometry that extends measurably
    #     below z=0 will collide with the build plate when sliced. The
    #     classic trigger is a wrongly-signed rotation that tips a tray
    #     downward instead of upward (e.g. `.rotate((0,0,0),(1,0,0),-30)`
    #     applied to a body whose origin sits at +Y), leaving the front of
    #     the tray ~80mm below the print bed. The vision critic frequently
    #     ignores this — bbox_z_min is the unambiguous signal.
    #
    #     We trust this signal more than any planner keyword check because
    #     no valid FDM design intentionally places solid geometry below the
    #     build plate. The only sustainable exception is wall-mount parts
    #     that the planner placed in their "in use" pose rather than their
    #     print pose — handled below.
    # ------------------------------------------------------------------
    z_min = geometry_stats.get("bbox_z_min_mm")
    pu = plan.physical_use
    orientation_text = (getattr(pu, "orientation", "") or "").lower() if pu else ""
    contact_text = (getattr(pu, "contact_surfaces", "") or "").lower() if pu else ""
    # When the planner explicitly states this is a wall-mount / hanging
    # part shown in its in-use pose, the floor check is meaningless — the
    # part doesn't print in that orientation. Detect the small set of
    # phrasings that signal "not laid flat for printing".
    _IN_USE_POSE_WORDS = (
        "wall-mount", "wall mount", "hangs", "hung", "suspended",
        "ceiling", "mounted vertically",
    )
    is_in_use_pose = any(
        w in orientation_text or w in contact_text for w in _IN_USE_POSE_WORDS
    )
    if (
        z_min is not None
        and z_min < -FLOOR_VIOLATION_TOL_MM
        and not is_in_use_pose
    ):
        reasons.append(
            f"Geometry extends {abs(z_min):.1f}mm BELOW the build plate "
            f"(bbox_z_min = {z_min:.1f}mm). The slicer cannot print solid "
            f"matter below Z=0. This is almost always a sign that a "
            f"`.rotate(...)` flipped a component the wrong way (e.g. tilted "
            f"the tray downward at the front instead of upward) or that a "
            f"body was placed at z=0 center without lifting it onto the "
            f"base. Translate every component up by half its height so its "
            f"bottom sits at z>=0, AND verify the rotation sign tilts the "
            f"part in the direction the plan describes."
        )
        # A big below-floor offset is geometry-breaking — drop the score
        # hard so this iteration cannot beat a properly-grounded one.
        score = min(score, 0.2)

    # ------------------------------------------------------------------
    # 4b. Top-heavy / floor-alignment check (only when the plan says the
    #     part rests on a horizontal surface)
    # ------------------------------------------------------------------
    # Generic across product categories: applies to anything the planner
    # described as sitting on a desk/table/shelf. Catches the real failure
    # where the LLM centers a backrest at z=0 so half of it extends below
    # the base plate, leaving no flat contact patch — independent of bbox
    # match. Skipped silently for wall-mount, hanging, or hand-held parts.
    rests_on_surface = any(
        w in orientation_text or w in contact_text
        for w in _RESTS_ON_SURFACE_WORDS
    )
    com_z = geometry_stats.get("center_of_mass_z")
    z_max = geometry_stats.get("bbox_z_max_mm")
    if (
        rests_on_surface
        and com_z is not None
        and z_min is not None
        and z_max is not None
        and z_max > z_min
    ):
        # CoM as a fraction of bbox_z, measured from the part's actual
        # bottom face. Works for parts placed at z=0, parts centered at
        # z=0, and parts translated arbitrarily — we just measure relative
        # to bbox_z_min. >TOP_HEAVY_COM_Z_FRACTION means the mass is too
        # high to be a stable desk part.
        com_fraction = (com_z - z_min) / (z_max - z_min)
        if com_fraction > TOP_HEAVY_COM_Z_FRACTION:
            reasons.append(
                f"Plan describes a part that rests on a horizontal surface, but "
                f"the rendered center of mass sits {com_fraction*100:.0f}% up the "
                f"part's height (above the {TOP_HEAVY_COM_Z_FRACTION*100:.0f}% "
                f"top-heavy threshold). The part will tip or has tall geometry "
                f"sitting above a too-small base. Ensure the bottom of EVERY "
                f"component is aligned to the same floor Z and the footprint is "
                f"wider than the part is tall."
            )
            score = min(score, 0.4)

    passed = len(reasons) == 0
    return ConformanceReport(
        passed=passed,
        score=score if passed else min(score, 0.4),
        reasons=reasons,
        expected_bbox=expected_bbox,
        measured_bbox=(bbox_x, bbox_y, bbox_z),
        expected_solids=expected_solids,
        measured_solids=measured_solids,
    )
