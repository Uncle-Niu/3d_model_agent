# 3D Model Agent

## Useful commands
python -m backend.run

cd frontend
npm run dev

UI:
http://localhost:5173/

Ollama:
http://localhost:11434/
ollama pull qwen3.6:27b
Get-Process | Where-Object {$_.ProcessName -like "*ollama*"}
netstat -ano | findstr :11434

## Benchmark local Ollama models for CadQuery generation

```
python scripts/benchmark_ollama_cadquery.py --all
python scripts/benchmark_ollama_cadquery.py --models qwen3.6:27b phi4:14b --case simple_mounting_block
python scripts/benchmark_ollama_cadquery.py --all --generation-context direct
python scripts/benchmark_ollama_cadquery.py --all --skip-vision
```

Results are written to `data/benchmarks/cadquery_models/<run-id>/results.json`.
By default the benchmark runs 4 described cases (2 simple, 2 medium), 3 repeats
per model/case, one repair attempt, fixed benchmark plans fed to code generation,
deterministic CadQuery validation/export, and render-based vision judging averaged
across `qwen3.6:27b`, `gemma4:31b`, and `nemotron3:33b`. Artifacts for each
attempt include the generation prompt, raw model response, extracted code,
process result, and vision critiques.

## Debug a single CAD generation end-to-end (no UI required)

```
VISION_DISABLE_SMOKE_TEST=1 python -m scripts.smoke_pipeline "your prompt here" --keep
```

Prints every pipeline event — design plan, planner reasoning, code generation
reasoning, CadQuery execution, multi-angle renders, vision critique with
checklist, and any repair iterations. `--keep` preserves the temp directory so
you can inspect `renders/*.png` and `metadata.json` afterwards.

## Restore local CAD example banks

The agent uses cloned open-source CAD repositories under `data/cad_sources/` as
a local RAG/reference bank. `data/` is gitignored, so run this on a new machine:

```
powershell -ExecutionPolicy Bypass -File scripts/bootstrap_cad_sources.ps1
```

To update already-cloned banks:

```
powershell -ExecutionPolicy Bypass -File scripts/bootstrap_cad_sources.ps1 -Update
```

These repos are reference-only prompt context; generated code still runs in the
normal CadQuery sandbox.

## Env vars

- `LLM_MODEL` / `VISION_MODEL` — defaults to `qwen3.6:27b`. Both code-gen and
  vision use the same model unless overridden.
- `VISION_DISABLE_SMOKE_TEST=1` — trust capability metadata, skip the red-square
  smoke test (useful when VRAM is tight and the smoke call transiently fails).
- `LLM_BASE_URL` / `VISION_BASE_URL` — defaults to `http://localhost:11434/v1`.
