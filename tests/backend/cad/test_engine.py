"""
Unit tests for CadQuery execution engine security and functionality.
"""

import unittest
from backend.cad.engine import (
    execute_cadquery_code,
    looks_like_parameter_only_stub,
    sanitize_traceback,
    strip_reasoning_leakage,
    try_auto_scale_for_fit,
    try_patch_standalone_workplane_hole,
    try_patch_workplane_bounding_box,
    try_remove_failing_fillet,
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

    def test_strips_markdown_bullet_reasoning(self):
        leaked = (
            "import cadquery as cq\n"
            "- Let's carefully follow the dimensions and orientation.\n"
            "- VESA plate: 100x100 mm, thickness 4 mm.\n"
            "- `bad = cq.Workplane('XY').hole(3)`\n"
            "base = cq.Workplane('XY').box(10, 10, 10)\n"
            "result = base\n"
        )
        cleaned = strip_reasoning_leakage(leaked)
        self.assertIsNotNone(cleaned)
        self.assertNotIn("Let's carefully", cleaned)
        self.assertNotIn("VESA plate", cleaned)
        self.assertIn("result = base", cleaned)
        import ast
        ast.parse(cleaned)

    def test_all_markdown_reasoning_becomes_empty_patch(self):
        prose = (
            "code block.\n"
            "- Assign final shape to `result`.\n"
            "- Let's carefully follow the dimensions and orientation.\n"
            "- VESA plate: 100x100 mm, thickness 4 mm.\n"
        )
        self.assertEqual(strip_reasoning_leakage(prose), "\n")

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


class TestLooksLikeParameterOnlyStub(unittest.TestCase):
    def test_detects_parameter_block_without_geometry(self):
        code = (
            "tray_width = 300.0\n"
            "tray_depth = 200.0\n"
            "tray_thickness = 4.0\n"
            "vesa_hole_spacing = 75.0\n"
        )
        self.assertTrue(looks_like_parameter_only_stub(code))

    def test_ignores_real_cadquery_program_missing_result(self):
        code = (
            "import cadquery as cq\n"
            "tray_width = 120.0\n"
            "body = cq.Workplane('XY').box(tray_width, 80, 5)\n"
        )
        self.assertFalse(looks_like_parameter_only_stub(code))

    def test_ignores_existing_result_assignment(self):
        code = (
            "import cadquery as cq\n"
            "result = cq.Workplane('XY').box(10, 10, 5)\n"
        )
        self.assertFalse(looks_like_parameter_only_stub(code))


class TestAutoScaleForFit(unittest.TestCase):
    """Mechanical scale-down when geometry overflows the print volume.

    Real failure mode (laptop-tray VESA mount log): the LLM kept producing
    designs whose AABB ballooned past 256mm after a -30° rotation, then
    repeatedly failed to scale them down by tweaking individual parameters.
    A single `result.val().scale(f)` line fixes the entire family of
    failures deterministically.
    """

    BASE_CODE = (
        "import cadquery as cq\n"
        "vesa = cq.Workplane('XY').box(100, 100, 5)\n"
        "tray = cq.Workplane('XY').box(270, 130, 3).translate((0, 175, 56.5))\n"
        "result = vesa.union(tray)\n"
    )

    def test_appends_scale_when_oversize(self):
        stats = {"bbox_x_mm": 260.0, "bbox_y_mm": 270.0, "bbox_z_mm": 97.0}
        patched = try_auto_scale_for_fit(
            self.BASE_CODE, stats,
            max_x_mm=256.0, max_y_mm=256.0, max_z_mm=256.0,
        )
        self.assertIsNotNone(patched)
        self.assertIn("result.val().scale(", patched)
        # Factor = (256 / 270) * 0.97 ~= 0.9197
        self.assertIn("0.91", patched)

    def test_returns_none_when_geometry_already_fits(self):
        stats = {"bbox_x_mm": 100.0, "bbox_y_mm": 100.0, "bbox_z_mm": 5.0}
        self.assertIsNone(try_auto_scale_for_fit(
            self.BASE_CODE, stats,
            max_x_mm=256.0, max_y_mm=256.0, max_z_mm=256.0,
        ))

    def test_returns_none_when_source_already_has_auto_scale_patch(self):
        already_scaled = (
            self.BASE_CODE
            + "\n# Auto-scale to fit print volume: 270x301x97mm -> factor 0.8247 (cap 256x256x256mm).\n"
            + "result = cq.Workplane('XY').newObject([result.val().scale(0.8247)])\n"
        )
        stats = {"bbox_x_mm": 270.0, "bbox_y_mm": 301.1, "bbox_z_mm": 97.0}
        self.assertIsNone(try_auto_scale_for_fit(
            already_scaled, stats,
            max_x_mm=256.0, max_y_mm=256.0, max_z_mm=256.0,
        ))

    def test_returns_none_for_runaway_oversize(self):
        # Large overflow -> factor < 0.85. Likely a bad plan; escalate to LLM.
        stats = {"bbox_x_mm": 2500.0, "bbox_y_mm": 100.0, "bbox_z_mm": 5.0}
        self.assertIsNone(try_auto_scale_for_fit(
            self.BASE_CODE, stats,
            max_x_mm=256.0, max_y_mm=256.0, max_z_mm=256.0,
        ))

    def test_returns_none_for_large_shrink_that_would_change_intent(self):
        stats = {"bbox_x_mm": 352.0, "bbox_y_mm": 164.0, "bbox_z_mm": 161.0}
        self.assertIsNone(try_auto_scale_for_fit(
            self.BASE_CODE, stats,
            max_x_mm=256.0, max_y_mm=256.0, max_z_mm=256.0,
        ))

    def test_returns_none_without_bbox_stats(self):
        self.assertIsNone(try_auto_scale_for_fit(
            self.BASE_CODE, {},
            max_x_mm=256.0, max_y_mm=256.0, max_z_mm=256.0,
        ))

    def test_returns_none_when_no_result_assignment(self):
        no_result = (
            "import cadquery as cq\n"
            "vesa = cq.Workplane('XY').box(100, 100, 5)\n"
        )
        stats = {"bbox_x_mm": 300.0, "bbox_y_mm": 300.0, "bbox_z_mm": 100.0}
        self.assertIsNone(try_auto_scale_for_fit(
            no_result, stats,
            max_x_mm=256.0, max_y_mm=256.0, max_z_mm=256.0,
        ))

    def test_returns_none_when_result_is_assembly(self):
        # Assemblies aren't safely uniform-scalable via the
        # `result.val().scale(...)` pattern — escalate to LLM repair.
        assembly_code = (
            "import cadquery as cq\n"
            "vesa = cq.Workplane('XY').box(100, 100, 5)\n"
            "result = cq.Assembly().add(vesa, name='base')\n"
        )
        stats = {"bbox_x_mm": 300.0, "bbox_y_mm": 300.0, "bbox_z_mm": 100.0}
        self.assertIsNone(try_auto_scale_for_fit(
            assembly_code, stats,
            max_x_mm=256.0, max_y_mm=256.0, max_z_mm=256.0,
        ))

    def test_patched_code_executes_and_fits(self):
        # The whole point: the patched source actually runs and stays under
        # the cap, end-to-end, in the real CadQuery engine. Use stats that
        # match what the geometry actually produces (-50..240 in Y because
        # the tray is translated, plus -135..135 in X).
        from backend.cad.engine import execute_cadquery_code
        stats = {"bbox_x_mm": 270.0, "bbox_y_mm": 290.0, "bbox_z_mm": 58.0}
        patched = try_auto_scale_for_fit(
            self.BASE_CODE, stats,
            max_x_mm=256.0, max_y_mm=256.0, max_z_mm=256.0,
        )
        self.assertIsNotNone(patched)
        ok, shape, _msg = execute_cadquery_code(patched)
        self.assertTrue(ok)
        bb = shape.val().BoundingBox()
        self.assertLess(bb.xmax - bb.xmin, 256.0)
        self.assertLess(bb.ymax - bb.ymin, 256.0)
        self.assertLess(bb.zmax - bb.zmin, 256.0)


class TestTryRemoveFailingFillet(unittest.TestCase):
    def test_comments_out_traceback_fillet_assignment(self):
        code = (
            "import cadquery as cq\n"
            "tray = cq.Workplane('XY').box(10, 10, 2)\n"
            "tray = tray.edges('|Z').fillet(3)\n"
            "result = tray\n"
        )
        err = (
            'Traceback\n  File "<string>", line 3, in <module>\n'
            "OCP.OCP.Standard.Standard_ConstructionError: ChFi3d_Builder:only 2 faces"
        )
        patched = try_remove_failing_fillet(code, err)
        self.assertIsNotNone(patched)
        self.assertIn("Removed failed fillet", patched)
        self.assertIn("result = tray", patched)
        ok, shape, _msg = execute_cadquery_code(patched)
        self.assertTrue(ok)
        self.assertIsNotNone(shape)

    def test_returns_none_when_error_is_not_fillet(self):
        code = "import cadquery as cq\nresult = cq.Workplane('XY').box(1, 1, 1)\n"
        err = 'Traceback\n  File "<string>", line 2, in <module>\nValueError: nope'
        self.assertIsNone(try_remove_failing_fillet(code, err))

    def test_comments_out_traceback_fillet_chain_line(self):
        code = (
            "import cadquery as cq\n"
            "tray = (\n"
            "    cq.Workplane('XY')\n"
            "    .box(10, 10, 2)\n"
            "    .edges('|Z').fillet(5)\n"
            ")\n"
            "result = tray\n"
        )
        err = (
            'Traceback\n  File "<string>", line 5, in <module>\n'
            "OCP.OCP.StdFail.StdFail_NotDone: BRep_API: command not done"
        )
        patched = try_remove_failing_fillet(code, err)
        self.assertIsNotNone(patched)
        self.assertIn("Removed failed fillet chain line", patched)
        ok, shape, _msg = execute_cadquery_code(patched)
        self.assertTrue(ok)
        self.assertIsNotNone(shape)


class TestTryPatchStandaloneWorkplaneHole(unittest.TestCase):
    def test_replaces_fresh_workplane_hole_cutter_with_cylinders(self):
        code = (
            "import cadquery as cq\n"
            "vesa_thickness = 5\n"
            "vesa_plate = (\n"
            "    cq.Workplane('XZ')\n"
            "    .box(120, vesa_thickness, 120)\n"
            "    .translate((0, -80, 80))\n"
            ")\n"
            "hole_d = 4.5\n"
            "vesa_holes = (\n"
            "    cq.Workplane('XZ')\n"
            "    .pushPoints([(50, 50), (-50, 50), (50, -50), (-50, -50)])\n"
            "    .hole(hole_d)\n"
            "    .translate((0, -80, 80))\n"
            ")\n"
            "result = vesa_plate.cut(vesa_holes)\n"
        )
        err = (
            'Traceback\n  File "<string>", line 12, in <module>\n'
            "ValueError: Cannot find a solid on the stack or in the parent chain"
        )
        patched = try_patch_standalone_workplane_hole(code, err)
        self.assertIsNotNone(patched)
        self.assertIn("for hole_x, hole_z in", patched)
        self.assertIn(".extrude(max(float(hole_d) * 4.0, 20.0), both=True)", patched)
        ok, shape, _msg = execute_cadquery_code(patched)
        self.assertTrue(ok)
        self.assertIsNotNone(shape)

    def test_returns_none_for_unrelated_error(self):
        code = "import cadquery as cq\nresult = cq.Workplane('XY').box(1, 1, 1)\n"
        self.assertIsNone(try_patch_standalone_workplane_hole(code, "ValueError: nope"))


class TestTryPatchWorkplaneBoundingBox(unittest.TestCase):
    def test_patches_workplane_bounding_box_and_minmax_names(self):
        code = (
            "import cadquery as cq\n"
            "result = cq.Workplane('XY').box(10, 10, 2)\n"
            "bbox = result.BoundingBox()\n"
            "min_z = bbox.zMin\n"
            "result = result.translate((0, 0, -min_z))\n"
        )
        err = "AttributeError: 'Workplane' object has no attribute 'BoundingBox'"

        patched = try_patch_workplane_bounding_box(code, err)

        self.assertIsNotNone(patched)
        self.assertIn("result.val().BoundingBox()", patched)
        self.assertIn("bbox.zmin", patched)
        ok, shape, _msg = execute_cadquery_code(patched)
        self.assertTrue(ok)
        self.assertIsNotNone(shape)

    def test_returns_none_for_unrelated_error(self):
        code = "import cadquery as cq\nresult = cq.Workplane('XY').box(1, 1, 1)\n"
        self.assertIsNone(try_patch_workplane_bounding_box(code, "ValueError: nope"))


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
            self.assertIn("bbox_x_mm", result["geometry_stats"])
            self.assertIn("bbox_y_mm", result["geometry_stats"])
            self.assertIn("bbox_z_mm", result["geometry_stats"])

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
