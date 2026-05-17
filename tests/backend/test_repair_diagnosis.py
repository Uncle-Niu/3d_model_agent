"""Tests for the new <diagnosis> block in repair prompts and its extraction."""

from __future__ import annotations

import unittest

from backend.models.llm_service import (
    build_repair_prompt,
    build_repair_system_prompt,
    extract_code_from_response,
    extract_diagnosis_from_response,
)


class TestDiagnosisExtraction(unittest.TestCase):
    def test_extract_diagnosis_from_response_returns_inner_text(self):
        text = """
<diagnosis>
Root cause: missing `result =` assignment.
Fix: alias the last shape to result.
</diagnosis>

```python
import cadquery as cq
result = cq.Workplane("XY").box(10, 10, 10)
```
"""
        diag = extract_diagnosis_from_response(text)
        self.assertIn("missing `result =`", diag)
        self.assertIn("alias the last shape", diag)

    def test_extract_diagnosis_from_response_empty_when_missing(self):
        text = "```python\nimport cadquery as cq\nresult = cq.Workplane().box(1,1,1)\n```"
        self.assertEqual(extract_diagnosis_from_response(text), "")

    def test_extract_code_from_response_strips_diagnosis_when_no_fence(self):
        # If the model forgets the ```python fence but still emits <diagnosis>,
        # we should not let the diagnosis prose leak into the extracted code.
        text = """
<diagnosis>
Root cause: typo in variable name.
</diagnosis>
import cadquery as cq
result = cq.Workplane().box(1,1,1)
"""
        code = extract_code_from_response(text)
        self.assertNotIn("<diagnosis>", code)
        self.assertNotIn("Root cause", code)
        self.assertIn("import cadquery as cq", code)

    def test_extract_code_preserves_python_block_when_diagnosis_present(self):
        text = """<diagnosis>
A short note.
</diagnosis>
```python
import cadquery as cq
result = cq.Workplane().box(2, 2, 2)
```
"""
        code = extract_code_from_response(text)
        self.assertIn("import cadquery as cq", code)
        self.assertNotIn("A short note", code)


class TestRepairPromptInstructsDiagnosis(unittest.TestCase):
    def test_repair_prompt_mentions_diagnosis_block(self):
        prompt = build_repair_prompt(
            original_code="import cadquery as cq\nresult = cq.Workplane().box(1,1,1)",
            error_message="SyntaxError: bad",
            iteration=2,
        )
        self.assertIn("<diagnosis>", prompt)

    def test_repair_system_prompt_keeps_python_block_discipline(self):
        sys_prompt = build_repair_system_prompt()
        self.assertIn("<diagnosis>", sys_prompt)
        self.assertIn("python", sys_prompt.lower())


if __name__ == "__main__":
    unittest.main()
