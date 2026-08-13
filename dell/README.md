# DOURMOUSE COMPUTE NODE — the Dell (192.168.1.108)

The Dell is **compute infrastructure, not DOURMOUSE**. It runs one small
FastAPI server that serves Qwen3 1.7B from the Dell's own Ollama over the
LAN. All memory, integrations, the orchestrator and the UI stay on the MAIN
computer.

```
MAIN DOURMOUSE ──> http://192.168.1.108:8000/v1/* ──> Ollama ──> qwen3:1.7b
```

LAN ONLY. Never port-forward this machine. Never expose port 8000 publicly.

---

## 1. Deploy on the Dell (one-time)

On the Dell (Windows), in a terminal:

```powershell
# 1) Put this folder on the Dell (copy dell_server.py + requirements.txt).
# 2) Use the existing Python venv (or make one):
python -m venv C:\dourmouse-node\.venv
C:\dourmouse-node\.venv\Scripts\python -m pip install -r C:\dourmouse-node\requirements.txt

# 3) Run it (foreground first, to confirm):
C:\dourmouse-node\.venv\Scripts\python C:\dourmouse-node\dell_server.py
```

Optional env for the Dell (set as Windows user env vars or in the script):

| Env var                    | Meaning                                  | Default                    |
|----------------------------|------------------------------------------|----------------------------|
| `DOURMOUSE_SERVER_MODEL`   | model served to Ollama                   | `qwen3:1.7b`               |
| `OLLAMA_URL`               | the Dell's Ollama root                   | `http://127.0.0.1:11434`   |
| `DOURMOUSE_SERVER_API_KEY` | optional bearer key for the /v1 routes   | *(none — no auth)*         |
| `DOURMOUSE_SERVER_PORT`    | port                                      | `8000`                     |

With a key set, the main computer sends `X-API-Key: <key>` (set
`DOURMOUSE_SERVER_API_KEY` there too).

## 2. Verify from the MAIN computer

```bash
curl http://192.168.1.108:8000/status        # legacy: online
curl http://192.168.1.108:8000/v1/status     # new: online + model + ollama
curl -X POST http://192.168.1.108:8000/v1/generate \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"Say hello in one sentence."}'
curl -X POST http://192.168.1.108:8000/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"Hello"}]}'
```

Then in the DOURMOUSE app, ask the orchestrator to use it —
"offload to the compute node" / "use the Dell" — or open WORLD/SETTINGS and
the **SERVER** row shows `● Node-01 · qwen3:1.7b · Nms`. When the Dell is
off, that row shows OFFLINE and every request automatically falls back to
the local AI.

## 3. Autostart after Windows boot (reversible, no admin hacks)

The clean mechanism is a Scheduled Task. Ready-to-run scripts are included
in this folder — from an **Administrator** PowerShell on the Dell:

```powershell
# Install (validates the node paths, then registers DOURMOUSE-ComputeNode):
powershell -ExecutionPolicy Bypass -File C:\dourmouse-node\install_autostart.ps1

# Uninstall (fully reversible):
powershell -ExecutionPolicy Bypass -File C:\dourmouse-node\remove_autostart.ps1
```

`install_autostart.ps1` registers `DOURMOUSE-ComputeNode` to run at startup
as the logged-in user (`-RunLevel Limited`, no stored password), with
auto-restart on failure (3 tries, 1 min apart) and **no execution-time
limit** (the default 3-day task kill is disabled, so the server runs
indefinitely — it is built for long-running CPU inference on 8 GB RAM). It
refuses to run if the node is not deployed, and overwrites an existing task
with `-Force`.

Options:

| Flag         | Meaning                                                            |
|--------------|--------------------------------------------------------------------|
| `-NodeDir`   | deployment folder (default `C:\dourmouse-node`)                    |
| `-TaskName`  | task name (default `DOURMOUSE-ComputeNode`)                        |
| `-OllamaExe` | also register `DOURMOUSE-Ollama` to run `ollama.exe serve` at boot  |
|              | — only needed if Ollama does not autostart on its own               |
| `-StartNow`  | start the server task immediately after registering                |
| `-WhatIf`    | preview without changing anything                                  |

`remove_autostart.ps1` unregisters the task(s); pass `-RemoveOllamaTask` to
also remove the Ollama task, and `-RemoveNodeDir` to additionally delete the
deployment folder (it refuses unless the folder contains `dell_server.py`).

Manual equivalent (reference):

```powershell
$action  = New-ScheduledTaskAction -Execute "C:\dourmouse-node\.venv\Scripts\python.exe" `
           -Argument "C:\dourmouse-node\dell_server.py" -WorkingDirectory "C:\dourmouse-node"
$trigger = New-ScheduledTaskTrigger -AtStartup
Register-ScheduledTask -TaskName "DOURMOUSE-ComputeNode" -Action $action -Trigger $trigger `
  -Description "DOURMOUSE compute node (LAN inference)" -RunLevel Limited
```

To undo manually (fully reversible):

```powershell
Unregister-ScheduledTask -TaskName "DOURMOUSE-ComputeNode" -Confirm:$false
```

Notes:
- `-RunLevel Limited` — runs as the logged-in user, no elevated rights needed.
- If the Dell's Ollama itself does not autostart, run the installer with
  `-OllamaExe "C:\Program Files\Ollama\ollama.exe"` — it registers a
  separate `DOURMOUSE-Ollama` task (a task's actions run in sequence, so a
  blocking `ollama.exe serve` must never share the server's task).
- The server is single-process, no reload, low RAM — built for long-running
  CPU inference on 8 GB RAM.

## 4. Logging

Requests, model, latency and errors go to stdout (visible in the console /
task output). Request bodies and API keys are NEVER logged.
