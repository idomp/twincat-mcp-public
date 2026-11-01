"""Tier 4 on the designated jig, one approved step per invocation.

Approved by the user on 2026-10-29, at the jig: matching login, online change (add one unused
variable, then remove it), PLC Stop/Run, and redeploy of the same application. On a mismatch at
login, stop and ask. Target, project copy and symbols come from site.json. The runner trusts
site.json to name the approved project copy. It checks the PLC name and port in that copy, not
its source. Every step needs the system and the PLC in Run.
Usage: run_t4.py login-oc | stoprun | deploy
"""
import asyncio
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from harness import Blocked, check, execute, guarded, route_netid, server, site

SITE = site()
TARGET, PORT, ROUTE, TC = SITE["target"], SITE["port"], SITE["route"], SITE["tcVersion"]
SLN, PLC, APP, CYCLES = Path(SITE["solution"]), SITE["plc"], SITE["app"], SITE["cycles"]
GVL_FILE, GVL_PATH, GVL_SYM = Path(SITE["gvlFile"]), SITE["gvlPath"], SITE["gvlSymbol"]
WATCH = {SITE["writes"][k]: v for k, v in SITE["writes"]["expected"].items()}  # must hold throughout
PROBE = "qualOcProbe"
STEPS = ("login-oc", "stoprun", "deploy")


def res(r):
    return r.get("result") or {}


def msg(r):
    return str(res(r).get("errorMessage") or res(r).get("message") or r.get("code"))[:200]


def configured_plcs(sln):
    """{name: port} from the project files. engineering.list_plcs and engineering.info reported
    851 for a PLC configured on 852 (measured)."""
    refs = re.findall(r'^Project\("[^"]*"\)\s*=\s*"[^"]*",\s*"([^"]+\.tspp?roj)"',
                      sln.read_text(encoding="utf-8-sig"), re.MULTILINE | re.IGNORECASE)
    check(len(refs) == 1, f"{sln.name} names {len(refs)} TwinCAT projects, expected 1")
    tsproj = sln.parent / refs[0]
    plcs = {}
    for node in ET.parse(tsproj).getroot().findall("./Project/Plc/Project"):
        if node.get("File"):  # an independent project file
            node = ET.parse(tsproj.parent / "_Config" / "PLC" / node.get("File")).getroot().find("./Project")
        plcs[node.get("Name")] = int(node.get("AmsPort"))
    return plcs


async def main(run, step):
    st = {"grants": []}
    async with server(run, f"t4-{step}") as c:
        async def read_opt(sym):
            """(True, value) for a read that succeeded, (False, error) otherwise."""
            r = await c.run_op("runtime.read_var", {"amsNetId": TARGET, "port": PORT, "symbol": sym})
            return (True, res(r).get("value")) if r.get("state") == "succeeded" else (False, msg(r))

        async def read(sym):
            done, value = await read_opt(sym)
            check(done, f"read {sym}: {value}")
            return value

        async def state(port):
            r = await c.run_op("runtime.get_state", {"amsNetId": TARGET, "port": port})
            return res(r).get("adsState")

        async def identity(label):
            c.test = f"ID-{label}"
            check(route_netid(ROUTE) == TARGET, f"route {ROUTE} does not name {TARGET} exactly once")
            app_read, app = await read_opt("TwinCAT_SystemInfoVarList._AppInfo.ProjectName")
            snap = dict(system=await state(10000), plc=await state(PORT), appRead=app_read, app=app,
                        oc=(await read_opt("TwinCAT_SystemInfoVarList._AppInfo.OnlineChangeCnt"))[1])
            for s in WATCH:
                snap[s] = await read_opt(s)
            run.write(dict(phase="snapshot", testId=c.test, snapshot=snap))
            check(app_read and app == APP, f"application {app}, expected {APP} (PLC {snap['plc']})")
            check(snap["system"] == "Run" and snap["plc"] == "Run", f"state system {snap['system']} PLC {snap['plc']}")
            for s, want in WATCH.items():
                check(snap[s] == (True, want), f"{s} reads {snap[s]}, expected {want}")
            return snap

        async def advancing():
            a = await read(CYCLES)
            await asyncio.sleep(1.5)
            b = await read(CYCLES)
            return int(b) > int(a)

        async def open_ctx():
            on_disk = configured_plcs(SLN)
            check(on_disk == {PLC: PORT}, f"{SLN.name} configures {on_disk}, expected only {PLC} on port {PORT}")
            r = await c.run_op("context.open", {"solutionPath": str(SLN), "amsNetId": TARGET, "plcName": PLC, "tcVersion": TC})
            check(r.get("state") == "succeeded", f"context.open: {msg(r)}")
            ctx = res(r)["contextHandle"]
            st["ctx"] = ctx
            listed = await c.run_op("engineering.list_plcs", {"contextHandle": ctx})
            check(listed.get("state") == "succeeded", f"list_plcs: {msg(listed)}")
            names = [p.get("name") for p in res(listed).get("plcProjects") or []]
            check(names == [PLC], f"{SLN.name} holds PLC projects {names}, expected only {PLC}")
            info = await c.run_op("engineering.info", {"contextHandle": ctx})
            check(info.get("state") == "succeeded", f"info: {msg(info)}")
            return ctx, res(info)

        async def grant(ops, **scope):
            r = await c.run_op("safety.grant", dict(operations=ops, reason="tier 4 approved by the user at the jig", ttlSeconds=1800, **scope))
            check(r.get("state") == "succeeded", f"grant: {msg(r)}")
            st["grants"].append(res(r)["grantHandle"])
            return res(r)["grantHandle"]

        try:
            if step == "login-oc":
                await login_oc_step(run, c, st, identity, advancing, open_ctx, grant, read, read_opt)
            elif step == "stoprun":
                await stoprun_step(run, c, st, identity, grant, state)
            elif step == "deploy":
                await deploy_step(run, c, st, identity, advancing, open_ctx, grant)
        finally:
            unconfirmed = list(st["grants"])
            try:
                for g in st["grants"]:
                    r = await c.ctl("safety.revoke", {"grantHandle": g})
                    run.write(dict(phase="revoke", testId=c.test, receipt=r))
                    if r.get("revoked") is True:
                        unconfirmed.remove(g)
            finally:
                if unconfirmed:
                    run.record("T4-REVOKE", 4, "FAIL", f"{len(unconfirmed)} of {len(st['grants'])} revokes not confirmed")
                if st.get("ctx"):
                    await c.run_op("context.close", {"contextHandle": st["ctx"]})


async def login_oc_step(run, c, st, identity, advancing, open_ctx, grant, read, read_opt):
    async def login_oc():
        await identity("start")
        check(await advancing(), "cycle counter not advancing before login")
        ctx, info = await open_ctx()
        platform = info.get("targetPlatform")
        g = await grant(["engineering.matching_login", "engineering.edit_plc_source", "engineering.online_change"], contextHandle=ctx)
        c.test = "O01"
        ml = await c.run_op("engineering.matching_login", {"contextHandle": ctx, "grantHandle": g, "port": PORT,
                                                            "configuration": "Release", "platform": platform, "confirm": "CONFIRM"})
        x = res(ml)
        run.write(dict(phase="observation", testId="O01", platform=platform, login=x))
        if not (ml.get("state") == "succeeded" and x.get("loggedIn") is True and x.get("promptRejected") is not True):
            st["stopped"] = f"matching login did not match: state {ml.get('state')}, loggedIn {x.get('loggedIn')}, promptRejected {x.get('promptRejected')}: {msg(ml)}"
            raise Blocked(st["stopped"])
        login = f"matching login on {platform}"
        c.test = "O02-jig"
        src = await c.run_op("engineering.read_plc_source", {"contextHandle": ctx, "path": GVL_PATH, "section": "declaration"})
        check(src.get("state") == "succeeded", f"read source: {msg(src)}")
        original, h0 = res(src)["text"], res(src)["sha256"]
        check("END_VAR" in original and PROBE not in original, "unexpected GVL declaration")
        edited = original.replace("END_VAR", f"\t{PROBE}\t: UDINT;\t// temporary MCP qualification probe\nEND_VAR", 1)

        async def apply(text, expected_hash, label, want_probe):
            c.test = f"O03-{label}"
            e = await c.run_op("engineering.edit_plc_source", {"contextHandle": ctx, "grantHandle": g, "path": GVL_PATH,
                                                               "section": "declaration", "text": text, "expectedSha256": expected_hash})
            check(e.get("state") == "succeeded", f"edit {label}: {msg(e)}")
            chk = await c.run_op("engineering.check_all_objects", {"contextHandle": ctx})
            check(chk.get("state") == "succeeded", f"check_all_objects {label}: {msg(chk)}")
            k = int(await read("TwinCAT_SystemInfoVarList._AppInfo.OnlineChangeCnt"))
            c.test = f"O04-{label}"
            oc = await c.run_op("engineering.online_change", {"contextHandle": ctx, "grantHandle": g, "port": PORT,
                                                              "contextFile": str(GVL_FILE), "cycleSymbol": CYCLES,
                                                              "expectedOnlineChangeCount": k, "timeoutMs": 10000, "confirm": "CONFIRM"})
            y = res(oc)
            run.write(dict(phase="observation", testId=c.test, onlineChange=y))
            check(oc.get("state") == "succeeded", f"online change {label} {oc.get('state')}: {msg(oc)}")
            k2 = int(await read("TwinCAT_SystemInfoVarList._AppInfo.OnlineChangeCnt"))
            check(k2 == k + 1, f"online change count {k} -> {k2}")
            probe_read, _ = await read_opt(f"{GVL_SYM}.{PROBE}")
            check(probe_read == want_probe, f"probe readable={probe_read}, wanted {want_probe}")
            check(await advancing(), f"cycle counter stopped after online change {label}")
            return k2, y

        st["probe"] = "the GVL copy may hold the probe"  # set before the edit; cleared after the removal
        k1, y1 = await apply(edited, h0, "add", True)
        st["probe"] = f"{PROBE} is in the PLC and can be in the GVL copy"
        again = await c.run_op("engineering.read_plc_source", {"contextHandle": ctx, "path": GVL_PATH, "section": "declaration"})
        check(again.get("state") == "succeeded" and res(again).get("text") == edited,
              "the declaration changed after the probe was added; not removing it over another edit")
        k2, y2 = await apply(original, res(again)["sha256"], "remove", False)
        st.pop("probe")
        await identity("after-oc")
        return (f"{login}; online change added {PROBE} (count {k1}, readable), then removed it (count {k2}, not readable); "
                f"cycle counter advanced throughout; watched values unchanged; flags {json.dumps({k: y1.get(k) for k in ('dispatched', 'runtimeVerified')})}")
    try:
        await guarded(run, "T4-LOGIN-OC", 4, login_oc())
    finally:  # guarded() does not catch a cancellation
        if st.get("probe"):
            run.record("T4-STOP", 4, "BLOCKED", f"{st['probe']}. Reconcile it by hand before any deploy; the runner does not repeat an online change.")
        elif st.get("stopped"):
            run.record("T4-STOP", 4, "BLOCKED", st["stopped"] + "; stopped, asking the user")


async def stoprun_step(run, c, st, identity, grant, state):
    async def stoprun():
        await identity("start")
        g = await grant(["runtime.set_state"], amsNetId=TARGET)
        c.test = "O20a-stop"
        st["stopped_plc"] = True  # set before the call: the target can apply a Stop whose reply is lost
        s = await c.run_op("runtime.set_state", {"amsNetId": TARGET, "port": PORT, "state": "Stop", "grantHandle": g})
        run.write(dict(phase="observation", testId=c.test, receipt=res(s)))
        await asyncio.sleep(2)
        after_stop = dict(system=await state(10000), plc=await state(PORT))
        check(s.get("state") == "succeeded" and after_stop["plc"] == "Stop" and after_stop["system"] == "Run",
              f"Stop: receipt {s.get('state')}, now {after_stop}")
        await asyncio.sleep(3)
        c.test = "O20a-run"
        # Run on a PLC port goes to port 10000 when the system is not in Run; only a PLC Run is approved.
        before_run = dict(system=await state(10000), plc=await state(PORT))
        check(before_run == dict(system="Run", plc="Stop"), f"state changed before Run: {before_run}; not sending Run")
        r = await c.run_op("runtime.set_state", {"amsNetId": TARGET, "port": PORT, "state": "Run", "grantHandle": g})
        x = res(r)
        run.write(dict(phase="observation", testId=c.test, receipt=x))
        check(x.get("port") == PORT and x.get("plcRunDispatched") is False, f"Run went beyond port {PORT}: {json.dumps(x)[:300]}")
        await asyncio.sleep(3)
        after_run = dict(system=await state(10000), plc=await state(PORT))
        check(after_run["plc"] == "Run", f"Run receipt {r.get('state')} (port {x.get('port')}, '{msg(r)}'), but PLC is {after_run['plc']}")
        st.pop("stopped_plc")
        await identity("end")
        return f"Stop left system Run, PLC Stop; Run receipt port {x.get('port')}; PLC back in Run"
    try:
        await guarded(run, "O20a", 4, stoprun())
    finally:
        if st.get("stopped_plc"):
            run.record("T4-STOP", 4, "BLOCKED", f"the PLC on port {PORT} can be left in Stop; the runner does not retry Run")


async def deploy_step(run, c, st, identity, advancing, open_ctx, grant):
    async def deploy():
        await identity("start")
        ctx, _ = await open_ctx()
        g = await grant(["workflow.deploy"], contextHandle=ctx)
        c.test = "O11"
        t = time.monotonic()
        st["deploy_sent"] = True  # set before the call: the target can apply a deploy whose reply is lost
        d = await c.run_op("workflow.deploy", {"contextHandle": ctx, "grantHandle": g, "skipBuild": False, "dryRun": False, "confirm": "CONFIRM"}, timeout=1200)
        run.write(dict(phase="observation", testId="O11", deploy=res(d)))
        check(d.get("state") == "succeeded", f"deploy {d.get('state')}: {msg(d)}")
        w = await c.run_op("workflow.wait_state", {"amsNetId": TARGET, "port": PORT, "expectedState": "Run", "timeoutSeconds": 120, "intervalSeconds": 2})
        check(w.get("state") == "succeeded", f"PLC not Run after deploy: {msg(w)}")
        snap = await identity("end")
        check(await advancing(), "cycle counter not advancing after deploy")
        st.pop("deploy_sent")
        return f"deployed in {round(time.monotonic() - t)} s; steps {[s_.get('action') for s_ in res(d).get('steps', [])]}; PLC Run, app {snap['app']}, OC count {snap['oc']}"
    try:
        await guarded(run, "T4-DEPLOY", 4, deploy())
    finally:
        if st.get("deploy_sent"):
            run.record("T4-STOP", 4, "BLOCKED", f"the deploy was sent and did not verify; the PLC on port {PORT} can be left stopped; the runner does not repeat a deploy")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in STEPS:
        sys.exit(f"Usage: run_t4.py {' | '.join(STEPS)}")
    execute(f"tier4-{sys.argv[1]}", f"Tier 4 {sys.argv[1]}", 4, lambda run: main(run, sys.argv[1]))
