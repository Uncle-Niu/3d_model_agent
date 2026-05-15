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
    build_system_prompt,
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
