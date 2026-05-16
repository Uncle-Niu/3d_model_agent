"""Smoke test for the LocalKnowledgeService against a real Ollama.

Runs subject detection + multi-model recall for the iphone-holder prompt and
prints what each model returned plus the final consensus. Useful for tuning
prompts and confirming the 5-model chain actually agrees on common facts.

    python scripts/smoke_recall.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# UTF-8 stdout on Windows so we can print em-dashes from model output.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from backend.config import DEFAULT_LLM_MODEL
from backend.knowledge.local_recall import (
    DEFAULT_MODEL_CHAIN,
    LocalKnowledgeService,
    format_recall_for_prompt,
)


PROMPT = "Generate a model for iphone 16 pro max holder"
MAIN_MODEL = DEFAULT_LLM_MODEL


async def main():
    svc = LocalKnowledgeService()
    print(f"User prompt: {PROMPT}")
    print(f"Main model: {MAIN_MODEL}")
    print(f"Model chain: {DEFAULT_MODEL_CHAIN}")
    print("=" * 80)

    print("\n[1] Detecting subjects...")

    async def _show_raw(raw: str):
        print("    raw model output:")
        for line in raw.splitlines()[:40]:
            print(f"      | {line}")

    subjects = await svc.detect_subjects(PROMPT, main_model=MAIN_MODEL, on_raw=_show_raw)
    if not subjects:
        print("  (no subjects detected — recall would be skipped)")
        return
    for s in subjects:
        print(f"  - {s.subject}")
        print(f"    fields: {s.fields}")
        if s.reasoning:
            print(f"    why:    {s.reasoning}")

    shown_prompts: set[str] = set()

    async def on_step(event: str, payload: dict):
        if event == "model_start":
            subj = payload.get("subject", "")
            if subj not in shown_prompts:
                shown_prompts.add(subj)
                print(f"  prompt sent to each model (system + user):")
                print(f"    [system] {payload.get('system_prompt','')}")
                for line in (payload.get("prompt") or "").splitlines():
                    print(f"    [user]   {line}")
                print()
            print(f"  → asking {payload['model']:24s} ...", end="", flush=True)
        elif event == "model_done":
            err = payload.get("error")
            if err:
                print(f" ERROR: {err[:80]}")
            else:
                print(f" {payload['field_count']:2d} fields in {payload['latency_s']:.1f}s")
                for fname, fv in (payload.get("fields") or {}).items():
                    val = fv.get("value")
                    if val is None:
                        continue
                    print(f"      {fname:30s} = {val!r:30s} conf={fv.get('confidence', 0):.2f}")

    consensuses = []
    for subj in subjects:
        print(f"\n[2] Querying chain for: {subj.subject}")
        consensus = await svc.extract_knowledge(
            subject=subj.subject,
            fields=subj.fields,
            on_step=on_step,
        )
        consensuses.append(consensus)
        print(f"\n  Consensus ({len(consensus.fields)} agreed, "
              f"{len(consensus.uncertain_fields)} uncertain):")
        for fname, fv in consensus.fields.items():
            note = f" // {fv.note}" if fv.note else ""
            print(f"    {fname:30s} = {fv.value!r:30s} conf={fv.confidence:.2f}{note}")
        if consensus.uncertain_fields:
            print(f"    uncertain: {consensus.uncertain_fields}")
        print(f"    contributing: {consensus.contributing_models}")

    print("\n[3] Planner-prompt formatted recall context:")
    print("-" * 80)
    print(format_recall_for_prompt(consensuses) or "(empty)")


if __name__ == "__main__":
    asyncio.run(main())
