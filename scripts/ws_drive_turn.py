"""
End-to-end driver for a single chat turn through the backend WebSocket.

Used to validate the repair-loop changes (prior-attempts threading + fillet
guidance) against the same prompt that hit the 6-iteration loop:

    Design a iphone 14 pro max holder so I can watch movie on a desk

Usage:
    python scripts/ws_drive_turn.py PROJECT_ID THREAD_ID "user prompt..."

Writes a JSONL transcript of every WS event to scripts/_ws_transcript.jsonl
and prints a compact stage line for each. Exits 0 on `final` / `error`, 1 on
WS disconnect or timeout.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import websockets

TRANSCRIPT = Path(__file__).parent / "_ws_transcript.jsonl"
SUMMARY = Path(__file__).parent / "_ws_summary.txt"


async def drive(project_id: str, thread_id: str, prompt: str, *, timeout_s: float = 3600) -> int:
    url = f"ws://localhost:8000/ws/{project_id}?thread_id={thread_id}"
    print(f"[ws] connecting: {url}", flush=True)
    start = time.time()

    iter_seen = set()
    repair_seen = []
    last_stage = ""

    with TRANSCRIPT.open("w", encoding="utf-8") as f, SUMMARY.open("w", encoding="utf-8") as summary:
        async with websockets.connect(url, max_size=64 * 1024 * 1024) as ws:
            await ws.send(json.dumps({
                "type": "chat_message",
                "content": prompt,
                "thread_id": thread_id,
            }))
            print(f"[ws] sent chat_message ({len(prompt)} chars)", flush=True)

            while True:
                elapsed = time.time() - start
                if elapsed > timeout_s:
                    print(f"[ws] TIMEOUT after {elapsed:.0f}s", flush=True)
                    return 1
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=120)
                except asyncio.TimeoutError:
                    print(f"[ws] no event for 120s (elapsed={elapsed:.0f}s) — still waiting...", flush=True)
                    continue
                except websockets.ConnectionClosed as exc:
                    print(f"[ws] disconnected after {elapsed:.0f}s: {exc}", flush=True)
                    return 1

                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                f.write(raw + "\n")
                f.flush()

                etype = event.get("type")
                stage = event.get("stage") or etype or "?"
                msg = (event.get("message") or "")[:140]
                data = event.get("data") or {}
                it = data.get("iteration")

                # Compact stage line
                stage_key = f"{stage}|{it}"
                if stage in ("status_update", "pipeline_step") and stage_key != last_stage:
                    last_stage = stage_key
                    print(f"[{elapsed:6.0f}s] iter={it} stage={data.get('stage') or stage} :: {msg}", flush=True)
                elif etype == "status_update":
                    s = data.get("stage")
                    if s and stage_key != last_stage:
                        last_stage = stage_key
                        print(f"[{elapsed:6.0f}s] iter={it} {s} :: {msg}", flush=True)

                # Capture repair prompt details to verify our new feature is wired
                if data.get("stage") in ("repairing",) or stage == "repairing" or msg.startswith("Repairing"):
                    repair_kind = data.get("repair_kind") or "?"
                    repair_user_prompt = data.get("prompt") or ""
                    has_prior = "Prior repair attempts on this turn" in repair_user_prompt
                    has_fillet = "StdFail_NotDone" in repair_user_prompt
                    has_structural = "structurally different" in repair_user_prompt
                    repair_seen.append({
                        "iteration": it,
                        "kind": repair_kind,
                        "has_prior_block": has_prior,
                        "has_fillet_hint": has_fillet,
                        "has_structural_demand": has_structural,
                        "user_prompt_len": len(repair_user_prompt),
                    })
                    summary.write(json.dumps({"repair_attempt": repair_seen[-1]}, indent=2) + "\n")
                    summary.flush()
                    print(
                        f"[{elapsed:6.0f}s] REPAIR captured: kind={repair_kind} iter={it} "
                        f"prior_block={has_prior} fillet_hint={has_fillet} structural={has_structural}",
                        flush=True,
                    )

                if etype in ("final", "error", "done"):
                    print(f"[{elapsed:6.0f}s] TERMINAL event: {etype} -- {msg}", flush=True)
                    summary.write("\n=== Repair attempts seen ===\n")
                    for r in repair_seen:
                        summary.write(json.dumps(r) + "\n")
                    summary.write(f"\n=== Final ===\n{etype}: {msg}\n")
                    return 0


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: ws_drive_turn.py PROJECT_ID THREAD_ID 'prompt'")
        sys.exit(2)
    rc = asyncio.run(drive(sys.argv[1], sys.argv[2], sys.argv[3]))
    sys.exit(rc)
