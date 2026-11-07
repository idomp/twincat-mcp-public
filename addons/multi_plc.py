"""Fork-only add-on: TMC auto-reload and multi-PLC builds on top of the v3 server.

v3 has no operation for either fork tool (twincat_enable_tmc_auto_reload, twincat_multi_plc_build).

- reload: sets ReloadTmc="true" on PLC projects of an integration solution, so its build re-reads the
  TMC files that standalone PLC builds produce. It edits the project files on disk, so XAE must not
  hold the solution open. Without --apply it only reports.
- build: builds standalone PLC solutions in order, then the integration solution. Each solution gets
  its own v3 context, opened and closed in turn, because a server holds one context at a time.

Usage:
  python addons/multi_plc.py reload <integration.sln> [--plc NAME ...] [--include-embedded] [--apply]
  python addons/multi_plc.py build <plan.json>
plan.json: {"amsNetId": "...", "clean": true, "continueOnError": false, "tcVersion": "...",
            "subSolutions": [{"solution": "...", "plc": "..."}], "integration": {"solution": "...", "plc": "..."}}
"""
import argparse
import asyncio
import datetime
import json
import os
import re
import sys
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TERMINAL = {"succeeded", "failed", "cancelled", "outcome_unknown"}
_SLN_PROJECT = re.compile(r'^Project\("[^"]*"\)\s*=\s*"[^"]*",\s*"([^"]+\.tspp?roj)"', re.MULTILINE | re.IGNORECASE)


def integration_project(sln):
    sln = Path(sln)
    refs = _SLN_PROJECT.findall(sln.read_text(encoding="utf-8-sig"))
    if len(refs) != 1:
        raise ValueError(f"{sln.name} names {len(refs)} TwinCAT projects, expected 1")
    return (sln.parent / refs[0]).resolve()


def classify(project):
    if project.get("PrjFilePath"):
        return "embedded"
    instance = project.find("Instance")
    return "external" if instance is not None and instance.get("TmcPath") else "unknown"


def _plc_window(raw):
    """Byte range of the PLC container of a .tsproj, so a Safety project of the same name is never touched."""
    start, end = re.search(rb"<Plc>", raw), re.search(rb"</Plc>", raw)
    if not start or not end or end.start() < start.end():
        raise ValueError("no <Plc> container")
    return start.end(), end.start()


def set_reload_tmc(raw, name, window=None):
    """raw with ReloadTmc="true" on the one <Project Name="name"> tag. Every other byte stays."""
    lo, hi = window or (0, len(raw))
    tag = re.compile(rb'<Project\b[^>]*?\bName="' + re.escape(name.encode("utf-8")) + rb'"[^>]*?(/?)>', re.DOTALL)
    found = [m for m in tag.finditer(raw, lo, hi)]
    if len(found) != 1:
        raise ValueError(f"PLC project {name!r}: {len(found)} opening tags, expected 1")
    m = found[0]
    head = raw[m.start():m.end()]
    attr = re.search(rb'\bReloadTmc="([^"]*)"', head)
    if attr:
        new = head[:attr.start(1)] + b"true" + head[attr.end(1):]
    else:
        cut = len(head) - len(m.group(1)) - 1  # before "/>" or ">"
        new = head[:cut] + b' ReloadTmc="true"' + head[cut:]
    return raw[:m.start()] + new + raw[m.end():]


def enable_tmc_auto_reload(sln, plc_names=None, include_embedded=False, apply=False):
    tsproj = integration_project(sln)
    root = ET.parse(tsproj).getroot()
    wanted = {n.lower() for n in plc_names or []}
    edits, seen = {}, set()
    report = dict(project=str(tsproj), applied=bool(apply), modified=[], alreadySet=[], skipped=[], backups=[])
    for node in root.findall("./Project/Plc/Project"):
        source, project = tsproj, node
        if node.get("File"):  # an independent project file, as in the TwinCAT samples
            source = tsproj.parent / "_Config" / "PLC" / node.get("File")
            project = ET.parse(source).getroot().find("./Project")
        name, kind = project.get("Name") or "", classify(project)
        seen.add(name.lower())
        if wanted and name.lower() not in wanted:
            report["skipped"].append(dict(name=name, reason="not requested"))
        elif not (kind == "external" or (include_embedded and kind == "embedded")):
            report["skipped"].append(dict(name=name, reason=f"{kind} project"))
        elif (project.get("ReloadTmc") or "").lower() == "true":
            report["alreadySet"].append(name)
        else:
            edits.setdefault(source, []).append(name)
            report["modified"].append(dict(name=name, kind=kind, file=str(source), previousValue=project.get("ReloadTmc")))
    missing = wanted - seen
    if missing:
        raise ValueError(f"no PLC project named {sorted(missing)}")
    new_bytes = {}
    for source, names in edits.items():  # every edit is computed before any file is written
        raw = source.read_bytes()
        out = raw
        for name in names:
            out = set_reload_tmc(out, name, _plc_window(out) if source == tsproj else None)
        new_bytes[source] = (raw, out)
    if apply:
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        for source, (raw, out) in new_bytes.items():
            backup = source.with_name(f"{source.name}.bak.{stamp}")
            backup.write_bytes(raw)
            report["backups"].append(str(backup))
            source.write_bytes(out)
    return report


def build_ok(receipt):
    result = receipt.get("result") or {}
    return receipt.get("state") == "succeeded" and result.get("success") is not False and result.get("compilerSuccess") is True


async def multi_plc_build(run_op, plan):
    """run_op(operation, arguments) -> receipt. Builds each sub-solution, then the integration solution."""
    report = dict(subSolutions=[], integration=None, integrationSkipped=None, success=False)

    async def build(entry):
        args = dict(solutionPath=entry["solution"], amsNetId=plan["amsNetId"], plcName=entry["plc"])
        if plan.get("tcVersion"):
            args["tcVersion"] = plan["tcVersion"]
        opened = await run_op("context.open", args)
        if opened.get("state") != "succeeded":
            return dict(solution=entry["solution"], ok=False, step="context.open", receipt=opened)
        ctx = opened["result"]["contextHandle"]
        try:
            built = await run_op("engineering.build", dict(contextHandle=ctx, clean=bool(plan.get("clean", True)),
                                                           timeoutSeconds=int(plan.get("timeoutSeconds", 600))))
            return dict(solution=entry["solution"], ok=build_ok(built), step="engineering.build",
                        unresolved=bool(built.get("unresolved")), receipt=built)
        finally:
            await run_op("context.close", dict(contextHandle=ctx))

    for entry in plan["subSolutions"]:
        r = await build(entry)
        report["subSolutions"].append(r)
        if not r["ok"] and (r.get("unresolved") or not plan.get("continueOnError")):
            report["integrationSkipped"] = f"{Path(entry['solution']).name}: {r['step']} did not succeed"
            return report
    report["integration"] = await build(plan["integration"])
    report["success"] = all(r["ok"] for r in report["subSolutions"]) and report["integration"]["ok"]
    return report


async def _serve(plan):
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    # The SDK passes only a fixed set of variables by default; TWINCAT_MCP_STATE_DIR and others must reach the server.
    params = StdioServerParameters(command=sys.executable, args=[str(REPO / "mcp-server" / "server.py")],
                                   env=dict(os.environ), cwd=str(REPO / "mcp-server"))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            async def call(op, args, key=None, wait=0):
                body = dict(operation=op, arguments=args)
                if key:
                    body.update(requestKey=key, waitSeconds=wait)
                res = await session.call_tool("twincat_execute", body)
                return res.structured_content or json.loads(res.content[0].text)

            async def run_op(op, args):
                receipt = await call(op, args, str(uuid.uuid4()), 30)
                while receipt.get("jobHandle") and receipt.get("state") not in TERMINAL:
                    nxt = await call("job.wait", dict(jobHandle=receipt["jobHandle"], waitSeconds=30))
                    if "state" not in nxt:
                        return dict(receipt, unresolved=True, waitError=nxt)
                    receipt = nxt
                if receipt.get("resultAvailable") and "result" not in receipt:
                    text, offset = "", 0
                    while offset is not None:
                        page = await call("job.result", dict(jobHandle=receipt["jobHandle"], offset=offset, limit=8000))
                        text, offset = text + page["text"], page.get("nextOffset")
                    receipt["result"] = json.loads(text)
                return receipt

            return await multi_plc_build(run_op, plan)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("reload")
    r.add_argument("solution")
    r.add_argument("--plc", action="append")
    r.add_argument("--include-embedded", action="store_true")
    r.add_argument("--apply", action="store_true")
    b = sub.add_parser("build")
    b.add_argument("plan")
    a = p.parse_args(argv)
    if a.cmd == "reload":
        out = enable_tmc_auto_reload(a.solution, a.plc, a.include_embedded, a.apply)
        ok = True
    else:
        out = asyncio.run(_serve(json.loads(Path(a.plan).read_text(encoding="utf-8"))))
        ok = out["success"]
    print(json.dumps(out, indent=1, default=str))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
