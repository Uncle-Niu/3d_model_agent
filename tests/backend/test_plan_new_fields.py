"""Tests for the new plan-schema fields: physical_use, feature_decisions,
connections, and per-component spec_source. Also covers the recipe gate's
new behavior: an explicit "not needed" feature decision should suppress the
corresponding missing-feature complaint.
"""

from __future__ import annotations

import unittest

from backend.cad.recipes import (
    RECIPES,
    validate_plan_against_recipes,
)
from backend.domain.models import (
    Connection,
    DesignComponent,
    DesignPlan,
    FeatureDecision,
    PhysicalUse,
)
from backend.models.llm_service import parse_design_plan, plan_to_prompt_text


def _bracket_recipe():
    for r in RECIPES:
        if r.recipe_id == "bracket_or_mount":
            return r
    raise RuntimeError("bracket recipe missing")


class TestPlanSchemaParsing(unittest.TestCase):
    def test_parse_physical_use_block(self):
        raw = """
        <thinking>some reasoning</thinking>
        <physical_use>
          <orientation>flat on a desk, base down</orientation>
          <contact_surfaces>bottom face touches desk</contact_surfaces>
          <applied_forces>200g phone pulling forward on lip</applied_forces>
          <use_cycle>insert phone, remove daily</use_cycle>
        </physical_use>
        <design_plan>
          <summary>phone stand</summary>
          <components>
            <component>
              <name>base</name>
              <description>base plate</description>
              <primitive>box</primitive>
              <operation>base</operation>
              <spec_source>inferred</spec_source>
            </component>
          </components>
        </design_plan>
        """
        plan = parse_design_plan(raw)
        self.assertIsNotNone(plan.physical_use)
        self.assertEqual(plan.physical_use.orientation, "flat on a desk, base down")
        self.assertIn("200g phone", plan.physical_use.applied_forces)
        self.assertEqual(plan.components[0].spec_source, "inferred")

    def test_parse_feature_decisions_attribute_style(self):
        raw = """
        <feature_decisions>
          <decision feature="fasteners_or_mounting_holes" needed="false">stand sits on a desk, no attachment</decision>
          <decision feature="retention_geometry" needed="true">needs a front lip to hold the phone</decision>
        </feature_decisions>
        <design_plan>
          <summary>x</summary>
        </design_plan>
        """
        plan = parse_design_plan(raw)
        self.assertEqual(len(plan.feature_decisions), 2)
        by_name = {d.feature: d for d in plan.feature_decisions}
        self.assertFalse(by_name["fasteners_or_mounting_holes"].needed)
        self.assertTrue(by_name["retention_geometry"].needed)
        self.assertIn("front lip", by_name["retention_geometry"].rationale)

    def test_parse_connections(self):
        raw = """
        <design_plan>
          <summary>two-piece bracket</summary>
          <connections>
            <connection from="base" to="upright" kind="union">welded together</connection>
            <connection from="lid" to="body" kind="press_fit">0.2mm clearance</connection>
          </connections>
        </design_plan>
        """
        plan = parse_design_plan(raw)
        self.assertEqual(len(plan.connections), 2)
        self.assertEqual(plan.connections[0].kind, "union")
        self.assertEqual(plan.connections[1].kind, "press_fit")
        self.assertEqual(plan.connections[1].from_part, "lid")

    def test_old_plan_without_new_blocks_still_parses(self):
        # Schema must stay backwards-compatible with planners that haven't
        # learned to emit the new blocks yet.
        raw = """
        <design_plan>
          <summary>plain plan</summary>
          <components>
            <component>
              <name>body</name>
              <description>just a box</description>
              <primitive>box</primitive>
              <operation>base</operation>
            </component>
          </components>
          <key_features>
            <feature>flat top</feature>
          </key_features>
        </design_plan>
        """
        plan = parse_design_plan(raw)
        self.assertEqual(plan.summary, "plain plan")
        self.assertIsNone(plan.physical_use)
        self.assertEqual(plan.feature_decisions, [])
        self.assertEqual(plan.connections, [])
        # spec_source default is empty string when missing
        self.assertEqual(plan.components[0].spec_source, "")


class TestPlanToPromptTextIncludesNewSections(unittest.TestCase):
    def test_physical_use_appears_in_prompt(self):
        plan = DesignPlan(
            summary="x",
            components=[DesignComponent(name="b", description="base", primitive="box", operation="base")],
            physical_use=PhysicalUse(
                orientation="flat",
                applied_forces="200g phone load",
            ),
        )
        text = plan_to_prompt_text(plan)
        self.assertIn("Real-world use", text)
        self.assertIn("200g phone load", text)

    def test_feature_decisions_split_included_excluded(self):
        plan = DesignPlan(
            summary="x",
            components=[DesignComponent(name="b", description="base", primitive="box", operation="base")],
            feature_decisions=[
                FeatureDecision(feature="retention_geometry", needed=True, rationale="needs lip"),
                FeatureDecision(feature="fasteners_or_mounting_holes", needed=False, rationale="no attachment"),
            ],
        )
        text = plan_to_prompt_text(plan)
        self.assertIn("does NOT need", text)
        self.assertIn("DOES need", text)
        self.assertIn("retention_geometry", text)
        self.assertIn("fasteners_or_mounting_holes", text)


class TestRecipeGateHonorsFeatureDecisions(unittest.TestCase):
    def test_no_decisions_means_strict_validation_as_before(self):
        # Bracket recipe wants mounting holes. A bare plan misses them.
        plan = DesignPlan(
            summary="plain bracket",
            components=[
                DesignComponent(name="plate", description="plate", primitive="box", operation="base"),
                DesignComponent(name="upright", description="upright", primitive="box", operation="union"),
                DesignComponent(name="rib", description="rib", primitive="box", operation="union"),
            ],
        )
        report = validate_plan_against_recipes(plan, [_bracket_recipe()])
        # Should flag mounting holes as missing.
        joined = " ".join(report.missing_features) + " " + " ".join(report.missing_negative_space)
        self.assertIn("mounting holes", joined.lower())

    def test_explicit_no_fasteners_suppresses_mounting_hole_complaint(self):
        plan = DesignPlan(
            summary="bracket that just sits on a shelf",
            components=[
                DesignComponent(name="plate", description="plate", primitive="box", operation="base"),
                DesignComponent(name="upright", description="upright", primitive="box", operation="union"),
                DesignComponent(name="rib", description="rib", primitive="box", operation="union"),
            ],
            feature_decisions=[
                FeatureDecision(
                    feature="fasteners_or_mounting_holes",
                    needed=False,
                    rationale="this bracket isn't bolted to anything; it rests on a shelf",
                ),
            ],
        )
        report = validate_plan_against_recipes(plan, [_bracket_recipe()])
        joined = " ".join(report.missing_features) + " " + " ".join(report.missing_negative_space)
        self.assertNotIn("mounting holes", joined.lower())
        # The fastener-related negative-space feature should also be suppressed.
        self.assertNotIn("through holes", joined.lower())


if __name__ == "__main__":
    unittest.main()
