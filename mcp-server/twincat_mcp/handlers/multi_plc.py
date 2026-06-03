"""
Multi-PLC project handlers:
  - twincat_enable_tmc_auto_reload   one-shot setup: turn on auto-reload
                                     for external (standalone) PLC refs
                                     in an integration .tsproj
  - twincat_multi_plc_build          orchestrator: build N sub-PLC
                                     solutions, then build the integration
                                     solution that references them

The auto-reload tool is a pure Python XML edit on the .tsproj (no shell
host needed). The orchestrator chains existing `twincat_build` steps
via `run_shell_step`.

Both target the workflow described in TwinCAT InfoSys as "stand-alone
PLC project": each sub-PLC has its own .sln that produces a .tmc, and
the integration .tsproj references those .tmc files by relative path. With
ReloadTmc="true" set on each external <Project>, every rebuild of the
integration solution will re-read the latest TMC instead of stalling on a
TmcHash mismatch.
"""

import datetime
import re
from pathlib import Path

from lxml import etree
from mcp.types import TextContent

from ..dispatch import run_shell_step
from ..formatting import add_timing_to_output
from ._registry import register


# Match either TwinCAT project GUID in a .sln file:
#   B1E792BE — classic XAE project (.tsproj, full hardware+PLC)
#   DFBE7525 — XAE Shell / library project (.tspproj, PLC-only)
_SLN_TSPROJ_RE = re.compile(
    r'Project\("\{(?:B1E792BE-AA5F-4E3C-8C82-674BF9C0715B'
    r'|DFBE7525-6864-4E62-8B2E-D530D69D9D96)\}"\)\s*=\s*'
    r'"[^"]*",\s*"(?P<tsproj>[^"]+\.tspp?roj)"',
    re.IGNORECASE,
)


def _find_tsproj(solution_path: str) -> Path | None:
    """Locate the .tsproj/.tspproj referenced by a .sln. Mirrors the
    C# TcFileUtilities.FindTwinCATProjectFile logic so behavior stays
    consistent with the build/info tools."""
    sln = Path(solution_path)
    if not sln.is_file():
        return None
    try:
        for line in sln.read_text(encoding="utf-8", errors="replace").splitlines():
            m = _SLN_TSPROJ_RE.search(line)
            if m:
                tsproj_rel = m.group("tsproj")
                return (sln.parent / tsproj_rel).resolve()
    except Exception:
        return None
    return None


def _classify_plc_project(project_el) -> str:
    """Return 'embedded', 'external', or 'unknown' for a <Project>
    element under <Plc>. Embedded PLCs carry PrjFilePath; externals
    only have a TmcPath on their child <Instance>."""
    prj_file = project_el.get("PrjFilePath")
    if prj_file:
        return "embedded"
    instance = project_el.find("Instance")
    if instance is not None and instance.get("TmcPath"):
        return "external"
    return "unknown"


def _insert_attr_on_plc_project_lines(
    raw: bytes,
    target_names: list[str],
    new_attr: str,
    new_value: str,
) -> tuple[bytes, list[str], list[str]]:
    """Surgical byte-level edit: insert `new_attr="new_value"` into the
    opening tag of each `<Project Name="..." ...>` under `<Plc>` whose
    Name matches a target. Does NOT touch `<Project>` elements outside
    the `<Plc>` container (e.g. those under `<Safety>`), and skips
    lines that already carry the attribute.

    Returns (new_bytes, names_modified, names_already_set).

    Why bytes instead of an XML round-trip: lxml's serializer rewrites
    the XML declaration, normalizes CRLF→LF, and collapses empty tags
    (<Type></Type> → <Type/>). On a 5 MB tsproj that means tens of
    thousands of irrelevant diff lines that obscure the actual change
    and may confuse TwinCAT on next open. Operating on raw bytes
    guarantees the only changes are the attribute insertions.
    """
    # Locate the <Plc> ... </Plc> span byte-wise. We're only looking
    # for the OUTER PLC container that holds the PLC projects, not any
    # nested PlcConfig sub-tag. Two anchors:
    #   <Plc>   (start of PLC config block, no attributes — matches the
    #            outer container element in TcSmProject)
    #   </Plc>  (its closing tag)
    # Both appear on their own lines in well-formed tsproj output.
    plc_start_match = re.search(rb"<Plc>\s*$", raw, re.MULTILINE)
    plc_end_match = re.search(rb"^\s*</Plc>", raw, re.MULTILINE)
    if not plc_start_match or not plc_end_match:
        # Fall back to searching the whole file. Less safe (a Safety
        # PLC named the same as a regular PLC would collide), but
        # better than erroring out — the caller's discovery step
        # already confirmed these Names live under <Plc>.
        scan_start = 0
        scan_end = len(raw)
    else:
        scan_start = plc_start_match.end()
        scan_end = plc_end_match.start()

    modified: list[str] = []
    already_set: list[str] = []

    new_attr_b = new_attr.encode("utf-8")
    insert_text = f' {new_attr}="{new_value}"'.encode("utf-8")

    # Build the edited byte string with one pass per target. We rescan
    # the region for each name to keep behavior obvious; the file isn't
    # large enough for this to matter.
    out = raw
    offset_delta = 0  # how many bytes we've inserted so far

    for name in target_names:
        # Match the opening tag of a <Project ...> whose Name attr equals
        # this PLC name. Tag can span across the same line only — TwinCAT
        # always emits the whole open tag on one line.
        # Use re.escape to defend against names with regex metachars.
        name_pattern = re.escape(name).encode("utf-8")
        # Two attribute orderings to consider:
        #   <Project Name="X" ...>   (external)
        #   <Project GUID="..." Name="X" ...>  (embedded)
        # Both have Name= somewhere in the attribute list; match the
        # full opening tag and check Name inside.
        tag_pattern = re.compile(
            rb'<Project\b([^>]*?\bName="' + name_pattern + rb'"[^>]*)>',
            re.DOTALL,
        )

        # Search only inside the [scan_start, scan_end] window — but
        # those offsets shift as we insert. Track via offset_delta.
        window_start = scan_start + offset_delta
        window_end = scan_end + offset_delta
        m = tag_pattern.search(out, window_start, window_end)
        if m is None:
            # Name not found inside <Plc>. Fall through silently — the
            # discovery step caller already vetted this name.
            continue

        attrs = m.group(1)
        if re.search(rb'\b' + new_attr_b + rb'\s*=', attrs):
            already_set.append(name)
            continue

        # Insert `<attr>="<value>"` right before the closing '>' of the
        # opening tag.
        insert_at = m.end() - 1  # position of '>'
        out = out[:insert_at] + insert_text + out[insert_at:]
        offset_delta += len(insert_text)
        modified.append(name)

    return out, modified, already_set


@register("twincat_enable_tmc_auto_reload")
async def handle_enable_tmc_auto_reload(
    arguments: dict, tool_start_time: float
) -> list[TextContent]:
    """
    Add ReloadTmc="true" to each external PLC <Project> in the integration
    .tsproj, so subsequent builds pick up TMC changes from standalone
    sub-PLC builds without manual right-click "Reload TMC".

    Args (from MCP):
      solutionPath  (required) — full path to the integration .sln
      plcNames      (optional) — list[str]; if given, only these PLC
                                 names are touched. Default: every
                                 external PLC.
      includeEmbedded (optional, default false) — also re-affirm
                                  ReloadTmc on embedded PLCs that
                                  somehow don't have it set
      dryRun        (optional, default false) — report what would
                                  change but don't write the file
    """
    solution_path = (arguments.get("solutionPath") or "").strip()
    plc_names_filter = arguments.get("plcNames") or []
    include_embedded = bool(arguments.get("includeEmbedded", False))
    dry_run = bool(arguments.get("dryRun", False))

    if not solution_path:
        return [TextContent(type="text", text=add_timing_to_output(
            "❌ solutionPath is required", tool_start_time))]

    tsproj = _find_tsproj(solution_path)
    if tsproj is None or not tsproj.is_file():
        return [TextContent(type="text", text=add_timing_to_output(
            f"❌ Could not find a .tsproj/.tspproj referenced by:\n  {solution_path}",
            tool_start_time))]

    try:
        raw = tsproj.read_bytes()
    except Exception as e:
        return [TextContent(type="text", text=add_timing_to_output(
            f"❌ Failed to read {tsproj}: {e}", tool_start_time))]

    try:
        parser = etree.XMLParser(remove_blank_text=False, strip_cdata=False)
        tree = etree.fromstring(raw, parser)
    except Exception as e:
        return [TextContent(type="text", text=add_timing_to_output(
            f"❌ Failed to parse {tsproj}: {e}", tool_start_time))]

    # PLC projects live under <TcSmProject>/<Project>/<Plc>/<Project>.
    # <Safety>/<Project> entries are intentionally excluded — safety
    # PLCs have their own reload behavior and a different file format.
    plc_container = tree.find(".//Plc")
    if plc_container is None:
        return [TextContent(type="text", text=add_timing_to_output(
            f"❌ No <Plc> container found in {tsproj} — is this an integration "
            "project, or a standalone PLC project?",
            tool_start_time))]

    name_filter_lower = {n.lower() for n in plc_names_filter}

    discovered: list[dict] = []
    skipped_already_set_pre: list[str] = []
    skipped_not_external: list[dict] = []
    skipped_not_in_filter: list[str] = []

    for project_el in plc_container.findall("Project"):
        name = project_el.get("Name") or "(unnamed)"
        kind = _classify_plc_project(project_el)

        if name_filter_lower and name.lower() not in name_filter_lower:
            skipped_not_in_filter.append(name)
            continue

        eligible = (kind == "external") or (include_embedded and kind == "embedded")
        if not eligible:
            skipped_not_external.append({"name": name, "kind": kind})
            continue

        existing = project_el.get("ReloadTmc")
        if existing and existing.lower() == "true":
            skipped_already_set_pre.append(name)
            continue

        discovered.append({"name": name, "kind": kind,
                           "previousValue": existing or "(unset)"})

    backup_path: Path | None = None
    modified: list[dict] = list(discovered)
    skipped_already_set: list[str] = list(skipped_already_set_pre)

    if discovered and not dry_run:
        # Surgical byte-level edit: only touches the opening tag lines
        # of the discovered PLC <Project> elements. Preserves CRLF, XML
        # declaration, empty-element form, and every other byte.
        target_names = [d["name"] for d in discovered]
        new_bytes, byte_modified, byte_already = _insert_attr_on_plc_project_lines(
            raw, target_names, "ReloadTmc", "true",
        )
        # Reconcile: anything the byte editor reported as already-set
        # but discovery thought was unset should move buckets. Anything
        # discovery flagged but the byte editor couldn't locate stays
        # out of `modified` (rare edge case: name collision between
        # <Plc> and another container).
        byte_already_set = set(byte_already)
        byte_modified_set = set(byte_modified)
        modified = [d for d in discovered if d["name"] in byte_modified_set]
        for d in discovered:
            if d["name"] in byte_already_set and d["name"] not in byte_modified_set:
                skipped_already_set.append(d["name"])

        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = tsproj.with_name(f"{tsproj.name}.bak.{ts}")
        try:
            backup_path.write_bytes(raw)
        except Exception as e:
            return [TextContent(type="text", text=add_timing_to_output(
                f"❌ Could not write backup {backup_path}: {e}",
                tool_start_time))]

        try:
            tsproj.write_bytes(new_bytes)
        except Exception as e:
            return [TextContent(type="text", text=add_timing_to_output(
                f"❌ Failed to write {tsproj}: {e}\n"
                f"Original is preserved at {backup_path}",
                tool_start_time))]

    # ---- format the response ------------------------------------------------
    header = "🔍 DRY RUN: " if dry_run else "✅ "
    if not modified:
        if skipped_already_set:
            header = "ℹ️ "
            verb = (
                f"Nothing to do — {len(skipped_already_set)} PLC(s) already "
                f'have ReloadTmc="true"'
            )
        else:
            verb = "No external PLC projects matched"
    elif dry_run:
        verb = f"Would set ReloadTmc=\"true\" on {len(modified)} PLC project(s)"
    else:
        verb = f"Set ReloadTmc=\"true\" on {len(modified)} PLC project(s)"

    lines = [f"{header}{verb}\n"]
    lines.append(f"Integration .tsproj: {tsproj}")
    if backup_path:
        lines.append(f"Backup:        {backup_path}")

    if modified:
        lines.append("\nModified:")
        for m in modified:
            lines.append(f"  • {m['name']} ({m['kind']}, was: {m['previousValue']})")

    if skipped_already_set:
        lines.append(f"\nAlready set, skipped ({len(skipped_already_set)}):")
        for n in skipped_already_set:
            lines.append(f"  • {n}")

    if skipped_not_external:
        lines.append(f"\nNot external (skipped, {len(skipped_not_external)}):")
        for s in skipped_not_external:
            lines.append(f"  • {s['name']} ({s['kind']})")

    if skipped_not_in_filter:
        lines.append(f"\nNot in plcNames filter, skipped ({len(skipped_not_in_filter)}):")
        for n in skipped_not_in_filter:
            lines.append(f"  • {n}")

    if modified and not dry_run:
        lines.append(
            "\n💡 Next integration build will re-read each PLC's .tmc file. "
            "Verify with `twincat_build` against the integration .sln."
        )

    return [TextContent(type="text", text=add_timing_to_output(
        "\n".join(lines), tool_start_time))]


@register("twincat_multi_plc_build")
async def handle_multi_plc_build(
    arguments: dict, tool_start_time: float
) -> list[TextContent]:
    """
    Build N standalone PLC solutions and then the integration solution that
    references them. Designed for multi-PLC projects where each
    sub-PLC has its own .sln that produces a .tmc, and an integration
    .tsproj pulls those .tmc files in.

    Args (from MCP):
      subSolutions    (required) — list[str] of paths to each
                                   sub-PLC .sln, built in given order
      infraSolution   (required) — path to the integration .sln (built last)
      clean           (optional, default true) — clean each solution
                                   before building
      tcVersion       (optional) — pinned TwinCAT version for all builds
      continueOnError (optional, default false) — if false, stop the
                                   moment a sub-PLC build fails and
                                   skip the integration build
      timeoutMinutes  (optional) — per-build timeout, default 15

    Tip: run `twincat_enable_tmc_auto_reload` once against the integration
    solution before relying on this orchestrator. Without ReloadTmc,
    the integration build may fail with a TmcHash mismatch on whichever
    sub-PLC's TMC just changed.
    """
    sub_solutions: list[str] = list(arguments.get("subSolutions") or [])
    integration_solution = (arguments.get("infraSolution") or "").strip()
    clean = bool(arguments.get("clean", True))
    tc_version = arguments.get("tcVersion")
    continue_on_error = bool(arguments.get("continueOnError", False))
    timeout_minutes = int(arguments.get("timeoutMinutes") or 15)

    if not sub_solutions:
        return [TextContent(type="text", text=add_timing_to_output(
            "❌ subSolutions is required (list of standalone PLC .sln paths)",
            tool_start_time))]
    if not integration_solution:
        return [TextContent(type="text", text=add_timing_to_output(
            "❌ infraSolution is required (path to the integration .sln)",
            tool_start_time))]

    sub_results: list[dict] = []
    integration_result: dict | None = None
    integration_skipped = False
    abort_reason: str | None = None

    for idx, sub in enumerate(sub_solutions, start=1):
        result, _ = run_shell_step(
            "build",
            {"clean": clean},
            solution_path=sub,
            tc_version=tc_version,
            timeout_minutes=timeout_minutes,
        )
        sub_results.append({"solution": sub, "result": result})

        if not result.get("success") and not continue_on_error:
            abort_reason = f"sub-PLC #{idx} ({Path(sub).name}) build failed"
            integration_skipped = True
            break

    if not integration_skipped:
        integration_result, _ = run_shell_step(
            "build",
            {"clean": clean},
            solution_path=integration_solution,
            tc_version=tc_version,
            timeout_minutes=timeout_minutes,
        )

    # ---- format ------------------------------------------------------------
    lines: list[str] = []
    all_subs_ok = all(r["result"].get("success") for r in sub_results)
    overall_ok = all_subs_ok and (
        integration_result is not None and integration_result.get("success")
    )

    header = "✅ All builds succeeded" if overall_ok else (
        "⚠️ Some builds failed" if not integration_skipped else "🛑 Aborted before integration build"
    )
    lines.append(f"{header}\n")

    lines.append(f"Sub-PLC builds ({len(sub_results)}):")
    for entry in sub_results:
        r = entry["result"]
        sub = entry["solution"]
        if r.get("success"):
            summary = r.get("summary", "Build succeeded")
            lines.append(f"  ✅ {Path(sub).name} — {summary}")
        else:
            err = r.get("errorMessage") or r.get("summary") or "build failed"
            lines.append(f"  ❌ {Path(sub).name} — {err}")

    lines.append("")
    if integration_skipped:
        lines.append(f"⏭️ Integration build skipped — {abort_reason}")
        lines.append(f"   ({Path(integration_solution).name})")
    elif integration_result is None:
        lines.append("⚠️ Integration build did not run (unknown reason)")
    elif integration_result.get("success"):
        lines.append(
            f"✅ Integration build — {Path(integration_solution).name}: "
            f"{integration_result.get('summary', 'Build succeeded')}"
        )
    else:
        err = integration_result.get("errorMessage") or integration_result.get("summary") or "build failed"
        lines.append(f"❌ Integration build — {Path(integration_solution).name}: {err}")
        if "TmcHash" in (err or "") or "TMC" in (err or "").upper():
            lines.append(
                "\n💡 TMC mismatch suggests ReloadTmc is not set on the "
                "external <Project> nodes in the integration .tsproj. Run "
                "`twincat_enable_tmc_auto_reload` against the integration .sln "
                "and retry."
            )

    return [TextContent(type="text", text=add_timing_to_output(
        "\n".join(lines), tool_start_time))]
