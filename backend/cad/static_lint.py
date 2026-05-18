"""
Static, pre-execution lint for generated CadQuery code.

Catches a specific class of LLM bugs that are cheap to detect from the AST and
expensive to detect later — the canonical example is misusing CadQuery's
``.rotate(p1, p2, angle)`` API as if it were ``.rotate(origin, direction, angle)``.

CadQuery contract: ``rotate(axisStartPoint, axisEndPoint, angleDegrees)`` rotates
around the LINE through ``p1`` and ``p2``. A call like

    .rotate((0, 0, -60), (1, 0, 0), -15)

does NOT rotate around X — the axis line direction is ``(1, 0, 60)``, an oblique
axis that tilts a part along both Y and Z. This pattern came up in a real
iPhone-holder generation: the plan locked the snippet ``.rotate((0,0,0),(1,0,0),-15)``
but the code generator combined the plan's axis direction with a non-zero pivot
on the start point, producing a visually-wrong backrest that the bbox-based
plan-conformance check did not catch.

Running this lint between code validation and execution gives the orchestrator a
fast, deterministic signal it can either auto-correct (when the intent is
unambiguous) or feed straight into the repair loop — saving the cost of running
the geometry engine, the renderer, and the vision critic before catching the
bug.

This module never imports CadQuery — it works on the source string alone so it
stays cheap and safe to call from anywhere.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Optional

from ..domain.models import DesignPlan, Rotation


# Tolerance for "is this float effectively zero?". Plans always emit clean
# integers, but the LLM frequently expresses pivots as expressions like
# ``-backrest_height_mm/2`` that constant-fold to messy decimals; this slack
# absorbs FP rounding without masking real off-axis components.
_AXIS_EPSILON = 1e-6

# Snake-case → CamelCase variants the LLM uses when naming variables that
# correspond to plan components ("backrest" plan name → "backrest_panel" or
# "back_rest"). We match conservatively; only direct substring overlap counts
# as a candidate so we don't auto-fix a rotation that happens to share a token.
_NAME_TOKEN_SPLIT = re.compile(r"[_\-\s]+")


@dataclass
class LintFinding:
    """One detected lint problem."""
    line: int                       # 1-indexed source line, or 0 if unknown
    code: str                       # short stable identifier ("rotate_oblique_axis")
    severity: str                   # "error" | "warning" | "info"
    message: str                    # human-readable, included in repair prompts
    suggested_fix: Optional[str] = None   # canonical rewrite or empty
    autofix_applied: bool = False   # set true after the source has been rewritten


@dataclass
class LintReport:
    findings: list[LintFinding] = field(default_factory=list)
    # Source code after auto-fixes have been folded in. Equal to the input
    # source when no auto-fix fired.
    rewritten_source: str = ""

    @property
    def has_blocking(self) -> bool:
        """True if any non-auto-fixed finding is severe enough to block."""
        return any(
            f.severity == "error" and not f.autofix_applied
            for f in self.findings
        )

    @property
    def autofix_summary(self) -> list[str]:
        """Short bullet list of fixes the lint applied — for the timeline."""
        return [
            f"line {f.line}: {f.message}"
            for f in self.findings
            if f.autofix_applied
        ]

    @property
    def blocking_messages(self) -> list[str]:
        return [
            f"line {f.line}: {f.message}"
            + (f"\n  suggested fix: {f.suggested_fix}" if f.suggested_fix else "")
            for f in self.findings
            if f.severity == "error" and not f.autofix_applied
        ]


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _collect_top_level_constants(tree: ast.Module) -> dict[str, float]:
    """Constant-fold top-level ``name = number`` assignments.

    The LLM commonly writes ``.rotate((0, 0, -backrest_height/2), …)`` where
    ``backrest_height = 120.0`` is declared at the top of the file. We want
    the lint to evaluate the actual rotation axis with those substitutions
    rather than missing the bug because the args aren't pure literals.

    We deliberately support only what the planner asks the LLM to emit at the
    top of the file: simple float/int assignments and simple ``a / b`` divisions
    or ``-name`` negations of an already-known constant. Anything more complex
    is skipped, leaving the call expression "unknown" and the lint conservative.
    """
    consts: dict[str, float] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        name = node.targets[0].id
        value = _try_eval_const(node.value, consts)
        if value is not None:
            consts[name] = value
    return consts


def _try_eval_const(node: ast.AST, consts: dict[str, float]) -> Optional[float]:
    """Best-effort constant evaluation for the small subset of expressions the
    LLM uses inside ``.rotate(...)`` argument tuples. Returns None when the
    expression isn't statically known."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _try_eval_const(node.operand, consts)
        return -inner if inner is not None else None
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.UAdd):
        return _try_eval_const(node.operand, consts)
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    if isinstance(node, ast.BinOp):
        left = _try_eval_const(node.left, consts)
        right = _try_eval_const(node.right, consts)
        if left is None or right is None:
            return None
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div) and right != 0:
            return left / right
        if isinstance(node.op, ast.FloorDiv) and right != 0:
            return left // right
    return None


def _eval_tuple_arg(node: ast.AST, consts: dict[str, float]) -> Optional[tuple[float, float, float]]:
    """Evaluate a 3-tuple/list argument. Returns None on partial knowledge so
    the caller can short-circuit conservatively (no false positives on
    dynamically computed axes)."""
    if not isinstance(node, (ast.Tuple, ast.List)):
        return None
    if len(node.elts) != 3:
        return None
    xs = [_try_eval_const(e, consts) for e in node.elts]
    if any(v is None for v in xs):
        return None
    return float(xs[0]), float(xs[1]), float(xs[2])


def _classify_axis(direction: tuple[float, float, float]) -> Optional[str]:
    """Return 'X' / 'Y' / 'Z' if ``direction`` is axis-aligned (one nonzero
    component, magnitude > epsilon). Returns None otherwise."""
    nonzero = [i for i, v in enumerate(direction) if abs(v) > _AXIS_EPSILON]
    if len(nonzero) != 1:
        return None
    return ("X", "Y", "Z")[nonzero[0]]


def _dominant_axis(direction: tuple[float, float, float]) -> Optional[str]:
    """Return the single axis with the largest magnitude. Used by the
    auto-fix heuristic: if a (1, 0, 60)-style oblique axis has 60× more Z
    than X, the LLM clearly *intended* Z; correcting it to pure X would
    silently change the design. Only return when ONE axis dominates by ≥2×.
    """
    mags = [abs(v) for v in direction]
    if max(mags) < _AXIS_EPSILON:
        return None
    sorted_mags = sorted(mags, reverse=True)
    if sorted_mags[1] > 1e-6 and sorted_mags[0] < 2 * sorted_mags[1]:
        return None  # ambiguous — refuse to guess
    idx = max(range(3), key=lambda i: mags[i])
    return ("X", "Y", "Z")[idx]


def _name_tokens(name: str) -> set[str]:
    """Lowercase tokens of a name for fuzzy matching plan components to code
    variables. ``rear_gusset_left`` → {'rear', 'gusset', 'left'}."""
    return {t for t in _NAME_TOKEN_SPLIT.split(name.lower()) if t}


def _find_matching_plan_rotation(
    var_name: Optional[str],
    plan_rotations: list[tuple[str, Rotation]],
) -> Optional[Rotation]:
    """Return the plan-locked rotation whose component name overlaps most with
    ``var_name``. Returns None when ``var_name`` doesn't share any token with a
    plan component (we won't auto-correct on a wild guess).
    """
    if not var_name or not plan_rotations:
        return None
    var_tokens = _name_tokens(var_name)
    if not var_tokens:
        return None
    best: tuple[int, Optional[Rotation]] = (0, None)
    for comp_name, rot in plan_rotations:
        comp_tokens = _name_tokens(comp_name)
        overlap = len(var_tokens & comp_tokens)
        if overlap > best[0]:
            best = (overlap, rot)
    return best[1]


def _assignment_target_name(parent_stmt: Optional[ast.AST]) -> Optional[str]:
    """If the enclosing statement is ``name = <expr>`` or ``name = (... .rotate(...))``,
    return the LHS name. Used to associate a ``.rotate(...)`` call with the
    variable it builds so we can match it back to a plan component."""
    if not isinstance(parent_stmt, ast.Assign):
        return None
    if len(parent_stmt.targets) != 1:
        return None
    tgt = parent_stmt.targets[0]
    if isinstance(tgt, ast.Name):
        return tgt.id
    return None


# ---------------------------------------------------------------------------
# Rotate lint
# ---------------------------------------------------------------------------


def _format_canonical_rotate(
    pivot: tuple[float, float, float],
    axis: str,
    angle_deg: float,
) -> str:
    """Render the canonical ``.rotate(p1, p2, angle)`` snippet that rotates
    around ``axis`` through ``pivot``."""
    deltas = {"X": (1, 0, 0), "Y": (0, 1, 0), "Z": (0, 0, 1)}[axis]
    px, py, pz = pivot
    qx, qy, qz = px + deltas[0], py + deltas[1], pz + deltas[2]
    def _fmt(v: float) -> str:
        if abs(v - round(v)) < 1e-6:
            return f"{int(round(v))}"
        return f"{v:g}"
    return f".rotate(({_fmt(px)}, {_fmt(py)}, {_fmt(pz)}), ({_fmt(qx)}, {_fmt(qy)}, {_fmt(qz)}), {_fmt(angle_deg)})"


def _call_chain_contains_method(node: ast.AST, method_name: str) -> bool:
    """Return True if a fluent receiver chain contains ``.<method_name>(...)``."""
    current: Optional[ast.AST] = node
    while isinstance(current, ast.Call):
        func = current.func
        if isinstance(func, ast.Attribute):
            if func.attr == method_name:
                return True
            current = func.value
            continue
        break
    return False


def _check_rotate_call(
    call: ast.Call,
    parent_stmt: Optional[ast.AST],
    consts: dict[str, float],
    plan_rotations: list[tuple[str, Rotation]],
) -> Optional[tuple[LintFinding, Optional[str]]]:
    """Return (finding, replacement_segment) if ``call`` is a problematic
    ``.rotate(...)``. ``replacement_segment`` is the canonical snippet to
    splice in for an auto-fix, or None when the lint just reports.
    """
    # Must look like ``X.rotate(...)`` — instance call, attribute "rotate".
    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr != "rotate":
        return None
    # CadQuery rotate takes exactly 3 positional args; tolerate kwargs by name.
    args = list(call.args)
    if len(args) < 3:
        return None
    p1 = _eval_tuple_arg(args[0], consts)
    p2 = _eval_tuple_arg(args[1], consts)
    angle = _try_eval_const(args[2], consts)
    if p1 is None or p2 is None or angle is None:
        # Dynamic args — refuse to lint. False positives here are expensive
        # because they trigger repair cycles on legitimately complex code.
        return None
    direction = (p2[0] - p1[0], p2[1] - p1[1], p2[2] - p1[2])
    actual_axis = _classify_axis(direction)
    var_name = _assignment_target_name(parent_stmt)
    plan_rot = _find_matching_plan_rotation(var_name, plan_rotations)
    if actual_axis is not None and plan_rot is not None:
        expected_axis = plan_rot.axis.upper()
        expected_angle = plan_rot.angle_deg
        mismatches: list[str] = []
        if actual_axis != expected_axis:
            mismatches.append(
                f"axis is {actual_axis} but the plan locks {expected_axis}"
            )
        if abs(angle - expected_angle) > 0.25:
            mismatches.append(
                f"angle is {angle:g} degrees but the plan locks {expected_angle:g} degrees"
            )
        if plan_rot.pivot is not None:
            expected_pivot = tuple(float(v) for v in plan_rot.pivot)
            if any(abs(a - b) > 1e-6 for a, b in zip(p1, expected_pivot)):
                mismatches.append(
                    f"pivot is ({p1[0]:g}, {p1[1]:g}, {p1[2]:g}) but the plan locks "
                    f"({expected_pivot[0]:g}, {expected_pivot[1]:g}, {expected_pivot[2]:g})"
                )
        if _call_chain_contains_method(call.func.value, "translate") and plan_rot.pivot is None:
            mismatches.append(
                "the component is translated before rotation; for plan-locked local tilts, "
                "rotate the component first, then apply the final translate so it does not "
                "orbit around a world-space axis"
            )
        if mismatches:
            suggestion = _format_canonical_rotate(
                tuple(plan_rot.pivot) if plan_rot.pivot else p1,
                expected_axis,
                expected_angle,
            )
            msg = (
                f"Axis-aligned `.rotate(...)` for `{var_name or 'unknown component'}` "
                f"does not match the structured design-plan rotation: "
                f"{'; '.join(mismatches)}. Copy the plan's rotation snippet verbatim "
                f"and place it before the final `.translate(...)` unless the plan declares "
                f"an explicit world-space pivot."
            )
            return (
                LintFinding(
                    line=getattr(call, "lineno", 0) or 0,
                    code="rotate_plan_mismatch",
                    severity="error",
                    message=msg,
                    suggested_fix=suggestion,
                    autofix_applied=False,
                ),
                None,
            )
    if actual_axis is not None:
        # Already axis-aligned — nothing to flag.
        return None

    # ---- Oblique axis detected. Decide what to do. ----
    var_name = _assignment_target_name(parent_stmt)
    plan_rot = _find_matching_plan_rotation(var_name, plan_rotations)

    # Infer the LLM's *intended* axis. Two signals, in order of reliability:
    # 1. ``p2`` itself is axis-aligned (e.g. ``(1, 0, 0)``). This is the
    #    smoking gun of "the LLM thought p2 was the direction vector" —
    #    which is exactly the misuse pattern this lint exists to catch. The
    #    oblique-ness then comes entirely from a non-zero pivot in p1.
    # 2. The dominant component of ``(p2 - p1)``. Less reliable because a
    #    large-magnitude pivot can drag dominance onto an axis the LLM did
    #    not intend.
    intended_axis = _classify_axis(p2)
    if intended_axis is None:
        intended_axis = _dominant_axis(direction)

    # Auto-fix only when both:
    # (a) the plan locked an axis for this component, AND
    # (b) our inferred LLM intent agrees with the plan.
    # When the inferred intent disagrees with the plan we cannot silently
    # rewrite — the LLM may legitimately have intended a different axis than
    # the plan suggested, and rewriting could change the design behind the
    # user's back.
    canonical_axis: Optional[str] = None
    autofix_basis = ""
    if plan_rot is not None and intended_axis == plan_rot.axis.upper():
        canonical_axis = plan_rot.axis.upper()
        # The plan's angle is authoritative — the LLM may have flipped the
        # sign to "compensate" for the wrong axis but the plan was right
        # the first time.
        canonical_angle = plan_rot.angle_deg
        canonical_pivot = tuple(plan_rot.pivot) if plan_rot.pivot else p1
        autofix_basis = f"plan-locked rotation for `{var_name}`"
    elif intended_axis is not None:
        # Report-only: surface a suggested canonical snippet using the
        # inferred intent, keep the LLM's pivot and angle so the suggestion
        # is plausible if the inference was right.
        canonical_axis = intended_axis
        canonical_angle = angle
        canonical_pivot = p1
    else:
        canonical_axis = None
        canonical_angle = angle
        canonical_pivot = p1

    suggestion = None
    if canonical_axis is not None:
        suggestion = _format_canonical_rotate(canonical_pivot, canonical_axis, canonical_angle)

    msg = (
        f".rotate(p1, p2, angle) takes TWO POINTS on the rotation axis line; "
        f"here (p2 - p1) = ({direction[0]:g}, {direction[1]:g}, {direction[2]:g}), which is "
        f"NOT axis-aligned, so the part rotates around an oblique axis instead of pure X/Y/Z. "
        f"Common cause: writing `.rotate(pivot, axis_direction, angle)` (the format used in some "
        f"other libraries). CadQuery wants two points: p2 must equal p1 plus a unit vector along "
        f"the desired axis."
    )

    autofix = bool(autofix_basis) and suggestion is not None
    if autofix:
        msg = f"{msg} Auto-corrected using {autofix_basis}: {suggestion}."

    return (
        LintFinding(
            line=getattr(call, "lineno", 0) or 0,
            code="rotate_oblique_axis",
            severity="error" if not autofix else "info",
            message=msg,
            suggested_fix=suggestion,
            autofix_applied=autofix,
        ),
        suggestion if autofix else None,
    )


def _splice_rotate_call(source: str, call: ast.Call, replacement: str) -> Optional[str]:
    """Replace the source text of one ``.rotate(...)`` call with
    ``replacement`` (which already starts with ``.rotate(``).

    Uses ``ast.get_source_segment`` for exact-span detection so we don't
    have to re-tokenize. Returns the new source on success, None on failure
    (call segment unresolvable; refuse to corrupt the file)."""
    seg = ast.get_source_segment(source, call)
    if seg is None or not seg:
        return None
    # ``seg`` is the *whole* call expression including the receiver
    # (``cq.Workplane(...).box(...).rotate(...)``). We only want to replace
    # the trailing ``.rotate(...)``. Find the last occurrence of ``.rotate(``
    # in the segment and rewrite from there.
    anchor = seg.rfind(".rotate(")
    if anchor < 0:
        return None
    new_seg = seg[:anchor] + replacement
    # Source replacement: find the first occurrence of ``seg`` in ``source``
    # starting from the call's line. ``get_source_segment`` guarantees the
    # exact substring is present.
    idx = source.find(seg)
    if idx < 0:
        return None
    return source[:idx] + new_seg + source[idx + len(seg):]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def lint_cadquery_source(
    source: str,
    plan: Optional[DesignPlan] = None,
) -> LintReport:
    """Run all static lints over ``source``.

    Returns a ``LintReport`` whose ``rewritten_source`` is the (possibly
    auto-corrected) source. Findings with ``autofix_applied=True`` have
    already been folded into the rewritten source; findings with
    ``severity='error'`` and ``autofix_applied=False`` should block execution
    and feed the repair loop.

    Conservative by design: lints only fire when the AST evaluation is
    unambiguous (constant args, top-level parameter folding only). Dynamic
    rotations are left alone.
    """
    report = LintReport(rewritten_source=source)
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # A syntax error is already caught by ``validate_cadquery_code``;
        # we return an empty report so we don't double-flag it.
        return report

    consts = _collect_top_level_constants(tree)

    plan_rotations: list[tuple[str, Rotation]] = []
    if plan is not None:
        for c in plan.components or []:
            if c.rotation is not None and c.rotation.axis:
                plan_rotations.append((c.name, c.rotation))

    # Walk the tree paired with the enclosing top-level statement so we can
    # associate each ``.rotate(...)`` with the variable it builds (which we
    # match to plan component names for the autofix anchor).
    pending_fixes: list[tuple[ast.Call, str]] = []
    for stmt in tree.body:
        for node in ast.walk(stmt):
            if not isinstance(node, ast.Call):
                continue
            result = _check_rotate_call(node, stmt, consts, plan_rotations)
            if result is None:
                continue
            finding, replacement = result
            report.findings.append(finding)
            if finding.autofix_applied and replacement is not None:
                pending_fixes.append((node, replacement))

    # Apply auto-fixes back-to-front so source offsets stay valid. Sorting by
    # ``col_offset`` and then line number — descending — handles overlapping
    # edits the cheap way without a structured rewrite library.
    pending_fixes.sort(
        key=lambda pair: (getattr(pair[0], "lineno", 0), getattr(pair[0], "col_offset", 0)),
        reverse=True,
    )
    rewritten = source
    for call, replacement in pending_fixes:
        candidate = _splice_rotate_call(rewritten, call, replacement)
        if candidate is None:
            # The splice failed — the in-place autofix flag is now misleading,
            # so demote it back to error so the repair branch handles it.
            for f in report.findings:
                if f.line == getattr(call, "lineno", 0) and f.autofix_applied:
                    f.autofix_applied = False
                    f.severity = "error"
            continue
        rewritten = candidate
    report.rewritten_source = rewritten

    return report
