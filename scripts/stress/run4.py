import sys, time, threading, socket, json, os, resource
sys.path.insert(0,"/Users/aditagrawal/Documents/DOURMOUSE/stress")
import harness
calls=[]
class Slow(harness.Stub):
    def create(s, **kw):
        calls.append(time.time()); time.sleep(1.0); return harness._Resp("slow reply")
harness.Stub=Slow
srv,port,tmp=harness.start()
def raw_post(path, body, host=True, hold=True):
    s=socket.create_connection(("127.0.0.1",port),timeout=10)
    b=json.dumps(body).encode()
    s.send(f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Type: application/json\r\nContent-Length: {len(b)}\r\n\r\n".encode()+b)
    return s
# 3. 30 chats with no tab_id (one shared session lock); drop 25 sockets immediately
socks=[raw_post("/api/chat",{"prompt":f"abandoned {i}","screen":"HOME"}) for i in range(30)]
time.sleep(0.3)
for s in socks[5:]: s.close()
t0=time.time()
time.sleep(40)  # let the queue drain
print(f"3. 30 queued chats, 25 abandoned: model calls made={len(calls)} (5 clients stayed), finished draining in <= {time.time()-t0:.0f}s, threads now={threading.active_count()}")
for s in socks[:5]: s.close()
# 7. upload abuse: declared 40 MB, sent 1 MB then closed
try:
    s=socket.create_connection(("127.0.0.1",port),timeout=10)
    s.send(f"POST /api/upload?name=zz.bin HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Type: application/octet-stream\r\nContent-Length: {40*1024*1024}\r\n\r\n".encode()+b"x"*(1024*1024)); time.sleep(0.5); s.close()
except Exception as e: print("upload open failed", e)
time.sleep(2)
up=[ (os.path.getsize(os.path.join(r,f)),os.path.join(r,f)) for r,_,fs in os.walk(tmp+"/ws") for f in fs if "upload" in r.lower()]
print("7. after a cut-off 40 MB upload, files in uploads:", up[:3] or "none", "| status for oversize declared 200MB:", harness.req(port,"POST","/api/upload?name=big.bin",body=b"x", headers={"Content-Length":str(200*1024*1024)}))
print("threads at end:", threading.active_count())
