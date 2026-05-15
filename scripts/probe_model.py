"""One-shot probe: send the recall prompt to a specific model and print the
raw response. Used to debug parsing failures.

    PYTHONPATH=. python scripts/probe_model.py nemotron3:33b
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from backend.knowledge.local_recall import LocalKnowledgeService


SUBJECT = "iPhone 16 Pro Max"
FIELDS = [
    "body_length_mm", "body_width_mm", "body_thickness_mm",
    "weight_g", "camera_bump_dimensions_mm",
]


async def main():
    if len(sys.argv) < 2:
        print("usage: probe_model.py <ollama-model-tag>")
        sys.exit(1)
    model = sys.argv[1]
    svc = LocalKnowledgeService()
    print(f"Probing {model} for: {SUBJECT}")
    print(f"Fields: {FIELDS}")
    print("=" * 80)
    r = await svc._query_one_model(model, SUBJECT, FIELDS)
    print(f"latency: {r.latency_s:.1f}s")
    print(f"error:   {r.error}")
    print(f"parsed fields ({len(r.fields)}):")
    for fname, fv in r.fields.items():
        print(f"  {fname:30s} = {fv.value!r:30s} conf={fv.confidence:.2f}")
    print()
    print("--- raw response ---")
    print(r.raw_response)


if __name__ == "__main__":
    asyncio.run(main())
