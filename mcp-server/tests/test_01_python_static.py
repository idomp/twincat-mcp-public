"""
Group 1 – Python layer static/structural checks.

These tests never invoke TcAutomation.exe or TwinCAT; they only inspect
Python module contents and constants.
"""
import sys
from pathlib import Path

import pytest

# Ensure mcp-server is importable from tests/
sys.path.insert(0, str(Path(__file__).parent.parent))

# ---------------------------------------------------------------------------
# Handler registration
# ---------------------------------------------------------------------------

def _get_handlers():
    """Import all handler modules and return the populated HANDLERS dict."""
    from twincat_mcp.handlers import _registry
    import twincat_mcp.handlers.ads
    import twincat_mcp.handlers.batch
    import twincat_mcp.handlers.deploy
    import twincat_mcp.handlers.safety
    import twincat_mcp.handlers.scope
    import twincat_mcp.handlers.shell
    import twincat_mcp.handlers.tcunit
    return _registry.HANDLERS


EXPECTED_HANDLER_COUNT = 40

EXPECTED_HANDLERS = {
    # Shell-based
    "twincat_build", "twincat_get_info", "twincat_clean",
    "twincat_set_target", "twincat_activate", "twincat_restart",
    "twincat_list_plcs", "twincat_set_boot_project", "twincat_disable_io",
    "twincat_set_variant", "twincat_list_tasks", "twincat_configure_task",
    "twincat_configure_rt", "twincat_check_all_objects", "twincat_static_analysis",
    "twincat_generate_library", "twincat_get_error_list",
    # ADS
    "twincat_get_state", "twincat_set_state",
    "twincat_read_var", "twincat_write_var",
    "twincat_read_var_list", "twincat_write_var_list",
    "twincat_ping_target", "twincat_list_symbols", "twincat_read_plc_log",
    "twincat_ads_record",
    # Scope
    "twincat_scope_create_config", "twincat_scope_export",
    "twincat_scope_start_record", "twincat_scope_stop_record",
    "twincat_scope_get_status",
    # Safety / meta
    "twincat_arm_dangerous_operations", "twincat_kill_stale",
    "twincat_host_status", "twincat_list_routes",
    "twincat_set_default_target",
    # Batch + deploy + tcunit
    "twincat_batch", "twincat_deploy", "twincat_run_tcunit",
}


def test_handler_count():
    handlers = _get_handlers()
    assert len(handlers) == EXPECTED_HANDLER_COUNT, (
        f"Expected {EXPECTED_HANDLER_COUNT} handlers, got {len(handlers)}.\n"
        f"Registered: {sorted(handlers.keys())}"
    )


@pytest.mark.parametrize("name", sorted(EXPECTED_HANDLERS))
def test_expected_handler_registered(name):
    handlers = _get_handlers()
    assert name in handlers, f"Handler '{name}' not registered in HANDLERS"


def test_no_duplicate_names():
    """The @register decorator already raises on duplicates, but verify here too."""
    handlers = _get_handlers()
    names = list(handlers.keys())
    assert len(names) == len(set(names))


# ---------------------------------------------------------------------------
# Schema ↔ handler consistency
# ---------------------------------------------------------------------------

def test_schema_names_match_handlers():
    """Every tool in get_tool_schemas() must have a matching handler."""
    from twincat_mcp.tools.schemas import get_tool_schemas
    handlers = _get_handlers()
    schema_names = {t.name for t in get_tool_schemas()}
    handler_names = set(handlers.keys())

    missing_handlers = schema_names - handler_names
    missing_schemas  = handler_names - schema_names

    assert not missing_handlers, f"Tools in schema but no handler: {sorted(missing_handlers)}"
    assert not missing_schemas,  f"Handlers without schema entry: {sorted(missing_schemas)}"


def test_all_schemas_have_input_schema():
    from twincat_mcp.tools.schemas import get_tool_schemas
    for tool in get_tool_schemas():
        assert tool.inputSchema is not None, f"Tool '{tool.name}' has no inputSchema"
        assert tool.inputSchema.get("type") == "object", (
            f"Tool '{tool.name}' inputSchema.type is not 'object'"
        )


def test_all_schemas_have_description():
    from twincat_mcp.tools.schemas import get_tool_schemas
    for tool in get_tool_schemas():
        assert tool.description and len(tool.description.strip()) > 0, (
            f"Tool '{tool.name}' has empty or missing description"
        )


# ---------------------------------------------------------------------------
# Safety constants consistency
# ---------------------------------------------------------------------------

def test_dangerous_tools_have_handlers():
    from twincat_mcp.safety import DANGEROUS_TOOLS
    handlers = _get_handlers()
    for tool in DANGEROUS_TOOLS:
        assert tool in handlers, (
            f"DANGEROUS_TOOL '{tool}' has no handler registered"
        )


def test_confirmation_required_subset_of_dangerous():
    from twincat_mcp.safety import DANGEROUS_TOOLS, CONFIRMATION_REQUIRED_TOOLS
    for tool in CONFIRMATION_REQUIRED_TOOLS:
        assert tool in DANGEROUS_TOOLS, (
            f"'{tool}' is in CONFIRMATION_REQUIRED_TOOLS but not in DANGEROUS_TOOLS"
        )


def test_dangerous_batch_commands_are_nonempty():
    from twincat_mcp.safety import DANGEROUS_BATCH_COMMANDS, CONFIRMATION_REQUIRED_BATCH_COMMANDS
    assert len(DANGEROUS_BATCH_COMMANDS) > 0
    for cmd in CONFIRMATION_REQUIRED_BATCH_COMMANDS:
        assert cmd in DANGEROUS_BATCH_COMMANDS


# ---------------------------------------------------------------------------
# Defaults module
# ---------------------------------------------------------------------------

def test_resolve_ams_net_id_uses_explicit():
    from twincat_mcp.defaults import resolve_ams_net_id
    assert resolve_ams_net_id("1.2.3.4.1.1") == "1.2.3.4.1.1"


def test_resolve_ams_net_id_falls_back():
    from twincat_mcp.defaults import resolve_ams_net_id, DEFAULT_AMS_NET_ID
    assert resolve_ams_net_id(None) == DEFAULT_AMS_NET_ID
    assert resolve_ams_net_id("") == DEFAULT_AMS_NET_ID


def test_is_local_target_loopback():
    from twincat_mcp.defaults import is_local_target
    assert is_local_target("127.0.0.1.1.1") is True
    assert is_local_target("127.0.0.1.2.3") is True


def test_is_local_target_remote():
    from twincat_mcp.defaults import is_local_target
    assert is_local_target("192.168.1.100.1.1") is False
    assert is_local_target("10.0.0.1.1.1") is False


def test_is_local_target_127_prefixed_remote_is_not_local():
    # An AMS Net ID is not an IP: 127.0.0.10.x / 127.0.0.100.x are distinct
    # REMOTE targets and must not be mistaken for the loopback 127.0.0.1.x.
    from twincat_mcp.defaults import is_local_target
    assert is_local_target("127.0.0.10.1.1") is False
    assert is_local_target("127.0.0.100.1.1") is False
    assert is_local_target("127.0.0.11.5.1") is False


def test_default_ams_net_id_is_set():
    from twincat_mcp.defaults import DEFAULT_AMS_NET_ID
    assert DEFAULT_AMS_NET_ID and len(DEFAULT_AMS_NET_ID) > 0


# ---------------------------------------------------------------------------
# Safety gate state (module-level mutable state)
# ---------------------------------------------------------------------------

def test_arm_disarm_cycle():
    from twincat_mcp.safety import (
        arm_dangerous_operations, disarm_dangerous_operations, is_armed
    )
    disarm_dangerous_operations()          # start clean
    assert not is_armed()
    arm_dangerous_operations("unit-test")
    assert is_armed()
    disarm_dangerous_operations()
    assert not is_armed()


def test_arm_requires_reason():
    from twincat_mcp.safety import arm_dangerous_operations, disarm_dangerous_operations
    disarm_dangerous_operations()
    # Should not raise even with minimal reason
    arm_dangerous_operations("test")
    disarm_dangerous_operations()
