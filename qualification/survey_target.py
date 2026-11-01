"""Read-only survey of the designated target before any write test. Reads only."""
import asyncio
import json
import sys

from harness import Run, server, site

SITE = site()
TARGET, PORT = SITE["target"], SITE["port"]


async def main(prefixes, symbols):
    run = Run("survey")
    out = {}
    async with server(run, "survey") as c:
        c.test = "SURVEY"
        for port in (10000, PORT):
            r = await c.run_op("runtime.get_state", {"amsNetId": TARGET, "port": port})
            out[f"state{port}"] = (r.get("result") or {}).get("adsState")
        for prefix in prefixes:
            r = await c.run_op("runtime.list_symbols", {"amsNetId": TARGET, "port": PORT, "prefix": prefix, "max": 60, "includeTypes": True})
            out[prefix] = [(s["name"], s.get("typeName"), s.get("size")) for s in (r.get("result") or {}).get("symbols", [])]
        for sym in symbols:
            r = await c.run_op("runtime.read_var", {"amsNetId": TARGET, "port": PORT, "symbol": sym})
            x = r.get("result") or {}
            out[sym] = x.get("value") if r.get("state") == "succeeded" else f"ERR {str(x.get('errorMessage'))[:80]}"
    print(json.dumps(out, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    args = json.loads(sys.argv[1])
    asyncio.run(main(args["prefixes"], args["symbols"]))
