import sys, time, json, http.client, os, resource
sys.path.insert(0,"/Users/aditagrawal/Documents/DOURMOUSE/stress")
import harness
srv,port,tmp=harness.start()
def chat(prompt):
    c=http.client.HTTPConnection("127.0.0.1",port,timeout=120); t0=time.perf_counter()
    c.request("POST","/api/chat",body=json.dumps({"prompt":prompt,"screen":"HOME"}),headers={"Content-Type":"application/json"})
    r=c.getresponse(); r.read(); c.close(); return time.perf_counter()-t0
big="the quick brown fox jumps over the lazy dog. "*120   # ~5 KB
marks={}
for i in range(1,301):
    dt=chat(f"{big} turn {i}")
    if i in (5,50,100,200,300): marks[i]=dt
print("per-turn latency (s):", {k:round(v,2) for k,v in marks.items()})
sess=[(os.path.getsize(os.path.join(r,f)),f) for r,_,fs in os.walk(tmp+"/ws/sessions") for f in fs] if os.path.isdir(tmp+"/ws/sessions") else []
print("session files:", sorted(sess,reverse=True)[:3])
for p in ("/api/session/current","/api/sessions/recent"):
    print(p, harness.req(port,"GET",p))
print("peak RSS MB:", round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1e6))
