# Fork to v3 compatibility

This document is fork-only. It does not go to upstream.

It is the compatibility contract between the 42 tools of the `twincat-automation` fork and the v3 server. The v3 server exposes three MCP tools: `twincat_search`, `twincat_describe` and `twincat_execute`. Every operation runs through `twincat_execute` with `operation` and `arguments`.

The operation ids come from `mcp-server/twincat_mcp/operations.json` and from the control operations in `mcp-server/twincat_mcp/catalog.py`.

## Terms

| Term | Meaning |
| --- | --- |
| Context | The operation needs a `contextHandle` from `context.open`. |
| Context grant | The Grant column says "Context". The operation needs a `safety.grant` handle scoped to the `contextHandle`. |
| Target grant | The Grant column says "Target". The operation needs a `safety.grant` handle scoped to the `amsNetId`. |
| Confirm | The operation also takes `confirm: "CONFIRM"`. |
| Quarantined | The catalog marks the operation `available: false`. Search does not list it. An execute fails before dispatch with `unsupported_integration`. |

## Tool map

| # | Fork tool | v3 operation, add-on or status | Context | Grant | Status |
| --- | --- | --- | --- | --- | --- |
| 1 | `twincat_activate` | `engineering.activate` | Yes | Context, confirm | Mapped |
| 2 | `twincat_ads_record` | `runtime.record` | No | Target | Mapped. The row model changed. |
| 3 | `twincat_arm_dangerous_operations` | `safety.grant` and `safety.revoke` | No | n/a | Mapped. A grant names its operations, one scope and a TTL. |
| 4 | `twincat_batch` | `workflow.sequence` | No | Per step | Mapped with losses |
| 5 | `twincat_build` | `engineering.build`, or `workflow.build_verify` with expectations | Yes | No | Mapped |
| 6 | `twincat_check_all_objects` | `engineering.check_all_objects` | Yes | No | Mapped |
| 7 | `twincat_clean` | `engineering.clean` | Yes | No | Mapped |
| 8 | `twincat_configure_rt` | `engineering.configure_rt` | Yes | Context | Mapped |
| 9 | `twincat_configure_task` | `engineering.configure_task` | Yes | Context | Mapped |
| 10 | `twincat_deploy` | `workflow.deploy` | Yes | Context, confirm | Mapped. `dryRun` previews the workflow. |
| 11 | `twincat_disable_io` | `engineering.disable_io` | Yes | Context | Mapped |
| 12 | `twincat_enable_tmc_auto_reload` | `addons/multi_plc.py reload` | n/a | n/a | Add-on. Run it from a shell. |
| 13 | `twincat_generate_library` | `engineering.generate_library` | Yes | Context | Mapped with losses |
| 14 | `twincat_get_error_list` | `engineering.get_error_list` | Yes | No | Mapped |
| 15 | `twincat_get_info` | `engineering.info` | Yes | No | Mapped |
| 16 | `twincat_get_state` | `runtime.get_state` | No | No | Mapped |
| 17 | `twincat_host_status` | `system.status` and `context.status` | No | No | Mapped with losses |
| 18 | `twincat_kill_stale` | `context.close` and `system.reap_orphans` | No | No | Mapped |
| 19 | `twincat_list_plcs` | `engineering.list_plcs` | Yes | No | Mapped |
| 20 | `twincat_list_routes` | `system.routes` | No | No | Mapped |
| 21 | `twincat_list_symbols` | `runtime.list_symbols` | No | No | Mapped |
| 22 | `twincat_list_tasks` | `engineering.list_tasks` | Yes | No | Mapped |
| 23 | `twincat_multi_plc_build` | None | n/a | n/a | Dropped by a user decision on 2026-11-07 |
| 24 | `twincat_ping_target` | `runtime.ping_target` | No | No | Mapped |
| 25 | `twincat_read_plc_log` | `runtime.read_plc_log` | No | No | Mapped |
| 26 | `twincat_read_var` | `runtime.read_var` | No | No | Mapped |
| 27 | `twincat_read_var_list` | `runtime.read_var_list` | No | No | Mapped. The list-receipt rule applies. |
| 28 | `twincat_restart` | `engineering.restart` | Yes | Context, confirm | Mapped |
| 29 | `twincat_run_tcunit` | `workflow.test` | Yes | Context, confirm | Mapped |
| 30 | `twincat_scope_create_config` | `scope.create_config` | No | Target | Mapped. It needs a TE13xx-enabled worker. |
| 31 | `twincat_scope_export` | `scope.export` | n/a | n/a | Quarantined. Read the CSV of `runtime.record`. |
| 32 | `twincat_scope_get_status` | `scope.status` | n/a | n/a | Quarantined. Read the `runtime.record` job with `job.get`. |
| 33 | `twincat_scope_start_record` | `scope.start` | n/a | n/a | Quarantined. Use `runtime.record`. |
| 34 | `twincat_scope_stop_record` | `scope.stop` | n/a | n/a | Quarantined. End a `runtime.record` with `durationSec` or `stopTrigger`. |
| 35 | `twincat_set_boot_project` | `engineering.set_boot_project` | Yes | Context | Mapped |
| 36 | `twincat_set_default_target` | None | n/a | n/a | No persistent equivalent. `context.open` takes an explicit target. |
| 37 | `twincat_set_state` | `runtime.set_state` | No | Target | Mapped |
| 38 | `twincat_set_target` | `engineering.set_target` | Yes | Context | Mapped. It sets the target that `context.open` fixed. |
| 39 | `twincat_set_variant` | `engineering.set_variant` | Yes | Context | Mapped |
| 40 | `twincat_static_analysis` | `engineering.static_analysis` | Yes | No | Mapped |
| 41 | `twincat_write_var` | `runtime.write_var` | No | Target | Mapped |
| 42 | `twincat_write_var_list` | `runtime.write_var_list` | No | Target | Mapped. The list-receipt rule applies. |

## Accepted losses

### Sequence

| Item | Content |
| --- | --- |
| Loss | `workflow.sequence` always stops on the first failure. No `stopOnError: false` exists. |
| Loss | A sequence holds 32 steps at most. |
| Loss | A sequence excludes control operations and nested sequences. The controls are `context.*`, `safety.*`, `job.*`, `system.status` and `system.reap_orphans`. |
| Reason | The v3 catalog defines a sequence as validated steps in order. It stops on the first failure. It has no rollback, nesting or replay. |
| Alternative | To continue past a failure, run each operation in its own `twincat_execute`. |
| Alternative | For more than 32 steps, split the work into several sequences. |
| Alternative | Run the controls outside the sequence. Open the context and create the grants first. |

### Host status

| Item | Content |
| --- | --- |
| Loss | `system.status` has no worker or DTE process IDs, no uptime and no call counts. |
| Reason | `system.status` reports the version, the operation count, the protocol, the unavailable operations, the queue status and the server lifecycle (server, parent and client process IDs). It does not start TwinCAT. |
| Alternative | `context.status` inspects a context without starting TwinCAT. |
| Alternative | For process IDs, read the Windows process list. `system.reap_orphans` cleans workers of dead parents. |

### Default target

| Item | Content |
| --- | --- |
| Loss | No persistent default target exists. |
| Reason | Every runtime operation takes an explicit `amsNetId`. `context.open` fixes the target of an engineering context. |
| Alternative | Pass `amsNetId` in each runtime call. Pass it to `context.open` for engineering work. |

### Library export

| Item | Content |
| --- | --- |
| Loss | `engineering.generate_library` cannot install into the library repository. |
| Loss | `engineering.generate_library` cannot overwrite an output. |
| Reason | The operation uses the documented export call without repository install. It writes new absolute `.library` outputs only. |
| Alternative | Export to a new path for each run. Install the exported file in XAE by hand. |

### Recording

| Item | Content |
| --- | --- |
| Loss | `runtime.record` has no fixed-cadence timer. |
| Reason | Recording rows are channel-0 snapshots. When channel 0 notifies after every channel has reported, the server writes a row. |
| Consequence | `sampleTimeMs` sets the ADS notification cycle. It guarantees no row rate and no row count. |
| Alternative | Put the timing reference at channel 0. Compute the real intervals from the `Timestamp` column. |

## List receipts

The rule covers `runtime.read_var_list` and `runtime.write_var_list`.

- If any item fails, the receipt fails.
- The receipt keeps the partial results.
- A list read is not an atomic ADS sum read.
- A list write is not atomic. The server does not roll it back.
- A write whose outcome is unknown is never replayed. Read the target state before any further step.

## Restored fork behaviour

These fork behaviours are back in v3, measured on a TwinCAT 3.1.4024 simulator on 2026-11-07.

| Behaviour | v3 contract |
| --- | --- |
| Reads while the PLC is in Stop | `runtime.read_var` and `runtime.read_var_list` read in Run or Stop. Every other state refuses the read. |
| Numeric and Boolean JSON values in list writes | `runtime.write_var_list` takes strings, numbers and Booleans. An integer keeps its exact value. A decimal number travels as a double, so a string is the way to send exact decimal text. A Boolean becomes `TRUE` or `FALSE`. Null, objects and arrays refuse the request before any write. |
| Invariant number text | List read and write results use a decimal point, whatever the culture of the PC. |
| Ordered list results | `runtime.read_var_list` returns `items`: every result in request order, with its index and symbol. `results` keeps the map keyed by symbol. |

Not restored: duplicate symbols in one `runtime.read_var_list` request. The upstream schema refuses them (`uniqueItems`), and the fork keeps that contract.
