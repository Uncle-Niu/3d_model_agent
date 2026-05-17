"""
Unit tests for the LLM service:
  - System prompt builder
  - Repair prompt builder (failure-type routing)
  - extract_code_from_response
"""

import unittest

from backend.domain.models import HardConstraints, SoftConstraints
from backend.models.llm_service import (
    build_repair_prompt,
    build_repair_system_prompt,
    build_system_prompt,
    detect_repair_deletion,
    extract_code_from_response,
    parse_design_plan,
)


class TestBuildSystemPrompt(unittest.TestCase):

    def test_contains_hard_constraints(self):
        hc = HardConstraints(max_x_mm=150, max_y_mm=120, max_z_mm=100)
        prompt = build_system_prompt(hc)
        self.assertIn("150", prompt)
        self.assertIn("120", prompt)
        self.assertIn("100", prompt)

    def test_contains_wall_thickness(self):
        hc = HardConstraints(min_wall_thickness_mm=2.0)
        prompt = build_system_prompt(hc)
        self.assertIn("2.0", prompt)

    def test_contains_soft_constraints(self):
        sc = SoftConstraints(material="PETG", overhang_angle_max=40)
        prompt = build_system_prompt(soft_constraints=sc)
        self.assertIn("PETG", prompt)
        self.assertIn("40", prompt)

    def test_contains_notes(self):
        sc = SoftConstraints(notes="Use ribbed walls for stiffness")
        prompt = build_system_prompt(soft_constraints=sc)
        self.assertIn("ribbed walls", prompt)

    def test_no_notes_when_empty(self):
        sc = SoftConstraints(notes="")
        prompt = build_system_prompt(soft_constraints=sc)
        self.assertNotIn("Additional notes:", prompt)

    def test_contains_api_reference(self):
        prompt = build_system_prompt()
        # Key API terms should be present
        self.assertIn("Workplane", prompt)
        self.assertIn(".box(", prompt)
        self.assertIn(".fillet(", prompt)

    def test_contains_examples(self):
        prompt = build_system_prompt()
        self.assertIn("import cadquery as cq", prompt)
        self.assertIn("result", prompt)

    def test_output_rules_present(self):
        prompt = build_system_prompt()
        self.assertIn("```python", prompt)
        self.assertIn("result", prompt)

    def test_defaults_used_when_none(self):
        prompt = build_system_prompt(None, None)
        # Should not raise; defaults applied
        self.assertIsInstance(prompt, str)
        self.assertGreater(len(prompt), 100)


class TestBuildRepairPrompt(unittest.TestCase):

    SAMPLE_CODE = 'import cadquery as cq\nresult = cq.Workplane("XY").box(10, 10, 10)'
    SAMPLE_ERROR = "AttributeError: 'NoneType' object has no attribute 'fillet'"

    def test_contains_original_code(self):
        prompt = build_repair_prompt(self.SAMPLE_CODE, self.SAMPLE_ERROR, 1)
        self.assertIn("import cadquery as cq", prompt)

    def test_contains_error_message(self):
        prompt = build_repair_prompt(self.SAMPLE_CODE, self.SAMPLE_ERROR, 1)
        self.assertIn("AttributeError", prompt)

    def test_contains_iteration_number(self):
        prompt = build_repair_prompt(self.SAMPLE_CODE, self.SAMPLE_ERROR, 3)
        self.assertIn("3", prompt)

    def test_syntax_error_guidance(self):
        prompt = build_repair_prompt(
            self.SAMPLE_CODE, self.SAMPLE_ERROR, 1, failure_type="syntax_error"
        )
        self.assertIn("Syntax Error", prompt)
        self.assertIn("parentheses", prompt)

    def test_geometry_invalid_guidance(self):
        prompt = build_repair_prompt(
            self.SAMPLE_CODE, self.SAMPLE_ERROR, 1, failure_type="geometry_invalid"
        )
        self.assertIn("Invalid Geometry", prompt)
        self.assertIn("manifold", prompt)

    def test_constraint_violation_guidance(self):
        prompt = build_repair_prompt(
            self.SAMPLE_CODE, self.SAMPLE_ERROR, 1, failure_type="constraint_violation"
        )
        self.assertIn("Constraint Violation", prompt)
        self.assertIn("print volume", prompt)

    def test_generic_guidance_when_no_type(self):
        prompt = build_repair_prompt(
            self.SAMPLE_CODE, self.SAMPLE_ERROR, 1, failure_type=None
        )
        self.assertIn("Execution Error", prompt)

    def test_geometry_stats_injected(self):
        stats = {"bounding_box": "50 × 30 × 10 mm", "volume": "15000 mm³"}
        prompt = build_repair_prompt(
            self.SAMPLE_CODE, self.SAMPLE_ERROR, 1, geometry_stats=stats
        )
        self.assertIn("50 × 30 × 10 mm", prompt)
        self.assertIn("15000 mm³", prompt)

    def test_long_error_truncated(self):
        long_error = "E" * 3000
        prompt = build_repair_prompt(self.SAMPLE_CODE, long_error, 1)
        # Should not be larger than reasonable; error was truncated to 1500
        self.assertLess(len(prompt), 10000)

    def test_output_contains_result_instruction(self):
        prompt = build_repair_prompt(self.SAMPLE_CODE, self.SAMPLE_ERROR, 1)
        self.assertIn("result", prompt)


class TestExtractCodeFromResponse(unittest.TestCase):

    def test_extract_python_block(self):
        response = '```python\nimport cadquery as cq\nresult = cq.Workplane("XY").box(10,10,10)\n```'
        code = extract_code_from_response(response)
        self.assertIn("import cadquery", code)
        self.assertIn("result =", code)

    def test_extract_plain_block(self):
        response = '```\nimport cadquery as cq\nresult = cq.Workplane("XY").box(10,10,10)\n```'
        code = extract_code_from_response(response)
        self.assertIn("import cadquery", code)

    def test_fallback_to_raw_text(self):
        code_str = 'import cadquery as cq\nresult = cq.Workplane("XY").box(10,10,10)'
        code = extract_code_from_response(code_str)
        self.assertIn("import cadquery", code)

    def test_prose_before_block_stripped(self):
        response = "Here's the corrected code:\n\n```python\nimport cadquery as cq\nresult = cq.Workplane().box(5,5,5)\n```\n\nThis should work."
        code = extract_code_from_response(response)
        self.assertNotIn("Here's", code)
        self.assertNotIn("This should", code)
        self.assertIn("import cadquery", code)

    def test_python_prefix_stripped(self):
        # Some LLMs emit ``` python (with space) or ```python\n
        response = "```python\nimport cadquery as cq\nresult = cq.Workplane().box(1,1,1)\n```"
        code = extract_code_from_response(response)
        self.assertFalse(code.startswith("python"))

    def test_empty_response_returns_empty(self):
        code = extract_code_from_response("   ")
        self.assertEqual(code, "")

    def test_multiple_blocks_first_extracted(self):
        response = "```python\nresult = cq.Workplane().box(1,1,1)\n```\n```python\nresult = cq.Workplane().box(2,2,2)\n```"
        code = extract_code_from_response(response)
        self.assertIn("box(1,1,1)", code)

    def test_uniform_tail_indent_in_python_block_is_normalized(self):
        response = (
            "```python\n"
            "import cadquery as cq\n"
            "   import math\n"
            "\n"
            "   base = cq.Workplane('XY').box(10, 10, 10)\n"
            "   result = base\n"
            "```"
        )
        code = extract_code_from_response(response)
        self.assertIn("import math", code)
        self.assertIn("result = base", code)
        self.assertNotIn("   import math", code)
        import ast
        ast.parse(code)


class TestDetectRepairDeletion(unittest.TestCase):
    """Anti-deletion guard for repair LLM output.

    Scenario from the iPhone-holder failure log: the repair model was given a
    ~70-line program with the `Cannot find a solid` error and "fixed" it by
    returning a single line. Without a guard, the next iteration inherits a
    2-line stub and the cascade is fatal.
    """

    FULL_PROGRAM = """\
import cadquery as cq

base_length = 120.0
base_width = 100.0
base_thickness = 14.0
backrest_height = 130.0
gusset_leg = 30.0

base_block = (
    cq.Workplane("XY")
    .box(base_length, base_width, base_thickness)
)

backrest_plate = (
    cq.Workplane("XY")
    .box(90, backrest_height, 8)
    .translate((0, 50, 70))
)

holder_body = base_block.union(backrest_plate)

left_gusset = (
    cq.Workplane("YZ")
    .polyline([(0, 0), (gusset_leg, 0), (gusset_leg, gusset_leg)])
    .close()
    .extrude(8)
    .translate((-45, 65, 14))
)

holder_body = holder_body.union(left_gusset)

result = holder_body.edges().fillet(2.0)
"""

    def test_accepts_real_repair(self):
        # A real repair changes a few lines but keeps the program intact.
        repaired = self.FULL_PROGRAM.replace(
            ".extrude(8)\n    .translate((-45, 65, 14))",
            ".extrude(8)\n    .translate((-45, 65, 14.0))",
        )
        self.assertIsNone(detect_repair_deletion(self.FULL_PROGRAM, repaired))

    def test_rejects_truncated_stub(self):
        # The exact failure mode from the chat log.
        stub = "holder_body = holder_body.cut(mounting_holes)"
        reason = detect_repair_deletion(self.FULL_PROGRAM, stub)
        self.assertIsNotNone(reason)

    def test_rejects_dropped_cadquery_import(self):
        no_import = "\n".join(
            ln for ln in self.FULL_PROGRAM.splitlines() if "import cadquery" not in ln
        )
        reason = detect_repair_deletion(self.FULL_PROGRAM, no_import)
        self.assertIsNotNone(reason)
        self.assertIn("cadquery", reason)

    def test_rejects_empty_repair(self):
        self.assertIsNotNone(detect_repair_deletion(self.FULL_PROGRAM, ""))

    def test_small_original_not_flagged_for_minor_changes(self):
        # If the input is tiny, a tiny repair is fine. Don't over-trigger.
        small = "import cadquery as cq\nresult = cq.Workplane('XY').box(10, 10, 10)"
        repaired = "import cadquery as cq\nresult = cq.Workplane('XY').box(10, 10, 12)"
        self.assertIsNone(detect_repair_deletion(small, repaired))


class TestBuildRepairSystemPrompt(unittest.TestCase):

    def test_is_smaller_than_generation_system_prompt(self):
        gen = build_system_prompt()
        repair = build_repair_system_prompt()
        # The repair prompt drops the full example bank; should be smaller.
        self.assertLess(len(repair), len(gen))

    def test_contains_preserve_geometry_rule(self):
        repair = build_repair_system_prompt()
        self.assertIn("MINIMUM", repair)
        self.assertIn("Preserve", repair)


class TestPreservationInRepairPrompt(unittest.TestCase):

    SAMPLE_CODE = (
        "import cadquery as cq\n"
        "base = cq.Workplane('XY').box(10, 10, 10)\n"
        "result = base.edges().fillet(1)"
    )

    def test_contains_preserve_design_block(self):
        prompt = build_repair_prompt(self.SAMPLE_CODE, "boom", 1)
        self.assertIn("Preserve the design", prompt)
        self.assertIn("DO NOT delete", prompt)

    def test_line_numbers_in_failed_code(self):
        prompt = build_repair_prompt(self.SAMPLE_CODE, "boom", 1)
        # First line should be tagged " 1:" or similar — this lets the model
        # cross-reference traceback line numbers.
        self.assertTrue(
            any(line.lstrip().startswith("1:") for line in prompt.splitlines()),
            f"expected a line-numbered source in prompt; got: {prompt[:500]}",
        )

    def test_extra_preservation_warning_only_when_requested(self):
        plain = build_repair_prompt(self.SAMPLE_CODE, "boom", 1)
        warn = build_repair_prompt(
            self.SAMPLE_CODE, "boom", 1, extra_preservation_warning=True
        )
        self.assertNotIn("previous repair attempt deleted", plain)
        self.assertIn("previous repair attempt deleted", warn)

    def test_fillet_stdfail_guidance_in_generic_execution_path(self):
        # The fillet-specific guidance must be present in the generic
        # execution-error branch so the model has a concrete alternative when
        # OCC fails on .fillet() / .chamfer().
        prompt = build_repair_prompt(self.SAMPLE_CODE, "boom", 1, failure_type=None)
        self.assertIn("StdFail_NotDone", prompt)
        self.assertIn(".fillet()", prompt)

    def test_prior_attempts_block_renders_when_provided(self):
        prior = [
            {
                "iteration": 2,
                "error_first_line": "StdFail_NotDone: BRep_API: command not done",
                "failing_source_line": 'line 85: result = base.edges("|Z").fillet(3)',
            }
        ]
        prompt = build_repair_prompt(
            self.SAMPLE_CODE, "boom", 3, prior_attempts=prior
        )
        self.assertIn("Prior repair attempts on this turn", prompt)
        self.assertIn("Attempt 2", prompt)
        self.assertIn(".fillet(3)", prompt)

    def test_prior_attempts_absent_block_not_rendered(self):
        prompt = build_repair_prompt(self.SAMPLE_CODE, "boom", 1)
        self.assertNotIn("Prior repair attempts on this turn", prompt)

    def test_same_error_recurring_switches_to_structural_framing(self):
        # When the prior attempt's error signature matches the current
        # error's, the prompt should drop the "minimum fix" framing and
        # demand a structurally different change.
        err = (
            "Execution error:\n"
            "Traceback (most recent call last):\n"
            "  File \"<string>\", line 3, in <module>\n"
            "OCP.OCP.StdFail.StdFail_NotDone: BRep_API: command not done"
        )
        prior = [
            {
                "iteration": 2,
                "error_first_line": "OCP.OCP.StdFail.StdFail_NotDone: BRep_API: command not done",
                "failing_source_line": "line 3: result = base.edges().fillet(3)",
            }
        ]
        prompt = build_repair_prompt(
            self.SAMPLE_CODE, err, 3, prior_attempts=prior
        )
        self.assertIn("same error", prompt.lower())
        self.assertIn("structurally different", prompt)
        # The strong "DO NOT shorten" framing should be replaced when stuck.
        self.assertNotIn(
            "DO NOT shorten the program. Your output should have at least", prompt
        )


class TestParseDesignPlan(unittest.TestCase):

    def test_accepts_design_plan_with_attributes(self):
        raw = """<thinking>Use a flat print orientation.</thinking>
<design_plan version="1">
  <summary>Bracket with screw holes</summary>
  <components>
    <component>
      <name>base_plate</name>
      <description>flat mounting base</description>
    </component>
  </components>
</design_plan>"""

        plan = parse_design_plan(raw)

        self.assertEqual(plan.summary, "Bracket with screw holes")
        self.assertEqual(plan.raw_reasoning, "Use a flat print orientation.")
        self.assertEqual(plan.components[0].name, "base_plate")

    def test_accepts_inner_xml_without_design_plan_wrapper(self):
        raw = """<thinking>Keep walls thick enough.</thinking>
<summary>Phone stand with a cable slot</summary>
<key_features>
  <feature>angled back support</feature>
  <feature>front lip</feature>
</key_features>"""

        plan = parse_design_plan(raw)

        self.assertEqual(plan.summary, "Phone stand with a cable slot")
        self.assertEqual(plan.key_features, ["angled back support", "front lip"])


if __name__ == "__main__":
    unittest.main()
