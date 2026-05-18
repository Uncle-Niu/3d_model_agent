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
    Rotation,
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

    def test_rotation_renders_with_precomputed_rotate_snippet(self):
        # The whole point of the structured rotation field: the LLM gets a
        # paste-ready .rotate(...) call instead of having to translate
        # "around X" into (1,0,0) itself.
        plan = DesignPlan(
            summary="phone stand",
            components=[
                DesignComponent(
                    name="backrest",
                    description="angled support",
                    primitive="box",
                    rotation=Rotation(axis="X", angle_deg=-20.0, intent="tilt backward"),
                ),
            ],
        )
        text = plan_to_prompt_text(plan)
        # Axis vector pre-computed; angle preserved with sign; pivot defaults to origin.
        self.assertIn(".rotate((0, 0, 0), (1, 0, 0), -20)", text)
        self.assertIn("around X axis", text)
        self.assertIn("tilt backward", text)

    def test_rotation_around_y_uses_y_axis_vector(self):
        # Regression guard: "Y" must render as (0,1,0), not (1,0,0). The
        # specific failure mode this whole feature exists to prevent is
        # the planner saying "around Y" while the LLM writes (1,0,0,X).
        plan = DesignPlan(
            summary="hinge",
            components=[
                DesignComponent(
                    name="arm",
                    description="hinged arm",
                    primitive="box",
                    rotation=Rotation(axis="Y", angle_deg=90.0, intent="swing outward"),
                ),
            ],
        )
        text = plan_to_prompt_text(plan)
        self.assertIn("(0, 1, 0)", text)
        self.assertNotIn("(1, 0, 0)", text)

    def test_rotation_omitted_when_none(self):
        # Components with no rotation should not get a rotation: bullet.
        plan = DesignPlan(
            summary="plain",
            components=[
                DesignComponent(name="body", description="just a box", primitive="box"),
            ],
        )
        text = plan_to_prompt_text(plan)
        self.assertNotIn("rotation:", text)

    def test_parse_rotation_attribute_form(self):
        raw = """
        <design_plan>
          <summary>stand</summary>
          <components>
            <component>
              <name>backrest</name>
              <description>angled support</description>
              <primitive>box</primitive>
              <rotation axis="X" angle_deg="-20" intent="tilt backward"/>
              <operation>union</operation>
            </component>
          </components>
        </design_plan>
        """
        plan = parse_design_plan(raw)
        rot = plan.components[0].rotation
        self.assertIsNotNone(rot)
        self.assertEqual(rot.axis, "X")
        self.assertEqual(rot.angle_deg, -20.0)
        self.assertEqual(rot.intent, "tilt backward")

    def test_parse_rotation_zero_angle_is_none(self):
        # A zero-angle rotation is a no-op; the parser drops it so we don't
        # emit a pointless .rotate(...) bullet to the LLM.
        raw = """
        <design_plan>
          <summary>x</summary>
          <components>
            <component>
              <name>body</name>
              <description>box</description>
              <primitive>box</primitive>
              <rotation axis="Z" angle_deg="0" intent=""/>
              <operation>base</operation>
            </component>
          </components>
        </design_plan>
        """
        plan = parse_design_plan(raw)
        self.assertIsNone(plan.components[0].rotation)

    def test_parse_rotation_bad_axis_returns_none(self):
        raw = """
        <design_plan>
          <summary>x</summary>
          <components>
            <component>
              <name>body</name>
              <description>box</description>
              <primitive>box</primitive>
              <rotation axis="diagonal" angle_deg="45"/>
              <operation>base</operation>
            </component>
          </components>
        </design_plan>
        """
        plan = parse_design_plan(raw)
        # Compound / unknown axes are rejected; the LLM is forced to
        # decompose them in the planner output rather than send through
        # ambiguity.
        self.assertIsNone(plan.components[0].rotation)

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
        # Pass an explicit bracket-prompt so the new prompt-gate keeps the
        # fastener requirement active (otherwise the absence of a bracket
        # keyword in user_message would short-circuit it and we'd miss the
        # point of this test).
        report = validate_plan_against_recipes(
            plan,
            [_bracket_recipe()],
            user_message="wall mount bracket",
        )
        joined = " ".join(report.missing_features) + " " + " ".join(report.missing_negative_space)
        self.assertNotIn("mounting holes", joined.lower())
        # The fastener-related negative-space feature should also be suppressed.
        self.assertNotIn("through holes", joined.lower())


class TestExtendedPhysicalUseFields(unittest.TestCase):
    """The planner template grew ``containment_strategy`` and ``pose_intent``
    fields so the planner explicitly names how the held object is retained
    against gravity and which components carry a structured tilt. Parser and
    prompt-rendering must surface them so the downstream code generator and
    the vision verifier can act on them. These tests pin those round-trips.
    """

    def test_parse_extended_physical_use_fields(self):
        raw = """
        <physical_use>
          <orientation>rests on a desk</orientation>
          <containment_strategy>front lip + side guides</containment_strategy>
          <pose_intent>backrest tilts back 25 degrees around X axis</pose_intent>
        </physical_use>
        <design_plan>
          <summary>stand</summary>
        </design_plan>
        """
        plan = parse_design_plan(raw)
        self.assertIsNotNone(plan.physical_use)
        self.assertEqual(plan.physical_use.containment_strategy, "front lip + side guides")
        self.assertIn("backrest", plan.physical_use.pose_intent)

    def test_extended_physical_use_appears_in_prompt(self):
        plan = DesignPlan(
            summary="phone stand",
            components=[DesignComponent(name="b", description="base", primitive="box")],
            physical_use=PhysicalUse(
                orientation="rests on a desk",
                containment_strategy="front lip catches the phone bottom edge",
                pose_intent="backrest leans back 25° around the X axis",
            ),
        )
        text = plan_to_prompt_text(plan)
        self.assertIn("Containment", text)
        self.assertIn("Pose intent", text)
        self.assertIn("front lip catches", text)


if __name__ == "__main__":
    unittest.main()
