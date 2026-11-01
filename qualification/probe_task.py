"""Probe: does configure_task(enable=false) disable the task natively, and does list_tasks see it?"""
import asyncio
import json

from harness import Run, server
from run_t1 import ENG_OPS, fixture, ok, open_ctx


async def main():
    run = Run("probe-task")
    sln = fixture(run, "P")
    async with server(run, "probe") as c:
        c.test = "PROBE-O24"
        ctx = await open_ctx(c, sln)
        g = ok(await c.run_op("safety.grant", {"operations": ENG_OPS, "contextHandle": ctx, "reason": "probe", "ttlSeconds": 600}), "grant")["grantHandle"]
        before = ok(await c.run_op("engineering.list_tasks", {"contextHandle": ctx}), "list")["tasks"][0]
        set1 = ok(await c.run_op("engineering.configure_task", {"contextHandle": ctx, "grantHandle": g, "taskName": "PlcTask", "enable": False}), "disable")
        listed = ok(await c.run_op("engineering.list_tasks", {"contextHandle": ctx}), "list")["tasks"][0]
        native = ok(await c.run_op("engineering.configure_task", {"contextHandle": ctx, "grantHandle": g, "taskName": "PlcTask", "autostart": before["autoStart"]}), "read native")
        await c.run_op("context.close", {"contextHandle": ctx})
    tsproj = next(sln.parent.glob("*/*.tsproj")).read_text(encoding="utf-8")
    task_xml = tsproj[tsproj.index("<Task "):tsproj.index("</Task>") + 7]
    print(json.dumps(dict(before=before, configureSaid=dict(prev=set1["previousDisabled"], new=set1["newDisabled"]),
                          listTasksAfter=listed["disabled"], nativeAfter=native["previousDisabled"], savedTaskXml=task_xml), indent=1))


if __name__ == "__main__":
    asyncio.run(main())
