"""
Download diffusion models for Mission Crafter's reference-image bank.

Storage:
    data/diffusion_models/<model_id>/

We pick three models, all open weights, all comfortably under 32GB VRAM
EITHER alone or paired with Ollama's qwen3.6:27b (~17GB) on a 5090:

    sdxl_base_1.0          ~7 GB fp16   - text->image, well-supported, good quality
    sdxl_turbo             ~7 GB fp16   - 1-4 step inference for fast iteration
    stable_zero123         ~3 GB fp16   - single-view -> novel-view (multi-angle from one image)

Total on disk ~17 GB. Only one is loaded into VRAM at a time.

Run with --skip <id> to skip any model. --dry-run prints what would happen.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from huggingface_hub import snapshot_download

ROOT = Path(__file__).resolve().parents[1] / "data" / "diffusion_models"

MODELS = [
    {
        "id": "sdxl_base_1.0",
        "repo": "stabilityai/stable-diffusion-xl-base-1.0",
        # Only fp16 weights, no .bin duplicates — cuts download roughly in half.
        "allow_patterns": [
            "*.json", "*.txt",
            "scheduler/*", "tokenizer/*", "tokenizer_2/*",
            "text_encoder/*.fp16.safetensors", "text_encoder/*.json",
            "text_encoder_2/*.fp16.safetensors", "text_encoder_2/*.json",
            "unet/*.fp16.safetensors", "unet/*.json",
            "vae/*.fp16.safetensors", "vae/*.json",
        ],
        "purpose": "Text->image reference renders. Good general quality, well-supported by diffusers.",
    },
    {
        "id": "sdxl_turbo",
        "repo": "stabilityai/sdxl-turbo",
        "allow_patterns": [
            "*.json", "*.txt",
            "scheduler/*", "tokenizer/*", "tokenizer_2/*",
            "text_encoder/*.fp16.safetensors", "text_encoder/*.json",
            "text_encoder_2/*.fp16.safetensors", "text_encoder_2/*.json",
            "unet/*.fp16.safetensors", "unet/*.json",
            "vae/*.fp16.safetensors", "vae/*.json",
        ],
        "purpose": "Same architecture as SDXL but converges in 1-4 steps. Use during planning when latency matters more than fidelity.",
    },
    {
        "id": "stable_zero123",
        "repo": "stabilityai/stable-zero123",
        # The model only ships one checkpoint and a config.
        "allow_patterns": ["*.json", "*.yaml", "*.ckpt"],
        "purpose": "Single-image -> novel-view renderer. Feed it one front view, get back front/side/top/iso of the same object. Use for multi-angle reference images.",
    },
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip", action="append", default=[], help="Model id(s) to skip")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    ROOT.mkdir(parents=True, exist_ok=True)
    skipped = set(args.skip)

    for m in MODELS:
        target = ROOT / m["id"]
        print(f"\n=== {m['id']} ({m['repo']}) ===", flush=True)
        print(f"    purpose: {m['purpose']}", flush=True)
        print(f"    target:  {target}", flush=True)
        if m["id"] in skipped:
            print("    SKIPPED via --skip", flush=True)
            continue
        if args.dry_run:
            continue

        t0 = time.time()
        try:
            snapshot_download(
                repo_id=m["repo"],
                local_dir=str(target),
                allow_patterns=m["allow_patterns"],
            )
            print(f"    OK in {time.time()-t0:.0f}s", flush=True)
        except Exception as exc:
            print(f"    FAILED: {exc!r}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
