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
import json
import traceback
from pathlib import Path
from typing import Any, Optional

import cadquery as cq

from ..domain.models import HardConstraints
from .parameters import extract_parameters, extract_features


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


def try_patch_missing_result(code: str) -> Optional[str]:
    """Mechanical repair for the most common single-line failure: the model
    wrote a valid CadQuery chain but never assigned it to `result`.

    Strategy: parse the AST; find the last top-level assignment to a Name; if
    that target isn't already `result`, append `result = <that_name>` to the
    source. Returns the patched source on success, or None if the AST didn't
    parse, the file is empty, or there is no obvious shape variable to alias.

    We do this in code, deterministically, so the LLM doesn't get a chance to
    "fix" the missing assignment by also throwing away half the geometry — the
    actual failure mode that produced a flat plate for an iPhone holder request.

    Refuses to patch source that doesn't look like a real CadQuery module
    (no `import cadquery`, fewer than 3 top-level statements). Aliasing a
    stub to `result` only hides the real problem — a prior LLM repair pass
    that deleted most of the geometry — and produces a NameError downstream.
    """
    if not code or not code.strip():
        return None
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None

    # Don't dress up a stub. If the source has no cadquery import and only a
    # handful of statements, the previous repair almost certainly dropped the
    # real geometry — surface it as a normal failure instead.
    has_cq_import = any(
        (isinstance(n, ast.Import) and any(a.name == "cadquery" for a in n.names))
        or (isinstance(n, ast.ImportFrom) and n.module == "cadquery")
        for n in tree.body
    )
    if not has_cq_import or len(tree.body) < 3:
        return None

    last_target: Optional[str] = None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    last_target = target.id
    if not last_target or last_target == "result":
        return None
    # Append the alias. A trailing newline keeps source.py tidy.
    suffix = "\n" if code.endswith("\n") else "\n\n"
    return f"{code}{suffix}result = {last_target}\n"


def sanitize_traceback(text: str) -> str:
    """Strip noise from a captured traceback before showing it to the LLM.

    Two specific artifacts cause confusion:

    1. Windows multiprocessing-spawn bootstrap. If any code inside `exec()`
       triggers `multiprocessing.Process`, the child Python interpreter starts
       by re-running `from multiprocessing.spawn import spawn_main; spawn_main(...)`
       through its own `<string>` source. That stub bubbles back up as a frame
       in the parent's traceback, looking like the original source's line 1.
       Repair LLMs see "line 1: multiprocessing.spawn" and assume the user code
       imported multiprocessing. Drop those frames.

    2. The bare leading `Execution error:` prefix from execute_cadquery_code.
       Useful as a tag but adds nothing for the model.
    """
    if not text:
        return text
    lines = text.splitlines()
    cleaned: list[str] = []
    eat_caret = False
    for line in lines:
        if eat_caret:
            # Python 3.11+ tracebacks emit a caret-only line under the source
            # frame to highlight the failing token. If we just dropped the
            # spawn source line, the next caret line is leftover noise.
            eat_caret = False
            if line.strip() and set(line.strip()) <= set("^~ "):
                continue
        # Drop the spawn bootstrap frame and its source line.
        if "multiprocessing.spawn" in line and "spawn_main" in line:
            eat_caret = True
            # Also drop the preceding "File ..., line 1, in <module>" if we
            # added one already.
            if cleaned and cleaned[-1].lstrip().startswith("File \"<string>\""):
                cleaned.pop()
            continue
        cleaned.append(line)
    return "\n".join(cleaned)


# ---------------------------------------------------------------------------
# Sandboxed execution
# ---------------------------------------------------------------------------

def _create_load_import(project_id: str, storage: Any):
    """Create a scoped load_import function for a specific project."""
    def load_import(import_name_or_id: str) -> cq.Workplane:
        # 1. Resolve import
        import_data = storage.get_import(project_id, import_name_or_id)
        if not import_data:
            # Try searching by name if ID fails
            imports = storage.list_imports(project_id)
            for i in imports:
                if i["name"] == import_name_or_id:
                    import_data = i
                    break
        
        if not import_data:
            raise ValueError(f"Import '{import_name_or_id}' not found in project")

        import_id = import_data["import_id"]
        filename = import_data["filename"]
        ext = import_data["extension"].lower()
        
        project_dir = storage.get_project_dir(project_id)
        file_path = project_dir / "imports" / import_id / filename
        
        if not file_path.exists():
            raise FileNotFoundError(f"Import file not found at {file_path}")

        # 2. Load based on extension
        if ext in [".step", ".stp"]:
            return cq.importers.importStep(str(file_path))
        elif ext == ".stl":
            return cq.Workplane("XY").add(cq.importers.importSTL(str(file_path)))
        else:
            raise ValueError(f"Format {ext} cannot be loaded into CadQuery for booleans")

    return load_import


def execute_cadquery_code(
    code: str, 
    project_id: Optional[str] = None, 
    storage: Optional[Any] = None
) -> tuple[bool, Any, str]:
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

    # Inject project-specific helpers if available
    if project_id and storage:
        safe_globals["load_import"] = _create_load_import(project_id, storage)

    local_vars: dict[str, Any] = {}

    try:
        exec(code, safe_globals, local_vars)
    except Exception:
        tb = sanitize_traceback(traceback.format_exc())
        return False, None, f"Execution error:\n{tb}"

    result = local_vars.get("result")
    if result is None:
        return False, None, "Code executed but 'result' variable is None or not set"

    # Accept CadQuery Workplane, Shape, or Assembly objects
    if isinstance(result, (cq.Workplane, cq.Assembly)):
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


def export_step(shape: cq.Workplane | cq.Assembly, output_path: Path) -> Path:
    """Export shape to STEP format."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(shape, cq.Assembly):
        shape.export(str(output_path), exportType="STEP")
    else:
        cq.exporters.export(shape, str(output_path), exportType="STEP")
    return output_path


def export_stl(shape: cq.Workplane | cq.Assembly, output_path: Path, tolerance: float = 0.01) -> Path:
    """Export shape to STL format."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(shape, cq.Assembly):
        shape.export(str(output_path), exportType="STL", tolerance=tolerance)
    else:
        cq.exporters.export(shape, str(output_path), exportType="STL", tolerance=tolerance)
    return output_path


def export_part_stl(assembly: cq.Assembly, part_name: str, output_path: Path, tolerance: float = 0.01) -> Path:
    """Export a single part from an assembly to STL."""
    if part_name not in assembly.objects:
        raise ValueError(f"Part '{part_name}' not found in assembly")
    obj = assembly.objects[part_name].obj
    if obj is None:
        raise ValueError(f"Part '{part_name}' has no geometry")
    if not isinstance(obj, cq.Workplane):
        obj = cq.Workplane("XY").add(obj)
    cq.exporters.export(obj, str(output_path), exportType="STL", tolerance=tolerance)
    return output_path


def export_part_step(assembly: cq.Assembly, part_name: str, output_path: Path) -> Path:
    """Export a single part from an assembly to STEP."""
    if part_name not in assembly.objects:
        raise ValueError(f"Part '{part_name}' not found in assembly")
    obj = assembly.objects[part_name].obj
    if obj is None:
        raise ValueError(f"Part '{part_name}' has no geometry")
    if not isinstance(obj, cq.Workplane):
        obj = cq.Workplane("XY").add(obj)
    cq.exporters.export(obj, str(output_path), exportType="STEP")
    return output_path


def export_glb(
    shape: cq.Workplane | cq.Assembly,
    output_path: Path,
    name: str = "part",
    tolerance: float = 0.01,
    angular_tolerance: float = 0.1,
) -> Path:
    """
    Export shape to glTF binary (.glb) format.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if isinstance(shape, cq.Assembly):
        shape.export(
            str(output_path),
            tolerance=tolerance,
            angularTolerance=angular_tolerance,
        )
    else:
        assy = cq.Assembly()
        assy.add(shape, name=name, color=cq.Color(0.4, 0.6, 0.8, 1.0))
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
    project_id: Optional[str] = None,
    storage: Optional[Any] = None,
) -> dict:
    """
    Full pipeline: validate code → execute → validate geometry → export all formats.

    Returns a dict with:
        success: bool
        message: str
        files: dict of generated file paths
        violations: list of hard constraint failures
        warnings: list of soft geometry warnings
        geometry_stats: dict of measurements (for vision critique)
        failure_type: str hint for repair routing
    """
    if constraints is None:
        constraints = HardConstraints()

    result: dict[str, Any] = {
        "success": False,
        "message": "",
        "files": {},
        "violations": [],
        "warnings": [],
        "geometry_stats": {},
        "failure_type": None,
        "cad_source": code,
    }

    # Always persist the source code first — even if validation/execution fails
    # later. Otherwise failed iterations have no source.py on disk and the
    # frontend gets a 404 when it tries to show the failing code.
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        source_path = output_dir / "source.py"
        source_path.write_text(code, encoding="utf-8")
        result["files"]["source"] = str(source_path)
    except Exception:
        # Source persistence is best-effort; downstream rendering can still
        # work in-memory if the disk write fails.
        pass

    # 1. Validate code (AST)
    valid, msg = validate_cadquery_code(code)
    if not valid:
        result["message"] = f"Code validation failed: {msg}"
        result["failure_type"] = "syntax_error"
        return result

    # 2. Execute
    success, shape, msg = execute_cadquery_code(code, project_id=project_id, storage=storage)
    if not success:
        result["message"] = msg
        result["failure_type"] = "execution_error"
        return result

    # 3. Enhanced geometry validation
    try:
        from ..validation.validator import validate_geometry_enhanced
        from ..domain.models import AssemblyManifest, AssemblyPart

        assembly_parts = []
        
        if isinstance(shape, cq.Assembly):
            # Iterate through children
            for name, obj in shape.objects.items():
                # Skip nodes without an actual shape (e.g. sub-assembly containers)
                if obj.obj is None:
                    continue
                
                # obj.obj is the actual shape (Workplane or Shape)
                part_shape = obj.obj
                if not isinstance(part_shape, cq.Workplane):
                    part_shape = cq.Workplane("XY").add(part_shape)
                
                val_result = validate_geometry_enhanced(part_shape, constraints)
                
                # Collect violations
                result["violations"].extend([f"[{name}] {v}" for v in val_result.violations])
                result["warnings"].extend([f"[{name}] {w}" for w in val_result.warnings])
                
                # Convert GeometryAnalysis to GeometryStats
                from ..domain.models import GeometryStats
                geo_stats = None
                if val_result.analysis:
                    geo_stats = GeometryStats(**{
                        k: v for k, v in val_result.analysis.__dict__.items()
                        if k in GeometryStats.model_fields
                    })
                
                assembly_parts.append(AssemblyPart(
                    name=name,
                    geometry_stats=geo_stats,
                    manufacturability=val_result.manufacturability
                ))

            result["assembly"] = AssemblyManifest(
                parts=assembly_parts,
                total_parts=len(assembly_parts)
            )
            
            # For backward compatibility and top-level stats, use the combined bounding box if possible
            if assembly_parts:
                result["geometry_stats"] = assembly_parts[0].geometry_stats.model_dump() if assembly_parts[0].geometry_stats else {}
                result["manufacturability"] = assembly_parts[0].manufacturability

        else:
            # Single part
            val_result = validate_geometry_enhanced(shape, constraints)
            result["violations"] = val_result.violations
            result["warnings"] = val_result.warnings

            geo_stats = None
            if val_result.analysis:
                from ..domain.models import GeometryStats
                geo_stats = GeometryStats(**{
                    k: v for k, v in val_result.analysis.__dict__.items()
                    if k in GeometryStats.model_fields
                })
                result["geometry_stats"] = val_result.analysis.to_stats_dict()
            
            result["manufacturability"] = val_result.manufacturability
            
            result["assembly"] = AssemblyManifest(
                parts=[AssemblyPart(
                    name="part",
                    geometry_stats=geo_stats,
                    manufacturability=val_result.manufacturability
                )],
                total_parts=1
            )

        if result["violations"]:
            if any("exceeds" in v for v in result["violations"]):
                result["failure_type"] = "constraint_violation"
            else:
                result["failure_type"] = "geometry_invalid"
            result["message"] = f"Geometry validation failed: {'; '.join(result['violations'])}"

    except Exception as e:
        result["warnings"].append(f"Validation error: {e}")
        result["message"] = f"Validation failed with internal error: {e}"

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
        result["warnings"].append(f"STL export failed: {e}")

    try:
        glb_path = export_glb(shape, output_dir / "model.glb", name=model_name)
        result["files"]["glb"] = str(glb_path)
    except Exception as e:
        result["warnings"].append(f"glTF export failed: {e}")

    # Save source code
    source_path = output_dir / "source.py"
    source_path.write_text(code, encoding="utf-8")
    result["files"]["source"] = str(source_path)

    # Expose the shape for downstream use (rendering, vision)
    result["_shape"] = shape

    # 5. Save Assembly Manifest and Features
    if "assembly" in result:
        # Save new rich manifest
        manifest_path = output_dir / "assembly_manifest.json"
        manifest_path.write_text(result["assembly"].model_dump_json(indent=2), encoding="utf-8")
        result["files"]["assembly"] = str(manifest_path)

        # Save legacy features.json for backward compatibility
        feature_manifest = []
        for part in result["assembly"].parts:
            feature_manifest.append({
                "name": part.name,
                "type": "assembly_part",
                "center": [part.geometry_stats.center_of_mass_x, part.geometry_stats.center_of_mass_y, part.geometry_stats.center_of_mass_z] if part.geometry_stats else [0,0,0]
            })
        feature_path = output_dir / "features.json"
        feature_path.write_text(json.dumps(feature_manifest, indent=2), encoding="utf-8")
        result["files"]["features"] = str(feature_path)

    # 6. Extract Editable Parameters and Features
    try:
        from ..domain.models import FeatureManifest
        params = extract_parameters(code)
        features = extract_features(code)
        
        manifest = FeatureManifest(
            features=features,
            parameters=params
        )
        
        manifest_path = output_dir / "feature_manifest.json"
        manifest_path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
        result["files"]["feature_manifest"] = str(manifest_path)
        result["parameters"] = params
        result["features"] = features
    except Exception as e:
        result["warnings"].append(f"Feature/Parameter extraction failed: {e}")

    # 7. File Size Check
    max_size_bytes = constraints.max_file_size_mb * 1024 * 1024
    for fmt, path_str in result["files"].items():
        path = Path(path_str)
        if path.exists():
            size_mb = path.stat().st_size / (1024 * 1024)
            if path.stat().st_size > max_size_bytes:
                msg = f"{fmt.upper()} file size ({size_mb:.2f}MB) exceeds limit of {constraints.max_file_size_mb}MB"
                result["violations"].append(msg)
                result["failure_type"] = "constraint_violation"

    if result["violations"]:
        # Geometry had violations but we exported anyway
        result["success"] = False
    else:
        result["success"] = True
        result["message"] = "OK"

    return result
