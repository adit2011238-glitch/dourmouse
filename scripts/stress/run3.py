import sys, time, threading, json, http.client, os
sys.path.insert(0,"/Users/aditagrawal/Documents/DOURMOUSE/stress")
import harness
sizes=[]
class Rec(harness.Stub):
    def create(s, **kw):
        msgs=kw.get("messages",[]); sizes.append((len(msgs), sum(len(str(m.get("content",""))) for m in msgs)))
        return super().create(**kw)
harness.Stub=Rec
srv,port,tmp=harness.start()
def chat(prompt, tab="", screen="HOME"):
    c=http.client.HTTPConnection("127.0.0.1",port,timeout=60)
    t0=time.perf_counter()
    c.request("POST","/api/chat",body=json.dumps({"prompt":prompt,"screen":screen,"tab_id":tab}),headers={"Content-Type":"application/json"})
    r=c.getresponse(); body=r.read(); c.close()
    return r.status, time.perf_counter()-t0, body.count(b"data:")
# A. 40 sequential turns on one conversation: how does the request size grow?
for i in range(40): chat(f"please remember number {i} and tell me something about it "*3)
first=sizes[0]; last=sizes[-1]
print(f"A. growth over 40 turns: messages {first[0]}->{last[0]}, chars {first[1]}->{last[1]}")
n_before=len(sizes)
# B. concurrency: 16 different tabs x 4 turns
res=[]
def w(k):
    for j in range(4): res.append(chat(f"hello {k}-{j}", tab=f"tab{k}"))
th=[threading.Thread(target=w,args=(k,)) for k in range(16)]; t0=time.perf_counter(); [t.start() for t in th]; [t.join() for t in th]
ok=[r for r in res if r[0]==200]; lat=sorted(r[1] for r in ok)
print(f"B. 64 concurrent turns: {len(ok)} ok of {len(res)}, wall {time.perf_counter()-t0:.1f}s, p50={lat[len(lat)//2]:.2f}s max={lat[-1]:.2f}s, statuses={sorted(set(r[0] for r in res))}")
# C. same tab, 8 parallel turns (shared session lock)
res2=[]
th=[threading.Thread(target=lambda k=k: res2.append(chat(f"same tab {k}", tab="shared"))) for k in range(8)]; t0=time.perf_counter(); [t.start() for t in th]; [t.join() for t in th]
print(f"C. 8 parallel turns on ONE tab: {sum(r[0]==200 for r in res2)} ok, wall {time.perf_counter()-t0:.1f}s (serial would be ~{8*0.9:.0f}s)")
# D. what is on disk afterwards
tot=0; big=[]
for root,_,files in os.walk(tmp):
    for f in files:
        p=os.path.join(root,f); sz=os.path.getsize(p); tot+=sz; big.append((sz,p.replace(tmp,"")))
big.sort(reverse=True); print(f"D. disk after test: {tot/1e6:.1f} MB in {len(big)} files; biggest: {big[:3]}")
print("threads at end:", threading.active_count())
