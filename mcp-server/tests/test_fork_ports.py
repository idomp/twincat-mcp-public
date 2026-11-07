"""Fork test cases that still apply to v3, rewritten for unittest with mocks only."""

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from twincat_mcp import cli
from twincat_mcp.catalog import Catalog
from twincat_mcp.dispatch import run_shell_step
from twincat_mcp.host import HostError, NotDispatched, ShellHost


def live_host():
    host = ShellHost(Path("unused.exe"))
    proc = Mock()
    proc.poll.return_value = None
    host._proc = proc
    return host, proc


class ExecutableTests(unittest.TestCase):
    def test_first_existing_default_path_wins(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ):
            os.environ.pop("TWINCAT_AUTOMATION_EXE", None)
            a, b, c = (Path(folder) / n for n in ("a.exe", "b.exe", "c.exe"))
            b.touch()
            c.touch()
            with patch.object(cli, "TC_AUTOMATION_PATHS", [a, b, c]):
                self.assertEqual(b, cli.find_tc_automation_exe())
            with patch.object(cli, "TC_AUTOMATION_PATHS", [a]):
                with self.assertRaises(FileNotFoundError) as caught:
                    cli.find_tc_automation_exe()
            self.assertIn(str(a), str(caught.exception))


class CliRunTests(unittest.TestCase):
    def run_cli(self, stdout="", stderr="", code=0, communicate=None, popen=None):
        proc = Mock(returncode=code)
        proc.communicate.side_effect = communicate or [(stdout, stderr)]
        with (
            patch.object(cli, "find_tc_automation_exe", return_value=Path("C:/x/TcAutomation.exe")),
            patch.object(cli.subprocess, "Popen", side_effect=popen, return_value=proc) as start,
        ):
            result = cli._run("info", ["--x"], 5)
        return result, proc, start

    def test_result_and_progress(self):
        (result, progress), _, start = self.run_cli('{"success": true, "a": 1}', "[PROGRESS] step one\n")
        self.assertEqual({"success": True, "a": 1}, result)
        self.assertEqual(["step one"], progress)
        self.assertEqual([str(Path("C:/x/TcAutomation.exe")), "info", "--x"], start.call_args.args[0])

    def test_nonzero_exit_overrides_success(self):
        (result, _), _, _ = self.run_cli('{"success": true}', code=1)
        self.assertFalse(result["success"])
        self.assertIn("code 1", result["errorMessage"])

    def test_invalid_output_is_uncertain(self):
        for stdout in ("not json", "[1]"):
            (result, _), _, _ = self.run_cli(stdout, "tail of stderr")
            self.assertTrue(result["outcomeUnknown"])
            self.assertIn("tail of stderr", result["stderr"])

    def test_timeout_kills_once_and_is_uncertain(self):
        (result, _), proc, start = self.run_cli(communicate=[subprocess.TimeoutExpired("x", 5), ("", "")])
        proc.kill.assert_called_once()
        start.assert_called_once()
        self.assertEqual(5, proc.communicate.call_args_list[0].kwargs.get("timeout"), "the first wait is bounded")
        self.assertEqual(2, proc.communicate.call_count, "the killed process is reaped")
        self.assertTrue(result["outcomeUnknown"])

    def test_start_failure_is_not_dispatched(self):
        (result, _), _, _ = self.run_cli(popen=OSError("denied"))
        self.assertFalse(result["dispatched"])


class CatalogShapeTests(unittest.TestCase):
    def test_every_operation_has_an_object_schema_and_a_description(self):
        for op_id, entry in Catalog().entries.items():
            self.assertEqual("object", entry["inputSchema"].get("type"), op_id)
            self.assertTrue((entry.get("description") or "").strip(), op_id)


@patch("twincat_mcp.host.HOST_DISABLED", False)
class DispatchTests(unittest.TestCase):
    def test_delivered_failure_reaches_the_caller_once(self):
        host, _ = live_host()
        # The real receipt path: an ok:false reply that names the command is a delivered rejection.
        host._responses.put({"id": 1, "ok": False, "command": "build",
                             "result": {"success": False, "errors": ["E1"], "errorMessage": "compile failed"}})
        with (
            patch.object(host, "ensure_solution"),
            patch.object(host, "_send_request", return_value=1) as call,
            patch("twincat_mcp.dispatch.get_shell_host", return_value=host),
        ):
            result, _ = run_shell_step("build", {}, solution_path="x.sln")
        self.assertIs(False, result["success"])
        self.assertEqual(["E1"], result["errors"])
        self.assertEqual("compile failed", result["errorMessage"])
        self.assertNotIn("outcomeUnknown", result)
        call.assert_called_once()

    def assert_uncertain(self, host):
        with patch("twincat_mcp.dispatch.get_shell_host", return_value=host):
            result, _ = run_shell_step("set-state", {})
        self.assertTrue(result["outcomeUnknown"])
        self.assertNotIn("dispatched", result)

    def test_error_reply_after_send_is_uncertain(self):
        for reply in ({"id": 1, "ok": False, "error": "COMException"},
                      {"id": 1, "ok": False, "command": "other", "result": {"success": False}}):
            host, _ = live_host()
            host._responses.put(reply)
            with patch.object(host, "_send_request", return_value=1):
                with self.assertRaises(HostError) as caught:
                    host._call_raw_locked("execute-step", {"command": "set-state"}, timeout=1)
            self.assertNotIsInstance(caught.exception, NotDispatched)
            host._responses.put(reply)
            with patch.object(host, "_send_request", return_value=1):
                self.assert_uncertain(host)

    def test_write_failure_is_uncertain(self):
        host, proc = live_host()
        proc.stdin.write.side_effect = BrokenPipeError()
        with self.assertRaises(HostError) as caught:
            host._call_raw_locked("execute-step", {"command": "set-state"}, timeout=1)
        self.assertNotIsInstance(caught.exception, NotDispatched)
        self.assert_uncertain(host)

    def test_response_timeout_is_uncertain(self):
        host, _ = live_host()
        with patch.object(host, "_send_request", return_value=1):
            with self.assertRaises(HostError) as caught:
                host._call_raw_locked("execute-step", {"command": "set-state"}, timeout=0.2)
        self.assertNotIsInstance(caught.exception, NotDispatched)

    def test_ready_timeout_kills_the_worker(self):
        host = ShellHost(Path("unused.exe"))
        proc = Mock()
        proc.poll.return_value = None
        with (
            patch("twincat_mcp.host.subprocess.Popen", return_value=proc),
            patch("twincat_mcp.host.threading.Thread"),
            patch.object(ShellHost, "READY_TIMEOUT_SEC", 0.2),
        ):
            with self.assertRaises(HostError):
                host._start_locked()
        proc.kill.assert_called_once()

    def test_selection_and_dispatch_hold_the_lock(self):
        host, _ = live_host()
        order = []

        def other():
            with host._lock:
                order.append("other")

        waiter = threading.Thread(target=other)

        def select(*_args, **_kwargs):
            waiter.start()
            time.sleep(0.3)  # the other thread now waits for the lock

        def dispatch(*_args, **_kwargs):
            time.sleep(0.1)
            order.append("dispatch")
            return {"result": {"success": True}}

        with (
            patch.object(host, "ensure_solution", side_effect=select),
            patch.object(host, "_call_raw_locked", side_effect=dispatch),
        ):
            host.execute_step("build", {}, "x.sln", None)
        waiter.join(5)
        self.assertEqual(["dispatch", "other"], order, "the lock was released between selection and dispatch")

if __name__ == "__main__":
    unittest.main()
