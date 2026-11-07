"""Fork-only add-on: TMC auto-reload on top of the v3 server.

v3 has no operation for the fork tool twincat_enable_tmc_auto_reload. The fork tool
twincat_multi_plc_build was dropped by a user decision on 2026-11-07.

- reload: sets ReloadTmc="true" on PLC projects of an integration solution, so its build re-reads the
  TMC files that standalone PLC builds produce. It edits the project files on disk, so XAE must not
  hold the solution open. Without --apply it only reports.

Usage:
  python addons/multi_plc.py reload <integration.sln> [--plc NAME ...] [--include-embedded] [--apply]
"""
import argparse
import datetime
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

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


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("reload")
    r.add_argument("solution")
    r.add_argument("--plc", action="append")
    r.add_argument("--include-embedded", action="store_true")
    r.add_argument("--apply", action="store_true")
    a = p.parse_args(argv)
    out = enable_tmc_auto_reload(a.solution, a.plc, a.include_embedded, a.apply)
    print(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
