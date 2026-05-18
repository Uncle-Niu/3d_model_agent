"""Plan-conformance gate tests.

The motivating real-world failure was a floating backrest: the rotated panel
sat within the planned bounding box but was not unioned to the base, so the
result was a 3-solid pile instead of a single fused holder. The original
solid-count check was gated on the bbox check also failing, which masked
exactly this case. The new "disconnected-sub-shape" branch fires on its own.
"""

import unittest

from backend.agent.plan_conformance import check_plan_conformance
from backend.domain.models import Connection, DesignComponent, DesignPlan


def _make_plan_with_union_connections(num_components: int = 4, num_unions: int = 3) -> DesignPlan:
    """A typical single-body plan: several `union` components fused into one part."""
    components = [
        DesignComponent(
            name=f"part_{i}",
            description="component",
            primitive="box",
            operation="union" if i > 0 else "base",
            dimensions={"length": 10, "width": 10, "height": 10},
        )
        for i in range(num_components)
    ]
    connections = [
        Connection(
            from_part="part_0",
            to_part=f"part_{i}",
            kind="union",
            description="fused",
        )
        for i in range(1, num_unions + 1)
    ]
    return DesignPlan(
        summary="Multi-part fused holder",
        overall_dimensions_mm=[180.0, 100.0, 98.0],
        components=components,
        connections=connections,
    )


class TestDisconnectedSolidsCheck(unittest.TestCase):

    def _stats(self, bbox, solid_count):
        return {
            "bbox_x_mm": bbox[0],
            "bbox_y_mm": bbox[1],
            "bbox_z_mm": bbox[2],
            "solid_count": solid_count,
        }

    def test_flags_multiple_solids_when_plan_unions_them(self):
        # The exact failure pattern from the iPhone-holder log: bbox sits
        # comfortably inside slack (192×108×137 vs planned 180×100×98) but
        # the result is 3 disconnected lumps because one component is
        # floating above the base.
        plan = _make_plan_with_union_connections()
        stats = self._stats(bbox=(192.0, 108.0, 137.0), solid_count=3)
        report = check_plan_conformance(plan, stats)
        self.assertIsNotNone(report)
        self.assertFalse(report.passed)
        joined = " ".join(report.reasons).lower()
        self.assertIn("disconnected", joined)
        self.assertIn("floating", joined)

    def test_single_solid_result_passes(self):
        # The success case: same plan, but every union actually fused.
        plan = _make_plan_with_union_connections()
        stats = self._stats(bbox=(180.0, 100.0, 98.0), solid_count=1)
        report = check_plan_conformance(plan, stats)
        self.assertIsNotNone(report)
        self.assertTrue(report.passed)

    def test_no_union_connections_allows_multi_solid(self):
        # Plans without explicit union connections (e.g. user wants a true
        # multi-part output) must not be flagged. The check keys off the
        # planner's stated union intent, not just the component count.
        plan = DesignPlan(
            summary="Two separate stands",
            overall_dimensions_mm=[180.0, 100.0, 98.0],
            components=[
                DesignComponent(name="stand_a", description="", primitive="box", operation="base"),
                DesignComponent(name="stand_b", description="", primitive="box", operation="base"),
            ],
            connections=[],  # no union intent
        )
        stats = self._stats(bbox=(180.0, 100.0, 98.0), solid_count=2)
        report = check_plan_conformance(plan, stats)
        # Still passes — multi-solid is acceptable when the plan didn't ask
        # for a union.
        self.assertIsNotNone(report)
        self.assertTrue(report.passed)


class TestBboxCheckStillFires(unittest.TestCase):
    """Confirm the existing bbox-based reasoning was not weakened."""

    def test_oversized_bbox_still_flagged(self):
        plan = _make_plan_with_union_connections()
        # 2x oversized → flagged by the bbox path.
        stats = {
            "bbox_x_mm": 400.0, "bbox_y_mm": 220.0, "bbox_z_mm": 220.0,
            "solid_count": 1,
        }
        report = check_plan_conformance(plan, stats)
        self.assertIsNotNone(report)
        self.assertFalse(report.passed)


if __name__ == "__main__":
    unittest.main()
