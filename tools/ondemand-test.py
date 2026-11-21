#!/usr/bin/env python3
"""Check that a feed starts only for a tile a visible page is viewing.

Acts like the page: 12 s viewing nothing, then 12 s viewing TX 2, then nothing.
Read the GATEWAY LOG of a test gateway: expect no "feed N: starting" in the first
12 s, then "feed 2: starting" and no other feed. Verified so 2026-11-20.
Usage (inside the kvm image): ondemand-test.py http://127.0.0.1:8821
"""
import asyncio, sys, aiohttp

async def main(base):
    async with aiohttp.ClientSession() as s:
        ws = await s.ws_connect(base.replace("http", "ws") + "/input", origin=base)
        await ws.send_json({"t": "vis", "v": "visible"})
        await ws.send_json({"t": "view", "ids": []})
        print("viewing nothing", flush=True); await asyncio.sleep(12)
        await ws.send_json({"t": "view", "ids": [2]})
        print("viewing 2", flush=True); await asyncio.sleep(12)
        await ws.close()

asyncio.run(main(sys.argv[1].rstrip("/")))
