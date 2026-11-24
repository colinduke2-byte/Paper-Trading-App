import asyncio, json, websockets, time, httpx
B="127.0.0.1:18431"
async def main():
    good = await websockets.connect(f"ws://{B}/ws")
    await good.send(json.dumps({"symbols":["MKTX"],"account":"A0001"}))
    m = json.loads(await asyncio.wait_for(good.recv(), 5)); print("good got", m["type"], list(m)[:6])
    # cross-origin ws
    for origin in ["http://evil.example", None, f"http://{B}"]:
        try:
            w = await websockets.connect(f"ws://{B}/ws", origin=origin); print("origin", origin, "accepted"); await w.close()
        except Exception as e: print("origin", origin, "refused", repr(e)[:80])
    # victim connects AFTER the bad one in dict order? clients dict insertion order: good first. Make bad first then good2.
    bad = await websockets.connect(f"ws://{B}/ws")
    good2 = await websockets.connect(f"ws://{B}/ws")
    await good2.send(json.dumps({"symbols":["MKTX"],"account":"A0001"}))
    await asyncio.wait_for(good2.recv(), 5)
    await bad.send(json.dumps({"account":["unhashable"]}))
    await asyncio.sleep(1.5)
    # drain
    async def count(w, secs):
        n=0; end=time.time()+secs
        while time.time()<end:
            try: await asyncio.wait_for(w.recv(), end-time.time()); n+=1
            except Exception: break
        return n
    n1, n2 = await asyncio.gather(count(good, 5), count(good2, 5))
    print("after bad account: good(before bad) ticks in 5s:", n1, " good2(after bad):", n2)
    await bad.close(); await asyncio.sleep(1.5)
    n1, n2 = await asyncio.gather(count(good, 3), count(good2, 3)); print("after bad closed:", n1, n2)
    # malformed
    w = await websockets.connect(f"ws://{B}/ws"); await w.send("not json")
    try: print("malformed ->", await asyncio.wait_for(w.recv(), 3))
    except Exception as e: print("malformed -> closed", repr(e)[:100])
    # big message
    w = await websockets.connect(f"ws://{B}/ws", max_size=None)
    try:
        await w.send(json.dumps({"symbols":["A"*1000000]*15})); 
        r = await asyncio.wait_for(w.recv(), 5); print("15MB msg accepted, tick len", len(r))
    except Exception as e: print("big msg", repr(e)[:120])
    try:
        await w.send("x"*(17*1024*1024)); await asyncio.wait_for(w.recv(), 5)
    except Exception as e: print("17MB msg", repr(e)[:120])
    # the huge-equity account subscription
    w = await websockets.connect(f"ws://{B}/ws"); await w.send(json.dumps({"account":"A0010","symbols":[]}))
    await asyncio.sleep(1.5)
    n1, n2 = await asyncio.gather(count(good, 4), count(good2, 4)); print("with A0010 subscriber present:", n1, n2)
    await w.close()
asyncio.run(main())
