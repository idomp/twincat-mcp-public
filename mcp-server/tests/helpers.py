"""
Shared constants and helpers for the twincat-mcp test suite.
Importable by conftest.py and all test modules.
"""
import json
import os
import subprocess
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT     = Path(__file__).parent.parent.parent   # .../twincat-mcp
MCP_SERVER_DIR = REPO_ROOT / "mcp-server"
TC_EXE = REPO_ROOT / "TcAutomation" / "bin" / "Release" / "TcAutomation.exe"


def _load_test_solutions():
    """Solution-level tests run against your own local TwinCAT solutions.

    Configure them via the ``TWINCAT_TEST_SOLUTIONS`` env var so no local
    project names/paths are committed to the repo. Format (semicolon-separated):

        TWINCAT_TEST_SOLUTIONS="myplc=C:/path/to/MyPlc.sln;other=C:/path/to/Other.sln"

    When unset, the solution tests simply skip.
    """
    raw = os.environ.get("TWINCAT_TEST_SOLUTIONS", "").strip()
    out = {}
    for entry in filter(None, (e.strip() for e in raw.split(";"))):
        if "=" in entry:
            label, path = entry.split("=", 1)
            out[label.strip()] = Path(path.strip())
    return out


SOLUTIONS = _load_test_solutions()

# Optional: a solution that uses the newer .tspproj format (GUID DFBE7525),
# for the regression test guarding commit afbbe77. Point it at any such .sln:
#   TWINCAT_TEST_TSPPROJ_SOLUTION="C:/path/to/Tspproj.sln"
TSPPROJ_SOLUTION = (
    Path(os.environ["TWINCAT_TEST_TSPPROJ_SOLUTION"])
    if os.environ.get("TWINCAT_TEST_TSPPROJ_SOLUTION")
    else None
)


# ---------------------------------------------------------------------------
# CLI helper
# ---------------------------------------------------------------------------

def cli_run(tc_exe: Path, *args, timeout: int = 180) -> dict:
    """Run TcAutomation.exe and return parsed JSON (skips leading [DEBUG] lines)."""
    result = subprocess.run(
        [str(tc_exe)] + list(args),
        capture_output=True, text=True, timeout=timeout,
    )
    stdout = result.stdout
    json_start = stdout.find("{")
    if json_start == -1:
        raise ValueError(
            f"No JSON found in output.\nstdout: {stdout!r}\nstderr: {result.stderr!r}"
        )
    return json.loads(stdout[json_start:])
