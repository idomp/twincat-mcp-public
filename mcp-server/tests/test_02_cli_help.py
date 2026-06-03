"""
Group 2 – CLI structural tests (no PLC, no VS session needed).

These tests verify TcAutomation.exe can be invoked, responds to --help,
and exposes all expected subcommands.
"""
import subprocess
from pathlib import Path

import pytest

from tests.helpers import TC_EXE


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def help_text(tc_exe):
    result = subprocess.run(
        [str(tc_exe), "--help"],
        capture_output=True, text=True, timeout=15,
    )
    return result.stdout + result.stderr   # some CLIs write help to stderr


# ---------------------------------------------------------------------------
# Binary existence
# ---------------------------------------------------------------------------

def test_tc_automation_exe_exists(tc_exe):
    assert tc_exe.exists(), f"TcAutomation.exe not found at {tc_exe}"
    assert tc_exe.stat().st_size > 0, "TcAutomation.exe is empty"


# ---------------------------------------------------------------------------
# --help output
# ---------------------------------------------------------------------------

EXPECTED_SUBCOMMANDS = [
    "info",
    "build",
    "clean",
    "list-plcs",
    "list-tasks",
    "set-target",
    "activate",
    "restart",
    "deploy",
    "batch",
    "get-state",
    "set-state",
    "read-var",
    "write-var",
    "read-var-list",
    "write-var-list",
    "ads-record",
    "run-tcunit",
    "check-all-objects",
    "static-analysis",
    "get-error-list",
]

# Scope commands are only compiled when EnableScope=true (requires TE13xx license DLLs).
# They live inside #if SCOPE_AVAILABLE in Program.cs.
SCOPE_SUBCOMMANDS = ["scope-session", "scope-create", "scope-export"]


def test_help_exits_zero(tc_exe):
    result = subprocess.run(
        [str(tc_exe), "--help"],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, f"--help returned code {result.returncode}"


def test_scope_commands_absent_in_standard_build(tc_exe, help_text):
    """Scope commands require EnableScope=true build (TE13xx DLLs).
    Standard Release build should NOT list them, confirming conditional compilation."""
    scope_present = [cmd for cmd in SCOPE_SUBCOMMANDS if cmd in help_text]
    if scope_present:
        # Scope build — just document it, don't fail
        pytest.skip(f"This is a scope-enabled build; scope commands are present: {scope_present}")
    # Standard build: none should be present
    for cmd in SCOPE_SUBCOMMANDS:
        assert cmd not in help_text, (
            f"Scope command '{cmd}' found in standard build --help; "
            "expected only in EnableScope=true build"
        )


def test_help_mentions_twincat(tc_exe, help_text):
    combined = help_text.lower()
    assert "twincat" in combined, "Expected 'twincat' in --help output"


@pytest.mark.parametrize("cmd", EXPECTED_SUBCOMMANDS)
def test_subcommand_in_help(tc_exe, help_text, cmd):
    assert cmd in help_text, (
        f"Subcommand '{cmd}' not found in --help output.\n"
        f"Help text (truncated):\n{help_text[:2000]}"
    )


# ---------------------------------------------------------------------------
# Python-layer exe discovery
# ---------------------------------------------------------------------------

def test_find_tc_automation_exe_returns_correct_path():
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from twincat_mcp.cli import find_tc_automation_exe
    found = find_tc_automation_exe()
    assert found.exists(), f"find_tc_automation_exe() returned non-existent path: {found}"
    assert found.name == "TcAutomation.exe"
