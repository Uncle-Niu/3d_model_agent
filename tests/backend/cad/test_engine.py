"""
Unit tests for CadQuery execution engine security and functionality.
"""

import unittest
from backend.cad.engine import (
    execute_cadquery_code,
    sanitize_traceback,
    strip_reasoning_leakage,
    try_patch_missing_result,
    validate_cadquery_code,
)


class TestStripReasoningLeakage(unittest.TestCase):
    """Fast mechanical syntax-repair pre-pass.

    qwen3.x sometimes emits its chain-of-thought INSIDE the ```python block,
    which produces 'unterminated string literal' or similar parse errors. Going
    through the slow LLM repair path for this costs ~90s. The mechanical pass
    strips the prose lines and recovers in milliseconds.
    """

    GOOD = (
        "import cadquery as cq\n"
        "base = cq.Workplane('XY').box(10, 10, 10)\n"
        "result = base.edges().fillet(1)\n"
    )

    LEAKED = (
        "import cadquery as cq\n"
        "base = cq.Workplane('XY').box(10, 10, 10)\n"
        "Wait, the box is centered at origin. Let me check.\n"
        "Actually, I think the dimensions are off.\n"
        "The error is on line 58:\n"
        "`holes = cq.Workplane('XY').hole(3.5)`\n"
        "result = base.edges().fillet(1)\n"
    )

    INDENTED_TAIL = (
        "import cadquery as cq\n"
        "   import math\n"
        "\n"
        "   base_length = 130.0\n"
        "   base = cq.Workplane('XY').box(base_length, 100, 12)\n"
        "   result = base.edges('|Z').fillet(1)\n"
    )

    def test_passes_through_already_valid_code(self):
        # Valid code → None (no work needed). Caller stays on the fast path.
        self.assertIsNone(strip_reasoning_leakage(self.GOOD))

    def test_strips_prose_to_recover_parseable_code(self):
        cleaned = strip_reasoning_leakage(self.LEAKED)
        self.assertIsNotNone(cleaned)
        self.assertIn("import cadquery", cleaned)
        self.assertIn("result =", cleaned)
        self.assertNotIn("Wait", cleaned)
        self.assertNotIn("Actually", cleaned)
        self.assertNotIn("The error", cleaned)
        # Recovered code must actually parse now.
        import ast
        ast.parse(cleaned)

    def test_dedents_uniform_tail_indent(self):
        # Regression for data/projects/20260517-154023-cde5036b model-011:
        # the first import was flush-left, while every later line had a
        # uniform accidental indent. The old prefix fallback kept only line 1.
        cleaned = strip_reasoning_leakage(self.INDENTED_TAIL)
        self.assertIsNotNone(cleaned)
        self.assertIn("import math", cleaned)
        self.assertIn("base_length = 130.0", cleaned)
        self.assertIn("result =", cleaned)
        self.assertNotIn("   import math", cleaned)
        import ast
        ast.parse(cleaned)

    def test_truncates_to_longest_parseable_prefix(self):
        # Last line is broken (unterminated string); preceding lines are valid.
        partial = (
            "import cadquery as cq\n"
            "base = cq.Workplane('XY').box(10, 10, 10)\n"
            "result = base.edges().fillet(1)\n"
            "back_wall = back_wall.translate((0, -5\n"  # truncated mid-call
        )
        cleaned = strip_reasoning_leakage(partial)
        self.assertIsNotNone(cleaned)
        import ast
        ast.parse(cleaned)
        self.assertIn("result =", cleaned)
        self.assertNotIn("back_wall.translate((0, -5", cleaned)

    def test_returns_none_when_unrecoverable(self):
        # Pure garbage with no parseable prefix.
        self.assertIsNone(strip_reasoning_leakage("&&& invalid forever"))

    def test_empty_input(self):
        self.assertIsNone(strip_reasoning_leakage(""))
        self.assertIsNone(strip_reasoning_leakage("   \n   \n"))


class TestTryPatchMissingResult(unittest.TestCase):
    """Mechanical patch for source that forgot to assign `result`.

    The guard is important: a previous LLM repair pass may have stripped most
    of the program. Patching `result = <last_var>` onto a stub would only
    promote the truncation to the next iteration as a NameError.
    """

    FULL = (
        "import cadquery as cq\n\n"
        "base_thickness = 5\n"
        "base = cq.Workplane('XY').box(50, 30, base_thickness)\n"
        "filleted = base.edges('|Z').fillet(2)\n"
    )

    def test_appends_result_alias_to_real_program(self):
        patched = try_patch_missing_result(self.FULL)
        self.assertIsNotNone(patched)
        self.assertIn("result = filleted", patched)
        # Original lines preserved.
        self.assertIn("import cadquery", patched)

    def test_refuses_to_patch_stub_missing_cadquery_import(self):
        stub = "holder_body = holder_body.cut(mounting_holes)"
        self.assertIsNone(try_patch_missing_result(stub))

    def test_refuses_to_patch_too_few_statements(self):
        # Has the import but nothing else of substance — likely an LLM truncation.
        tiny = "import cadquery as cq\nbase = cq.Workplane('XY').box(1, 1, 1)\n"
        self.assertIsNone(try_patch_missing_result(tiny))

    def test_returns_none_when_already_assigns_result(self):
        good = self.FULL + "result = filleted\n"
        self.assertIsNone(try_patch_missing_result(good))

    def test_returns_none_for_empty(self):
        self.assertIsNone(try_patch_missing_result(""))

    def test_refuses_when_source_has_no_cadquery_call(self):
        # Source has `import cadquery as cq` and many statements, but they
        # are all numeric assignments — the LLM emitted only the parameter
        # block and truncated before any cq.Workplane(...) call. The
        # observed failure mode from the iPhone-holder turn at
        # 2026-05-17T17:54: aliasing `result = notch_d` (a float) just
        # promotes the truncation to a "got float" error on the next
        # iteration. The patcher must refuse.
        params_only = (
            "import cadquery as cq\n"
            "import math\n"
            "phone_w = 77.6\n"
            "phone_h = 160.7\n"
            "lat_clear = 2.3\n"
            "base_w = phone_w + 2 * lat_clear + 20\n"
            "notch_d = 12.0\n"
        )
        self.assertIsNone(try_patch_missing_result(params_only))

    def test_refuses_when_last_var_is_primitive_literal(self):
        # The source DOES build geometry, but the last top-level assignment
        # binds a float. Aliasing `result = wall_thickness` would mask the
        # real failure mode (the model forgot to assign the final solid).
        # Sending the original error to the LLM repair branch is better.
        code = (
            "import cadquery as cq\n"
            "base = cq.Workplane('XY').box(50, 30, 5)\n"
            "filleted = base.edges('|Z').fillet(2)\n"
            "wall_thickness = 1.6\n"
        )
        self.assertIsNone(try_patch_missing_result(code))

    def test_refuses_when_last_var_is_negative_literal(self):
        # `-5.0` is `UnaryOp(USub, Constant(5.0))` in the AST — still a
        # primitive, must still be refused.
        code = (
            "import cadquery as cq\n"
            "base = cq.Workplane('XY').box(50, 30, 5)\n"
            "rotation_deg = -5.0\n"
        )
        self.assertIsNone(try_patch_missing_result(code))

    def test_still_patches_when_last_var_is_a_cq_chain(self):
        # The whole point of the patcher: a real CadQuery program that
        # forgot the final `result = ...` line should still get aliased.
        code = (
            "import cadquery as cq\n"
            "wall = 2.0\n"
            "base = cq.Workplane('XY').box(50, 30, 5)\n"
            "filleted = base.edges('|Z').fillet(2)\n"
        )
        patched = try_patch_missing_result(code)
        self.assertIsNotNone(patched)
        self.assertTrue(patched.rstrip().endswith("result = filleted"))


class TestSanitizeTraceback(unittest.TestCase):
    """Strip multiprocessing-spawn bootstrap frames from execution tracebacks.

    On Windows, if exec'd CadQuery code triggers a child process spawn, the
    child re-runs `from multiprocessing.spawn import spawn_main; spawn_main(...)`
    through `<string>`. That frame bubbles up as a fake "line 1 of your source"
    and confuses repair LLMs into thinking the user imported multiprocessing.
    """

    NOISY_TB = (
        "Traceback (most recent call last):\n"
        "  File \"D:\\app\\engine.py\", line 258, in execute_cadquery_code\n"
        "    exec(code, safe_globals, local_vars)\n"
        "  File \"<string>\", line 1, in <module>\n"
        "    from multiprocessing.spawn import spawn_main; spawn_main(parent_pid=1, pipe_handle=2)\n"
        "NameError: name 'holder_body' is not defined\n"
    )

    def test_strips_spawn_frame(self):
        clean = sanitize_traceback(self.NOISY_TB)
        self.assertNotIn("multiprocessing.spawn", clean)
        self.assertNotIn("spawn_main", clean)

    def test_preserves_real_error(self):
        clean = sanitize_traceback(self.NOISY_TB)
        self.assertIn("NameError", clean)
        self.assertIn("holder_body", clean)

    def test_passthrough_for_clean_traceback(self):
        clean_tb = (
            "Traceback (most recent call last):\n"
            "  File \"<string>\", line 12, in <module>\n"
            "  File \"<string>\", line 8, in make_gusset\n"
            "NameError: name 'gusset_leg' is not defined\n"
        )
        out = sanitize_traceback(clean_tb)
        self.assertEqual(out.strip(), clean_tb.strip())

    def test_handles_empty(self):
        self.assertEqual(sanitize_traceback(""), "")


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

    def test_pipeline_feature_manifest(self):
        import tempfile
        import json
        from pathlib import Path
        from backend.cad.engine import process_cadquery_code
        
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            code = """import cadquery as cq
length = 50
result = cq.Workplane('XY').box(length, 10, 10).fillet(1)"""
            result = process_cadquery_code(code, tmp_path)
            
            self.assertTrue(result["success"])
            self.assertIn("feature_manifest", result["files"])
            
            manifest_path = Path(result["files"]["feature_manifest"])
            self.assertTrue(manifest_path.exists())
            
            with open(manifest_path, "r") as f:
                data = json.load(f)
            
            # Should have features and parameters
            self.assertIn("features", data)
            self.assertIn("parameters", data)
            
            # Check parameter
            params = data["parameters"]
            self.assertTrue(any(p["name"] == "length" for p in params))
            
            # Check features
            features = data["features"]
            feat_types = {f["type"] for f in features}
            self.assertIn("box", feat_types)
            self.assertIn("fillet", feat_types)


if __name__ == "__main__":
    unittest.main()
