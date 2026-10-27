import sys, time, threading, socket
sys.path.insert(0,"/Users/aditagrawal/Documents/DOURMOUSE/stress")
import harness
srv,port,tmp=harness.start()
hub=srv.events_broadcast
print("handler timeout:", getattr(__import__("dourmouse.webui",fromlist=["x"])._Handler,"timeout",None))
# a client that connects to /api/events, then never reads
s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,4096); s.connect(("127.0.0.1",port))
s.send(f"GET /api/events HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode()); time.sleep(0.5)
print("clients registered:", len(hub._clients))
payload={"type":"agent_trace","entry":{"type":"thinking_delta","text":"x"*20000,"agent":"orchestrator"}}
worst=0; t_all=time.perf_counter(); stalled_at=None
for i in range(400):
    t0=time.perf_counter()
    try: hub.broadcast(payload)
    except BaseException as e: print("broadcast raised", type(e).__name__, "at event", i, "after", round(time.perf_counter()-t0,1),"s"); break
    dt=time.perf_counter()-t0
    worst=max(worst,dt)
    if dt>1 and stalled_at is None: stalled_at=(i,round(dt,1))
    if time.perf_counter()-t_all>150: print("gave up after 60s at event",i); break
print(f"broadcasts done {i+1}/400 of 20KB: total {time.perf_counter()-t_all:.1f}s, worst single call {worst:.1f}s, first stall {stalled_at}")
