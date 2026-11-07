# Fork add-ons

This tool is fork-only. It does not go to upstream. v3 has no operation for the fork tool that it replaces. The fork tool `twincat_multi_plc_build` was dropped by a user decision on 2026-11-07.

| Command | Replaces | What it touches |
|---|---|---|
| `multi_plc.py reload <integration.sln>` | `twincat_enable_tmc_auto_reload` | The project files of the solution, and only with `--apply` |

## reload

The command sets `ReloadTmc="true"` on PLC projects, so an integration build re-reads the TMC files that standalone PLC builds produce.

- Close the solution in XAE before you use `--apply`. The command edits the files on disk.
- By default, the command changes external PLC projects only. Add `--include-embedded` to change embedded ones, and `--plc NAME` to select projects.
- The command finds a PLC project inline in the `.tsproj`, or in its `.xti` file under `_Config\PLC`.
- It changes only the opening tag of each selected project. Line endings, the XML declaration and empty-element forms stay as they were.
- It writes a `.bak.<time>` copy of each file before it writes the file.

Without `--apply`, the command reports what it would change and writes nothing.

## Tests

```bash
cd addons && ../.venv/Scripts/python.exe -m unittest discover -s tests
```

Measured on 2026-11-07: `reload` reported the PLC of a project copy as embedded and wrote nothing.
