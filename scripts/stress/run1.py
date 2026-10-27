import sys, time, threading, socket, json, os, statistics, resource, gc
sys.path.insert(0,"/Users/aditagrawal/Documents/DOURMOUSE/stress")
import harness
srv,port,tmp=harness.start()
def rss(): return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1e6  # MB on macOS (bytes/1e6)
out=[]
def rec(name,detail): out.append((name,detail)); print(f"{name:34} {detail}", flush=True)

# 1. concurrency on cheap GET routes
def hammer(path,n_threads,per):
    lat=[];errs=[]
    def w():
        for _ in range(per):
            s,t,_=harness.req(port,"GET",path)
            (lat if s==200 else errs).append(t if s==200 else s)
    th=[threading.Thread(target=w) for _ in range(n_threads)]
    t0=time.perf_counter(); [t.start() for t in th]; [t.join() for t in th]; dt=time.perf_counter()-t0
    lat.sort(); p=lambda q: lat[int(len(lat)*q)-1]*1000 if lat else -1
    return f"{n_threads*per} req in {dt:.1f}s ({n_threads*per/dt:.0f}/s) p50={p(.5):.0f}ms p99={p(.99):.0f}ms errors={len(errs)} {set(errs) or ''}"
for path in ("/api/activity","/api/security_dashboard","/api/os/office/floors"):
    rec("GET "+path, hammer(path,32,40))

# 2. oversize and malformed bodies
big=b"x"*(8*1024*1024)
rec("POST 8MB body to /api/chat", str(harness.req(port,"POST","/api/chat",body=big)))
rec("POST bad json", str(harness.req(port,"POST","/api/chat",body=b"{not json")))
rec("POST huge int json", str(harness.req(port,"POST","/api/chat",body=b'{"prompt":'+b"9"*6000+b"}")))
rec("POST deep nesting", str(harness.req(port,"POST","/api/chat",body=b"["*50000+b"]"*50000)))
rec("POST unicode/NUL prompt", str(harness.req(port,"POST","/api/chat",body={"prompt":"a\x00b\ud800 ‮"*100})))
rec("path traversal GET", str(harness.req(port,"GET","/../../etc/passwd")))
rec("very long URL (64KB)", str(harness.req(port,"GET","/"+"a"*65536)))
rec("1000 headers", str(harness.req(port,"GET","/api/activity",headers={f"X-{i}":"v" for i in range(1000)})))

# 3. slowloris: 200 sockets that open and send nothing; server should keep serving
socks=[]
for _ in range(200):
    try:
        s=socket.create_connection(("127.0.0.1",port),timeout=2); s.send(b"GET /api/activity HTTP/1.1\r\nHost: 127.0.0.1\r\n"); socks.append(s)
    except Exception as e: rec("slowloris open fail",type(e).__name__); break
t0=time.perf_counter(); st=harness.req(port,"GET","/api/activity",timeout=20)
rec("200 half-open sockets then GET", f"{st[0]} in {time.perf_counter()-t0:.2f}s, threads={threading.active_count()}")
for s in socks: s.close()
time.sleep(1); rec("threads after closing", str(threading.active_count()))

# 4. SSE clients: 100 connect and drop
def sse():
    try:
        s=socket.create_connection(("127.0.0.1",port),timeout=3); s.send(b"GET /api/events HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"); time.sleep(0.2); s.close()
    except Exception: pass
th=[threading.Thread(target=sse) for _ in range(100)]; [t.start() for t in th]; [t.join() for t in th]
time.sleep(2); rec("after 100 SSE connect/drop", f"threads={threading.active_count()} hub_clients={len(srv.events_broadcast._clients)}")

# 5. memory growth over many requests
gc.collect(); m0=rss()
for _ in range(3000): harness.req(port,"GET","/api/activity")
for _ in range(500): harness.req(port,"POST","/api/chat",body={"prompt":"hi "*50,"screen":"HOME"})
gc.collect(); m1=rss()
rec("memory after 3500 requests", f"{m0:.0f}MB -> {m1:.0f}MB (+{m1-m0:.0f}MB)")
# 6. ring buffers/state sizes
for name in ("tracker","attention","office_log"):
    o=getattr(srv,name,None); rec("attr "+name, type(o).__name__ if o is not None else "none")
print("done")
