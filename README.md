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
