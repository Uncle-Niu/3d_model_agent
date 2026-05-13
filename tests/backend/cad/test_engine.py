"""
Unit tests for CadQuery execution engine security and functionality.
"""

import unittest
from backend.cad.engine import execute_cadquery_code, validate_cadquery_code


class TestCadQueryCodeValidation(unittest.TestCase):
    """Tests for code validation (AST-level checks)."""

    def test_valid_cadquery_code(self):
        """Valid CadQuery code should pass validation."""
        code = """import cadquery as cq
result = cq.Workplane("XY").box(10, 10, 10)"""
        is_valid, msg = validate_cadquery_code(code)
        self.assertTrue(is_valid, f"Expected valid, got: {msg}")
        self.assertEqual(msg, "OK")

    def test_syntax_error_detected(self):
        """Invalid Python syntax should be rejected."""
        code = """import cadquery as cq
result = cq.Workplane("XY").box(10, 10"""  # Missing closing paren
        is_valid, msg = validate_cadquery_code(code)
        self.assertFalse(is_valid)
        self.assertIn("Syntax error", msg)

    def test_forbidden_subprocess_import(self):
        """subprocess import should be forbidden."""
        code = """import subprocess
import cadquery as cq
result = cq.Workplane("XY").box(10, 10, 10)"""
        is_valid, msg = validate_cadquery_code(code)
        self.assertFalse(is_valid)
        self.assertIn("Forbidden import", msg)

    def test_forbidden_os_import(self):
        """os import should be forbidden."""
        code = """import os
import cadquery as cq
result = cq.Workplane("XY").box(10, 10, 10)"""
        is_valid, msg = validate_cadquery_code(code)
        self.assertFalse(is_valid)
        self.assertIn("Forbidden import", msg)

    def test_forbidden_sys_import(self):
        """sys import should be forbidden."""
        code = """import sys
import cadquery as cq
result = cq.Workplane("XY").box(10, 10, 10)"""
        is_valid, msg = validate_cadquery_code(code)
        self.assertFalse(is_valid)
        self.assertIn("Forbidden import", msg)

    def test_forbidden_shutil_import(self):
        """shutil import should be forbidden."""
        code = """from shutil import rmtree
import cadquery as cq
result = cq.Workplane("XY").box(10, 10, 10)"""
        is_valid, msg = validate_cadquery_code(code)
        self.assertFalse(is_valid)
        self.assertIn("Forbidden import", msg)

    def test_forbidden_multiprocessing_import(self):
        """multiprocessing import should be forbidden."""
        code = """from multiprocessing.spawn import spawn_main
import cadquery as cq
result = cq.Workplane("XY").box(10, 10, 10)"""
        is_valid, msg = validate_cadquery_code(code)
        self.assertFalse(is_valid)
        self.assertIn("Forbidden import", msg)

    def test_result_assignment_required(self):
        """Code must assign to 'result' variable."""
        code = """import cadquery as cq
my_shape = cq.Workplane("XY").box(10, 10, 10)"""
        is_valid, msg = validate_cadquery_code(code)
        self.assertFalse(is_valid)
        self.assertIn("result", msg)

    def test_allowed_math_import(self):
        """math import should be allowed."""
        code = """import math
import cadquery as cq
result = cq.Workplane("XY").box(10, 10, 10)"""
        is_valid, msg = validate_cadquery_code(code)
        self.assertTrue(is_valid, f"Expected math import to be allowed, got: {msg}")


class TestCadQueryCodeExecution(unittest.TestCase):
    """Tests for code execution with security restrictions."""

    def test_valid_code_executes(self):
        """Valid CadQuery code should execute successfully."""
        code = """import cadquery as cq
result = (
    cq.Workplane("XY")
    .box(50, 30, 10)
    .edges("|Z")
    .fillet(2)
)"""
        success, shape, msg = execute_cadquery_code(code)
        self.assertTrue(success, f"Expected success, got: {msg}")
        self.assertIsNotNone(shape)
        self.assertEqual(msg, "OK")

    def test_simple_box(self):
        """Simple box creation should work."""
        code = """import cadquery as cq
result = cq.Workplane("XY").box(10, 20, 30)"""
        success, shape, msg = execute_cadquery_code(code)
        self.assertTrue(success)
        self.assertIsNotNone(shape)

    def test_math_functions_work(self):
        """Built-in math functions should be available."""
        code = """import cadquery as cq
size = max(10, 20)
diameter = abs(-15)
result = cq.Workplane("XY").box(size, diameter, 10)"""
        success, shape, msg = execute_cadquery_code(code)
        self.assertTrue(success, f"Math functions failed: {msg}")
        self.assertIsNotNone(shape)

    def test_math_module_import(self):
        """math module import and use should work."""
        code = """import math
import cadquery as cq
size = math.ceil(15.3)
result = cq.Workplane("XY").box(size, 10, 10)"""
        success, shape, msg = execute_cadquery_code(code)
        self.assertTrue(success, f"math module failed: {msg}")
        self.assertIsNotNone(shape)

    def test_loops_and_conditions(self):
        """Loops and conditionals should work."""
        code = """import cadquery as cq
result = cq.Workplane("XY").box(10, 10, 10)
for i in range(2):
    if i == 0:
        result = result.edges("|Z").fillet(1)"""
        success, shape, msg = execute_cadquery_code(code)
        self.assertTrue(success, f"Loop/condition failed: {msg}")
        self.assertIsNotNone(shape)

    def test_runtime_subprocess_blocked(self):
        """Attempting to import subprocess at runtime should fail."""
        code = """import cadquery as cq
subprocess = __import__('subprocess')
result = cq.Workplane("XY").box(10, 10, 10)"""
        success, shape, msg = execute_cadquery_code(code)
        self.assertFalse(success)
        self.assertIn("not allowed", msg)

    def test_runtime_os_blocked(self):
        """Attempting to import os at runtime should fail."""
        code = """import cadquery as cq
os = __import__('os')
result = cq.Workplane("XY").box(10, 10, 10)"""
        success, shape, msg = execute_cadquery_code(code)
        self.assertFalse(success)
        self.assertIn("not allowed", msg)

    def test_open_function_blocked(self):
        """open() function should not be available."""
        code = """import cadquery as cq
f = open('/etc/passwd', 'r')
result = cq.Workplane("XY").box(10, 10, 10)"""
        success, shape, msg = execute_cadquery_code(code)
        self.assertFalse(success)
        self.assertIn("not defined", msg)

    def test_eval_blocked(self):
        """eval() function should not be available."""
        code = """import cadquery as cq
code = eval("1 + 1")
result = cq.Workplane("XY").box(10, 10, 10)"""
        success, shape, msg = execute_cadquery_code(code)
        self.assertFalse(success)
        self.assertIn("not defined", msg)

    def test_exec_blocked(self):
        """exec() function should not be available."""
        code = """import cadquery as cq
exec("x = 1")
result = cq.Workplane("XY").box(10, 10, 10)"""
        success, shape, msg = execute_cadquery_code(code)
        self.assertFalse(success)
        self.assertIn("not defined", msg)

    def test_compile_blocked(self):
        """compile() function should not be available."""
        code = """import cadquery as cq
compiled = compile("x = 1", "<string>", "exec")
result = cq.Workplane("XY").box(10, 10, 10)"""
        success, shape, msg = execute_cadquery_code(code)
        self.assertFalse(success)
        self.assertIn("not defined", msg)

    def test_input_blocked(self):
        """input() function should not be available."""
        code = """import cadquery as cq
user_input = input("Enter something: ")
result = cq.Workplane("XY").box(10, 10, 10)"""
        success, shape, msg = execute_cadquery_code(code)
        self.assertFalse(success)
        self.assertIn("not defined", msg)

    def test_missing_result_variable(self):
        """Execution should fail if result is not set."""
        code = """import cadquery as cq
shape = cq.Workplane("XY").box(10, 10, 10)"""
        success, shape, msg = execute_cadquery_code(code)
        self.assertFalse(success)
        self.assertIn("result", msg)

    def test_result_none(self):
        """Execution should fail if result is None."""
        code = """import cadquery as cq
result = None"""
        success, shape, msg = execute_cadquery_code(code)
        self.assertFalse(success)
        self.assertIn("None", msg)

    def test_list_comprehension(self):
        """List comprehensions should work."""
        code = """import cadquery as cq
sizes = [x * 5 for x in range(1, 5)]
result = cq.Workplane("XY").box(sizes[0], sizes[1], sizes[2])"""
        success, shape, msg = execute_cadquery_code(code)
        self.assertTrue(success, f"List comprehension failed: {msg}")
        self.assertIsNotNone(shape)

    def test_dictionary_operations(self):
        """Dictionary operations should work."""
        code = """import cadquery as cq
params = {"x": 10, "y": 20, "z": 30}
result = cq.Workplane("XY").box(params["x"], params["y"], params["z"])"""
        success, shape, msg = execute_cadquery_code(code)
        self.assertTrue(success, f"Dictionary operations failed: {msg}")
        self.assertIsNotNone(shape)


class TestCadQueryPipeline(unittest.TestCase):
    """Tests for the full process_cadquery_code pipeline."""

    def test_pipeline_success(self):
        import tempfile
        from pathlib import Path
        from backend.cad.engine import process_cadquery_code
        
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            code = "import cadquery as cq\nresult = cq.Workplane('XY').box(10, 10, 10)"
            result = process_cadquery_code(code, tmp_path)
            
            self.assertTrue(result["success"])
            self.assertIn("step", result["files"])
            self.assertIn("stl", result["files"])
            self.assertIn("glb", result["files"])
            self.assertTrue(Path(result["files"]["step"]).exists())

    def test_file_size_constraint_violation(self):
        import tempfile
        from pathlib import Path
        from backend.cad.engine import process_cadquery_code
        from backend.domain.models import HardConstraints
        
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            # Set a very small file size limit (0.0001 MB ≈ 100 bytes)
            constraints = HardConstraints(max_file_size_mb=0.0001)
            code = "import cadquery as cq\nresult = cq.Workplane('XY').box(10, 10, 10)"
            result = process_cadquery_code(code, tmp_path, constraints=constraints)
            
            self.assertFalse(result["success"])
            self.assertEqual(result["failure_type"], "constraint_violation")
            self.assertTrue(any("file size" in v for v in result["violations"]))


if __name__ == "__main__":
    unittest.main()
