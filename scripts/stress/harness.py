"""Isolated stress harness: real route table and real agent roster, temp workspace and config,
no cloud model (a stub client), its own port. Never touches the owner's app or data."""
import os, sys, tempfile, threading, time, json, http.client
tmp = tempfile.mkdtemp(prefix="dm_stress_")
os.environ["DOURMOUSE_WORKSPACE"] = tmp + "/ws"
os.environ["DOURMOUSE_CONFIG_DIR"] = tmp + "/cfg"
for k in ("OLLAMA_API_KEY","GEMINI_API_KEY","NVIDIA_API_KEY","ANTHROPIC_API_KEY"): os.environ.pop(k, None)
sys.path.insert(0, os.path.expanduser("~/dourmouse-recon"))
from dourmouse.webui import run_server
from dourmouse.general_roster import build_general_registry

class _Msg:
    def __init__(s, c): s.content = c; s.tool_calls = None; s.role = "assistant"
class _Resp:
    def __init__(s, c): s.choices = [type("C", (), {"message": _Msg(c), "finish_reason": "stop"})()]; s.usage = None
class Stub:
    def __init__(s):
        s.chat = type("X", (), {})(); s.chat.completions = s
    def create(s, **kw):
        time.sleep(0.05); return _Resp("stub reply")

def start():
    srv = run_server(build_general_registry(), port=0, client=Stub(), config=None)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1], tmp

def req(port, method, path, body=None, headers=None, timeout=15):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    h = {"Content-Type": "application/json"}; h.update(headers or {})
    t0 = time.perf_counter()
    try:
        c.request(method, path, body=(json.dumps(body) if isinstance(body, (dict, list)) else body), headers=h)
        r = c.getresponse(); data = r.read(); return r.status, time.perf_counter() - t0, len(data)
    except Exception as e:
        return type(e).__name__, time.perf_counter() - t0, 0
    finally:
        c.close()
