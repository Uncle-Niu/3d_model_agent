"""Benchmark local Ollama models on CadQuery generation.

Run from repo root:
    python scripts/benchmark_ollama_cadquery.py --all
    python scripts/benchmark_ollama_cadquery.py --models qwen3.6:27b phi4:14b --case simple_mounting_block

Default benchmark shape:
    - 4 cases: 2 simple, 2 medium-hard
    - 3 repeats per model/case
    - fixed benchmark plan included in the code-generation prompt
    - deterministic CadQuery validity score
    - render + vision intent score averaged across qwen, gemma, and nemotron

The final score is intentionally split into deterministic validity and visual
intent matching. A model that makes syntactically valid but semantically wrong
geometry should no longer look like a top CAD model.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.cad.engine import (
    process_cadquery_code,
    strip_reasoning_leakage,
    try_patch_missing_result,
)
from backend.domain.models import DesignComponent, DesignPlan, HardConstraints, SoftConstraints
from backend.models.llm_service import LLMService, extract_code_from_response, plan_to_prompt_text
from backend.render import RenderService
from backend.vision.critic import VisionCritic


DEFAULT_VISION_MODELS = ["qwen3.6:27b", "gemma4:31b", "nemotron3:33b"]


@dataclass(frozen=True)
class BenchmarkCase:
    id: str
    difficulty: str
    prompt: str
    plan: DesignPlan


def _component(
    name: str,
    description: str,
    primitive: str,
    dimensions: dict[str, float],
    operation: str,
    position: list[float] | None = None,
    orientation: str = "",
) -> DesignComponent:
    return DesignComponent(
        name=name,
        description=description,
        primitive=primitive,
        dimensions=dimensions,
        operation=operation,
        position=position,
        orientation=orientation,
    )


CASES = [
    BenchmarkCase(
        id="simple_mounting_block",
        difficulty="simple",
        prompt=(
            "Create one FDM-printable CadQuery part: a rectangular mounting block "
            "60 mm long, 35 mm wide, and 12 mm tall. Add four vertical M4 through "
            "holes, one near each corner, centered 8 mm from the nearest long and "
            "short edges. Add a vertical 20 mm diameter circular lightening cutout "
            "through the center. Add 2 mm external fillets where they are safe. "
            "The final shape must be assigned to result."
        ),
        plan=DesignPlan(
            summary="Rectangular mounting block with four corner through holes and a center circular cutout.",
            overall_dimensions_mm=[60.0, 35.0, 12.0],
            components=[
                _component("base_block", "60 x 35 x 12 mm rectangular solid body", "box", {"length": 60, "width": 35, "height": 12}, "base"),
                _component("corner_holes", "Four M4 vertical through holes 8 mm in from each edge", "cylinder_cut_pattern", {"diameter": 4.2, "count": 4}, "cut"),
                _component("center_cutout", "20 mm diameter vertical circular lightening hole through the center", "cylinder_cut", {"diameter": 20, "height": 16}, "cut"),
                _component("external_fillets", "Small external fillets around safe outer edges", "fillet", {"radius": 2}, "fillet"),
            ],
            key_features=[
                "single rectangular block, not an assembly",
                "four visible corner through holes",
                "one centered circular through cutout larger than the screw holes",
                "rounded external edges",
                "overall proportions close to 60 x 35 x 12 mm",
            ],
        ),
    ),
    BenchmarkCase(
        id="simple_control_knob",
        difficulty="simple",
        prompt=(
            "Create one FDM-printable CadQuery control knob. The knob is a round "
            "cylinder 38 mm in diameter and 18 mm tall. Add 24 evenly spaced "
            "vertical grip ribs around the outside rim. Cut a D-shaped shaft bore "
            "from the bottom: 6 mm round shaft with one flat side, 12 mm deep. Add "
            "a shallow pointer notch on the top face near the front edge. Chamfer "
            "or fillet the top and bottom edges. Assign the final shape to result."
        ),
        plan=DesignPlan(
            summary="Cylindrical control knob with grip ribs, D-shaped bore, top pointer notch, and softened edges.",
            overall_dimensions_mm=[38.0, 38.0, 18.0],
            components=[
                _component("knob_body", "Main 38 mm diameter by 18 mm tall cylinder", "cylinder", {"diameter": 38, "height": 18}, "base"),
                _component("grip_ribs", "24 repeated raised vertical ribs around the outside", "patterned_boxes_or_cylinders", {"count": 24, "height": 16}, "union"),
                _component("d_shaft_bore", "Bottom D-shaped blind shaft bore 12 mm deep", "cylinder_plus_flat_cut", {"diameter": 6, "depth": 12}, "cut"),
                _component("pointer_notch", "Small shallow notch on top near front edge", "slot_cut", {"length": 12, "width": 2, "depth": 1.5}, "cut"),
                _component("edge_softening", "Chamfered or filleted top and bottom edges", "fillet_or_chamfer", {"size": 1}, "fillet"),
            ],
            key_features=[
                "round cylindrical knob body",
                "repeated raised grip ribs around the outer rim",
                "bottom D-shaped shaft bore or visibly flattened bore feature",
                "top pointer notch near one edge",
                "softened top and bottom edges",
            ],
        ),
    ),
    BenchmarkCase(
        id="medium_phone_stand",
        difficulty="medium",
        prompt=(
            "Create one FDM-printable desk stand for an iPhone 16 Pro Max in "
            "landscape or portrait use. The design should have a flat stable base "
            "about 120 mm wide by 95 mm deep by 6 mm thick, a backrest plate leaning "
            "back 15 degrees from vertical, two side guide rails sized for a phone "
            "about 78 mm wide and 9 mm thick with clearance, a bottom support lip, "
            "two triangular side gussets joining the base to the backrest, and a "
            "center cable notch through the bottom lip. Use printable wall thickness "
            "and fillets. Assign the final shape to result."
        ),
        plan=DesignPlan(
            summary="Angled phone stand with stable base, leaning backrest, side guides, bottom lip, gussets, and cable notch.",
            overall_dimensions_mm=[120.0, 95.0, 145.0],
            components=[
                _component("base_plate", "Stable flat base roughly 120 x 95 x 6 mm", "box", {"length": 120, "width": 95, "height": 6}, "base"),
                _component("angled_backrest", "Back support leaning back about 15 degrees from vertical", "box", {"width": 95, "height": 135, "thickness": 6}, "union"),
                _component("side_guides", "Two raised rails that keep the phone laterally centered", "box_pair", {"rail_thickness": 4, "rail_height": 25, "slot_width": 82}, "union"),
                _component("bottom_lip", "Front bottom ledge that supports the phone weight", "box", {"width": 95, "depth": 12, "height": 12}, "union"),
                _component("triangular_gussets", "Two triangular reinforcement ribs from base to backrest", "triangular_extrusions", {"count": 2, "thickness": 6}, "union"),
                _component("cable_notch", "Centered notch cut through the support lip for charging cable", "slot_or_cylinder_cut", {"width": 18, "height": 12}, "cut"),
            ],
            key_features=[
                "wide flat base for desk stability",
                "single backrest plate visibly angled backward",
                "pair of side guide rails forming a phone slot",
                "bottom support lip or ledge",
                "two triangular gussets connecting base and backrest",
                "center cable notch through the bottom lip",
                "filleted or chamfered printable edges",
            ],
        ),
    ),
    BenchmarkCase(
        id="medium_shelf_bracket",
        difficulty="medium",
        prompt=(
            "Create one FDM-printable right-angle shelf bracket. It needs a vertical "
            "wall plate 80 mm tall, 45 mm wide, 6 mm thick; a horizontal shelf plate "
            "70 mm deep, 45 mm wide, 6 mm thick; two triangular ribs between the "
            "plates; four countersunk screw holes on the wall plate in a rectangular "
            "pattern; two vertical shelf screw holes on the horizontal plate; and "
            "2 mm fillets at stress concentration edges where safe. Assign result."
        ),
        plan=DesignPlan(
            summary="Right-angle shelf bracket with wall plate, shelf plate, two ribs, screw holes, countersinks, and fillets.",
            overall_dimensions_mm=[45.0, 70.0, 80.0],
            components=[
                _component("wall_plate", "Vertical rectangular wall plate", "box", {"height": 80, "width": 45, "thickness": 6}, "base"),
                _component("shelf_plate", "Horizontal rectangular shelf support plate", "box", {"depth": 70, "width": 45, "thickness": 6}, "union"),
                _component("triangular_ribs", "Two triangular ribs bridging inside corner", "triangular_extrusions", {"count": 2, "thickness": 5}, "union"),
                _component("wall_holes", "Four countersunk holes on wall plate", "countersunk_hole_pattern", {"count": 4, "diameter": 4.5}, "cut"),
                _component("shelf_holes", "Two vertical screw holes on horizontal plate", "hole_pattern", {"count": 2, "diameter": 4.5}, "cut"),
                _component("stress_fillets", "Small fillets along safe outside and rib junction edges", "fillet", {"radius": 2}, "fillet"),
            ],
            key_features=[
                "vertical wall plate and horizontal shelf plate at a right angle",
                "two triangular support ribs in the inside corner",
                "four wall screw holes arranged as a rectangle",
                "wall holes have countersink or enlarged top bevels",
                "two screw holes through the horizontal shelf plate",
                "rounded stress-relief edges where visible",
            ],
        ),
    ),
]


@dataclass
class AttemptResult:
    ok: bool
    code: str
    raw: str
    generation_prompt: str
    process: dict[str, Any]
    latency_s: float
    repaired: bool = False
    repair_count: int = 0


def _safe_name(text: str) -> str:
    return text.replace(":", "_").replace("/", "_").replace("\\", "_")


def _ollama_models() -> list[str]:
    completed = subprocess.run(
        ["ollama", "list"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    lines = completed.stdout.splitlines()[1:]
    models: list[str] = []
    for line in lines:
        parts = line.split()
        if parts:
            models.append(parts[0])
    return models


def _sanitize_for_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _sanitize_for_json(v) for k, v in value.items() if not k.startswith("_")}
    if isinstance(value, list):
        return [_sanitize_for_json(v) for v in value]
    if hasattr(value, "model_dump"):
        return _sanitize_for_json(value.model_dump())
    if isinstance(value, Path):
        return str(value)
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def _prepare_code(raw: str) -> str:
    code = extract_code_from_response(raw)
    stripped = strip_reasoning_leakage(code)
    if stripped:
        code = stripped
    patched = try_patch_missing_result(code)
    if patched:
        code = patched
    return code


def _generation_prompt(case: BenchmarkCase, generation_context: str) -> str:
    if generation_context == "direct":
        return case.prompt

    plan_text = plan_to_prompt_text(case.plan)
    return f"""\
Generate CadQuery code for the original request, implementing the fixed design plan exactly.
This benchmark is testing CAD code generation, not planning. Do not simplify or replace the
planned components with placeholders.

## Original Request
{case.prompt}

{plan_text}

## Benchmark Requirements
- Implement every listed component and key feature where geometrically feasible.
- Use the named dimensions from the plan as explicit parameters near the top of the source.
- Assign the final CadQuery shape to `result`.
"""


def _deterministic_score(result: AttemptResult) -> float:
    process = result.process
    if not process.get("success"):
        failure_type = process.get("failure_type")
        return {
            "syntax_error": 5.0,
            "execution_error": 15.0,
            "geometry_invalid": 25.0,
            "constraint_violation": 30.0,
            "cad_worker_timeout": 10.0,
            "cad_worker_crash": 10.0,
            "cad_worker_error": 10.0,
            "cad_worker_bad_result": 10.0,
        }.get(str(failure_type), 0.0)

    score = 70.0
    manufacturability = process.get("manufacturability")
    if manufacturability is not None:
        m_score = getattr(manufacturability, "score", None)
        if m_score is None and isinstance(manufacturability, dict):
            m_score = manufacturability.get("score")
        if isinstance(m_score, (int, float)):
            score += 20.0 * float(m_score)
    else:
        score += 10.0

    stats = process.get("geometry_stats") or {}
    face_count = stats.get("face_count", 0) if isinstance(stats, dict) else 0
    edge_count = stats.get("edge_count", 0) if isinstance(stats, dict) else 0
    if isinstance(face_count, int) and isinstance(edge_count, int):
        score += min(10.0, (face_count + edge_count / 2.0) / 12.0)

    warnings = process.get("warnings") or []
    score -= min(10.0, len(warnings) * 1.5)
    if result.repaired:
        score -= min(8.0, result.repair_count * 4.0)
    return round(max(0.0, min(100.0, score)), 2)


def _composite_score(deterministic_score: float, vision_avg_score: float | None) -> float:
    if vision_avg_score is None:
        return deterministic_score
    return round(deterministic_score * 0.45 + (vision_avg_score * 100.0) * 0.55, 2)


def _write_attempt_artifacts(
    result: AttemptResult,
    artifact_dir: Path,
    *,
    vision: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    raw_path = artifact_dir / "raw_response.txt"
    prompt_path = artifact_dir / "generation_prompt.txt"
    code_path = artifact_dir / "extracted_code.py"
    process_path = artifact_dir / "process_result.json"
    prompt_path.write_text(result.generation_prompt, encoding="utf-8", errors="replace")
    raw_path.write_text(result.raw, encoding="utf-8", errors="replace")
    code_path.write_text(result.code, encoding="utf-8", errors="replace")
    process_path.write_text(json.dumps(_sanitize_for_json(result.process), indent=2), encoding="utf-8")

    artifacts = {
        "generation_prompt": str(prompt_path),
        "raw_response": str(raw_path),
        "extracted_code": str(code_path),
        "process_result": str(process_path),
    }
    if vision is not None:
        vision_path = artifact_dir / "vision_results.json"
        vision_path.write_text(json.dumps(_sanitize_for_json(vision), indent=2), encoding="utf-8")
        artifacts["vision_results"] = str(vision_path)
    return artifacts


def _worker_process_code_main() -> int:
    """Child-process CAD execution/rendering.

    CadQuery/OCC can occasionally terminate the interpreter from native code
    when handed pathological generated geometry. Running this stage in a child
    keeps the long benchmark alive and lets the parent score the attempt as a
    CAD worker crash instead of losing all accumulated progress.
    """
    code_path = Path(sys.argv[2])
    output_dir = Path(sys.argv[3])
    model_name = sys.argv[4] if len(sys.argv) > 4 else "benchmark"
    result_path = output_dir / "_worker_result.json"
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        code = code_path.read_text(encoding="utf-8", errors="replace")
        process = process_cadquery_code(
            code,
            output_dir,
            model_name=model_name,
            constraints=HardConstraints(),
        )
        shape = process.get("_shape")
        if process.get("success") and shape is not None:
            render_result = RenderService().render_shape(
                shape,
                output_dir,
                model_name=model_name,
                include_sections=False,
            )
            process["render_success"] = render_result.success
            process["render_message"] = render_result.message
            process["render_paths"] = render_result.renders
            if not render_result.success:
                process["warnings"] = list(process.get("warnings") or []) + [
                    f"Rendering failed: {render_result.message}"
                ]
        result_path.write_text(
            json.dumps(_sanitize_for_json(process), indent=2),
            encoding="utf-8",
        )
        return 0
    except Exception as exc:
        result_path.write_text(
            json.dumps(
                {
                    "success": False,
                    "failure_type": "cad_worker_error",
                    "message": f"CAD worker error: {exc}",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return 0


def _process_code_subprocess(
    code: str,
    output_dir: Path,
    model_name: str,
    timeout_s: float,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    code_path = output_dir / "_candidate_code.py"
    result_path = output_dir / "_worker_result.json"
    code_path.write_text(code, encoding="utf-8", errors="replace")

    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--_worker-process-code",
                str(code_path),
                str(output_dir),
                model_name,
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "failure_type": "cad_worker_timeout",
            "message": f"CAD worker timed out after {timeout_s:.0f}s",
        }

    if result_path.exists():
        try:
            return json.loads(result_path.read_text(encoding="utf-8", errors="replace"))
        except Exception as exc:
            return {
                "success": False,
                "failure_type": "cad_worker_bad_result",
                "message": f"CAD worker wrote unreadable result: {exc}",
            }

    return {
        "success": False,
        "failure_type": "cad_worker_crash",
        "message": (
            f"CAD worker exited with code {completed.returncode}. "
            f"stdout={completed.stdout[-500:]!r} stderr={completed.stderr[-500:]!r}"
        ),
    }


async def _run_generation(
    *,
    model: str,
    case: BenchmarkCase,
    generation_context: str,
    output_dir: Path,
    repairs: int,
    timeout_s: float,
    hard_constraints: HardConstraints,
    soft_constraints: SoftConstraints,
) -> AttemptResult:
    llm = LLMService(model=model)
    started = time.perf_counter()
    prompt = _generation_prompt(case, generation_context)
    raw = await asyncio.wait_for(
        llm.generate_cadquery(prompt, hard_constraints, soft_constraints),
        timeout=timeout_s,
    )
    code = _prepare_code(raw)
    process = _process_code_subprocess(
        code,
        output_dir,
        model_name="benchmark",
        timeout_s=timeout_s,
    )
    repair_count = 0

    while not process.get("success") and repair_count < repairs:
        repair_count += 1
        repaired_raw = await asyncio.wait_for(
            llm.repair_cadquery(
                code,
                process.get("message", "Unknown failure"),
                repair_count + 1,
                hard_constraints=hard_constraints,
                soft_constraints=soft_constraints,
                failure_type=process.get("failure_type"),
                geometry_stats=process.get("geometry_stats"),
            ),
            timeout=timeout_s,
        )
        raw = raw + "\n\n--- repair ---\n\n" + repaired_raw
        code = _prepare_code(repaired_raw)
        process = _process_code_subprocess(
            code,
            output_dir / f"repair_{repair_count}",
            model_name="benchmark",
            timeout_s=timeout_s,
        )

    return AttemptResult(
        ok=bool(process.get("success")),
        code=code,
        raw=raw,
        generation_prompt=prompt,
        process=process,
        latency_s=time.perf_counter() - started,
        repaired=repair_count > 0 and bool(process.get("success")),
        repair_count=repair_count,
    )


async def _run_vision_judges(
    *,
    result: AttemptResult,
    case: BenchmarkCase,
    run_dir: Path,
    vision_models: list[str],
    timeout_s: float,
) -> tuple[list[dict[str, Any]], float | None, float]:
    if not result.ok:
        return [], None, 0.0

    started = time.perf_counter()
    render_paths = result.process.get("render_paths")
    if isinstance(render_paths, dict) and render_paths:
        evaluations: list[dict[str, Any]] = [
            {
                "stage": "render",
                "success": bool(result.process.get("render_success", True)),
                "message": result.process.get("render_message", "Rendered by CAD worker"),
                "renders": render_paths,
            }
        ]
    else:
        evaluations = []

    shape = result.process.get("_shape")
    if not render_paths and shape is None:
        return [
            {
                "stage": "render",
                "success": False,
                "message": "No in-memory shape available for rendering.",
            }
        ], None, time.perf_counter() - started

    if not render_paths:
        render_result = RenderService().render_shape(shape, run_dir, model_name=case.id, include_sections=False)
        if not render_result.success:
            return [
                {
                    "stage": "render",
                    "success": False,
                    "message": render_result.message,
                }
            ], None, time.perf_counter() - started
        render_paths = render_result.renders
        evaluations.append(
            {
                "stage": "render",
                "success": True,
                "message": render_result.message,
                "renders": render_result.renders,
            }
        )

    scores: list[float] = []

    for vision_model in vision_models:
        judge_started = time.perf_counter()
        try:
            critic = VisionCritic(model=vision_model, timeout=timeout_s)
            critique = await critic.critique(
                render_paths,
                case.prompt,
                geometry_stats=result.process.get("geometry_stats"),
                plan=case.plan,
            )
            entry: dict[str, Any] = {
                "stage": "vision",
                "model": vision_model,
                "success": critique.success,
                "message": critique.message,
                "latency_s": round(time.perf_counter() - judge_started, 2),
                "matches_intent": critique.matches_intent,
                "raw_response": critique.raw_response,
            }
            if critique.report:
                entry["score"] = critique.report.overall_printability
                entry["confidence"] = critique.report.confidence
                entry["issues"] = [issue.model_dump() for issue in critique.report.issues]
                if critique.success:
                    scores.append(float(critique.report.overall_printability))
            evaluations.append(entry)
        except Exception as exc:
            evaluations.append(
                {
                    "stage": "vision",
                    "model": vision_model,
                    "success": False,
                    "message": str(exc),
                    "latency_s": round(time.perf_counter() - judge_started, 2),
                }
            )

    avg_score = round(sum(scores) / len(scores), 4) if len(scores) == len(vision_models) else None
    return evaluations, avg_score, time.perf_counter() - started


def _summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_model: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_model.setdefault(row["model"], []).append(row)

    summary = []
    for model, items in by_model.items():
        n = len(items)
        final_pass = sum(1 for i in items if i.get("cad_success"))
        first_pass = sum(1 for i in items if i.get("cad_success") and i.get("repairs_used") == 0)
        avg_score = sum(float(i.get("score") or 0.0) for i in items) / max(1, n)
        avg_det = sum(float(i.get("deterministic_score") or 0.0) for i in items) / max(1, n)
        vision_items = [i for i in items if isinstance(i.get("vision_avg_score"), (int, float))]
        avg_vision = (
            sum(float(i["vision_avg_score"]) for i in vision_items) / len(vision_items)
            if vision_items
            else None
        )
        gen_latencies = [float(i["generation_latency_s"]) for i in items if isinstance(i.get("generation_latency_s"), (int, float))]
        total_latencies = [float(i["total_latency_s"]) for i in items if isinstance(i.get("total_latency_s"), (int, float))]
        vision_judge_total = sum(int(i.get("vision_success_count") or 0) for i in items)
        vision_judge_expected = sum(int(i.get("vision_expected_count") or 0) for i in items)
        vision_match = sum(int(i.get("vision_match_count") or 0) for i in items)
        summary.append(
            {
                "model": model,
                "runs": n,
                "cad_success_rate": round(final_pass / n, 3),
                "first_pass_rate": round(first_pass / n, 3),
                "avg_score": round(avg_score, 2),
                "avg_deterministic_score": round(avg_det, 2),
                "avg_vision_score": round(avg_vision, 4) if avg_vision is not None else None,
                "vision_judge_success_rate": round(vision_judge_total / vision_judge_expected, 3) if vision_judge_expected else None,
                "vision_match_rate": round(vision_match / vision_judge_total, 3) if vision_judge_total else None,
                "avg_generation_latency_s": round(sum(gen_latencies) / len(gen_latencies), 1) if gen_latencies else None,
                "avg_total_latency_s": round(sum(total_latencies) / len(total_latencies), 1) if total_latencies else None,
            }
        )
    summary.sort(
        key=lambda r: (
            r["cad_success_rate"],
            r["avg_vision_score"] if r["avg_vision_score"] is not None else -1,
            r["avg_score"],
            r["first_pass_rate"],
        ),
        reverse=True,
    )
    return summary


def _print_summary(summary: list[dict[str, Any]]) -> None:
    print("\nRank | Model | CAD | First | Vision | Score | Total Latency")
    print("---- | ----- | --- | ----- | ------ | ----- | -------------")
    for idx, row in enumerate(summary, 1):
        vision = "n/a" if row["avg_vision_score"] is None else f"{row['avg_vision_score']:.2f}"
        total = "n/a" if row["avg_total_latency_s"] is None else f"{row['avg_total_latency_s']:.1f}s"
        print(
            f"{idx:>4} | {row['model']} | "
            f"{row['cad_success_rate']:.0%} | "
            f"{row['first_pass_rate']:.0%} | "
            f"{vision} | "
            f"{row['avg_score']:.1f} | "
            f"{total}"
        )


def _write_report(
    *,
    out_dir: Path,
    run_id: str,
    models: list[str],
    repeats: int,
    repairs: int,
    skip_vision: bool,
    vision_models: list[str],
    generation_context: str,
    selected_cases: list[BenchmarkCase],
    rows: list[dict[str, Any]],
    partial: bool,
) -> Path:
    summary = _summarize(rows)
    report = {
        "run_id": run_id,
        "partial": partial,
        "models": models,
        "repeats": repeats,
        "repairs": repairs,
        "vision_enabled": not skip_vision,
        "vision_models": [] if skip_vision else vision_models,
        "generation_context": generation_context,
        "score_formula": "0.45 * deterministic_score + 0.55 * (vision_avg_score * 100), or deterministic_score if vision is skipped/unavailable",
        "cases": [
            {
                "id": case.id,
                "difficulty": case.difficulty,
                "prompt": case.prompt,
                "plan": case.plan.model_dump(),
            }
            for case in selected_cases
        ],
        "summary": summary,
        "runs": _sanitize_for_json(rows),
    }
    report_path = out_dir / ("results.partial.json" if partial else "results.json")
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report_path


async def amain() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", nargs="+", help="Ollama generation model tags to test")
    parser.add_argument("--all", action="store_true", help="Benchmark every model from `ollama list`")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--repairs", type=int, default=1, help="LLM repair attempts after failed generation")
    parser.add_argument("--timeout", type=float, default=240.0, help="Seconds per generation/repair call")
    parser.add_argument("--vision-timeout", type=float, default=180.0, help="Seconds per vision model call")
    parser.add_argument("--vision-models", nargs="+", default=DEFAULT_VISION_MODELS)
    parser.add_argument("--skip-vision", action="store_true", help="Only run deterministic CadQuery checks")
    parser.add_argument(
        "--generation-context",
        choices=("planned", "direct"),
        default="planned",
        help="planned feeds the fixed benchmark plan to codegen; direct uses only the prose prompt",
    )
    parser.add_argument(
        "--case",
        action="append",
        dest="cases",
        help="Case id to run; repeat this flag for multiple cases",
    )
    parser.add_argument("--out", type=Path, default=Path("data") / "benchmarks" / "cadquery_models")
    parser.add_argument("--keep-temp", action="store_true", help="Keep per-run CAD exports")
    args = parser.parse_args()

    if args.all:
        models = _ollama_models()
    elif args.models:
        models = args.models
    else:
        models = _ollama_models()

    if not models:
        raise SystemExit("No models found. Start Ollama and/or pass --models.")

    selected_cases = CASES
    if args.cases:
        wanted = set(args.cases)
        selected_cases = [case for case in CASES if case.id in wanted]
        missing = sorted(wanted - {case.id for case in selected_cases})
        if missing:
            known = ", ".join(case.id for case in CASES)
            raise SystemExit(f"Unknown --case value(s): {', '.join(missing)}. Known cases: {known}")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir = args.out / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    temp_root = out_dir / "cad_exports" if args.keep_temp else Path(tempfile.mkdtemp(prefix="cad_bench_"))
    rows: list[dict[str, Any]] = []

    hard_constraints = HardConstraints()
    soft_constraints = SoftConstraints()

    try:
        for model in models:
            print(f"\n== {model} ==")
            for repeat in range(args.repeats):
                for case in selected_cases:
                    case_id = case.id
                    print(f"  {case_id} repeat={repeat + 1} ... ", end="", flush=True)
                    run_dir = temp_root / _safe_name(model) / f"{case_id}_{repeat + 1}"
                    run_dir.mkdir(parents=True, exist_ok=True)
                    artifact_dir = out_dir / "artifacts" / _safe_name(model) / f"{case_id}_{repeat + 1}"

                    try:
                        result = await _run_generation(
                            model=model,
                            case=case,
                            generation_context=args.generation_context,
                            output_dir=run_dir,
                            repairs=args.repairs,
                            timeout_s=args.timeout,
                            hard_constraints=hard_constraints,
                            soft_constraints=soft_constraints,
                        )
                        deterministic_score = _deterministic_score(result)
                        vision_results: list[dict[str, Any]] = []
                        vision_avg_score: float | None = None
                        vision_latency_s = 0.0
                        if result.ok and not args.skip_vision:
                            vision_results, vision_avg_score, vision_latency_s = await _run_vision_judges(
                                result=result,
                                case=case,
                                run_dir=run_dir,
                                vision_models=args.vision_models,
                                timeout_s=args.vision_timeout,
                            )

                        score = _composite_score(deterministic_score, vision_avg_score)
                        artifacts = _write_attempt_artifacts(result, artifact_dir, vision=vision_results)
                        vision_success_count = sum(
                            1 for item in vision_results if item.get("stage") == "vision" and item.get("success")
                        )
                        vision_match_count = sum(
                            1
                            for item in vision_results
                            if item.get("stage") == "vision" and item.get("success") and item.get("matches_intent")
                        )
                        row = {
                            "model": model,
                            "case_id": case_id,
                            "difficulty": case.difficulty,
                            "repeat": repeat + 1,
                            "cad_success": result.ok,
                            "score": score,
                            "deterministic_score": deterministic_score,
                            "vision_avg_score": vision_avg_score,
                            "vision_success_count": vision_success_count,
                            "vision_match_count": vision_match_count,
                            "vision_expected_count": 0 if args.skip_vision or not result.ok else len(args.vision_models),
                            "generation_latency_s": round(result.latency_s, 2),
                            "vision_latency_s": round(vision_latency_s, 2),
                            "total_latency_s": round(result.latency_s + vision_latency_s, 2),
                            "repairs_used": result.repair_count,
                            "failure_type": result.process.get("failure_type"),
                            "message": result.process.get("message", ""),
                            "warnings": result.process.get("warnings", []),
                            "violations": result.process.get("violations", []),
                            "geometry_stats": result.process.get("geometry_stats", {}),
                            "code_lines": len(result.code.splitlines()),
                            "artifacts": artifacts,
                            "output_dir": str(run_dir) if args.keep_temp else "",
                        }
                        rows.append(row)
                        _write_report(
                            out_dir=out_dir,
                            run_id=run_id,
                            models=models,
                            repeats=args.repeats,
                            repairs=args.repairs,
                            skip_vision=args.skip_vision,
                            vision_models=args.vision_models,
                            generation_context=args.generation_context,
                            selected_cases=selected_cases,
                            rows=rows,
                            partial=True,
                        )

                        status = "ok" if result.ok else f"fail:{row['failure_type']}"
                        vision_text = "vision=skip"
                        if not args.skip_vision and result.ok:
                            vision_text = (
                                f"vision={vision_avg_score:.2f} ({vision_success_count}/{len(args.vision_models)})"
                                if vision_avg_score is not None
                                else f"vision=fail (0/{len(args.vision_models)})"
                            )
                        print(
                            f"{status} det={deterministic_score:.1f} "
                            f"{vision_text} score={score:.1f} "
                            f"{result.latency_s + vision_latency_s:.1f}s repairs={result.repair_count}"
                        )
                    except Exception as exc:
                        rows.append(
                            {
                                "model": model,
                                "case_id": case_id,
                                "difficulty": case.difficulty,
                                "repeat": repeat + 1,
                                "cad_success": False,
                                "score": 0.0,
                                "deterministic_score": 0.0,
                                "vision_avg_score": None,
                                "generation_latency_s": None,
                                "vision_latency_s": None,
                                "total_latency_s": None,
                                "repairs_used": 0,
                                "failure_type": "benchmark_error",
                                "message": str(exc),
                            }
                        )
                        _write_report(
                            out_dir=out_dir,
                            run_id=run_id,
                            models=models,
                            repeats=args.repeats,
                            repairs=args.repairs,
                            skip_vision=args.skip_vision,
                            vision_models=args.vision_models,
                            generation_context=args.generation_context,
                            selected_cases=selected_cases,
                            rows=rows,
                            partial=True,
                        )
                        print(f"error: {exc}")

        report_path = _write_report(
            out_dir=out_dir,
            run_id=run_id,
            models=models,
            repeats=args.repeats,
            repairs=args.repairs,
            skip_vision=args.skip_vision,
            vision_models=args.vision_models,
            generation_context=args.generation_context,
            selected_cases=selected_cases,
            rows=rows,
            partial=False,
        )
        summary = _summarize(rows)

        _print_summary(summary)
        print(f"\nWrote {report_path}")
    finally:
        if not args.keep_temp:
            shutil.rmtree(temp_root, ignore_errors=True)
        else:
            print(f"Kept CAD exports under {temp_root}")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--_worker-process-code":
        raise SystemExit(_worker_process_code_main())
    asyncio.run(amain())


if __name__ == "__main__":
    main()
