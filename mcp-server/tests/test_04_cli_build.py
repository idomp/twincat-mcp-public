"""
Group 4 – CLI build tests (slow, VS/XAE needed, NO live PLC required).

These run a full TwinCAT build for each local solution and verify the
JSON output shape is correct. Mark with @pytest.mark.slow so they can
be skipped during fast iterations: pytest -m "not slow"
"""
import pytest

from tests.helpers import SOLUTIONS, cli_run


pytestmark = pytest.mark.slow  # opt-in with: pytest -m slow  (or just pytest to run all)


def get_solution(name):
    path = SOLUTIONS[name]
    if not path.exists():
        pytest.skip(f"Solution not found: {path}")
    return path


@pytest.mark.parametrize("sol_name", list(SOLUTIONS.keys()))
def test_build_returns_valid_json(tc_exe, sol_name):
    sol = get_solution(sol_name)
    data = cli_run(tc_exe, "build", "--solution", str(sol), timeout=300)
    assert data is not None


@pytest.mark.parametrize("sol_name", list(SOLUTIONS.keys()))
def test_build_has_success_field(tc_exe, sol_name):
    sol = get_solution(sol_name)
    data = cli_run(tc_exe, "build", "--solution", str(sol), timeout=300)
    assert "success" in data, f"'success' field missing in build response: {data}"


@pytest.mark.parametrize("sol_name", list(SOLUTIONS.keys()))
def test_build_has_error_and_warning_arrays(tc_exe, sol_name):
    sol = get_solution(sol_name)
    data = cli_run(tc_exe, "build", "--solution", str(sol), timeout=300)
    assert "errors" in data or "errorCount" in data, (
        f"Neither 'errors' nor 'errorCount' in build response: {data}"
    )
    assert "warnings" in data or "warningCount" in data, (
        f"Neither 'warnings' nor 'warningCount' in build response: {data}"
    )


@pytest.mark.parametrize("sol_name", list(SOLUTIONS.keys()))
def test_build_succeeds(tc_exe, sol_name):
    """Build must succeed (zero compiler errors)."""
    sol = get_solution(sol_name)
    data = cli_run(tc_exe, "build", "--solution", str(sol), timeout=300)
    assert data.get("success") is True, (
        f"Build FAILED for {sol_name}.\n"
        f"Errors: {data.get('errors') or data.get('errorCount')}\n"
        f"Summary: {data.get('summary')}"
    )
