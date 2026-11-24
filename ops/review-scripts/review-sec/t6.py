import httpx, json, time, os, subprocess, sys, threading, asyncio, websockets
B="http://127.0.0.1:18431"
c=httpx.Client(base_url=B, timeout=300)
r=c.post("/api/accounts", json={"name":"orders","starting_cash":"1000000000"}); aid=r.json()["id"]
q=c.get("/api/quotes?symbols=MKTX").json()["MKTX"]; print("last", q["last"])
async def tickrate(secs):
    w=await websockets.connect("ws://127.0.0.1:18431/ws"); await w.send(json.dumps({"symbols":["MKTX"],"account":aid}))
    n=0; end=time.time()+secs; gaps=[]; last=time.time()
    while time.time()<end:
        try: await asyncio.wait_for(w.recv(), max(0.01,end-time.time())); n+=1; gaps.append(time.time()-last); last=time.time()
        except Exception: break
    await w.close(); return n, max(gaps) if gaps else None
print("baseline ticks in 6s:", asyncio.run(tickrate(6)))
t0=time.time(); codes={}
for i in range(4000):
    t=time.time()
    r=c.post(f"/api/accounts/{aid}/orders", json={"symbol":"MKTX","side":"buy","qty":"1","type":"limit","limit_price":"1.00","tif":"gtc"})
    codes[r.status_code]=codes.get(r.status_code,0)+1
    if r.status_code!=200 and codes[r.status_code]==1: print(i, r.text[:200])
    if i in (0,999,1999,2999,3999): print(f"order #{i+1}: {1000*(time.time()-t):.0f} ms")
    if time.time()-t0>240: print("stopping at", i); break
print("codes", codes, f"total {time.time()-t0:.0f}s")
print("ticks in 10s with working orders:", asyncio.run(tickrate(10)))
t=time.time(); r=c.get(f"/api/accounts/{aid}/orders"); print("list orders", len(r.content), f"{time.time()-t:.2f}s")
t=time.time(); r=c.get(f"/api/accounts/{aid}"); print("summary", f"{time.time()-t:.2f}s")
print("state MB", os.path.getsize(sys.argv[1]+"/state.json")/1e6)
t=time.time(); r=c.delete(f"/api/accounts/{aid}/orders"); print("cancel all", r.text[:60], f"{time.time()-t:.2f}s")
print(c.delete(f"/api/accounts/{aid}").status_code)
