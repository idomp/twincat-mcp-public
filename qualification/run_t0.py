"""Tier 0: protocol, catalog, grants, contexts, jobs. No XAE and no ADS.

Dispatch is disabled (TWINCAT_DISABLE_HOST=1) and TWINCAT_AUTOMATION_EXE names a file
that does not exist, so no operation can reach a worker, XAE or a target.
"""
import asyncio
import json

from harness import QUAL, check, execute, guarded, server, site

FAKE = "192.0.2.1.1.1"  # TEST-NET-1: documentation range, never routed
OTHER = "192.0.2.2.1.1"
SCOPE_OPS = ["scope.start", "scope.stop", "scope.status", "scope.export"]


def code_of(receipt):
    res = receipt.get("result") or receipt
    return res.get("code")


async def main(run):
    sln = next((QUAL / "fixtures" / site()["fixtures"]["xti"]).glob("*.sln"))
    env = {
        "TWINCAT_DISABLE_HOST": "1",
        "TWINCAT_AUTOMATION_EXE": str(run.dir / "no-such-worker.exe"),
        "TWINCAT_MCP_SHUTDOWN_GRACE_SECONDS": "2",
    }
    state = {}

    async with server(run, "t0a", env) as c:
        state_dir = c.state_dir

        async def l01():
            c.test = "L01"
            tools = await c.session.list_tools()
            names = sorted(t.name for t in tools.tools)
            run.write(dict(phase="receipt", client="t0a", testId=c.test, tool="list_tools", names=names))
            check(names == ["twincat_describe", "twincat_execute", "twincat_search"], f"tools {names}")
            ids, offset = [], 0
            while offset is not None:
                page, err = await c.tool("twincat_search", {"query": "", "limit": 20, "offset": offset})
                check(not err, f"search error {page}")
                ids += [m["operation"] for m in page["matches"]]
                offset = page["nextOffset"]
            check(len(ids) == len(set(ids)) == 62, f"searchable {len(ids)} unique {len(set(ids))}")
            check(not set(ids) & set(SCOPE_OPS), "disabled Scope operations appear in search")
            all_ids = ids + SCOPE_OPS
            described = []
            for i in range(0, len(all_ids), 5):
                d, err = await c.tool("twincat_describe", {"operations": all_ids[i:i + 5]})
                check(not err, f"describe error {d}")
                described += d["operations"]
            check(len(described) == 66 and all("inputSchema" in e for e in described), f"described {len(described)}")
            unavailable = sorted(e["id"] for e in described if e.get("available") is False)
            check(unavailable == sorted(SCOPE_OPS), f"unavailable {unavailable}")
            state["ids"] = all_ids
            bad, err = await c.tool("twincat_describe", {"operations": all_ids[:6]})
            check(err, "describe accepted 6 ids")
            bad, err = await c.tool("twincat_nonexistent", {})
            check(err, "unknown tool accepted")
            bad, err = await c.tool("twincat_execute", {"operation": "no.such_op", "arguments": {}})
            check(err, "unknown operation accepted")
            bad, err = await c.tool("twincat_execute", {"operation": "system.status", "arguments": {}, "bogus": 1})
            check(err and bad.get("code") == "invalid_arguments", f"extra field: {bad}")
            bad, err = await c.tool("twincat_execute", {"arguments": {}})
            check(err and bad.get("code") == "invalid_arguments", f"missing operation: {bad}")
            return "3 tools, 62 searchable, 66 describable, 4 unavailable, malformed calls rejected"

        await guarded(run, "L01", 0, l01())

        async def c10():
            c.test = "C10"
            s = await c.ctl("system.status")
            check(s.get("success") and s["version"] == "3.0.0", f"version {s.get('version')}")
            check(s["operations"] == 66, f"operations {s['operations']}")
            check(sorted(s["unavailableOperations"]) == sorted(SCOPE_OPS), f"unavailable {s['unavailableOperations']}")
            check(s["protocol"] == "2026-07-28", f"protocol {s['protocol']}")
            return f"version 3.0.0, 66 operations, protocol {s['protocol']}"

        await guarded(run, "C10", 0, c10())

        scope_args = {
            "scope.start": {"configPath": str(run.dir / "dummy.tcscopex"), "grantHandle": "invalid-handle-0"},
            "scope.stop": {"recordingHandle": "invalid-handle-0", "outputPath": str(run.dir / "stop.csv"), "format": "csv"},
            "scope.status": {"recordingHandle": "invalid-handle-0"},
            "scope.export": {"inputPath": str(run.dir / "in.svdx"), "outputPath": str(run.dir / "export.csv"), "format": "csv"},
        }
        for n, op in zip(("O38", "O39", "O40", "O41"), SCOPE_OPS):
            async def scope_neg(op=op, n=n):
                c.test = n
                before = sorted(p.name for p in run.dir.iterdir())
                r = await c.run_op(op, scope_args[op])
                check(r.get("state") == "failed" and code_of(r) == "unsupported_integration", f"{op}: {r.get('state')} {code_of(r)}")
                after = sorted(p.name for p in run.dir.iterdir())
                check(before == after, f"{op} created files {set(after) - set(before)}")
                return f"{op} rejected with unsupported_integration, no files"
            await guarded(run, n, 0, scope_neg())

        async def contexts():
            c.test = "C01"
            r = await c.run_op("context.open", {"solutionPath": str(sln), "amsNetId": FAKE, "plcName": "PLC1"})
            check(r["state"] == "succeeded", f"open {r.get('state')} {r.get('result')}")
            ctx = r["result"]["contextHandle"]
            state["ctx"], state["open_job"] = ctx, r["jobHandle"]
            check(r["result"]["target"] == FAKE and r["result"]["plcName"] == "PLC1", f"open result {r['result']}")
            c.test = "C02"
            s = await c.ctl("context.status", {"contextHandle": ctx})
            check(s.get("valid") is True and s.get("workerAlive") is False, f"status {s}")
            c.test = "L02"
            r2 = await c.run_op("context.open", {"solutionPath": str(sln), "amsNetId": FAKE, "plcName": "PLC1"})
            check(r2["state"] == "failed" and code_of(r2) == "context_busy", f"second open {r2.get('state')} {code_of(r2)}")
            return "open is lazy (workerAlive false), status valid, second open context_busy"

        await guarded(run, "C01-C02-L02", 0, contexts())

        async def grants():
            c.test = "C04"
            g = await c.run_op("safety.grant", {"operations": ["runtime.write_var"], "amsNetId": FAKE, "reason": "tier0 qualification", "ttlSeconds": 60})
            check(g["state"] == "succeeded", f"grant {g.get('result')}")
            G = g["result"]["grantHandle"]
            check(g["result"].get("operations") == ["runtime.write_var"], f"grant ops {g['result']}")
            two = await c.run_op("safety.grant", {"operations": ["runtime.write_var"], "amsNetId": FAKE, "contextHandle": state["ctx"], "reason": "x"})
            check(code_of(two) == "invalid_scope", f"two scopes: {code_of(two)}")
            ungated = await c.run_op("safety.grant", {"operations": ["engineering.build"], "contextHandle": state["ctx"], "reason": "x"})
            check(code_of(ungated) == "invalid_grant", f"ungated op: {code_of(ungated)}")
            wrongscope = await c.run_op("safety.grant", {"operations": ["engineering.set_target"], "amsNetId": FAKE, "reason": "x"})
            check(code_of(wrongscope) == "invalid_grant", f"engineering op with target scope: {code_of(wrongscope)}")
            c.test = "L04"
            w = lambda grant, target=FAKE: {"amsNetId": target, "symbol": "GVL.x", "value": "1", "grantHandle": grant}
            forged = await c.run_op("runtime.write_var", w("forged-handle-000000"))
            check(code_of(forged) == "grant_required", f"forged: {code_of(forged)}")
            mism = await c.run_op("runtime.write_var", w(G, OTHER))
            check(code_of(mism) == "grant_scope_mismatch", f"other target: {code_of(mism)}")
            g2 = (await c.run_op("safety.grant", {"operations": ["runtime.set_state"], "amsNetId": FAKE, "reason": "x"}))["result"]["grantHandle"]
            wrongop = await c.run_op("runtime.write_var", w(g2))
            check(code_of(wrongop) == "grant_scope_mismatch", f"wrong operation: {code_of(wrongop)}")
            nog, err = await c.submit("runtime.write_var", {"amsNetId": FAKE, "symbol": "GVL.x", "value": "1"})
            check(nog.get("code") == "invalid_arguments", f"missing grantHandle: {nog}")
            ok = await c.run_op("runtime.write_var", w(G))
            check(code_of(ok) not in {"grant_required", "grant_scope_mismatch"}, f"valid grant rejected: {code_of(ok)}")
            state["dispatch_no_worker"] = dict(state=ok.get("state"), code=code_of(ok))
            c.test = "C05"
            rv = await c.ctl("safety.revoke", {"grantHandle": G})
            check(rv.get("revoked") is True, f"revoke {rv}")
            after = await c.run_op("runtime.write_var", w(G))
            check(code_of(after) == "grant_required", f"after revoke: {code_of(after)}")
            short = (await c.run_op("safety.grant", {"operations": ["runtime.write_var"], "amsNetId": FAKE, "reason": "x", "ttlSeconds": 1}))["result"]["grantHandle"]
            await asyncio.sleep(2.2)
            exp = await c.run_op("runtime.write_var", w(short))
            check(code_of(exp) == "grant_required", f"expired: {code_of(exp)}")
            state["G2"] = g2
            return f"scope rules, forged/mismatch/wrong-op/missing/revoked/expired all rejected; valid grant passed preflight then {state['dispatch_no_worker']}"

        await guarded(run, "C04-C05-L04", 0, grants())

        async def ctx_scope():
            c.test = "L05"
            gs = await c.run_op("safety.grant", {"operations": ["engineering.set_target"], "contextHandle": state["ctx"], "reason": "x"})
            G = gs["result"]["grantHandle"]
            cl = await c.run_op("context.close", {"contextHandle": state["ctx"]})
            check(cl["state"] == "succeeded" and cl["result"].get("closed"), f"close {cl.get('result')}")
            st = await c.ctl("context.status", {"contextHandle": state["ctx"]})
            check(st.get("code") == "expired_context", f"status after close {st}")
            rel = await c.run_op("context.open", {"solutionPath": "relative.sln", "amsNetId": FAKE, "plcName": "PLC1"})
            check(code_of(rel) == "invalid_solution", f"relative path: {code_of(rel)}")
            o2 = await c.run_op("context.open", {"solutionPath": str(sln), "amsNetId": FAKE, "plcName": "PLC1"})
            c2 = o2["result"]["contextHandle"]
            state["ctx2"] = c2
            m = await c.run_op("engineering.set_target", {"contextHandle": c2, "grantHandle": G})
            check(code_of(m) == "grant_scope_mismatch", f"old grant on new context: {code_of(m)}")
            old = await c.run_op("engineering.set_target", {"contextHandle": state["ctx"], "grantHandle": G})
            check(code_of(old) == "expired_context", f"old context: {code_of(old)}")
            return "closed context rejected; old grant does not cover a new context"

        await guarded(run, "L05", 0, ctx_scope())

        async def jobs():
            c.test = "C06"
            j = state["open_job"]
            g = await c.ctl("job.get", {"jobHandle": j})
            for k in ("jobHandle", "operation", "state", "cancelRequested", "progress", "createdAt", "updatedAt", "resultAvailable", "nextOperation"):
                check(k in g, f"job.get lacks {k}")
            unk = await c.ctl("job.get", {"jobHandle": "unknown-job-handle-0000"})
            check(unk.get("success") is False and unk.get("code"), f"unknown job {unk}")
            c.test = "C07"
            wt = await c.ctl("job.wait", {"jobHandle": j, "waitSeconds": 0})
            check(wt["state"] == "succeeded" and wt["waitTimedOut"] is False, f"wait {wt}")
            c.test = "C08"
            whole = json.dumps(g["result"], ensure_ascii=False)
            text, off, pages = "", 0, 0
            while off is not None:
                p = await c.ctl("job.result", {"jobHandle": j, "offset": off, "limit": 256})
                text += p["text"]; off = p["nextOffset"]; pages += 1
            check(json.loads(text) == g["result"], f"reconstruction failed, pages={pages}")
            small = await c.ctl("job.result", {"jobHandle": j, "offset": 0, "limit": 10})
            check(small.get("code") == "invalid_arguments", f"limit below 256 accepted {small}")
            bad = await c.ctl("job.result", {"jobHandle": j, "offset": len(whole) + 5, "limit": 256})
            check(bad.get("code") == "invalid_offset", f"bad offset {bad}")
            c.test = "C09"
            cn = await c.ctl("job.cancel", {"jobHandle": j})
            check(cn["state"] == "succeeded" and cn["cancelRequested"] is False, f"cancel terminal {cn}")
            return f"receipt fields, unknown handle, wait, result reconstruction ({pages} page; multi-page in tier 1), limit and offset bounds, cancel of terminal job"

        await guarded(run, "C06-C09", 0, jobs())

        async def keys():
            c.test = "L07"
            args = {"amsNetId": FAKE, "port": 851}
            r1, key = await c.submit("runtime.get_state", args)
            r2, _ = await c.submit("runtime.get_state", args, key)
            check(r1["jobHandle"] == r2["jobHandle"], "same key gave a new job")
            conflict, _ = await c.submit("runtime.get_state", {"amsNetId": FAKE, "port": 852}, key)
            check(conflict.get("code") == "request_key_conflict", f"conflict {conflict}")
            nokey, _ = await c.tool("twincat_execute", {"operation": "runtime.get_state", "arguments": args})
            check(nokey.get("code") == "request_key_required", f"no key {nokey}")
            state["key"], state["key_job"] = key, r1["jobHandle"]
            return "replay returns same job; changed args conflict; missing key rejected"

        await guarded(run, "L07", 0, keys())

        async def idle():
            c.test = "L14"
            await asyncio.sleep(6)
            s = await c.ctl("system.status")
            check(s.get("success"), f"status after idle {s}")
            return "server alive after 6 s idle with 2 s shutdown grace"

        await guarded(run, "L14", 0, idle())

    async with server(run, "t0b", env, state_dir=state_dir) as c:
        async def restart():
            c.test = "L03"
            j = await c.ctl("job.get", {"jobHandle": state["key_job"]})
            check(j.get("jobHandle") == state["key_job"], f"job lost after restart {j}")
            r, _ = await c.submit("runtime.get_state", {"amsNetId": FAKE, "port": 851}, state["key"])
            check(r.get("jobHandle") == state["key_job"], "key replay after restart made a new job")
            st = await c.ctl("context.status", {"contextHandle": state["ctx2"]})
            check(st.get("code") == "expired_context", f"context survived restart {st}")
            g = await c.run_op("runtime.set_state", {"amsNetId": FAKE, "state": "Stop", "grantHandle": state["G2"]})
            check(code_of(g) == "grant_required", f"grant survived restart {code_of(g)}")
            return "jobs and keys persist; contexts and grants do not"

        await guarded(run, "L03", 0, restart())



if __name__ == "__main__":
    execute("tier0", "Tier 0 qualification", 0, main)
