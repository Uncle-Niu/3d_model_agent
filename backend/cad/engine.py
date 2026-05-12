"""
CadQuery execution engine.

Responsibilities:
- Validate generated CadQuery code (AST checks)
- Execute in a sandboxed namespace
- Export STEP, STL, glTF
- Validate geometry against hard constraints
"""

from __future__ import annotations

import ast
import io
import tempfile
import traceback
from pathlib import Path
from typing import Any, Optional

import cadquery as cq

from ..domain.models import HardConstraints

# ---------------------------------------------------------------------------
# Forbidden imports / builtins for sandboxing
# ---------------------------------------------------------------------------

FORBIDDEN_MODULES = {
    "subprocess",
    "shutil",
    "pathlib",
    "socket",
    "http",
    "urllib",
    "os",
    "sys",
    "ctypes",
    "importlib",
    "pickle",
    "shelve",
    "multiprocessing",
    "threading",
    "signal",
    "webbrowser",
}

def _restricted_import(name, *args, **kwargs):
    """Only allow importing cadquery and math (provided in globals)."""
    allowed_modules = {"cadquery", "math"}
    if name in allowed_modules:
        # Return the module from globals if available
        import sys
        if name == "cadquery":
            import cadquery
            return cadquery
        elif name == "math":
            import math
            return math
    raise ImportError(f"Importing '{name}' is not allowed.")


SAFE_BUILTINS = {
    # Basic types
    "int": int,
    "float": float,
    "str": str,
    "bool": bool,
    "list": list,
    "dict": dict,
    "tuple": tuple,
    "set": set,
    "frozenset": frozenset,
    # Math functions
    "abs": abs,
    "min": min,
    "max": max,
    "round": round,
    "sum": sum,
    "pow": pow,
    # Collections/iteration
    "range": range,
    "len": len,
    "enumerate": enumerate,
    "zip": zip,
    "map": map,
    "filter": filter,
    "reversed": reversed,
    "sorted": sorted,
    # Logic
    "any": any,
    "all": all,
    "isinstance": isinstance,
    "type": type,
    # Exceptions
    "ValueError": ValueError,
    "TypeError": TypeError,
    "RuntimeError": RuntimeError,
    "Exception": Exception,
    # Output
    "print": print,
    # Constants
    "True": True,
    "False": False,
    "None": None,
    # Import control
    "__import__": _restricted_import,
    # Special
    "__name__": "__main__",
    "__doc__": None,
    "__package__": None,
}


# ---------------------------------------------------------------------------
# Code validation
# ---------------------------------------------------------------------------


def validate_cadquery_code(code: str) -> tuple[bool, str]:
    """
    Validate generated CadQuery code before execution.

    Checks:
    1. Valid Python syntax
    2. No forbidden imports
    3. Assigns to 'result' variable

    Returns (is_valid, message).
    """
    # 1. Syntax check
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, f"Syntax error at line {e.lineno}: {e.msg}"

    # 2. Check for forbidden imports
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root_module = alias.name.split(".")[0]
                if root_module in FORBIDDEN_MODULES:
                    return False, f"Forbidden import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root_module = node.module.split(".")[0]
                if root_module in FORBIDDEN_MODULES:
                    return False, f"Forbidden import: {node.module}"

    # 3. Check for 'result' assignment
    has_result = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "result":
                    has_result = True

    if not has_result:
        return False, "Code must assign the final shape to a variable named 'result'"

    return True, "OK"


# ---------------------------------------------------------------------------
# Sandboxed execution
# ---------------------------------------------------------------------------


def execute_cadquery_code(code: str) -> tuple[bool, Any, str]:
    """
    Execute CadQuery code in a restricted namespace.

    Returns (success, result_shape_or_none, message).
    """
    import math

    safe_globals = {
        "__builtins__": SAFE_BUILTINS,
        "cq": cq,
        "cadquery": cq,
        "math": math,
    }

    local_vars: dict[str, Any] = {}

    try:
        exec(code, safe_globals, local_vars)
    except Exception:
        tb = traceback.format_exc()
        return False, None, f"Execution error:\n{tb}"

    result = local_vars.get("result")
    if result is None:
        return False, None, "Code executed but 'result' variable is None or not set"

    # Accept CadQuery Workplane or Shape objects
    if isinstance(result, cq.Workplane):
        return True, result, "OK"
    elif hasattr(result, "val") or hasattr(result, "Solids"):
        return True, result, "OK"
    else:
        return (
            False,
            None,
            f"'result' is not a CadQuery shape (got {type(result).__name__})",
        )


# ---------------------------------------------------------------------------
# Geometry validation
# ---------------------------------------------------------------------------


def validate_geometry(
    shape: cq.Workplane,
    constraints: HardConstraints,
) -> tuple[bool, list[str]]:
    """
    Validate geometry against hard constraints.

    Returns (is_valid, list_of_violations).
    """
    violations: list[str] = []

    try:
        bb = shape.val().BoundingBox()
    except Exception as e:
        return False, [f"Cannot compute bounding box: {e}"]

    # Dimension checks
    x_size = bb.xmax - bb.xmin
    y_size = bb.ymax - bb.ymin
    z_size = bb.zmax - bb.zmin

    if x_size > constraints.max_x_mm:
        violations.append(
            f"X dimension {x_size:.1f}mm exceeds max {constraints.max_x_mm}mm"
        )
    if y_size > constraints.max_y_mm:
        violations.append(
            f"Y dimension {y_size:.1f}mm exceeds max {constraints.max_y_mm}mm"
        )
    if z_size > constraints.max_z_mm:
        violations.append(
            f"Z dimension {z_size:.1f}mm exceeds max {constraints.max_z_mm}mm"
        )

    # Check shape has volume (not degenerate)
    try:
        solids = shape.solids().vals()
        if len(solids) == 0:
            violations.append("Shape has no solid bodies")
    except Exception:
        violations.append("Cannot enumerate solid bodies")

    is_valid = len(violations) == 0
    return is_valid, violations


# ---------------------------------------------------------------------------
# Export functions
# ---------------------------------------------------------------------------


def export_step(shape: cq.Workplane, output_path: Path) -> Path:
    """Export shape to STEP format."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cq.exporters.export(shape, str(output_path), exportType="STEP")
    return output_path


def export_stl(shape: cq.Workplane, output_path: Path, tolerance: float = 0.01) -> Path:
    """Export shape to STL format."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cq.exporters.export(shape, str(output_path), exportType="STL", tolerance=tolerance)
    return output_path


def export_glb(
    shape: cq.Workplane,
    output_path: Path,
    name: str = "part",
    tolerance: float = 0.01,
    angular_tolerance: float = 0.1,
) -> Path:
    """
    Export shape to glTF binary (.glb) format.

    Uses CadQuery's Assembly.export() for tessellation.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    assy = cq.Assembly()
    assy.add(shape, name=name, color=cq.Color(0.4, 0.6, 0.8, 1.0))  # steel blue
    assy.export(
        str(output_path),
        tolerance=tolerance,
        angularTolerance=angular_tolerance,
    )
    return output_path


# ---------------------------------------------------------------------------
# High-level pipeline: code → shape → files
# ---------------------------------------------------------------------------


def process_cadquery_code(
    code: str,
    output_dir: Path,
    model_name: str = "part",
    constraints: Optional[HardConstraints] = None,
) -> dict:
    """
    Full pipeline: validate code → execute → validate geometry → export all formats.

    Returns a dict with:
        success: bool
        message: str
        files: dict of generated file paths
        violations: list of constraint violations
    """
    if constraints is None:
        constraints = HardConstraints()

    result = {
        "success": False,
        "message": "",
        "files": {},
        "violations": [],
        "cad_source": code,
    }

    # 1. Validate code
    valid, msg = validate_cadquery_code(code)
    if not valid:
        result["message"] = f"Code validation failed: {msg}"
        return result

    # 2. Execute
    success, shape, msg = execute_cadquery_code(code)
    if not success:
        result["message"] = msg
        return result

    # 3. Validate geometry
    geo_valid, violations = validate_geometry(shape, constraints)
    result["violations"] = violations
    if not geo_valid:
        result["message"] = f"Geometry validation failed: {'; '.join(violations)}"
        # Still try to export for debugging, but mark as failed
        # Fall through to export

    # 4. Export all formats
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        step_path = export_step(shape, output_dir / "model.step")
        result["files"]["step"] = str(step_path)
    except Exception as e:
        result["message"] = f"STEP export failed: {e}"
        return result

    try:
        stl_path = export_stl(shape, output_dir / "model.stl")
        result["files"]["stl"] = str(stl_path)
    except Exception as e:
        result["violations"].append(f"STL export failed: {e}")

    try:
        glb_path = export_glb(shape, output_dir / "model.glb", name=model_name)
        result["files"]["glb"] = str(glb_path)
    except Exception as e:
        result["violations"].append(f"glTF export failed: {e}")

    # Save source code
    source_path = output_dir / "source.py"
    source_path.write_text(code, encoding="utf-8")
    result["files"]["source"] = str(source_path)

    if not geo_valid:
        # Geometry had violations but we exported anyway
        result["success"] = False
    else:
        result["success"] = True
        result["message"] = "OK"

    return result
