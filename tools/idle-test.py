#!/usr/bin/env python3
"""Check that only VISIBLE tabs keep the gateway awake.

Acts like the page: opens the input WebSocket, stays visible 16 s, reports the
tab hidden for 16 s while polling /feeds?bg=1 as a hidden tab does, then visible
again. Read the result in the GATEWAY LOG of a test gateway started with
--idle 10: expect "input client hidden", then "idle: no viewer" about 10 s
later, then "waking" at "input client visible". Verified so 2026-11-20.
Usage (inside the kvm image, which has aiohttp): idle-test.py http://127.0.0.1:8821
"""
import asyncio, sys, aiohttp

async def main(base):
    async with aiohttp.ClientSession() as s:
        async def bgpoll():
            async with s.get(base + "/feeds?bg=1") as r:
                await r.read()
        ws = await s.ws_connect(base.replace("http", "ws") + "/input", origin=base)
        await ws.send_json({"t": "vis", "v": "visible"})
        await asyncio.sleep(16)
        await ws.send_json({"t": "vis", "v": "hidden"})
        for _ in range(8):
            await asyncio.sleep(2); await bgpoll()
        await ws.send_json({"t": "vis", "v": "visible"})
        await asyncio.sleep(8)
        await ws.close()

asyncio.run(main(sys.argv[1].rstrip("/")))
