"""Static-lint tests.

The canonical bug the lint exists to catch is a misused
``.rotate(p1, p2, angle)`` call where ``(p2 - p1)`` is not axis-aligned. This
came up in a real iPhone-holder run: the plan locked the X-axis snippet
``.rotate((0,0,0), (1,0,0), -15)`` but the generator wrote
``.rotate((0, 0, -60), (1, 0, 0), -15)``. The axis line direction is then
``(1, 0, 60)`` — an oblique axis that tilts the backrest along both Y and Z.

These tests cover:
- The bug as it appeared in source.py (with named parameters).
- Auto-correction when the plan locks the intended axis.
- Restraint: no autofix when the LLM's intended axis is ambiguous or doesn't
  agree with any plan component.
- Negative cases: clean axis-aligned rotations are not flagged; dynamic
  arguments are not flagged.
"""

from __future__ import annotations

import unittest

from backend.cad.static_lint import lint_cadquery_source
from backend.domain.models import DesignComponent, DesignPlan, Rotation


def _plan_with_backrest_rotation() -> DesignPlan:
    """Mirrors the real plan that triggered the iPhone-holder rotation bug."""
    return DesignPlan(
        summary="iPhone holder",
        overall_dimensions_mm=[120.0, 85.0, 130.0],
        components=[
            DesignComponent(
                name="base_plate",
                description="base",
                primitive="box",
                operation="base",
                dimensions={"length": 120, "width": 85, "height": 8},
            ),
            DesignComponent(
                name="backrest",
                description="tilted back panel",
                primitive="box",
                operation="union",
                dimensions={"length": 120, "width": 8, "height": 120},
                rotation=Rotation(axis="X", angle_deg=-15.0, intent="tilt back"),
            ),
        ],
    )


class TestRotateObliqueAxis(unittest.TestCase):

    def test_iphone_holder_bug_is_detected_and_autofixed(self):
        """The exact source.py that shipped — minus everything except the
        offending rotate line — should be auto-corrected to the plan's locked
        axis."""
        source = (
            "import cadquery as cq\n"
            "\n"
            "backrest_height_mm = 120.0\n"
            "backrest_angle_deg = 15.0\n"
            "\n"
            "backrest = (\n"
            "    cq.Workplane(\"XY\")\n"
            "    .box(120, 8, backrest_height_mm)\n"
            "    .rotate((0, 0, -backrest_height_mm/2), (1, 0, 0), -backrest_angle_deg)\n"
            "    .translate((0, -42.5, 8 + backrest_height_mm/2))\n"
            ")\n"
            "\n"
            "result = backrest\n"
        )
        report = lint_cadquery_source(source, plan=_plan_with_backrest_rotation())

        # One finding, marked info (auto-corrected), no blocking errors.
        self.assertEqual(len(report.findings), 1)
        f = report.findings[0]
        self.assertEqual(f.code, "rotate_oblique_axis")
        self.assertTrue(f.autofix_applied)
        self.assertFalse(report.has_blocking)

        # The rewritten source must contain the canonical X-axis snippet
        # (two points on a line parallel to X). The original oblique call
        # must be gone.
        self.assertIn(".rotate((0, 0, -60), (1, 0, -60), -15)", report.rewritten_source)
        self.assertNotIn("(0, 0, -backrest_height_mm/2), (1, 0, 0)", report.rewritten_source)

    def test_pure_axis_rotation_is_not_flagged(self):
        """Clean axis-aligned rotations must not trip the lint."""
        source = (
            "import cadquery as cq\n"
            "\n"
            "result = cq.Workplane('XY').box(10, 10, 10).rotate((0, 0, 0), (1, 0, 0), -20)\n"
        )
        report = lint_cadquery_source(source, plan=_plan_with_backrest_rotation())
        self.assertEqual(report.findings, [])
        self.assertEqual(report.rewritten_source, source)

    def test_dynamic_arguments_are_skipped(self):
        """When p1/p2 cannot be statically evaluated we must NOT flag — false
        positives here would burn LLM repair cycles on perfectly legitimate
        code that happens to compute the axis from a runtime function."""
        source = (
            "import cadquery as cq\n"
            "import math\n"
            "\n"
            "def axis_for(theta):\n"
            "    return (math.cos(theta), 0, math.sin(theta))\n"
            "\n"
            "result = cq.Workplane('XY').box(10, 10, 10).rotate((0, 0, 0), axis_for(0.1), 30)\n"
        )
        report = lint_cadquery_source(source, plan=None)
        self.assertEqual(report.findings, [])

    def test_blocking_when_no_plan_rotation_to_anchor_on(self):
        """Without a plan-locked rotation, the lint reports the bug as a
        blocking error and surfaces a canonical suggestion — but does NOT
        auto-rewrite (we don't know which axis the LLM meant)."""
        source = (
            "import cadquery as cq\n"
            "\n"
            "panel = (cq.Workplane('XY').box(10, 5, 80)\n"
            "         .rotate((0, 0, -40), (1, 0, 0), -15))\n"
            "result = panel\n"
        )
        report = lint_cadquery_source(source, plan=None)
        self.assertEqual(len(report.findings), 1)
        finding = report.findings[0]
        self.assertEqual(finding.severity, "error")
        self.assertFalse(finding.autofix_applied)
        self.assertTrue(report.has_blocking)
        self.assertIsNotNone(finding.suggested_fix)
        # p2=(1,0,0) is axis-aligned — that's the LLM's "I thought p2 was
        # the direction" smoking gun. The suggestion picks X through the
        # given pivot (0,0,-40): the second point is (1, 0, -40).
        self.assertIn(".rotate((0, 0, -40), (1, 0, -40), -15)", finding.suggested_fix)

    def test_dominant_axis_fallback_when_p2_is_not_axis_aligned(self):
        """When p2 isn't itself an axis unit vector AND no plan exists, the
        lint falls back to the dominant component of (p2 - p1) for the
        suggestion. Still report-only — never auto-fix without a plan
        anchor."""
        source = (
            "import cadquery as cq\n"
            "\n"
            "thing = (cq.Workplane('XY').box(10, 5, 80)\n"
            "         .rotate((0, 0, 0), (0.1, 0, 5), -15))\n"
            "result = thing\n"
        )
        report = lint_cadquery_source(source, plan=None)
        self.assertEqual(len(report.findings), 1)
        finding = report.findings[0]
        self.assertFalse(finding.autofix_applied)
        # Dominant of (0.1, 0, 5) is Z → suggested Z-axis snippet.
        self.assertIn("(0, 0, 1)", finding.suggested_fix)

    def test_no_autofix_when_plan_axis_disagrees_with_dominant(self):
        """If the plan says X but the LLM's direction is clearly Z-dominant,
        we leave it as a blocking error so a human-readable repair message
        gets sent to the LLM. Auto-rewriting to X could silently change a
        design the LLM correctly intended to put on Z."""
        source = (
            "import cadquery as cq\n"
            "\n"
            "backrest = (cq.Workplane('XY').box(10, 5, 80)\n"
            "            .rotate((0, 0, 0), (0.1, 0, 5), -15))\n"
            "result = backrest\n"
        )
        report = lint_cadquery_source(source, plan=_plan_with_backrest_rotation())
        self.assertEqual(len(report.findings), 1)
        finding = report.findings[0]
        self.assertFalse(finding.autofix_applied)
        self.assertEqual(finding.severity, "error")

    def test_unmatched_variable_name_does_not_autofix(self):
        """If the variable holding the rotated body shares no name tokens with
        any plan component, we cannot safely pick which plan rotation to
        anchor on — even though one exists. Report-only."""
        source = (
            "import cadquery as cq\n"
            "\n"
            "thingamajig = (cq.Workplane('XY').box(10, 5, 80)\n"
            "               .rotate((0, 0, -10), (1, 0, 0), -15))\n"
            "result = thingamajig\n"
        )
        report = lint_cadquery_source(source, plan=_plan_with_backrest_rotation())
        # The dominant axis falls back to a suggestion-only finding.
        self.assertEqual(len(report.findings), 1)
        self.assertFalse(report.findings[0].autofix_applied)

    def test_multiple_rotate_calls_each_handled(self):
        """Two independent oblique-rotate bugs should each get their own
        finding and (if applicable) auto-fix without offset-shifting issues."""
        source = (
            "import cadquery as cq\n"
            "\n"
            "backrest = (cq.Workplane('XY').box(10, 5, 80)\n"
            "            .rotate((0, 0, -40), (1, 0, 0), -15))\n"
            "base_plate = (cq.Workplane('XY').box(50, 30, 5)\n"
            "              .rotate((0, 0, 0), (1, 0, 0), -5))\n"
            "result = backrest.union(base_plate)\n"
        )
        # Only the backrest call is oblique; base_plate is clean. Plan locks
        # an X rotation for backrest.
        report = lint_cadquery_source(source, plan=_plan_with_backrest_rotation())
        self.assertEqual(len(report.findings), 1)
        self.assertTrue(report.findings[0].autofix_applied)
        # The clean rotate must still appear unchanged.
        self.assertIn(".rotate((0, 0, 0), (1, 0, 0), -5)", report.rewritten_source)
        # The oblique one is rewritten.
        self.assertIn(".rotate((0, 0, -40), (1, 0, -40), -15)", report.rewritten_source)

    def test_syntax_error_does_not_crash(self):
        """A syntax error should be left to ``validate_cadquery_code`` — the
        lint must return an empty report, not raise."""
        report = lint_cadquery_source("import cadquery as cq\nresult = (", plan=None)
        self.assertEqual(report.findings, [])
        self.assertEqual(report.rewritten_source, "import cadquery as cq\nresult = (")


if __name__ == "__main__":
    unittest.main()
