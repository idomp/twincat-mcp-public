"""
Pytest fixtures for the twincat-mcp test suite.
"""
import time

import pytest
from tests.helpers import TC_EXE


@pytest.fixture(scope="session")
def tc_exe():
    """Path to TcAutomation.exe; skip all callers if not built."""
    if not TC_EXE.exists():
        pytest.skip(f"TcAutomation.exe not found at {TC_EXE}. Run scripts/build.ps1 first.")
    return TC_EXE


@pytest.fixture(autouse=True)
def vs_session_cooldown(request):
    """Add a short delay after any test that opens a VS/DTE session.

    TcAutomation.exe uses COM automation; the DTE process needs a moment to
    fully release its COM registrations before the next invocation. Without
    this, back-to-back VS sessions can cause RPC server unavailable errors
    (0x800706BA) or stale project load timeouts.
    """
    yield
    # Only sleep for tests in the CLI-solution or CLI-build groups
    markers = {m.name for m in request.node.iter_markers()}
    module = getattr(request.module, "__name__", "")
    if "test_03" in module or "test_04" in module:
        time.sleep(3)
