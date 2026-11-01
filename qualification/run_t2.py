"""Tier 2: read-only ADS against the designated target only.

Designated by the user on 2026-10-29. Route, AMS Net ID and PLC port come from site.json.
Another agent uses this PLC at the same time, so this runner only reads, keeps the load
small, and reports a state change as an observation rather than a failure.
No write, state change, activation, route change or login happens here.
"""
import csv
import subprocess
import uuid

from harness import check, execute, guarded, route_netid, server, sha256, site

SITE = site()
TARGET, PORT, ROUTE = SITE["target"], SITE["port"], SITE["route"]
NOROUTE = "192.0.2.1.1.1"  # TEST-NET-1: no route exists, so nothing is contacted
NUMERIC = {"BOOL", "BYTE", "WORD", "DWORD", "LWORD", "SINT", "USINT", "INT", "UINT", "DINT", "UDINT",
           "LINT", "ULINT", "REAL", "LREAL"}
STATE_OF = {"Run": "Run", "Stop": "Stop", "Config": "Config"}


def res(r):
    return r.get("result") or {}


def code_of(r):
    return res(r).get("code") or r.get("code")


def ok(r, what):
    check(r.get("state") == "succeeded" and res(r).get("success") is not False,
          f"{what}: state={r.get('state')} code={code_of(r)} msg={str(res(r).get('errorMessage') or res(r).get('message'))[:200]}")
    return res(r)


def module_present(name):
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          f"[bool](Get-Module -ListAvailable -Name {name})"], capture_output=True, text=True)
    return out.stdout.strip().lower() == "true"


async def main(run):
    st = {}
    async with server(run, "t2") as c:
        async def snapshot(label):
            c.test = f"SNAP-{label}"
            sysr = await c.run_op("runtime.get_state", {"amsNetId": TARGET, "port": 10000})
            plc = await c.run_op("runtime.get_state", {"amsNetId": TARGET, "port": PORT})
            snap = dict(system=res(sysr).get("adsState"), plc=res(plc).get("adsState"),
                        plcDevice=res(plc).get("deviceState"), systemOk=sysr.get("state") == "succeeded",
                        plcOk=plc.get("state") == "succeeded")
            run.write(dict(phase="snapshot", testId=c.test, snapshot=snap))
            return snap

        async def identity():
            c.test = "T2-ID"
            check(route_netid(ROUTE) == TARGET, f"route {ROUTE} does not name {TARGET} exactly once")
            p = ok(await c.run_op("runtime.ping_target", {"amsNetId": TARGET, "port": PORT, "timeoutMs": 2500}), "ping")
            check(p.get("systemServiceReachable") is True, f"system service unreachable: {p.get('systemServiceError')}")
            st["ping"] = p
            st["start"] = await snapshot("start")
            check(st["start"]["systemOk"], "system service state unreadable")
            return (f"route Net ID matches; classification {p.get('classification')}; system {st['start']['system']}, "
                    f"PLC {PORT} {st['start']['plc']} (runtimeReachable {p.get('runtimeReachable')})")
        await guarded(run, "T2-ID", 2, identity())
        if "start" not in st:
            run.record("TIER2", 2, "BLOCKED", "identity or reachability not established; no further test ran")
            return

        async def ping():
            c.test = "O18"
            p = st["ping"]
            for k in ("classification", "systemServiceReachable", "runtimeReachable", "systemServiceDurationMs"):
                check(k in p, f"ping lacks {k}")
            return f"{p['classification']}: system {p['systemServiceDurationMs']} ms, runtime {p.get('runtimeState')} {p.get('runtimeDurationMs')} ms"
        await guarded(run, "O18", 2, ping())

        async def get_state():
            c.test = "O19"
            out = []
            for port in (10000, PORT, PORT + 1):
                r = await c.run_op("runtime.get_state", {"amsNetId": TARGET, "port": port})
                x = res(r)
                if r.get("state") == "succeeded":
                    for k in ("amsNetId", "port", "adsState", "deviceState", "isRunning"):
                        check(k in x, f"get_state lacks {k}")
                    check(x["amsNetId"] == TARGET and x["port"] == port, f"echo {x['amsNetId']}:{x['port']}")
                out.append(f"{port}={x.get('adsState') or 'unreadable'}")
            st["plcRun"] = st["start"]["plc"] == "Run"
            return ", ".join(out)
        await guarded(run, "O19", 2, get_state())

        async def wait_state():
            c.test = "C13"
            cur = st["start"]["plc"]
            check(cur in STATE_OF, f"PLC state {cur} is not Run/Stop/Config")
            w = ok(await c.run_op("workflow.wait_state", {"amsNetId": TARGET, "port": PORT, "expectedState": cur, "timeoutSeconds": 5, "intervalSeconds": 0.5}), "wait current")
            other = "Stop" if cur == "Run" else "Run"
            t = await c.run_op("workflow.wait_state", {"amsNetId": TARGET, "port": PORT, "expectedState": other, "timeoutSeconds": 3, "intervalSeconds": 0.5})
            check(t.get("state") == "failed" and code_of(t) == "state_timeout", f"impossible state: {t.get('state')} {code_of(t)}")
            return f"current {cur} matched in {w.get('attempts')} attempt(s); {other} timed out with state_timeout"
        await guarded(run, "C13", 2, wait_state())

        async def list_symbols():
            c.test = "O16"
            a = ok(await c.run_op("runtime.list_symbols", {"amsNetId": TARGET, "port": PORT, "max": 500, "includeTypes": True}), "list")
            syms = a.get("symbols") or []
            check(syms and all("name" in s for s in syms), f"symbols {len(syms)}")
            check(len(syms) <= 500 and (a.get("truncated") is (a.get("totalMatched", 0) > len(syms))), f"cap/truncated {a.get('totalMatched')} {a.get('truncated')}")
            st["symbols"] = syms
            first = syms[0]["name"]
            prefix = first.split(".")[0] + "."
            b = ok(await c.run_op("runtime.list_symbols", {"amsNetId": TARGET, "port": PORT, "prefix": prefix, "max": 5}), "prefix")
            check(all(s["name"].lower().startswith(prefix.lower()) for s in b.get("symbols") or []), "prefix filter leaked")
            check(len(b.get("symbols") or []) <= 5, "max not applied")
            return f"{a.get('totalMatched')} matched, {len(syms)} returned, truncated {a.get('truncated')}; prefix '{prefix}' filter and max 5 hold"
        await guarded(run, "O16", 2, list_symbols())

        def pick():
            chosen, seen = [], set()
            for s in st.get("symbols", []):
                t = (s.get("typeName") or "").upper()
                if t in NUMERIC and t not in seen and "[" not in s["name"]:
                    chosen.append(s); seen.add(t)
            st["numeric"] = chosen[:6]
            st["string"] = next((s for s in st.get("symbols", []) if (s.get("typeName") or "").upper().startswith("STRING")), None)

        pick()

        async def read_var():
            c.test = "O21"
            check(st["numeric"], "no numeric scalar symbol found in the first 500")
            out = []
            for s in st["numeric"]:
                r = ok(await c.run_op("runtime.read_var", {"amsNetId": TARGET, "port": PORT, "symbol": s["name"]}), f"read {s['name']}")
                check(r.get("symbolName") == s["name"] and r.get("size") == s.get("size"), f"echo {r.get('symbolName')} size {r.get('size')} vs {s.get('size')}")
                out.append(f"{s.get('typeName')}={str(r.get('value'))[:12]}")
            miss = await c.run_op("runtime.read_var", {"amsNetId": TARGET, "port": PORT, "symbol": "NoSuch.Symbol_Qual"})
            check(miss.get("state") == "failed", "missing symbol read succeeded")
            return "; ".join(out) + "; missing symbol fails"
        await guarded(run, "O21", 2, read_var())

        async def read_list():
            c.test = "O34"
            names = [s["name"] for s in st["numeric"]]
            check(names, "no symbols to read")
            r = await c.run_op("runtime.read_var_list", {"amsNetId": TARGET, "port": PORT, "symbols": names})
            if not st.get("plcRun") and r.get("state") == "failed":
                return f"PLC not in Run: list read refused as documented ({str(res(r).get('errorMessage'))[:80]})"
            x = ok(r, "read list")
            check(sorted(x.get("results", {})) == sorted(names) and x.get("errorCount") == 0, f"results {sorted(x.get('results', {}))}")
            mixed = await c.run_op("runtime.read_var_list", {"amsNetId": TARGET, "port": PORT, "symbols": names[:2] + ["NoSuch.Symbol_Qual"]})
            m = res(mixed)
            check(mixed.get("state") == "failed", "mixed list reported success")
            check(m.get("successCount") == len(names[:2]) and m.get("errorCount") == 1, f"mixed counts {m.get('successCount')}/{m.get('errorCount')}")
            check("NoSuch.Symbol_Qual" in (m.get("results") or {}), "failed item missing from results")
            return f"{len(names)} symbols read with original keys; mixed list fails overall and keeps {m.get('successCount')} good + 1 failed item"
        await guarded(run, "O34", 2, read_list())

        async def plc_log():
            c.test = "O17"
            r = ok(await c.run_op("runtime.read_plc_log", {"amsNetId": TARGET, "waitSeconds": 5, "max": 20}), "read_plc_log")
            check("events" in r and "totalCaptured" in r, f"log keys {sorted(r)}")
            check(len(r["events"]) <= 20, "max not applied")
            return f"{len(r['events'])} events in 5 s window, totalCaptured {r.get('totalCaptured')}, truncated {r.get('truncated')}"
        await guarded(run, "O17", 2, plc_log())

        st["mid"] = await snapshot("mid")

        async def record():
            c.test = "O36"
            chans = [s for s in st["numeric"] if s.get("typeName", "").upper() != "BOOL"][:3] or st["numeric"][:3]
            check(chans, "no numeric channels")
            g = ok(await c.run_op("safety.grant", {"operations": ["runtime.record"], "amsNetId": TARGET, "reason": "tier2 read-only recording", "ttlSeconds": 300}), "grant")["grantHandle"]
            out = run.dir / "artifacts" / f"rec-{uuid.uuid4().hex[:6]}.csv"
            out.parent.mkdir(exist_ok=True)
            names = [s["name"] for s in chans]
            r = await c.run_op("runtime.record", {"amsNetId": TARGET, "port": PORT, "variables": names, "sampleTimeMs": 10,
                                                   "durationSec": 3, "maxTimeSec": 10, "outputPath": str(out), "grantHandle": g})
            x = ok(r, "record")
            check(out.exists(), "no CSV written")
            rows = list(csv.reader(out.open(encoding="utf-8-sig")))
            check(rows[0][0] == "Timestamp" and rows[0][1:] == names, f"header {rows[0]}")
            check(len(rows) - 1 == x.get("samplesCollected"), f"rows {len(rows) - 1} vs samplesCollected {x.get('samplesCollected')}")
            check(x.get("channelCount", len(names)) == len(names), f"channelCount {x.get('channelCount')}")
            before = sha256(out)
            again = await c.run_op("runtime.record", {"amsNetId": TARGET, "port": PORT, "variables": names, "sampleTimeMs": 10,
                                                       "durationSec": 1, "maxTimeSec": 5, "outputPath": str(out), "grantHandle": g})
            check(again.get("state") == "failed" and sha256(out) == before, "existing CSV overwritten or accepted")
            extra = ""
            if st.get("string"):
                bad = await c.run_op("runtime.record", {"amsNetId": TARGET, "port": PORT, "variables": [st["string"]["name"]], "sampleTimeMs": 10,
                                                         "durationSec": 1, "maxTimeSec": 5, "outputPath": str(run.dir / "artifacts" / "str.csv"), "grantHandle": g})
                check(bad.get("state") == "failed", "STRING channel accepted")
                extra = "; STRING channel refused"
            many, _ = await c.submit("runtime.record", {"amsNetId": TARGET, "variables": [f"X.v{i}" for i in range(501)], "grantHandle": g})
            check(many.get("code") == "invalid_arguments", f"501 channels: {many}")
            return (f"{x.get('samplesCollected')} rows x {len(names)} channels in {round(x.get('durationSeconds') or 0, 1)} s; "
                    f"header and row count match; overwrite refused{extra}; 501 channels refused at schema")
        await guarded(run, "O36", 2, record())

        async def routes():
            c.test = "O29"
            if module_present("TcXaeMgmt"):
                return "SKIP"
            r = await c.run_op("system.routes", {})
            check(r.get("state") == "failed", "routes succeeded without TcXaeMgmt")
            return f"explicit failure without TcXaeMgmt: {str(res(r).get('errorMessage') or res(r).get('message'))[:120]}"
        try:
            d = await routes()
            if d == "SKIP":
                run.record("O29", 2, "NOT_RUN", "TcXaeMgmt present; positive case waits for the target-contact boundary")
            else:
                run.record("O29", 2, "PASS", d)
        except AssertionError as exc:
            run.record("O29", 2, "FAIL", str(exc))

        async def no_route():
            c.test = "L15a"
            p = await c.run_op("runtime.ping_target", {"amsNetId": NOROUTE, "port": PORT, "timeoutMs": 1000})
            x = res(p)
            check(x.get("systemServiceReachable") is not True and x.get("runtimeReachable") is not True, f"unrouted target reachable: {x}")
            g = await c.run_op("runtime.get_state", {"amsNetId": NOROUTE, "port": PORT})
            check(g.get("state") != "succeeded", "get_state on unrouted target succeeded")
            w = await c.run_op("workflow.wait_state", {"amsNetId": NOROUTE, "port": PORT, "expectedState": "Run", "timeoutSeconds": 3, "intervalSeconds": 0.5})
            check(w.get("state") == "failed", "wait_state on unrouted target succeeded")
            return f"ping {x.get('classification')}; get_state {g.get('state')}; wait_state {code_of(w)}"
        await guarded(run, "L15a", 2, no_route())

        st["end"] = await snapshot("end")
        changes = {k: (st["start"][k], st["end"][k]) for k in ("system", "plc", "plcDevice") if st["start"][k] != st["end"][k]}
        if changes:
            run.record("T2-STATE", 2, "PASS", f"state changed during the run, not by this runner (read-only): {changes}")
        else:
            run.record("T2-STATE", 2, "PASS", f"target state unchanged: system {st['end']['system']}, PLC {st['end']['plc']}")

    run.record("L15b", 2, "NOT_RUN", "controlled outage of the designated target needs approval and would disturb the other agent")
    run.record("O36-load", 2, "NOT_RUN", "500-channel and long recordings skipped while another agent uses the PLC")


if __name__ == "__main__":
    execute("tier2", "Tier 2 qualification", 2, main)
