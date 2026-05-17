"""Benchmark local Ollama models on CadQuery generation.

Run from repo root:
    python scripts/benchmark_ollama_cadquery.py --models qwen3.6:27b phi4:14b
    python scripts/benchmark_ollama_cadquery.py --all --repeats 2

The benchmark deliberately scores generated code with the same CAD execution
path used by the app, so the result reflects CadQuery syntax, OCC robustness,
exportability, and manufacturability warnings rather than generic coding skill.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import tempfile
import time
import sys
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
from backend.domain.models import HardConstraints, SoftConstraints
from backend.models.llm_service import LLMService, extract_code_from_response


DEFAULT_PROMPTS = [
    {
        "id": "simple_hole_block",
        "prompt": (
            "Create a 60 x 35 x 12 mm FDM-printable mounting block with four "
            "M4 through holes near the corners, a 20 mm center lightening cutout, "
            "2 mm external fillets, and assign the final CadQuery shape to result."
        ),
    },
    {
        "id": "phone_stand",
        "prompt": (
            "Design a desk stand for an iPhone 16 Pro Max so I can watch movies. "
            "It needs a stable base, angled backrest, side guide rails, bottom lip, "
            "triangular gussets, a center cable notch, and printable fillets."
        ),
    },
    {
        "id": "shelf_bracket",
        "prompt": (
            "Create a right-angle shelf bracket for 3D printing: vertical wall plate, "
            "horizontal shelf plate, two triangular ribs, four countersunk wall screw "
            "holes, two shelf screw holes, rounded stress-relief edges."
        ),
    },
    {
        "id": "parametric_knob",
        "prompt": (
            "Create a parametric round control knob: 38 mm diameter, 18 mm tall, "
            "knurled/grippy outer rim using repeated ribs, D-shaped shaft bore, "
            "top pointer notch, chamfered edges, printable as one part."
        ),
    },
]


@dataclass
class AttemptResult:
    ok: bool
    code: str
    raw: str
    process: dict[str, Any]
    latency_s: float
    repaired: bool = False
    repair_count: int = 0


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
        return {
            k: _sanitize_for_json(v)
            for k, v in value.items()
            if not k.startswith("_")
        }
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


def _score(result: AttemptResult) -> float:
    process = result.process
    if not process.get("success"):
        # Preserve some signal for near misses that parse but fail later.
        failure_type = process.get("failure_type")
        return {
            "syntax_error": 5.0,
            "execution_error": 15.0,
            "geometry_invalid": 25.0,
            "constraint_violation": 30.0,
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
        # Reward non-trivial modeled geometry, gently capped.
        score += min(10.0, (face_count + edge_count / 2.0) / 12.0)

    warnings = process.get("warnings") or []
    score -= min(10.0, len(warnings) * 1.5)
    if result.repaired:
        score -= min(8.0, result.repair_count * 4.0)
    return round(max(0.0, min(100.0, score)), 2)


async def _run_one(
    *,
    model: str,
    prompt: str,
    output_dir: Path,
    repairs: int,
    timeout_s: float,
    hard_constraints: HardConstraints,
    soft_constraints: SoftConstraints,
) -> AttemptResult:
    llm = LLMService(model=model)
    started = time.perf_counter()
    raw = await asyncio.wait_for(
        llm.generate_cadquery(prompt, hard_constraints, soft_constraints),
        timeout=timeout_s,
    )
    code = _prepare_code(raw)
    process = process_cadquery_code(
        code,
        output_dir,
        model_name="benchmark",
        constraints=hard_constraints,
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
        process = process_cadquery_code(
            code,
            output_dir / f"repair_{repair_count}",
            model_name="benchmark",
            constraints=hard_constraints,
        )

    latency_s = time.perf_counter() - started
    return AttemptResult(
        ok=bool(process.get("success")),
        code=code,
        raw=raw,
        process=process,
        latency_s=latency_s,
        repaired=repair_count > 0 and bool(process.get("success")),
        repair_count=repair_count,
    )


def _summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_model: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_model.setdefault(row["model"], []).append(row)

    summary = []
    for model, items in by_model.items():
        n = len(items)
        first_pass = sum(1 for i in items if i["success"] and i["repairs_used"] == 0)
        final_pass = sum(1 for i in items if i["success"])
        avg_score = sum(i["score"] for i in items) / max(1, n)
        avg_latency = sum(i["latency_s"] for i in items) / max(1, n)
        summary.append(
            {
                "model": model,
                "runs": n,
                "first_pass_rate": round(first_pass / n, 3),
                "final_success_rate": round(final_pass / n, 3),
                "avg_score": round(avg_score, 2),
                "avg_latency_s": round(avg_latency, 1),
            }
        )
    summary.sort(
        key=lambda r: (
            r["final_success_rate"],
            r["first_pass_rate"],
            r["avg_score"],
            -r["avg_latency_s"],
        ),
        reverse=True,
    )
    return summary


def _print_summary(summary: list[dict[str, Any]]) -> None:
    print("\nRank | Model | Final | First pass | Score | Latency")
    print("---- | ----- | ----- | ---------- | ----- | -------")
    for idx, row in enumerate(summary, 1):
        print(
            f"{idx:>4} | {row['model']} | "
            f"{row['final_success_rate']:.0%} | "
            f"{row['first_pass_rate']:.0%} | "
            f"{row['avg_score']:.1f} | "
            f"{row['avg_latency_s']:.1f}s"
        )


async def amain() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", nargs="+", help="Ollama model tags to test")
    parser.add_argument("--all", action="store_true", help="Benchmark every model from `ollama list`")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--repairs", type=int, default=1, help="LLM repair attempts after a failed generation")
    parser.add_argument("--timeout", type=float, default=240.0, help="Seconds per generation/repair call")
    parser.add_argument(
        "--case",
        action="append",
        dest="cases",
        help="Prompt case id to run; repeat this flag for multiple cases",
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

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir = args.out / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix="cad_bench_"))
    rows: list[dict[str, Any]] = []
    prompts = DEFAULT_PROMPTS
    if args.cases:
        wanted = set(args.cases)
        prompts = [case for case in DEFAULT_PROMPTS if case["id"] in wanted]
        missing = sorted(wanted - {case["id"] for case in prompts})
        if missing:
            known = ", ".join(case["id"] for case in DEFAULT_PROMPTS)
            raise SystemExit(f"Unknown --case value(s): {', '.join(missing)}. Known cases: {known}")

    hard_constraints = HardConstraints()
    soft_constraints = SoftConstraints()

    try:
        for model in models:
            print(f"\n== {model} ==")
            for repeat in range(args.repeats):
                for case in prompts:
                    case_id = case["id"]
                    print(f"  {case_id} repeat={repeat + 1} ... ", end="", flush=True)
                    run_dir = temp_root / model.replace(":", "_").replace("/", "_") / f"{case_id}_{repeat + 1}"
                    run_dir.mkdir(parents=True, exist_ok=True)
                    try:
                        result = await _run_one(
                            model=model,
                            prompt=case["prompt"],
                            output_dir=run_dir,
                            repairs=args.repairs,
                            timeout_s=args.timeout,
                            hard_constraints=hard_constraints,
                            soft_constraints=soft_constraints,
                        )
                        score = _score(result)
                        process = result.process
                        row = {
                            "model": model,
                            "case_id": case_id,
                            "repeat": repeat + 1,
                            "success": result.ok,
                            "score": score,
                            "latency_s": round(result.latency_s, 2),
                            "repairs_used": result.repair_count,
                            "failure_type": process.get("failure_type"),
                            "message": process.get("message", ""),
                            "warnings": process.get("warnings", []),
                            "violations": process.get("violations", []),
                            "geometry_stats": process.get("geometry_stats", {}),
                            "code_lines": len(result.code.splitlines()),
                            "output_dir": str(run_dir) if args.keep_temp else "",
                        }
                        rows.append(row)
                        status = "ok" if result.ok else f"fail:{row['failure_type']}"
                        print(f"{status} score={score:.1f} {result.latency_s:.1f}s repairs={result.repair_count}")
                    except Exception as exc:
                        rows.append(
                            {
                                "model": model,
                                "case_id": case_id,
                                "repeat": repeat + 1,
                                "success": False,
                                "score": 0.0,
                                "latency_s": None,
                                "repairs_used": 0,
                                "failure_type": "benchmark_error",
                                "message": str(exc),
                            }
                        )
                        print(f"error: {exc}")

        summary = _summarize(rows)
        report = {
            "run_id": run_id,
            "models": models,
            "repeats": args.repeats,
            "repairs": args.repairs,
            "prompts": prompts,
            "summary": summary,
            "runs": _sanitize_for_json(rows),
        }
        report_path = out_dir / "results.json"
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

        _print_summary(summary)
        print(f"\nWrote {report_path}")
    finally:
        if not args.keep_temp:
            shutil.rmtree(temp_root, ignore_errors=True)
        else:
            print(f"Kept CAD exports under {temp_root}")


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
