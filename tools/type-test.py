#!/usr/bin/env python3
"""Drive "Type text" on a test gateway like the page does and print the
progress messages. Usage: type-test.py <gateway-url> <tx-id> <text> [stop-after-s]
Point it ONLY at an unused transmitter ID: the keys are really sent."""
import asyncio, json, sys
import aiohttp


async def main(url, n, text, stop_after=None):
    origin = url.split("/", 3)
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(url.rstrip("/") + "/input",
                                headers={"Origin": f"{origin[0]}//{origin[2]}"}) as ws:
            await ws.send_json({"t": "sel", "id": n})
            await ws.send_json({"t": "type", "text": text})
            if stop_after:
                await asyncio.sleep(stop_after)
                await ws.send_json({"t": "type-stop"})
            async for msg in ws:
                m = json.loads(msg.data)
                print(m, flush=True)
                if m.get("t") == "typed" and m["state"] != "running":
                    break


if __name__ == "__main__":
    a = sys.argv
    asyncio.run(main(a[1], int(a[2]), a[3], float(a[4]) if len(a) > 4 else None))
