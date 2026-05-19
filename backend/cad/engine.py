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
import re
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

    # The source must actually build geometry — at least one `cq.Workplane(...)`
    # call or one `cq.Assembly(...)` constructor. Without that, aliasing the
    # "last variable" produces results like `result = notch_d` (a float)
    # when the LLM truncated mid-output and emitted only the parameter
    # block. The downstream "'result' is not a CadQuery shape (got float)"
    # error costs another full iteration; refusing here surfaces the real
    # problem to the LLM repair branch on the next round.
    if not _source_builds_cq_geometry(tree):
        return None

    last_target: Optional[str] = None
    last_value: Optional[ast.AST] = None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    last_target = target.id
                    last_value = node.value
    if not last_target or last_target == "result":
        return None
    # If the last variable is bound to a primitive literal (the most common
    # truncation tail: `notch_d = 12.0`), refuse to alias it. A scalar
    # `result` blows up the next iteration with a confusing
    # "got float / int / str" message that costs an LLM repair to undo.
    if last_value is not None and _is_primitive_literal(last_value):
        return None
    # Append the alias. A trailing newline keeps source.py tidy.
    suffix = "\n" if code.endswith("\n") else "\n\n"
    return f"{code}{suffix}result = {last_target}\n"


def looks_like_parameter_only_stub(code: str) -> bool:
    """Return True when source looks like a truncated parameter block.

    These snippets are syntactically valid Python, but contain no CadQuery
    geometry construction. Sending them to a "minimal repair" prompt usually
    yields another stub, so the orchestrator should ask for full regeneration.
    """
    if not code or not code.strip():
        return False
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False

    has_result = any(
        isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "result" for target in node.targets)
        for node in ast.walk(tree)
    )
    if has_result or _source_builds_cq_geometry(tree):
        return False

    assignments = [node for node in tree.body if isinstance(node, ast.Assign)]
    if len(assignments) < 3:
        return False

    nontrivial_statements = [
        node for node in tree.body
        if not isinstance(node, (ast.Assign, ast.Import, ast.ImportFrom))
    ]
    return not nontrivial_statements


def try_auto_scale_for_fit(
    code: str,
    geometry_stats: dict,
    max_x_mm: float,
    max_y_mm: float,
    max_z_mm: float,
    *,
    safety_factor: float = 0.97,
    min_factor: float = 0.85,
) -> Optional[str]:
    """Mechanical repair for the most common post-execution failure: a model
    that executed successfully but whose bounding box exceeds the print
    volume — usually because a rotation expanded the AABB beyond what the
    planner anticipated.

    Strategy: compute the uniform scale needed to bring the largest
    overflowing axis under the print-volume cap (with a safety margin), then
    append a single ``result = cq.Workplane('XY').newObject([result.val().scale(f)])``
    line to the source. Re-executing the patched program produces a
    proportionally smaller geometry that preserves every feature and
    relationship the LLM already designed — exactly the fix the repair LLMs
    keep failing to make manually because their mental model of how rotation
    interacts with the AABB is wrong.

    Returns the patched source on success, or ``None`` when uniform scaling
    isn't appropriate:
    - no `bbox_*_mm` stats available (can't compute factor)
    - geometry already fits (no overflowing axis)
    - required factor below ``min_factor`` (likely a runaway plan, not just a
      barely-overflowing one — escalate to LLM)
    - source has no `result =` assignment yet (let other repairs run first)
    - source defines `result` as an ``cq.Assembly(...)`` whose children
      cannot be uniformly scaled by this snippet (we'd silently swap the
      assembly for a single solid).
    """
    if "Auto-scale to fit print volume" in code or (
        ".scale(" in code and "print volume" in code
    ):
        return None

    bbox_x = float(geometry_stats.get("bbox_x_mm") or 0.0)
    bbox_y = float(geometry_stats.get("bbox_y_mm") or 0.0)
    bbox_z = float(geometry_stats.get("bbox_z_mm") or 0.0)
    if bbox_x <= 0 and bbox_y <= 0 and bbox_z <= 0:
        return None

    factors: list[float] = []
    if bbox_x > max_x_mm:
        factors.append(max_x_mm / bbox_x)
    if bbox_y > max_y_mm:
        factors.append(max_y_mm / bbox_y)
    if bbox_z > max_z_mm:
        factors.append(max_z_mm / bbox_z)
    if not factors:
        return None

    factor = min(factors) * safety_factor
    if factor < min_factor or factor >= 1.0:
        return None

    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None

    has_result_assign = False
    result_is_assembly_call = False
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        targets = [t for t in node.targets if isinstance(t, ast.Name) and t.id == "result"]
        if not targets:
            continue
        has_result_assign = True
        # Refuse to scale an Assembly: `cq.Assembly(...)` doesn't expose the
        # same `.val().scale(...)` chain as a Workplane, and swapping it for
        # a flattened solid would silently lose the per-part structure the
        # downstream pipeline depends on. Walk the call chain — the
        # assembly constructor may be the root of `.add(...).add(...)`.
        for sub in ast.walk(node.value):
            if not isinstance(sub, ast.Call):
                continue
            func = sub.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "cq"
                and func.attr == "Assembly"
            ):
                result_is_assembly_call = True
                break

    if not has_result_assign or result_is_assembly_call:
        return None

    suffix = "\n" if code.endswith("\n") else "\n\n"
    bbox_summary = f"{bbox_x:g}x{bbox_y:g}x{bbox_z:g}"
    cap_summary = f"{max_x_mm:g}x{max_y_mm:g}x{max_z_mm:g}"
    return (
        f"{code}{suffix}"
        f"# Auto-scale to fit print volume: {bbox_summary}mm -> "
        f"factor {factor:.4f} (cap {cap_summary}mm).\n"
        f"result = cq.Workplane('XY').newObject([result.val().scale({factor:.4f})])\n"
    )


def try_remove_failing_fillet(code: str, error_message: str) -> Optional[str]:
    """Comment out a single fillet line when OCCT reports a fillet failure.

    Fillets are quality improvements, not primary geometry. If OCCT fails with
    a known ChFi3d/fillet construction error and the traceback points to a
    simple assignment line containing `.fillet(...)`, keep the existing shape
    variable unchanged by commenting out that one line.
    """
    if not code or not error_message:
        return None
    low = error_message.lower()
    if (
        "fillet" not in low
        and "chfi3d" not in low
        and "brep_api" not in low
        and "command not done" not in low
        and "stdfail_notdone" not in low
    ):
        return None
    m = re.search(r'File "<string>", line (\d+)', error_message)
    if not m:
        return None
    try:
        lineno = int(m.group(1))
    except ValueError:
        return None
    lines = code.splitlines()
    if not (1 <= lineno <= len(lines)):
        return None
    line = lines[lineno - 1]
    if ".fillet(" not in line:
        return None
    stripped = line.strip()
    indent = line[: len(line) - len(line.lstrip())]
    if "=" in stripped:
        lhs = stripped.split("=", 1)[0].strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", lhs):
            return None
        lines[lineno - 1] = (
            f"{indent}# Removed failed fillet; keeping `{lhs}` unchanged. Original: {stripped}"
        )
    elif stripped.startswith("."):
        lines[lineno - 1] = (
            f"{indent}# Removed failed fillet chain line. Original: {stripped}"
        )
    else:
        return None
    patched = "\n".join(lines) + ("\n" if code.endswith("\n") else "")
    try:
        ast.parse(patched)
    except SyntaxError:
        return None
    return patched


def try_patch_standalone_workplane_hole(code: str, error_message: str) -> Optional[str]:
    """Replace a cutter built via ``cq.Workplane(...).hole(...)`` with solids.

    ``.hole()`` must be chained from an existing solid. LLMs often build a
    standalone cutter variable like ``holes = cq.Workplane("XZ").pushPoints(...)
    .hole(d)`` and then subtract it from a plate. That fails with "Cannot find a
    solid on the stack". For that narrow pattern, generate actual cylindrical
    cutter solids at the same points and keep the downstream ``target.cut(holes)``
    line unchanged.
    """
    if not code or not error_message:
        return None
    low = error_message.lower()
    if "cannot find a solid" not in low or ".hole" not in code:
        return None
    m = re.search(r'File "<string>", line (\d+)', error_message)
    if not m:
        return None
    try:
        failing_lineno = int(m.group(1))
        tree = ast.parse(code)
    except (ValueError, SyntaxError):
        return None

    target_node: Optional[ast.Assign] = None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not (getattr(node, "lineno", 0) <= failing_lineno <= getattr(node, "end_lineno", 0)):
            continue
        if any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "hole"
            for call in ast.walk(node.value)
        ):
            target_node = node
            break
    if target_node is None or not target_node.targets:
        return None
    target = target_node.targets[0]
    if not isinstance(target, ast.Name):
        return None
    cutter_name = target.id
    block = ast.get_source_segment(code, target_node) or ""

    plane = "XY"
    for call in ast.walk(target_node.value):
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "cq"
            and call.func.attr == "Workplane"
            and call.args
            and isinstance(call.args[0], ast.Constant)
        ):
            continue
        if call.args[0].value in {"XY", "XZ", "YZ"}:
            plane = str(call.args[0].value)
            break

    hole_arg = None
    points = [(0, 0)]
    translate_expr = "(0, 0, 0)"
    for call in ast.walk(target_node.value):
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
            continue
        if call.func.attr == "hole" and call.args:
            hole_arg = ast.get_source_segment(code, call.args[0])
        elif call.func.attr == "pushPoints" and call.args:
            try:
                literal_points = ast.literal_eval(call.args[0])
            except Exception:
                literal_points = None
            if (
                isinstance(literal_points, list)
                and literal_points
                and all(isinstance(pt, tuple) and len(pt) == 2 for pt in literal_points)
            ):
                points = literal_points
        elif call.func.attr == "translate" and call.args:
            source = ast.get_source_segment(code, call.args[0])
            if source:
                translate_expr = source
    if not hole_arg:
        return None

    axis_names = {
        "XY": ("hole_x", "hole_y"),
        "XZ": ("hole_x", "hole_z"),
        "YZ": ("hole_y", "hole_z"),
    }[plane]
    indent = " " * getattr(target_node, "col_offset", 0)
    inner = indent + "    "
    replacement = [
        f"{indent}{cutter_name} = None",
        f"{indent}for {axis_names[0]}, {axis_names[1]} in {points!r}:",
        f"{inner}_hole_cutter = (",
        f"{inner}    cq.Workplane({plane!r})",
        f"{inner}    .center({axis_names[0]}, {axis_names[1]})",
        f"{inner}    .circle(({hole_arg}) / 2.0)",
        f"{inner}    .extrude(max(float({hole_arg}) * 4.0, 20.0), both=True)",
        f"{inner}    .translate({translate_expr})",
        f"{inner})",
        f"{inner}{cutter_name} = _hole_cutter if {cutter_name} is None else {cutter_name}.union(_hole_cutter)",
    ]
    lines = code.splitlines()
    start = target_node.lineno - 1
    end = target_node.end_lineno
    patched_lines = lines[:start] + replacement + lines[end:]
    patched = "\n".join(patched_lines) + ("\n" if code.endswith("\n") else "")
    try:
        ast.parse(patched)
    except SyntaxError:
        return None
    if block and cutter_name not in patched:
        return None
    return patched


def try_patch_workplane_bounding_box(code: str, error_message: str) -> Optional[str]:
    """Fix common CadQuery BoundingBox attribute mistakes from LLM output."""
    if not code or not error_message:
        return None
    low = error_message.lower()
    if "boundingbox" not in low and "zmin" not in low and "zmax" not in low:
        return None
    patched = code
    patched = re.sub(
        r"\b([A-Za-z_][A-Za-z0-9_]*)\.BoundingBox\(\)",
        r"\1.val().BoundingBox()",
        patched,
    )
    patched = patched.replace(".zMin", ".zmin").replace(".zMax", ".zmax")
    patched = patched.replace(".xMin", ".xmin").replace(".xMax", ".xmax")
    patched = patched.replace(".yMin", ".ymin").replace(".yMax", ".ymax")
    if patched == code:
        return None
    try:
        ast.parse(patched)
    except SyntaxError:
        return None
    return patched


def _source_builds_cq_geometry(tree: ast.Module) -> bool:
    """Return True if `tree` contains at least one `cq.Workplane(...)` or
    `cq.Assembly(...)` call. Used by `try_patch_missing_result` to refuse
    aliasing a parameter-only stub to `result`."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                if func.value.id == "cq" and func.attr in ("Workplane", "Assembly", "Sketch"):
                    return True
    return False


def _is_primitive_literal(value: ast.AST) -> bool:
    """Return True if `value` is a literal scalar (number, bool, str, None) or
    a tuple/list of such literals — i.e. anything that cannot possibly be a
    CadQuery shape."""
    if isinstance(value, ast.Constant):
        return isinstance(value.value, (int, float, bool, str, bytes, type(None)))
    if isinstance(value, ast.UnaryOp) and isinstance(value.op, (ast.USub, ast.UAdd)):
        return _is_primitive_literal(value.operand)
    if isinstance(value, (ast.Tuple, ast.List, ast.Set)):
        return all(_is_primitive_literal(elt) for elt in value.elts)
    if isinstance(value, ast.BinOp):
        # `phone_w + 2*lat_clear + 20` — an arithmetic expression over
        # numeric variables. Conservatively treat any binary-op tree whose
        # leaves are all Name or Constant scalars as a primitive.
        return _is_primitive_arith(value)
    return False


def _is_primitive_arith(value: ast.AST) -> bool:
    if isinstance(value, ast.BinOp):
        return _is_primitive_arith(value.left) and _is_primitive_arith(value.right)
    if isinstance(value, ast.UnaryOp):
        return _is_primitive_arith(value.operand)
    if isinstance(value, ast.Constant):
        return isinstance(value.value, (int, float, bool))
    if isinstance(value, ast.Name):
        # A bare Name in an arithmetic context typically refers to a
        # previously-bound scalar parameter. False negatives are fine
        # (we'd just allow the alias); false positives only hurt when
        # the user actually has `width = cq.Workplane(...)` — vanishingly
        # rare. Conservative-true here is the safer bet for our use case.
        return True
    return False


_REASONING_PROSE_PREFIXES = (
    "wait", "wait,", "actually", "actually,", "let's", "let me",
    "looking", "i think", "i need", "i'll", "i should", "i'd",
    "the error", "the issue", "this happens", "this is", "this means",
    "to fix", "the fix", "note:", "note that", "hmm", "hmm,",
    "so,", "but ", "but,", "however", "however,",
    "therefore", "therefore,", "given the", "given that",
    "first,", "second,", "next,", "finally,", "now,",
    "okay,", "ok,", "alright,", "code block",
)


def _looks_like_reasoning_prose(line: str) -> bool:
    """Return True if `line` is almost certainly free-form English narration
    that leaked into a code block (not a Python statement, comment, or empty
    line). Used by `strip_reasoning_leakage` to recover from qwen3-style
    repairs where the model dumps its chain-of-thought between code lines.
    """
    stripped = line.strip()
    if not stripped:
        return False
    # Real Python comments start with `#`. Leave those alone.
    if stripped.startswith("#"):
        return False
    # Thinking models often format their reasoning as markdown bullets inside
    # the fenced python block. Classify the bullet text, not the leading "-".
    if stripped.startswith(("-", "*")):
        stripped = stripped[1:].strip()
        if not stripped:
            return True
        if stripped.startswith("`") or stripped.lower().startswith(
            (
                "vesa ",
                "hole ",
                "tray ",
                "left ",
                "right ",
                "front ",
                "side ",
                "gusset",
                "fillet",
                "code ",
                "assign ",
                "use ",
                "check ",
            )
        ):
            return True
        if not re.match(
            r"^(?:import\s+|from\s+|def\s+|class\s+|if\s+|elif\s+|else\s*:|"
            r"for\s+|while\s+|try\s*:|except\s+|with\s+|return\b|raise\s+|"
            r"[A-Za-z_][A-Za-z0-9_]*\s*=|result\s*=|[A-Za-z_][A-Za-z0-9_]*\s*\.)",
            stripped,
        ):
            return True
    # Lines that include common reasoning openers.
    low = stripped.lower()
    for prefix in _REASONING_PROSE_PREFIXES:
        if low.startswith(prefix):
            return True
    # Inline-code backticks (`foo.bar()`) only appear in markdown narration.
    if "`" in stripped:
        return True
    # A sentence: starts with a capital letter, ends with `.`/`?`/`!`, and
    # contains no Python operators that would indicate a real statement.
    if (
        stripped[0].isupper()
        and stripped[-1] in ".?!"
        and "=" not in stripped
        and "(" not in stripped
        and ":" not in stripped
        and not stripped.startswith(("import ", "from ", "def ", "class ", "for ", "if ", "elif ", "else", "while ", "return ", "with ", "try", "except", "raise ", "yield "))
    ):
        return True
    return False


def _fix_uniform_tail_indent(code: str) -> Optional[str]:
    """Recover code blocks where every line after the first got indented.

    Some LLM responses produce a fenced block like::

        import cadquery as cq
           import math
           base = ...

    That is not reasoning leakage; it is a formatting artifact.  Removing the
    common leading spaces from the tail preserves the full program and avoids
    the more destructive "longest parseable prefix" fallback.
    """
    lines = code.splitlines()
    if len(lines) < 2 or lines[0].startswith((" ", "\t")):
        return None

    tail_with_text = [ln for ln in lines[1:] if ln.strip()]
    if not tail_with_text:
        return None
    indents = [len(ln) - len(ln.lstrip(" ")) for ln in tail_with_text]
    common_indent = min(indents)
    if common_indent <= 0:
        return None

    candidate_lines = [lines[0]]
    for ln in lines[1:]:
        if ln.startswith(" " * common_indent):
            candidate_lines.append(ln[common_indent:])
        else:
            candidate_lines.append(ln)
    candidate = "\n".join(candidate_lines)
    try:
        ast.parse(candidate)
    except SyntaxError:
        return None
    return candidate + ("\n" if not candidate.endswith("\n") else "")


def strip_reasoning_leakage(code: str) -> Optional[str]:
    """Mechanical syntax repair: strip free-form reasoning prose that the LLM
    accidentally pasted INTO a python code block.

    Observed failure mode (from the second iPhone-holder log): qwen3.6 emitted
    a code block whose last 20 lines were narrative musings like:

        back_wall = back_wall.translate((0, -5
        The error is on line 58:
        `holes = cq.Workplane("XY").pushPoints(hole_pts).hole(...)`
        Wait, holder is modified later. It's safer to create...

    The original `back_wall = ...` line was truncated, the rest is markdown.
    The Python validator rejects this with "unterminated string literal at
    line 40", and the orchestrator burns a full LLM repair cycle (~90s) to
    re-generate the program.

    Strategy:
    1. If the code parses, return None (no work needed).
    2. If the source has a uniform accidental tail indent, dedent it.
    3. Otherwise, drop any line that `_looks_like_reasoning_prose` says is
       narration, and try parsing again.
    4. If still failing AND the failure is in the trailing region, truncate
       to the longest prefix that parses. Returning None signals to the
       caller to fall back to the slow LLM-driven repair.

    The function never modifies real Python — it only removes lines that
    cannot plausibly be code.
    """
    if not code or not code.strip():
        return None
    try:
        ast.parse(code)
        return None  # already valid
    except SyntaxError:
        pass

    deindented = _fix_uniform_tail_indent(code)
    if deindented is not None:
        return deindented

    lines = code.splitlines()
    cleaned = [ln for ln in lines if not _looks_like_reasoning_prose(ln)]
    if cleaned == lines:
        # Nothing prose-like to remove — skip to prefix-truncation.
        candidate = "\n".join(cleaned)
    else:
        candidate = "\n".join(cleaned)
        try:
            ast.parse(candidate)
            if candidate.strip():
                return candidate + ("\n" if not candidate.endswith("\n") else "")
            return "\n"
        except SyntaxError:
            pass

    # Last-resort: find the longest line-aligned prefix that parses cleanly.
    # Useful when the model truncated a statement mid-line (the broken line
    # poisons everything after it, but the program up to it may be sound).
    prefix_lines: list[str] = []
    best: Optional[str] = None
    for ln in candidate.splitlines():
        prefix_lines.append(ln)
        try:
            ast.parse("\n".join(prefix_lines))
            best = "\n".join(prefix_lines)
        except SyntaxError:
            continue
    if best and best != code:
        return best + ("\n" if not best.endswith("\n") else "")
    return None


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
