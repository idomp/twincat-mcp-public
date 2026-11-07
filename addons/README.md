# Fork add-ons

These tools are fork-only. They do not go to upstream. v3 has no operation for the two fork tools that they replace.

| Command | Replaces | What it touches |
|---|---|---|
| `multi_plc.py reload <integration.sln>` | `twincat_enable_tmc_auto_reload` | The project files of the solution, and only with `--apply` |
| `multi_plc.py build <plan.json>` | `twincat_multi_plc_build` | One v3 context per solution, opened and closed in turn. Builds only. |

## reload

The command sets `ReloadTmc="true"` on PLC projects, so an integration build re-reads the TMC files that standalone PLC builds produce.

- Close the solution in XAE before you use `--apply`. The command edits the files on disk.
- By default, the command changes external PLC projects only. Add `--include-embedded` to change embedded ones, and `--plc NAME` to select projects.
- The command finds a PLC project inline in the `.tsproj`, or in its `.xti` file under `_Config\PLC`.
- It changes only the opening tag of each selected project. Line endings, the XML declaration and empty-element forms stay as they were.
- It writes a `.bak.<time>` copy of each file before it writes the file.

Without `--apply`, the command reports what it would change and writes nothing.

## build

The plan file names each solution and its PLC:

```json
{
  "amsNetId": "172.16.0.1.1.1",
  "clean": true,
  "continueOnError": false,
  "tcVersion": "3.1.4024.78",
  "subSolutions": [{"solution": "C:\\work\\A\\A.sln", "plc": "A"}],
  "integration": {"solution": "C:\\work\\I\\I.sln", "plc": "I"}
}
```

The command builds each sub-solution in order, then the integration solution. It stops at the first failed build. With `continueOnError`, it continues after a failed build, but it still stops at a build whose result was lost. The command exits 1 unless every build succeeded.

The server gets the environment of the command, so `TWINCAT_MCP_STATE_DIR` and `TWINCAT_AUTOMATION_EXE` apply.

## Tests

```bash
cd addons && ../.venv/Scripts/python.exe -m unittest discover -s tests
```

Measured on 2026-11-07: `build` built three fixture solutions on a local XAE in 103 s, and every context closed. `reload` reported the PLC of a project copy as embedded and wrote nothing.
