"""End-to-end smoke test for the agent pipeline against a live Ollama.

Run from repo root:
    python scripts/smoke_pipeline.py "make a phone stand with a cable cutout"

The script spins up the orchestrator with a tempdir-backed storage, runs the
full pipeline, and prints every status / debug / plan / reasoning event so you
can see exactly what the agent is thinking and where it spends its time.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

# Make stdout UTF-8 even on Windows cp1252 consoles so we can print live LLM
# tokens that contain unicode (em-dashes, etc.) without crashing the pipeline.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from backend.agent.orchestrator import AgentOrchestrator
from backend.domain.models import DesignPlan, ProjectConfig
from backend.models.llm_service import LLMService
from backend.storage import StorageService


async def run(prompt: str, keep_data: bool) -> None:
    tmpdir = Path(tempfile.mkdtemp(prefix="cad_smoke_"))
    try:
        storage = StorageService(tmpdir)
        project = ProjectConfig(project_id="smoke", name="Smoke Test")
        storage.create_project(project)

        llm = LLMService()

        reasoning_buffers: dict[str, list[str]] = {}

        async def on_status(stage, message, details=None, data=None):
            print(f"[STATUS] {stage:11s} | {message}")
            if details:
                first = details.splitlines()[0]
                print(f"          |- {first[:200]}")

        async def on_chunk(chunk: str):
            # Print code stream inline, but compact
            sys.stdout.write(chunk)
            sys.stdout.flush()

        async def on_debug(category, message, data=None):
            preview = ""
            if data:
                try:
                    preview = " " + json.dumps(data, default=str)[:300]
                except Exception:
                    preview = ""
            print(f"[DEBUG ] {category:22s} | {message}{preview}")

        async def on_model_ready(model_id, glb_url):
            print(f"\n[MODEL ] ready: {model_id} → {glb_url}")

        async def on_critique(report, render_urls):
            print(f"\n[CRITIQ] score={report.overall_printability:.2f} matches_intent={report.matches_intent} issues={len(report.issues)}")
            for i in report.issues:
                print(f"          - [{i.severity}] {i.issue_type}: {i.description} ({i.location_hint})")

        async def on_error(message, failure_type=None):
            print(f"\n[ERROR ] ({failure_type or '?'}): {message}")

        async def on_plan(plan: DesignPlan):
            print("\n[PLAN  ] -------- design plan --------")
            print(f"  summary: {plan.summary}")
            if plan.overall_dimensions_mm:
                print(f"  size:    {plan.overall_dimensions_mm} mm")
            for i, c in enumerate(plan.components, 1):
                dim = ", ".join(f"{k}={v}" for k, v in c.dimensions.items())
                print(f"  comp {i}: {c.name} [{c.operation}] {c.primitive}({dim}) — {c.description}")
            if plan.key_features:
                print(f"  features:")
                for f in plan.key_features:
                    print(f"    * {f}")
            if plan.assumptions:
                print("  assumptions:")
                for a in plan.assumptions:
                    print(f"    * {a}")
            if plan.risks:
                print("  risks:")
                for r in plan.risks:
                    print(f"    * {r}")
            print("[PLAN  ] -----------------------------\n")

        async def on_reasoning(channel: str, text: str):
            buf = reasoning_buffers.setdefault(channel, [])
            buf.append(text)
            # Flush per-line so we get a live view of planner thinking
            joined = "".join(buf)
            if "\n" in text:
                for line in joined.splitlines()[:-1]:
                    print(f"[REASON:{channel}] {line}")
                # Keep only the in-progress final line
                tail = joined.splitlines()[-1] if not joined.endswith("\n") else ""
                buf[:] = [tail] if tail else []

        orchestrator = AgentOrchestrator(
            storage=storage,
            llm=llm,
            on_status=on_status,
            on_chunk=on_chunk,
            on_debug=on_debug,
            on_model_ready=on_model_ready,
            on_critique=on_critique,
            on_error=on_error,
            on_plan=on_plan,
            on_reasoning=on_reasoning,
        )

        t0 = time.time()
        model_id = await orchestrator.run_pipeline(
            project_id="smoke",
            thread_id="default",
            user_message=prompt,
        )
        elapsed = time.time() - t0

        print(f"\n========= pipeline finished in {elapsed:.1f}s — result: {model_id} =========")
        if model_id:
            model_dir = storage.get_project_dir("smoke") / "models" / model_id
            renders_dir = model_dir / "renders"
            if renders_dir.exists():
                print(f"Renders saved to: {renders_dir}")
                for p in sorted(renders_dir.glob("render_*.png")):
                    print(f"  - {p}")
        if keep_data:
            print(f"Data dir preserved at: {tmpdir}")
    finally:
        if not keep_data:
            shutil.rmtree(tmpdir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt", help="What to build")
    parser.add_argument("--keep", action="store_true", help="Keep tempdir after run")
    args = parser.parse_args()
    asyncio.run(run(args.prompt, args.keep))


if __name__ == "__main__":
    main()
