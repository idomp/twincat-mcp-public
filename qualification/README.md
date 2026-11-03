# Live qualification

These runners drive `mcp-server/server.py` through a real stdio MCP client against a real XAE and a real PLC. Each run writes `calls.jsonl`, `results.json` and `report.md` under `%QUAL_DIR%\runs`. A runner exits 1 on any FAIL or BLOCKED result. `NOT_RUN` results are listed in the report and do not change the exit code.

## Tiers

| Runner | Tier | What it touches |
|---|---|---|
| `run_t0.py` | 0 | Local server state in the run folder only. Dispatch is disabled, and the target is a documentation address. |
| `run_t1.py` | 1 | Local XAE on copies of the fixtures. No runtime write. Deploy runs only as a dry run. It kills its own XAE shell and worker, and it calls `system.reap_orphans`. |
| `run_t2.py` | 2 | Read-only ADS on the site target. It also contacts a documentation address that has no route. |
| `run_t3.py` | 3 | The approved write set on the site target. It restores the changed threshold and stops all later writes after any unexpected or unverified write. |
| `run_t4.py` | 4 | One step per call on the site target: `login-oc` (matching login, then an online change that adds and removes one variable in the project copy), `stoprun`, or `deploy` of the project copy. Every step needs the system and the PLC in Run. |

`survey_target.py` reads states and symbols only and writes no report. `probe_task.py` is a tier-1 probe for `configure_task` and writes no report.

## Setup

1. Put the fixtures and `site.json` in a folder outside every repository.
2. Copy `site.example.json` to that folder as `site.json`.
3. Replace every value in `site.json` with the values of your site. An empty value stops every runner.
4. Set `QUAL_DIR` to that folder. A folder inside the repository or inside the real state folder is refused.
5. Build the worker with `scripts\build-no-vs.ps1` or `scripts\setup.ps1`.
6. Run a tier with `.venv\Scripts\python.exe qualification\run_t<N>.py`. `run_t4.py` takes `login-oc`, `stoprun` or `deploy`.

## Rules

- Never put site values in this folder. The target, route, project and symbol names stay in `site.json`.
- Run tier 3 only with the write set approved by the owner of the target.
- Run tier 4 only with a person at the machine who says it is safe.
- Each server gets its own `TWINCAT_MCP_STATE_DIR` in the run folder. Restart tests reopen the folder of a server that has stopped.

## Known gaps

- A grant covers an operation and a target or context. It does not limit the symbol, the value or the requested state.
- `run_t4.py deploy` trusts `site.json` to name the approved project copy. It checks the PLC name and port in that copy, not its source.
- Another client can change a value or the system state between a runner's read and its write. ADS has no compare-and-swap, and the worker routes Run to port 10000 when the system is not in Run.
- If a tier-4 step fails part-way, the runner records `T4-STOP` and does not repeat the step. A person reconciles the target.
- If a revoke is not confirmed, `run_t4.py` records `T4-REVOKE`. The grant then lasts until its server exits.
- A cancelled `run_t4.py` can lose its `context.close` (not tested). The run still records `RUNNER` as FAIL.
- L13 in tier 1 passes when the MCP SDK ends the server's job object after stdin closes. It does not prove the server's own client-loss path.
