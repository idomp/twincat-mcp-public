"""Tier 1: owned XAE and offline engineering on disposable fixture copies. No runtime ADS work.

Each server gets its own state folder and its own fixture copy. Contexts target the local
system, by the AMS Net ID in the registry; no operation here activates, logs in, downloads or
writes variables. A kill needs exactly one session record for the fixture, written by a running
child of this runner, and the PID, start time and executable in that record.
Known gap: L13 passes when the MCP SDK ends the server's job object after stdin closes, so it
does not prove the server's own client-loss path.
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from harness import Blocked, QUAL, ancestors, check, execute, guarded, kill_pinned, process_start_utc, same_process, server, sha256, site

FIX = site()["fixtures"]  # fixture folder names under QUAL_DIR\fixtures
SHELL_IMAGES = ("TcXaeShell.exe", "devenv.exe")
WORKER_IMAGES = ("TcAutomation.exe",)


def local_netid():
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          r"((Get-ItemProperty 'HKLM:\SOFTWARE\WOW6432Node\Beckhoff\TwinCAT3\System').AmsNetId | ForEach-Object { [int]$_ }) -join '.'"],
                         capture_output=True, text=True).stdout.strip()
    if not re.fullmatch(r"(\d{1,3}\.){5}\d{1,3}", out):
        raise RuntimeError(f"local AMS Net ID unreadable: {out!r}")
    return out


LOCAL = local_netid()  # the local system; object commands require the project's real target id
V78, V55 = "3.1.4024.78", "3.1.4024.55"
CONFIRM = "CONFIRM"
ENG_OPS = [
    "engineering.create_folder", "engineering.create_object", "engineering.update_object",
    "engineering.rename_object", "engineering.delete_object", "engineering.import_objects",
    "engineering.export_objects", "engineering.move_object", "engineering.generate_library",
    "engineering.set_target", "engineering.configure_task", "engineering.configure_rt",
    "engineering.disable_io", "engineering.set_variant", "workflow.deploy",
]


def fixture(run, name, master=FIX["embeddedLib"]):
    """Embedded variant: PLC1's <Project> node inlined into the .tsproj (no independent .xti)."""
    dst = run.dir / "fx" / name
    shutil.copytree(QUAL / "fixtures" / master, dst)
    return next(dst.glob("*.sln"))


def res(r):
    return r.get("result") or {}


def code_of(r):
    return res(r).get("code") or r.get("code")


def ok(r, what):
    check(r.get("state") == "succeeded" and res(r).get("success") is not False,
          f"{what}: state={r.get('state')} code={code_of(r)} msg={str(res(r).get('errorMessage') or res(r).get('message'))[:200]}")
    return res(r)


def records(c):
    out = []
    for rec in c.session_records():
        get = lambda k: rec.get(k, rec.get(k[0].lower() + k[1:]))
        out.append(dict(dte=get("DtePid"), dteStart=get("DteStartTimeUtc"), host=get("HostPid"),
                        hostStart=get("HostStartTimeUtc"), mcp=get("McpPid"), mcpStart=get("McpStartTimeUtc"),
                        tc=get("TcVersion"), sln=get("SolutionPath")))
    return out


def alive(pid):
    return bool(pid) and process_start_utc(pid) is not None


def kill_record(c, sln, role):
    """The one record for sln whose role process runs, written by a running child of this runner."""
    recs = [r for r in records(c) if r["sln"] and Path(r["sln"]).resolve() == Path(sln).resolve() and alive(r[role])]
    check(len(recs) == 1, f"expected one session record for {Path(sln).name} with a live {role}, found {len(recs)}")
    rec = recs[0]
    check(same_process(rec["mcp"], rec["mcpStart"]), f"MCP process {rec['mcp']} does not run with start {rec['mcpStart']}")
    check(os.getpid() in ancestors(rec["mcp"]), f"MCP process {rec['mcp']} is not a child of this runner")
    return rec


async def wait_dead(pid, seconds=90):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not alive(pid):
            return True
        await asyncio.sleep(1)
    return False


def foreign_processes():
    """Automation-owned TwinCAT processes at start, as [pid, start, path]. Explorer-started XAE
    windows belong to the desktop user, who can close them at any time, so they are not part of
    the isolation check. The start is None for a process that exited or cannot be read, and
    such a row leaves no baseline."""
    script = ("Get-CimInstance Win32_Process -Filter \"Name='TcXaeShell.exe' OR Name='TcAutomation.exe'\" | ForEach-Object { "
              "$par = Get-CimInstance Win32_Process -Filter \"ProcessId=$($_.ParentProcessId)\"; "
              "if ($par.Name -ne 'explorer.exe') { \"$($_.ProcessId)|$($_.ExecutablePath)\" } }")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True, timeout=120)
    if out.returncode:
        return None
    rows = [line.split("|", 1) for line in out.stdout.splitlines() if "|" in line]
    return [[pid, process_start_utc(pid), path] for pid, path in rows]


async def open_ctx(c, sln, version=V78):
    args = {"solutionPath": str(sln), "amsNetId": LOCAL, "plcName": "PLC1"}
    if version:
        args["tcVersion"] = version
    return ok(await c.run_op("context.open", args), "context.open")["contextHandle"]


async def matrix(run):
    sln = fixture(run, "A")
    st = {}
    async with server(run, "t1a") as c:
        st["ctx"] = await open_ctx(c, sln)
        cg = lambda **kw: dict(contextHandle=st["ctx"], grantHandle=st["G"], **kw)

        async def ensure():
            s_ = await c.ctl("context.status", {"contextHandle": st["ctx"]})
            if s_.get("valid") is True:
                return
            await c.run_op("context.close", {"contextHandle": st["ctx"]})
            st["ctx"] = await open_ctx(c, sln)
            st["reopened"] = st.get("reopened", 0) + 1
            run.write(dict(phase="observation", note="context expired; reopened", count=st["reopened"]))
            if "G" in st:
                g_ = ok(await c.run_op("safety.grant", {"operations": ENG_OPS, "contextHandle": st["ctx"], "reason": "tier1 regrant", "ttlSeconds": 3600}), "regrant")
                st["G"] = g_["grantHandle"]

        async def info():
            c.test = "O06"
            t = time.monotonic()
            r = ok(await c.run_op("engineering.info", {"contextHandle": st["ctx"]}), "info")
            st["launch_s"] = round(time.monotonic() - t, 1)
            recs = records(c)
            check(len(recs) == 1 and recs[0]["dte"], f"session records {recs}")
            st["rec"] = recs[0]
            check(recs[0]["tc"] == V78, f"effective version {recs[0]['tc']}")
            return f"first call {st['launch_s']} s incl. XAE launch; saved tcVersion {r.get('tcVersion')}, effective {recs[0]['tc']}; keys {sorted(r)[:8]}"
        await ensure()
        await guarded(run, "O06-L02-L09a", 1, info())

        async def lists():
            c.test = "O12"
            p = ok(await c.run_op("engineering.list_plcs", {"contextHandle": st["ctx"]}), "list_plcs")
            c.test = "O23"
            t = ok(await c.run_op("engineering.list_tasks", {"contextHandle": st["ctx"]}), "list_tasks")
            st["tasks"] = t
            names = [x.get("name") for x in p.get("plcProjects", [])]
            check("PLC1" in names, f"plc names {names}")
            return f"plcs {names}; tasks {[x.get('name') for x in t.get('tasks', [])]}"
        await ensure()
        await guarded(run, "O12-O23", 1, lists())

        async def build():
            c.test = "O05"
            t = time.monotonic()
            b = ok(await c.run_op("engineering.build", {"contextHandle": st["ctx"], "clean": False, "timeoutSeconds": 300}), "build")
            check(b.get("compilerSuccess") is True and b.get("failedProjectCount") == 0, f"compiler {b.get('compilerSuccess')} failed {b.get('failedProjectCount')}")
            return f"compilerSuccess, 0 failed projects, {round(time.monotonic() - t, 1)} s"
        await ensure()
        await guarded(run, "O05", 1, build())

        async def checks():
            c.test = "O26"
            ok(await c.run_op("engineering.check_all_objects", {"contextHandle": st["ctx"]}), "check_all_objects")
            c.test = "O32"
            e = ok(await c.run_op("engineering.get_error_list", {"contextHandle": st["ctx"], "waitSeconds": 0}), "get_error_list")
            return f"check_all_objects ok; error list keys {sorted(e)[:8]}"
        await ensure()
        await guarded(run, "O26-O32", 1, checks())

        async def symbols():
            c.test = "O52"
            s = ok(await c.run_op("engineering.symbols", {"contextHandle": st["ctx"], "kind": "symbols", "query": "", "offset": 0, "limit": 5}), "symbols")
            items = s.get("names") or s.get("items") or []
            check(items, f"no symbols, keys {sorted(s)}")
            first = items[0].get("name") if isinstance(items[0], dict) else items[0]
            ok(await c.run_op("engineering.symbols", {"contextHandle": st["ctx"], "kind": "types", "query": "", "offset": 0, "limit": 5}), "types")
            c.test = "O51"
            ok(await c.run_op("workflow.build_verify", {"contextHandle": st["ctx"], "clean": False, "expectedSymbols": [first]}), "build_verify")
            miss = await c.run_op("workflow.build_verify", {"contextHandle": st["ctx"], "clean": False, "expectedSymbols": ["NoSuch.Symbol_Qual"]})
            check(miss.get("state") == "failed", f"missing symbol passed: {miss.get('state')}")
            check(res(miss).get("compilerSuccess") is True, f"compiler result lost on verification failure: {res(miss).get('compilerSuccess')}")
            empty = await c.run_op("workflow.build_verify", {"contextHandle": st["ctx"], "clean": False, "expectedSymbols": []})
            check(empty.get("code") == "invalid_arguments" or empty.get("state") == "failed", f"empty expectations accepted {empty}")
            return f"symbol '{first}' verified; missing symbol fails with compiler success kept; empty list rejected"
        await ensure()
        await guarded(run, "O52-O51", 1, symbols())

        async def objects():
            c.test = "O42"
            seen, off, pages = [], 0, 0
            while off is not None:
                page = ok(await c.run_op("engineering.list_objects", {"contextHandle": st["ctx"], "offset": off, "limit": 2}), "list_objects")
                seen += page.get("objects") or page.get("children") or page.get("items") or []
                off = page.get("nextOffset"); pages += 1
                check(pages < 50, "list_objects paging did not end")
            paths = [o["path"] for o in seen]
            check(len(paths) == len(set(paths)) and paths, f"paths {paths}")
            st["root_children"] = paths
            st["root"] = paths[0].rsplit("^", 1)[0]
            pous = next(p for p in paths if p.endswith("^POUs"))
            kids = ok(await c.run_op("engineering.list_objects", {"contextHandle": st["ctx"], "path": pous, "limit": 50}), "list POUs")
            main = next(o["path"] for o in (kids.get("objects") or kids.get("children") or kids.get("items")) if o["path"].endswith("^MAIN"))
            st["main"] = main
            c.test = "O43"
            obj = ok(await c.run_op("engineering.read_object", {"contextHandle": st["ctx"], "path": main}), "read_object")
            check(len((obj.get("object") or {}).get("sha256", "")) == 64, f"object hash {obj.get('object', {}).get('sha256')}")
            c.test = "O02"
            src = await c.run_op("engineering.read_plc_source", {"contextHandle": st["ctx"], "path": main, "section": "declaration"})
            ok(src, "read_plc_source")
            c.test = "C08b"
            text, o2, n = "", 0, 0
            while o2 is not None:
                pg = await c.ctl("job.result", {"jobHandle": src["jobHandle"], "offset": o2, "limit": 256})
                text += pg["text"]; o2 = pg["nextOffset"]; n += 1
            check(json.loads(text) == res(src), "paged reconstruction differs")
            return f"{pages} pages at limit 2, {len(paths)} root children; read_object hash; source read; result rebuilt from {n} pages of 256"
        await ensure()
        await guarded(run, "O42-O43-O02-C08b", 1, objects())

        try:
            g = ok(await c.run_op("safety.grant", {"operations": ENG_OPS, "contextHandle": st["ctx"], "reason": "tier1 qualification on a disposable copy", "ttlSeconds": 3600}), "grant")
        except AssertionError as exc:
            run.record("MATRIX", 1, "FAIL", f"stopped before mutating tests: {exc}")
            return
        st["G"] = g["grantHandle"]

        async def authoring():
            root = st["root"]
            c.test = "O44"
            f = ok(await c.run_op("engineering.create_folder", cg(parentPath=root, name="QualScratch")), "create_folder")
            scratch = f.get("path") or f"{root}^QualScratch"
            dup = await c.run_op("engineering.create_folder", cg(parentPath=root, name="QualScratch"))
            check(dup.get("state") == "failed", "duplicate folder accepted")
            c.test = "O45"
            made = {}
            specs = [
                ("PRG_Qual", "program", "PROGRAM PRG_Qual\nVAR\n\tn : INT;\nEND_VAR\n", "n := n + 1;\n"),
                ("FB_Qual", "functionBlock", "FUNCTION_BLOCK FB_Qual\nVAR_INPUT\n\tx : REAL;\nEND_VAR\n", ""),
                ("GVL_Qual", "gvl", "{attribute 'qualified_only'}\nVAR_GLOBAL\n\tqBool : BOOL;\n\tqLreal : LREAL;\nEND_VAR\n", None),
                ("ST_Qual", "struct", "TYPE ST_Qual :\nSTRUCT\n\ta : INT;\n\tb : LREAL;\nEND_STRUCT\nEND_TYPE\n", None),
                ("E_Qual", "enum", "TYPE E_Qual :\n(\n\tIdle := 0,\n\tRun := 10\n);\nEND_TYPE\n", None),
            ]
            for name, kind, decl, impl in specs:
                a = cg(parentPath=scratch, name=name, objectType=kind, declaration=decl)
                if impl is not None:
                    a["implementation"] = impl
                o = ok(await c.run_op("engineering.create_object", a), f"create {kind}")
                made[name] = o.get("path") or f"{scratch}^{name}"
            meth = ok(await c.run_op("engineering.create_object", cg(parentPath=made["FB_Qual"], name="M_Run", objectType="method", returnType="BOOL",
                                                                   declaration="METHOD M_Run : BOOL\n", implementation="M_Run := TRUE;\n")), "create method")
            st["made"] = made
            c.test = "O46"
            h = ok(await c.run_op("engineering.read_object", {"contextHandle": st["ctx"], "path": made["PRG_Qual"]}), "read")["object"]["sha256"]
            ok(await c.run_op("engineering.update_object", cg(path=made["PRG_Qual"], expectedSha256=h, implementation="n := n + 2;\n")), "update")
            stale = await c.run_op("engineering.update_object", cg(path=made["PRG_Qual"], expectedSha256=h, implementation="n := 3;\n"))
            check(stale.get("state") == "failed", "stale hash accepted")
            c.test = "O47"
            h2 = ok(await c.run_op("engineering.read_object", {"contextHandle": st["ctx"], "path": made["PRG_Qual"]}), "read")["object"]["sha256"]
            ok(await c.run_op("engineering.rename_object", cg(path=made["PRG_Qual"], expectedSha256=h2, name="PRG_Qual2")), "rename")
            gone = await c.run_op("engineering.read_object", {"contextHandle": st["ctx"], "path": made["PRG_Qual"]})
            check(gone.get("state") == "failed", "old path still readable after rename")
            made["PRG_Qual2"] = made.pop("PRG_Qual").rsplit("^", 1)[0] + "^PRG_Qual2"
            st["scratch"] = scratch
            return f"folder, 5 object kinds + method, update with stale-hash rejection, rename; method path {meth.get('path')}"
        await ensure()
        await guarded(run, "O44-O45-O46-O47", 1, authoring())

        async def invalid():
            c.test = "O26b"
            bad = ok(await c.run_op("engineering.create_object", cg(parentPath=st["scratch"], name="PRG_Bad", objectType="program",
                                                                  declaration="PROGRAM PRG_Bad\nVAR\nEND_VAR\n", implementation="this is not structured text ;;;\n")), "create bad")
            r = await c.run_op("engineering.check_all_objects", {"contextHandle": st["ctx"]})
            check(r.get("state") == "failed" or res(r).get("success") is False, f"syntax error passed check_all_objects: {r.get('state')}")
            path = bad.get("path") or f"{st['scratch']}^PRG_Bad"
            h = ok(await c.run_op("engineering.read_object", {"contextHandle": st["ctx"], "path": path}), "read bad")["object"]["sha256"]
            ok(await c.run_op("engineering.delete_object", cg(path=path, expectedSha256=h, confirm=CONFIRM)), "delete bad")
            ok(await c.run_op("engineering.check_all_objects", {"contextHandle": st["ctx"]}), "check after delete")
            return "unused syntax error fails check_all_objects; passes again after delete"
        await ensure()
        await guarded(run, "O26b-O48a", 1, invalid())

        async def export_import():
            c.test = "O50"
            out = run.dir / "artifacts" / f"FB_Qual-{uuid.uuid4().hex[:6]}.TcPOU"
            out.parent.mkdir(exist_ok=True)
            e = ok(await c.run_op("engineering.export_objects", cg(format="native", filePath=str(out), paths=[st["made"]["FB_Qual"]])), "export")
            check(out.exists() and e.get("sha256") == sha256(out), f"export hash {e.get('sha256')} vs file")
            before = sha256(out)
            again = await c.run_op("engineering.export_objects", cg(format="native", filePath=str(out), paths=[st["made"]["FB_Qual"]]))
            check(again.get("state") == "failed" and sha256(out) == before, "existing export overwritten or accepted")
            c.test = "O49"
            imp = ok(await c.run_op("engineering.create_folder", cg(parentPath=st["root"], name="QualImport")), "import folder")
            st["import"] = imp.get("path") or f"{st['root']}^QualImport"
            # A native import keeps the object's GUID, so the original must go first (round trip).
            fb = st["made"]["FB_Qual"]
            h = ok(await c.run_op("engineering.read_object", {"contextHandle": st["ctx"], "path": fb}), "read FB")["object"]["sha256"]
            ok(await c.run_op("engineering.delete_object", cg(path=fb, expectedSha256=h, confirm=CONFIRM)), "delete FB before import")
            i = ok(await c.run_op("engineering.import_objects", cg(format="native", filePath=str(out), expectedFileSha256=before, parentPath=st["scratch"])), "import")
            check(i.get("importedPaths"), f"importedPaths {i}")
            ok(await c.run_op("engineering.read_object", {"contextHandle": st["ctx"], "path": fb}), "read re-imported FB")
            col = await c.run_op("engineering.import_objects", cg(format="native", filePath=str(out), expectedFileSha256=before, parentPath=st["import"]))
            check(col.get("state") == "failed", "duplicate-identity import accepted")
            wrong = await c.run_op("engineering.import_objects", cg(format="native", filePath=str(out), expectedFileSha256="0" * 64, parentPath=st["import"]))
            check(wrong.get("state") == "failed", "hash-mismatched import accepted")
            return (f"export hash matches file; overwrite refused; delete + native import round trip {i.get('importedPaths')}; "
                    f"duplicate identity and hash mismatch refused")
        await ensure()
        await guarded(run, "O50-O49", 1, export_import())

        async def move():
            c.test = "O53"
            gvl = st["made"]["GVL_Qual"]
            h = ok(await c.run_op("engineering.read_object", {"contextHandle": st["ctx"], "path": gvl}), "read gvl")["object"]["sha256"]
            m = ok(await c.run_op("engineering.move_object", cg(path=gvl, expectedSha256=h, newParentPath=st["import"], timeoutSeconds=300, confirm=CONFIRM)), "move")
            check(m.get("identityPreserved") is True and m.get("solutionReloaded") is True, f"move result {sorted(m)}")
            moved = f"{st['import']}^GVL_Qual"
            h2 = ok(await c.run_op("engineering.read_object", {"contextHandle": st["ctx"], "path": moved}), "read moved")["object"]["sha256"]
            ok(await c.run_op("engineering.move_object", cg(path=moved, expectedSha256=h2, newParentPath=st["scratch"], timeoutSeconds=300, confirm=CONFIRM)), "move back")
            return "moved with identityPreserved and solutionReloaded; moved back"
        await ensure()
        await guarded(run, "O53", 1, move())

        async def delete():
            c.test = "O48"
            refused = 0
            for folder in (st["import"], st["scratch"]):
                h = ok(await c.run_op("engineering.read_object", {"contextHandle": st["ctx"], "path": folder}), "read folder")["object"]["sha256"]
                kids = ok(await c.run_op("engineering.list_objects", {"contextHandle": st["ctx"], "path": folder, "limit": 50}), "list folder")
                if kids.get("objects") or kids.get("children") or kids.get("items"):
                    norec = await c.run_op("engineering.delete_object", cg(path=folder, expectedSha256=h, confirm=CONFIRM))
                    check(norec.get("state") == "failed", "non-empty folder deleted without recursive confirmation")
                    refused += 1
                    ok(await c.run_op("engineering.delete_object", cg(path=folder, expectedSha256=h, confirm=CONFIRM, recursive=True, recursiveConfirm="DELETE_CONTENTS")), "recursive delete")
                else:
                    ok(await c.run_op("engineering.delete_object", cg(path=folder, expectedSha256=h, confirm=CONFIRM)), "delete empty folder")
            check(refused >= 1, "no non-empty folder was tested")
            seen, off = [], 0
            while off is not None:
                page = ok(await c.run_op("engineering.list_objects", {"contextHandle": st["ctx"], "offset": off, "limit": 50}), "list")
                seen += [o["path"] for o in page.get("objects") or page.get("children") or page.get("items") or []]
                off = page.get("nextOffset")
            check(sorted(seen) == sorted(st["root_children"]), f"root differs after cleanup: {set(seen) ^ set(st['root_children'])}")
            ok(await c.run_op("engineering.build", {"contextHandle": st["ctx"], "clean": False, "timeoutSeconds": 300}), "rebuild after cleanup")
            return "recursive confirmation enforced; root restored; rebuild passes"
        await ensure()
        await guarded(run, "O48", 1, delete())

        async def library():
            c.test = "O28"
            lib = run.dir / "artifacts" / f"Qual-{uuid.uuid4().hex[:6]}.library"
            d = ok(await c.run_op("engineering.generate_library", cg(libraryLocation=str(lib), dryRun=True)), "library preview")
            check(d.get("buildPerformed") is False and d.get("exportAttempted") is False and not lib.exists(), f"preview effects {d}")
            r = await c.run_op("engineering.generate_library", cg(libraryLocation=str(lib), dryRun=False))
            if r.get("state") != "succeeded":
                raise Blocked(f"preview clean; export refused: {code_of(r)} {str(res(r).get('errorMessage') or res(r).get('message'))[:160]}")
            x = res(r)
            check(lib.exists() and x.get("installed") is False, f"export installed={x.get('installed')} exists={lib.exists()}")
            before = sha256(lib)
            again = await c.run_op("engineering.generate_library", cg(libraryLocation=str(lib), dryRun=False))
            check(again.get("state") == "failed" and sha256(lib) == before, "existing library overwritten")
            return f"preview has no effects; export installed=false, hash {before[:12]}; overwrite refused"
        await ensure()
        await guarded(run, "O28", 1, library())

        async def task_now(name):
            return next(x for x in ok(await c.run_op("engineering.list_tasks", {"contextHandle": st["ctx"]}), "list_tasks")["tasks"] if x["name"] == name)

        async def target():
            c.test = "O08"
            t = ok(await c.run_op("engineering.set_target", cg()), "set_target")
            check(t.get("newTarget") == LOCAL, f"newTarget {t.get('newTarget')}")
            return f"target {t.get('previousTarget')} -> {t.get('newTarget')}"
        await ensure()
        await guarded(run, "O08", 1, target())

        async def task_single():
            c.test = "O24"
            task = (st["tasks"].get("tasks") or [])[0]
            name = task["name"]
            ok(await c.run_op("engineering.configure_task", cg(taskName=name, enable=False)), "disable only")
            check((await task_now(name)).get("disabled") is True, "enable=false alone did not disable")
            ok(await c.run_op("engineering.configure_task", cg(taskName=name, enable=True)), "enable only")
            ok(await c.run_op("engineering.configure_task", cg(taskName=name, autostart=not task.get("autoStart"))), "autostart only")
            check((await task_now(name)).get("autoStart") is (not task.get("autoStart")), "autostart alone did not change")
            ok(await c.run_op("engineering.configure_task", cg(taskName=name, autostart=bool(task.get("autoStart")))), "restore autostart")
            back = await task_now(name)
            check(back.get("disabled") == task.get("disabled") and back.get("autoStart") == task.get("autoStart"), f"restore {back} vs {task}")
            return "enable-only and autostart-only changes applied and restored"
        await ensure()
        await guarded(run, "O24", 1, task_single())

        async def task_combined():
            c.test = "X03"
            task = (st["tasks"].get("tasks") or [])[0]
            name = task["name"]
            r = ok(await c.run_op("engineering.configure_task", cg(taskName=name, enable=False, autostart=not task.get("autoStart"))), "disable + autostart")
            now = await task_now(name)
            ok(await c.run_op("engineering.configure_task", cg(taskName=name, enable=not task.get("disabled"), autostart=bool(task.get("autoStart")))), "restore")
            check(now.get("disabled") is True, f"receipt said newDisabled={r.get('newDisabled')}, list_tasks says disabled={now.get('disabled')}")
            return "disable and autostart in one call both applied"
        await ensure()
        await guarded(run, "X03", 1, task_combined())

        async def rt_io_variant():
            c.test = "O25"
            rt = ok(await c.run_op("engineering.configure_rt", cg(maxCpus=1, loadLimit=50)), "configure_rt")
            c.test = "O14"
            io = ok(await c.run_op("engineering.disable_io", cg(enable=False)), "disable_io")
            c.test = "O15"
            var = await c.run_op("engineering.set_variant", cg())
            return f"rt {json.dumps({k: v for k, v in rt.items() if k != 'progress'})[:140]}; io modified {io.get('modifiedCount')}; variant query {var.get('state')}"
        await ensure()
        await guarded(run, "O25-O14-O15", 1, rt_io_variant())

        async def deploy_preview():
            c.test = "O11"
            d = ok(await c.run_op("workflow.deploy", cg(dryRun=True, skipBuild=False, confirm=CONFIRM)), "deploy preview")
            check(d.get("dryRun") is True, f"dryRun flag {d.get('dryRun')}")
            return f"preview steps {[s.get('action') for s in d.get('steps', [])]}"
        await ensure()
        await guarded(run, "O11-preview", 1, deploy_preview())

        async def scope_cfg():
            c.test = "O37"
            g = ok(await c.run_op("safety.grant", {"operations": ["scope.create_config"], "amsNetId": LOCAL, "reason": "tier1 artifact-only", "ttlSeconds": 120}), "grant")
            out = run.dir / "artifacts" / "qual.tcscopex"
            r = await c.run_op("scope.create_config", {"amsNetId": LOCAL, "variables": ["MAIN.x"], "outputPath": str(out), "grantHandle": g["grantHandle"]})
            check(r.get("state") == "failed" and not out.exists(), f"scope config without Scope build: {r.get('state')}")
            return f"explicit failure without Scope support: {str(res(r).get('errorMessage') or res(r).get('message'))[:120]}"
        try:
            detail = await scope_cfg()
            run.record("O37", 1, "EXPECTED_UNAVAILABLE", detail)
        except AssertionError as exc:
            run.record("O37", 1, "FAIL", str(exc))

        async def sequence():
            c.test = "C12"
            s = ok(await c.run_op("workflow.sequence", {"steps": [
                {"operation": "engineering.info", "arguments": {"contextHandle": st["ctx"]}},
                {"operation": "engineering.list_tasks", "arguments": {"contextHandle": st["ctx"]}}]}), "sequence")
            check(s.get("completedSteps") == 2, f"completed {s.get('completedSteps')}")
            f = await c.run_op("workflow.sequence", {"steps": [
                {"operation": "engineering.read_object", "arguments": {"contextHandle": st["ctx"], "path": st["root"] + "^NoSuchObject"}},
                {"operation": "engineering.info", "arguments": {"contextHandle": st["ctx"]}}]})
            check(f.get("state") == "failed" and res(f).get("completedSteps") == 0, f"failing sequence {f.get('state')} {res(f).get('completedSteps')}")
            nested = await c.run_op("workflow.sequence", {"steps": [{"operation": "workflow.sequence", "arguments": {"steps": []}}]})
            check(nested.get("state") == "failed" or nested.get("code"), "nested sequence accepted")
            return "2-step sequence ok; first failure stops later steps; nesting rejected"
        await ensure()
        await guarded(run, "C12", 1, sequence())

        async def cancel_and_queue():
            c.test = "L06"
            # A single build takes about 3 s here, so twelve clean builds hold the worker.
            blocker = [{"operation": "engineering.build", "arguments": {"contextHandle": st["ctx"], "clean": True, "timeoutSeconds": 300}}] * 12
            b, _ = await c.submit("workflow.sequence", {"steps": blocker})
            q, _ = await c.submit("engineering.info", {"contextHandle": st["ctx"]})
            w = await c.ctl("job.wait", {"jobHandle": b["jobHandle"], "waitSeconds": 1})
            check(w.get("waitTimedOut") is True, f"wait on running build {w.get('state')} {w.get('waitTimedOut')}")
            cn = await c.ctl("job.cancel", {"jobHandle": q["jobHandle"]})
            check(cn.get("cancelRequested") is True, f"cancel {cn}")
            c.test = "L18"
            handles, full = [], None
            for i in range(80):
                r, _ = await c.submit("engineering.info", {"contextHandle": st["ctx"]})
                if r.get("code") == "queue_full":
                    full = i
                    break
                handles.append(r["jobHandle"])
            for h in handles:
                await c.ctl("job.cancel", {"jobHandle": h})
            s = await c.ctl("system.status")
            check(s.get("success"), "status not responsive while queue full")
            c.test = "L06"
            end = time.monotonic() + 600
            while True:
                w = await c.ctl("job.wait", {"jobHandle": b["jobHandle"], "waitSeconds": 60})
                if w["state"] in {"succeeded", "failed", "cancelled", "outcome_unknown"} or time.monotonic() > end:
                    break
            qr = await c.ctl("job.get", {"jobHandle": q["jobHandle"]})
            check(qr["state"] == "cancelled", f"queued job state {qr['state']}")
            check(full is not None, "queue never reported queue_full")
            return f"wait timed out on running build; queued job cancelled; queue_full after {full} extra submissions; build ended {w['state']}"
        await ensure()
        await guarded(run, "L06-L18", 1, cancel_and_queue())

        async def reap():
            c.test = "C11"
            st["rec"] = next(r_ for r_ in records(c) if alive(r_["dte"]))
            r = ok(await c.run_op("system.reap_orphans", {}), "reap_orphans")
            check(alive(st["rec"]["dte"]), "reaper killed this server's own live shell")
            return f"own live shell untouched; result {json.dumps(r)[:160]}"
        await ensure()
        await guarded(run, "C11", 1, reap())

        async def close():
            c.test = "C03"
            st["rec"] = next(r_ for r_ in records(c) if alive(r_["dte"]))
            ok(await c.run_op("context.close", {"contextHandle": st["ctx"]}), "close")
            check(await wait_dead(st["rec"]["dte"], 90), f"owned shell {st['rec']['dte']} still alive 90 s after close")
            after = await c.run_op("engineering.info", {"contextHandle": st["ctx"]})
            check(after.get("state") == "failed", "closed context still usable")
            return "closed; owned shell exited; handle rejected"
        await ensure()
        await guarded(run, "C03", 1, close())


async def xti_layout(run):
    """X01: a PLC stored as an independent project file (.xti), the TwinCAT samples' layout."""
    sln = fixture(run, "X", master=FIX["xti"])
    async with server(run, "t1x") as c:
        async def x01():
            c.test = "X01"
            ctx = await open_ctx(c, sln)
            ok(await c.run_op("engineering.info", {"contextHandle": ctx}), "info")
            b = await c.run_op("engineering.build", {"contextHandle": ctx, "clean": False, "timeoutSeconds": 300})
            s = await c.run_op("engineering.symbols", {"contextHandle": ctx, "kind": "symbols", "limit": 5})
            after = await c.ctl("context.status", {"contextHandle": ctx})
            await c.run_op("context.close", {"contextHandle": ctx})
            detail = (f"build {b.get('state')}: {str(res(b).get('errorMessage'))[:90]}; symbols {s.get('state')}; "
                      f"context after symbols: {after.get('valid', after.get('code'))}")
            check(b.get("state") == "succeeded" and s.get("state") == "succeeded", detail)
            return detail
        await guarded(run, "X01", 1, x01())

        async def x01b():
            c.test = "X01b"
            broken = fixture(run, "Xb", master=FIX["xti"])
            next(broken.parent.glob("*/_Config/PLC/PLC1.xti")).unlink()
            ctx = await open_ctx(c, broken)
            s = await c.run_op("engineering.symbols", {"contextHandle": ctx, "kind": "symbols", "limit": 5})
            after = await c.ctl("context.status", {"contextHandle": ctx})
            await c.run_op("context.close", {"contextHandle": ctx})
            detail = f"symbols {s.get('state')}: {str(res(s).get('errorMessage'))[:120]}; context valid after: {after.get('valid')}"
            check(s.get("state") == "failed" and after.get("valid") is True, detail)
            return detail
        await guarded(run, "X01b", 1, x01b())


async def library_no_title(run):
    """X02: library export from a PLC project that has no Title (not a library project)."""
    sln = fixture(run, "L", master=FIX["embedded"])
    async with server(run, "t1l") as c:
        async def x02():
            c.test = "X02"
            ctx = await open_ctx(c, sln)
            g = ok(await c.run_op("safety.grant", {"operations": ["engineering.generate_library"], "contextHandle": ctx, "reason": "tier1", "ttlSeconds": 600}), "grant")
            lib = run.dir / "artifacts" / f"NoTitle-{uuid.uuid4().hex[:6]}.library"
            r = await c.run_op("engineering.generate_library", {"contextHandle": ctx, "grantHandle": g["grantHandle"], "libraryLocation": str(lib)})
            after = await c.ctl("context.status", {"contextHandle": ctx})
            await c.run_op("context.close", {"contextHandle": ctx})
            detail = (f"state {r.get('state')}; '{str(res(r).get('errorMessage'))[:80]}'; file written {lib.exists()}; "
                      f"context valid after: {after.get('valid')}")
            check(r.get("state") == "failed" and not lib.exists() and after.get("valid") is True, detail)
            return detail
        await guarded(run, "X02", 1, x02())


async def versions(run):
    sln = fixture(run, "V")
    async with server(run, "t1v") as c:
        async def absent():
            c.test = "L09b"
            ctx = await open_ctx(c, sln, version=None)
            r = await c.run_op("engineering.info", {"contextHandle": ctx})
            check(r.get("state") in {"failed", "outcome_unknown"}, f"saved 3.1.4024.12 opened: {r.get('state')}")
            msg = json.dumps(res(r))
            check("not installed" in msg or "4024.12" in msg, f"message {msg[:200]}")
            await c.run_op("context.close", {"contextHandle": ctx})
            return f"absent saved version refused without fallback: {msg[:140]}"
        await guarded(run, "L09b", 1, absent())

        async def override():
            c.test = "L09c"
            ctx = await open_ctx(c, sln, version=V55)
            ok(await c.run_op("engineering.info", {"contextHandle": ctx}), "info 4024.55")
            rec = [r for r in records(c) if r["dte"] and alive(r["dte"])]
            check(rec and rec[0]["tc"] == V55, f"effective {rec}")
            ok(await c.run_op("context.close", {"contextHandle": ctx}), "close")
            check(await wait_dead(rec[0]["dte"]), "4024.55 shell still alive after close")
            return "override 3.1.4024.55 effective and shell closed"
        await guarded(run, "L09c", 1, override())


async def kills(run):
    sln = fixture(run, "K")
    async with server(run, "t1k") as c:
        async def kill_shell():
            c.test = "L08b"
            ctx = await open_ctx(c, sln)
            ok(await c.run_op("engineering.info", {"contextHandle": ctx}), "info")
            rec = kill_record(c, sln, "dte")
            run.write(dict(phase="kill", testId=c.test, pid=rec["dte"], recordedStart=rec["dteStart"], role="own XAE shell"))
            done, actual = kill_pinned(rec["dte"], rec["dteStart"], SHELL_IMAGES)
            check(done, f"pinned kill refused: record {rec['dteStart']} actual {actual}")
            await asyncio.sleep(2)
            r = await c.run_op("engineering.info", {"contextHandle": ctx})
            x = res(r)
            check(x.get("code") == "worker_lost" and x.get("outcomeUnknown") is True and x.get("dispatched") is False,
                  f"after shell kill: state {r.get('state')} {json.dumps(x)[:200]}")
            again = await c.run_op("engineering.info", {"contextHandle": ctx})
            check(again.get("state") == "failed", "context still valid after worker_lost")
            await c.run_op("context.close", {"contextHandle": ctx})
            return "worker_lost, outcomeUnknown, dispatched false; context invalid; no replacement shell"
        await guarded(run, "L08b", 1, kill_shell())

        async def kill_worker():
            c.test = "L08c"
            ctx = await open_ctx(c, sln)
            ok(await c.run_op("engineering.info", {"contextHandle": ctx}), "info")
            rec = kill_record(c, sln, "host")
            steps = [{"operation": "engineering.build", "arguments": {"contextHandle": ctx, "clean": True, "timeoutSeconds": 300}}] * 12
            b, key = await c.submit("workflow.sequence", {"steps": steps})
            await asyncio.sleep(2)
            run.write(dict(phase="kill", testId=c.test, pid=rec["host"], recordedStart=rec["hostStart"], role="own worker"))
            done, actual = kill_pinned(rec["host"], rec["hostStart"], WORKER_IMAGES)
            check(done, f"pinned worker kill refused: {rec['hostStart']} vs {actual}")
            end = time.monotonic() + 120
            while True:
                w = await c.ctl("job.wait", {"jobHandle": b["jobHandle"], "waitSeconds": 30})
                if w["state"] not in {"queued", "running"} or time.monotonic() > end:
                    break
            check(w["state"] in {"outcome_unknown", "failed"}, f"job after worker kill: {w['state']}")
            replay, _ = await c.submit("workflow.sequence", {"steps": steps}, key)
            check(replay["jobHandle"] == b["jobHandle"], "same key started a second job")
            st = await c.run_op("engineering.info", {"contextHandle": ctx})
            check(st.get("state") == "failed", "context usable after worker loss")
            await asyncio.sleep(20)
            orphan_alive = alive(rec["dte"])
            reap = await c.run_op("system.reap_orphans", {})
            check(await wait_dead(rec["dte"], 30), f"orphaned shell {rec['dte']} survived reap_orphans: {json.dumps(res(reap))[:160]}")
            await c.run_op("context.close", {"contextHandle": ctx})
            return (f"job {w['state']}; key replay same job; context invalid; shell alive 20 s after worker kill: {orphan_alive}; "
                    f"reap_orphans removed it ({json.dumps(res(reap))[:100]})")
        await guarded(run, "L08c", 1, kill_worker())


async def two_clients(run):
    async def l10():
        sa, sb = fixture(run, "C1"), fixture(run, "C2")
        async with server(run, "t1c1") as a, server(run, "t1c2") as b:
            a.test = b.test = "L10"
            ca, cb = await asyncio.gather(open_ctx(a, sa), open_ctx(b, sb))
            ra, rb = await asyncio.gather(a.run_op("engineering.info", {"contextHandle": ca}), b.run_op("engineering.info", {"contextHandle": cb}))
            ok(ra, "info A"); ok(rb, "info B")
            da, db = records(a)[0], records(b)[0]
            check(da["dte"] != db["dte"], f"both clients share shell {da['dte']}")
            ok(await a.run_op("context.close", {"contextHandle": ca}), "close A")
            check(await wait_dead(da["dte"]), "shell A survived close")
            check(alive(db["dte"]), "closing A killed B's shell")
            ok(await b.run_op("engineering.list_tasks", {"contextHandle": cb}), "B after A closed")
            ok(await b.run_op("context.close", {"contextHandle": cb}), "close B")
            check(await wait_dead(db["dte"]), "shell B survived close")
            return f"distinct shells {da['dte']}/{db['dte']}; closing A left B working"
    await guarded(run, "L10", 1, l10())


async def bad_solution(run):
    sln = fixture(run, "B")
    tsproj = next(sln.parent.glob("*/*.tsproj"))
    tsproj.write_text("<not a valid project", encoding="utf-8")
    async with server(run, "t1b") as c:
        async def l17():
            c.test = "L17"
            ctx = await open_ctx(c, sln)
            r = await c.run_op("engineering.info", {"contextHandle": ctx})
            check(r.get("state") != "succeeded" or not res(r).get("plcProjects"), f"corrupt project reported usable: {json.dumps(res(r))[:200]}")
            r2 = await c.run_op("engineering.list_plcs", {"contextHandle": ctx})
            check(r2.get("state") != "succeeded" or not res(r2).get("plcProjects"), "second call reported a usable project")
            await c.run_op("context.close", {"contextHandle": ctx})
            return f"info {r.get('state')} ({code_of(r)}); list_plcs {r2.get('state')}"
        await guarded(run, "L17", 1, l17())


async def client_exit(run):
    sln = fixture(run, "E")
    state_dir = None
    rec = None
    key, handle = None, None
    async def phase1():
        nonlocal state_dir, rec, key, handle
        async with server(run, "t1e") as c:
            c.test = "L13"
            state_dir = c.state_dir
            ctx = await open_ctx(c, sln)
            ok(await c.run_op("engineering.info", {"contextHandle": ctx}), "info")
            rec = records(c)[0]
            b, key = await c.submit("engineering.build", {"contextHandle": ctx, "clean": True, "timeoutSeconds": 300})
            handle = b["jobHandle"]
            await asyncio.sleep(3)
        # leaving the block closes stdin and ends the server process
    async def l13():
        await phase1()
        check(await wait_dead(rec["host"], 120), f"worker {rec['host']} alive 120 s after client exit")
        check(await wait_dead(rec["dte"], 60), f"shell {rec['dte']} alive after client exit")
        async with server(run, "t1e2", state_dir=state_dir) as c:
            c.test = "L13"
            j = await c.ctl("job.get", {"jobHandle": handle})
            check(j.get("state") not in {"queued", "running"}, f"orphaned job still {j.get('state')}")
            r, _ = await c.submit("engineering.build", {"contextHandle": "x" * 43, "clean": True, "timeoutSeconds": 300}, key)
            check(r.get("code") == "request_key_conflict" or r.get("jobHandle") == handle, f"key reuse {r}")
            return f"worker and shell exited after client exit; build receipt {j.get('state')}"
    await guarded(run, "L13", 1, l13())


async def main(run):
    foreign = foreign_processes()
    if foreign is None or not all(f[1] for f in foreign):
        run.record("ISOLATION", 1, "BLOCKED", f"process enumeration incomplete ({foreign}); no baseline, so no tier-1 test ran")
        return
    run.write(dict(phase="snapshot", foreignProcesses=foreign))
    try:
        for part in (matrix, xti_layout, library_no_title, versions, kills, two_clients, bad_solution, client_exit):
            t = time.monotonic()
            await part(run)
            print(f"-- {part.__name__} done in {round(time.monotonic() - t)} s", flush=True)
    finally:
        # The same process: same PID, same start time to the microsecond.
        gone = [f for f in foreign if not same_process(f[0], f[1])]
        if gone:
            run.record("ISOLATION", 1, "FAIL", f"foreign processes gone or replaced: {gone}")
        else:
            run.record("ISOLATION", 1, "PASS", f"{len(foreign)} foreign processes from the other session unchanged")
    for name, reason in [("L11", "foreign COM activation needs an approved helper"),
                         ("L12", "modal dialog on the desktop needs approval"),
                         ("O27", "TE1200 licence state unknown; licensing UI possible"),
                         ("L16", "build-deadline fault injection not built yet")]:
        run.record(name, 1, "NOT_RUN", reason)


if __name__ == "__main__":
    execute("tier1", "Tier 1 qualification", 1, main)
