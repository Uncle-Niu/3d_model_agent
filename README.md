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

## Debug a single CAD generation end-to-end (no UI required)

```
VISION_DISABLE_SMOKE_TEST=1 python -m scripts.smoke_pipeline "your prompt here" --keep
```

Prints every pipeline event — design plan, planner reasoning, code generation
reasoning, CadQuery execution, multi-angle renders, vision critique with
checklist, and any repair iterations. `--keep` preserves the temp directory so
you can inspect `renders/*.png` and `metadata.json` afterwards.

## Env vars

- `LLM_MODEL` / `VISION_MODEL` — defaults to `qwen3.6:27b`. Both code-gen and
  vision use the same model unless overridden.
- `VISION_DISABLE_SMOKE_TEST=1` — trust capability metadata, skip the red-square
  smoke test (useful when VRAM is tight and the smoke call transiently fails).
- `LLM_BASE_URL` / `VISION_BASE_URL` — defaults to `http://localhost:11434/v1`.
