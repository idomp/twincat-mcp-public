"""Tier 3: approved variable writes on the designated target.

Approved by the user on 2026-10-29 for the target in site.json: same-value writes to three
parameters, one change-and-restore of one threshold, and rejection tests that write nothing.
No other symbol is written. Target, port, application and symbols come from site.json.

Before any grant, the runner checks every symbol's type, that the unknown symbol does not
exist, and the start values. After any unexpected or unverified write, no later write runs.
"""
import asyncio

from harness import Blocked, check, execute, guarded, route_netid, server, site

SITE = site()
TARGET, PORT, ROUTE, APP = SITE["target"], SITE["port"], SITE["route"], SITE["app"]
W = SITE["writes"]
ON, HYST, FLASH, HALF, UNKNOWN = W["on"], W["hyst"], W["flash"], W["half"], W["unknown"]
HOT, HOT_OK, YELLOW = W["hot"], W["hotValid"], W["yellow"]
EXPECTED = {W[k]: v for k, v in W["expected"].items()}
CHANGE, HOT_LIMIT = W["changeTo"], float(W["hotLimit"])
SAME = {s: EXPECTED[s].upper() for s in (ON, HYST, FLASH)}  # BOOL reads back as False, written as FALSE
# The rejection tests are only harmless for these types.
TYPES = {ON: "REAL", HYST: "REAL", FLASH: "BOOL", HALF: "TIME", HOT: "REAL", HOT_OK: "BOOL", YELLOW: "BOOL"}
WATCHED = (ON, HYST, FLASH, HALF, HOT, HOT_OK, YELLOW)


def res(r):
    return r.get("result") or {}


async def main(run):
    names = [W[k] for k in ("on", "hyst", "flash", "half", "unknown", "hot", "hotValid", "yellow")]
    if len(set(names)) != len(names) or set(W["expected"]) != {"on", "hyst", "flash"}:
        raise SystemExit("site.json writes must name eight distinct symbols, with expected values for on, hyst and flash.")
    st = {"stop": None, "start": None}
    async with server(run, "t3") as c:
        async def read_full(sym):
            r = await c.run_op("runtime.read_var", {"amsNetId": TARGET, "port": PORT, "symbol": sym})
            check(r.get("state") == "succeeded", f"read {sym}: {res(r).get('errorMessage')}")
            return res(r)

        async def read(sym):
            return (await read_full(sym)).get("value")

        async def snapshot(label):
            snap = {s: await read(s) for s in WATCHED}
            run.write(dict(phase="snapshot", testId=f"SNAP-{label}", snapshot=snap))
            return snap

        async def grant(ttl=300):
            gr = await c.run_op("safety.grant", {"operations": ["runtime.write_var", "runtime.write_var_list"], "amsNetId": TARGET,
                                                 "reason": "tier 3 approved write set only", "ttlSeconds": ttl})
            check(gr.get("state") == "succeeded", f"grant failed: {res(gr).get('errorMessage') or gr.get('code')}")
            return res(gr)["grantHandle"]

        async def revoke(g):
            r = await c.ctl("safety.revoke", {"grantHandle": g})
            done = r.get("revoked") is True
            run.write(dict(phase="revoke", testId=c.test, revoked=done, receipt=r))
            return done

        def w(g, sym, value):
            return {"amsNetId": TARGET, "port": PORT, "symbol": sym, "value": value, "grantHandle": g}

        async def mutating(test, body):
            """Run one write test with its own grant. Any failure stops every later write."""
            async def wrapped():
                if st["stop"]:
                    raise Blocked(f"an earlier write test failed: {st['stop']}")
                c.test = test
                g = await grant()
                revoked = False
                try:
                    detail = await body(g)
                finally:
                    revoked = await revoke(g)
                    if not revoked:
                        st["stop"] = st["stop"] or f"{test}: revoke of its write grant not confirmed"
                check(revoked, f"revoke of the {test} write grant not confirmed")
                return detail
            if not await guarded(run, test, 3, wrapped()) and not st["stop"]:
                st["stop"] = f"{test} did not pass"

        async def identity():
            c.test = "T3-ID"
            check(route_netid(ROUTE) == TARGET, f"route {ROUTE} does not name {TARGET} exactly once")
            check(await read("TwinCAT_SystemInfoVarList._AppInfo.ProjectName") == APP, "running application is not " + APP)
            g = await c.run_op("runtime.get_state", {"amsNetId": TARGET, "port": PORT})
            check(res(g).get("adsState") == "Run", f"PLC state {res(g).get('adsState')}")
            for sym, kind in TYPES.items():
                got = (await read_full(sym)).get("dataType")
                check(got == kind, f"{sym} is {got}, expected {kind}; the rejection tests depend on it")
            miss = await c.run_op("runtime.read_var", {"amsNetId": TARGET, "port": PORT, "symbol": UNKNOWN})
            check(miss.get("state") == "failed" and "SymbolNotFound" in str(res(miss).get("errorMessage")),
                  f"{UNKNOWN} must not exist: {miss.get('state')} {res(miss).get('errorMessage')}")
            snap = await snapshot("start")
            for sym, want in EXPECTED.items():
                check(snap[sym] == want, f"{sym} is {snap[sym]}, expected {want}; stopping before any write")
            hot = float(snap[HOT])
            check(snap[HOT_OK] == "True" and hot < HOT_LIMIT and hot < float(CHANGE),
                  f"hot reading {snap[HOT]} valid {snap[HOT_OK]}; must be under {HOT_LIMIT} and under {CHANGE}")
            st["start"] = snap
            return f"app {APP}, PLC Run, types as required, {UNKNOWN} absent, parameters {EXPECTED}, hot {hot} C"
        if not await guarded(run, "T3-ID", 3, identity()):
            run.record("TIER3", 3, "BLOCKED", "identity or start state not as approved; nothing was written")
            return

        async def same_value(g):
            out = []
            for sym, val in SAME.items():
                now = await read(sym)
                check(now == EXPECTED[sym], f"{sym} reads {now} before its write, expected {EXPECTED[sym]}")
                r = await c.run_op("runtime.write_var", w(g, sym, val))
                x = res(r)
                check(r.get("state") == "succeeded", f"{sym}: {x.get('errorMessage') or r.get('code')}")
                check(str(x.get("previousValue")) == str(x.get("newValue")), f"{sym} changed {x.get('previousValue')} -> {x.get('newValue')}")
                after = await read(sym)
                check(after == EXPECTED[sym], f"{sym} reads {after}")
                out.append(f"{sym.split('.')[-1]}={x.get('newValue')}")
            return "same-value writes " + ", ".join(out) + "; independent reads match"
        await mutating("O22a", same_value)

        async def same_list(g):
            for sym in SAME:
                now = await read(sym)
                check(now == EXPECTED[sym], f"{sym} reads {now} before the list write")
            r = await c.run_op("runtime.write_var_list", {"amsNetId": TARGET, "port": PORT, "grantHandle": g, "variables": SAME})
            x = res(r)
            check(r.get("state") == "succeeded" and x.get("successCount") == 3 and x.get("errorCount") == 0, f"list {r.get('state')} {x.get('successCount')}/{x.get('errorCount')}")
            for sym in SAME:
                check(await read(sym) == EXPECTED[sym], f"{sym} changed")
            return "3-item same-value list written; values unchanged"
        await mutating("O35a", same_list)

        async def change_restore(g):
            hot, ok = await read(HOT), await read(HOT_OK)
            check(ok == "True" and float(hot) < HOT_LIMIT and float(hot) < float(CHANGE), f"guard: hot {hot} valid {ok}")
            check(await read(ON) == EXPECTED[ON], f"{ON} is not at {EXPECTED[ON]} before the change")
            # Restore in every case: the target can apply a write whose reply is lost.
            try:
                r = await c.run_op("runtime.write_var", w(g, ON, CHANGE))
                check(r.get("state") == "succeeded", f"write {CHANGE}: {res(r).get('errorMessage') or r.get('code')}")
                check(await read(ON) == CHANGE, f"threshold did not read back {CHANGE}")
                await asyncio.sleep(0.5)
                check(await read(YELLOW) == "False", "yellow lamp output changed")
            finally:
                back = None
                try:
                    try:
                        g2 = await grant(60)  # its own grant, so an expired test grant cannot block the restore
                    except Exception as exc:
                        run.write(dict(phase="restore", testId="O22b", grantError=repr(exc)))
                        g2 = None  # the test grant can still be valid
                    try:
                        rr = await c.run_op("runtime.write_var", w(g2 or g, ON, SAME[ON]))
                    finally:
                        if g2 and not await revoke(g2):
                            st["stop"] = st["stop"] or "O22b: revoke of the restore grant not confirmed"
                    back = await read(ON)
                    run.write(dict(phase="restore", testId="O22b", symbol=ON, state=rr.get("state"), readBack=back))
                finally:
                    if back != EXPECTED[ON]:
                        st["stop"] = f"RESTORE NOT VERIFIED: {ON} reads {back}, must be {EXPECTED[ON]}"
            check(st["stop"] is None, st["stop"] or "")
            return f"{EXPECTED[ON]} -> {CHANGE} -> {EXPECTED[ON]} read back exactly; yellow output stayed False at hot {hot} C"
        await mutating("O22b", change_restore)

        async def rejections(g):
            out = []
            for sym, val, why in ((FLASH, "maybe", "invalid BOOL"), (ON, "1e40", "REAL overflow"), (HALF, "T#300MS", "TIME unsupported"),
                                  (UNKNOWN, "1", "unknown symbol")):
                before = await read(sym) if sym != UNKNOWN else None
                r = await c.run_op("runtime.write_var", w(g, sym, val))
                if sym == UNKNOWN:
                    gone = await c.run_op("runtime.read_var", {"amsNetId": TARGET, "port": PORT, "symbol": UNKNOWN})
                    check(gone.get("state") == "failed", f"{UNKNOWN} exists after the write")
                else:
                    after = await read(sym)
                    if after != before:
                        restored = None
                        if sym in SAME:
                            await c.run_op("runtime.write_var", w(g, sym, SAME[sym]))
                            restored = await read(sym)
                        st["stop"] = f"{why} changed {sym} {before} -> {after}; after restore it reads {restored}"
                        raise AssertionError(st["stop"])
                if r.get("state") != "failed" or r.get("unresolved"):
                    st["stop"] = f"{why}: write ended {r.get('state')} (unresolved {bool(r.get('unresolved'))}), not failed"
                    raise AssertionError(st["stop"])
                out.append(f"{why}: {str(res(r).get('errorMessage'))[:60]}")
            return "; ".join(out)
        await mutating("O22c", rejections)

        async def mixed_list(g):
            check(await read(ON) == EXPECTED[ON], f"{ON} is not at {EXPECTED[ON]} before the list write")
            r = await c.run_op("runtime.write_var_list", {"amsNetId": TARGET, "port": PORT, "grantHandle": g,
                                                           "variables": {ON: SAME[ON], UNKNOWN: "1"}})
            x = res(r)
            check(await read(ON) == EXPECTED[ON], "threshold changed")
            check(r.get("state") == "failed", f"mixed list state {r.get('state')}")
            check(x.get("successCount") == 1 and x.get("errorCount") == 1, f"counts {x.get('successCount')}/{x.get('errorCount')}")
            return f"mixed list fails overall; 1 same-value item written, 1 refused; results keys {sorted((x.get('results') or {}).keys())}"
        await mutating("O35b", mixed_list)

        async def final():
            c.test = "T3-END"
            snap = await snapshot("end")
            for sym in WATCHED:
                if sym not in (HOT, HOT_OK, YELLOW):
                    check(snap[sym] == st["start"][sym], f"{sym} ends at {snap[sym]}, started at {st['start'][sym]}")
            return f"parameters back at their start values; yellow out {snap[YELLOW]}"
        await guarded(run, "T3-END", 3, final())


if __name__ == "__main__":
    execute("tier3", "Tier 3 qualification", 3, main)
