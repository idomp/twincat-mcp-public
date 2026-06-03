"""
Group 3 – CLI solution-level tests (VS/XAE needed, NO live PLC required).

Tests: info, list-plcs, list-tasks against the solutions configured in
TWINCAT_TEST_SOLUTIONS (see tests/helpers.py). Skips when none are configured.
Build is tested separately in test_04 because it is slow.
"""
import pytest

from tests.helpers import SOLUTIONS, TSPPROJ_SOLUTION, cli_run


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def solution_ids():
    return list(SOLUTIONS.keys())


def get_solution(name):
    path = SOLUTIONS[name]
    if not path.exists():
        pytest.skip(f"Solution not found: {path}")
    return path


# ---------------------------------------------------------------------------
# info
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sol_name", solution_ids())
def test_info_returns_valid_json(tc_exe, sol_name):
    sol = get_solution(sol_name)
    data = cli_run(tc_exe, "info", "--solution", str(sol))
    assert data is not None


@pytest.mark.parametrize("sol_name", solution_ids())
def test_info_has_tc_version(tc_exe, sol_name):
    sol = get_solution(sol_name)
    data = cli_run(tc_exe, "info", "--solution", str(sol))
    assert "tcVersion" in data, f"'tcVersion' missing in info response: {data}"
    assert data["tcVersion"], "tcVersion is empty"


@pytest.mark.parametrize("sol_name", solution_ids())
def test_info_has_plc_projects(tc_exe, sol_name):
    sol = get_solution(sol_name)
    data = cli_run(tc_exe, "info", "--solution", str(sol))
    assert "plcProjects" in data, f"'plcProjects' missing in info response: {data}"
    assert isinstance(data["plcProjects"], list)
    assert len(data["plcProjects"]) >= 1, (
        f"Expected at least 1 PLC project in {sol_name}, got: {data['plcProjects']}"
    )


@pytest.mark.parametrize("sol_name", solution_ids())
def test_info_no_error_message(tc_exe, sol_name):
    sol = get_solution(sol_name)
    data = cli_run(tc_exe, "info", "--solution", str(sol))
    assert not data.get("errorMessage"), (
        f"info returned errorMessage: {data.get('errorMessage')}"
    )


# ---------------------------------------------------------------------------
# list-plcs
# ---------------------------------------------------------------------------

def _extract_plcs(data):
    """Extract PLC list from list-plcs response (handles PascalCase and camelCase)."""
    if isinstance(data, list):
        return data
    # list-plcs returns PascalCase; info returns camelCase — accept both
    return (
        data.get("PlcProjects")  # list-plcs (PascalCase)
        or data.get("plcProjects")  # info-style (camelCase)
        or data.get("plcs")
        or []
    )


@pytest.mark.parametrize("sol_name", solution_ids())
def test_list_plcs_returns_list(tc_exe, sol_name):
    sol = get_solution(sol_name)
    data = cli_run(tc_exe, "list-plcs", "--solution", str(sol))
    plcs = _extract_plcs(data)
    assert isinstance(plcs, list)
    assert len(plcs) >= 1, f"Expected at least 1 PLC in {sol_name}"


@pytest.mark.parametrize("sol_name", solution_ids())
def test_list_plcs_have_name(tc_exe, sol_name):
    sol = get_solution(sol_name)
    data = cli_run(tc_exe, "list-plcs", "--solution", str(sol))
    plcs = _extract_plcs(data)
    assert len(plcs) >= 1, f"Expected at least 1 PLC entry for {sol_name}"
    for plc in plcs:
        # PascalCase (list-plcs) or camelCase (info)
        name = plc.get("Name") or plc.get("name")
        assert name, f"PLC entry missing 'Name'/'name': {plc}"


# ---------------------------------------------------------------------------
# list-tasks
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sol_name", solution_ids())
def test_list_tasks_returns_json(tc_exe, sol_name):
    sol = get_solution(sol_name)
    data = cli_run(tc_exe, "list-tasks", "--solution", str(sol))
    assert data is not None


# ---------------------------------------------------------------------------
# tspproj-specific: solutions using the newer .tspproj format (GUID DFBE7525).
# Regression test protecting the fix from commit afbbe77. Configure a .tspproj
# solution via TWINCAT_TEST_TSPPROJ_SOLUTION; skips when unset.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(TSPPROJ_SOLUTION is None, reason="TWINCAT_TEST_TSPPROJ_SOLUTION not set")
def test_tspproj_solution_detected(tc_exe):
    """A .tspproj-based solution must be detected by info (regression for afbbe77 fix)."""
    if not TSPPROJ_SOLUTION.exists():
        pytest.skip(f"tspproj solution not found: {TSPPROJ_SOLUTION}")
    data = cli_run(tc_exe, "info", "--solution", str(TSPPROJ_SOLUTION))
    assert data.get("tcVersion"), ".tspproj solution not detected (tspproj GUID fix regression)"


# ---------------------------------------------------------------------------
# Python-layer round-trip: run_tc_automation('info', ...) via the Python wrapper
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sol_name", solution_ids())
def test_python_run_tc_automation_info(sol_name):
    """Verify the Python wrapper (not just raw subprocess) returns valid data."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from twincat_mcp.cli import run_tc_automation

    sol = get_solution(sol_name)
    data = run_tc_automation("info", ["--solution", str(sol)])
    assert data.get("tcVersion"), (
        f"Python run_tc_automation('info') gave no tcVersion for {sol_name}: {data}"
    )
