import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from twincat_mcp.backend import Backend, normalize
from twincat_mcp.catalog import Catalog
from twincat_mcp.cli import find_tc_automation_exe
from twincat_mcp.dispatch import run_shell_step
from twincat_mcp.errors import OperationError
from twincat_mcp.host import HostError, ShellHost
from twincat_mcp.routes import run_route_action
from twincat_mcp.scope import ScopeSession


class BackendTests(unittest.TestCase):
    def test_source_and_online_change_cannot_replace_or_start_session(self):
        for command in ("online-change", "edit-plc-source"):
            host = ShellHost(Path("unused.exe"))
            with (
                patch.object(host, "ensure_solution") as ensure,
                patch.object(host, "_start_locked") as start,
            ):
                result, _ = host.execute_step(command, {}, "sample.sln", None)
            self.assertFalse(result["dispatched"])
            ensure.assert_not_called()
            start.assert_not_called()

    def test_every_uncertain_dispatch_is_never_replayed(self):
        for command in (
            "activate",
            "restart",
            "deploy",
            "matching-login",
            "edit-plc-source",
            "online-change",
            "write-var-list",
        ):
            host = Mock()
            host.execute_step.side_effect = HostError("lost reply")
            with patch("twincat_mcp.dispatch.get_shell_host", return_value=host):
                result, _ = run_shell_step(command, {})
            self.assertTrue(result["outcomeUnknown"])
            host.execute_step.assert_called_once()

    def test_lost_reply_keeps_progress(self):
        host = ShellHost(Path("unused.exe"))

        def lost(*_args, **_kwargs):
            with host._progress_lock:
                host._progress.append("waiting for Run")
            raise HostError("lost reply")

        with (
            patch.object(host, "is_alive", return_value=True),
            patch.object(host, "_call_raw_locked", side_effect=lost),
            patch("twincat_mcp.dispatch.get_shell_host", return_value=host),
        ):
            result, progress = run_shell_step("set-state", {})
        self.assertTrue(result["outcomeUnknown"])
        self.assertEqual(["waiting for Run"], progress)

    def test_missing_host_does_not_fallback(self):
        with patch("twincat_mcp.dispatch.get_shell_host", return_value=None):
            result, _ = run_shell_step("online-change", {})
        self.assertFalse(result["dispatched"])

    def test_delivered_rejection_preserves_receipt(self):
        host = ShellHost(Path("unused.exe"))
        receipt = dict(
            Success=False, LoginAttempted=False, ErrorMessage="Configured ADS port mismatch"
        )
        host._responses.put(dict(id=7, ok=False, command="matching-login", result=receipt))
        with patch.object(host, "_send_request", return_value=7):
            result = host._call_raw_locked(
                "execute-step", dict(command="matching-login"), timeout=1
            )
        self.assertEqual(receipt, result["result"])

    def test_failed_ensure_solution_forgets_cached_solution(self):
        host = ShellHost(Path("unused.exe"))
        host._current_solution = "sample.sln"
        host._current_tc_version = None
        with (
            patch.object(host, "is_alive", return_value=True),
            patch.object(host, "_call_raw_locked", side_effect=HostError("restart failed")),
        ):
            with self.assertRaises(HostError):
                host.ensure_solution("sample.sln", "3.1.4024.55")
        with (
            patch.object(host, "is_alive", return_value=True),
            patch.object(
                host, "_call_raw_locked", return_value={"loaded": True, "solutionPath": "sample.sln"}
            ) as call,
        ):
            host.ensure_solution("sample.sln", None)
        call.assert_called_once()

    def test_unconfirmed_solution_is_not_cached(self):
        for reply in (
            {"loaded": False, "solutionPath": "sample.sln"},
            {"loaded": True, "solutionPath": "other.sln"},
            {"loaded": True},
            {"solutionPath": "sample.sln"},
        ):
            host = ShellHost(Path("unused.exe"))
            with (
                patch.object(host, "is_alive", return_value=True),
                patch.object(host, "_call_raw_locked", return_value=reply),
            ):
                with self.assertRaises(HostError):
                    host.ensure_solution("sample.sln", None)
            self.assertIsNone(host._current_solution, reply)

    def test_unconfirmed_solution_is_reported_as_not_dispatched(self):
        host = ShellHost(Path("unused.exe"))
        with (
            patch.object(host, "is_alive", return_value=True),
            patch.object(host, "_call_raw_locked", return_value={"loaded": False, "solutionPath": "s.sln"}) as call,
            patch("twincat_mcp.dispatch.get_shell_host", return_value=host),
        ):
            result, _ = run_shell_step("activate", {}, solution_path="s.sln")
        self.assertFalse(result["dispatched"])
        self.assertNotIn("outcomeUnknown", result)
        self.assertEqual(["ensure-solution"], [c.args[0] for c in call.call_args_list])

    def test_lost_ensure_solution_is_not_dispatched(self):
        host = ShellHost(Path("unused.exe"))
        with (
            patch.object(host, "is_alive", return_value=True),
            patch.object(host, "_call_raw_locked", side_effect=HostError("timed out")) as call,
            patch("twincat_mcp.dispatch.get_shell_host", return_value=host),
        ):
            result, _ = run_shell_step("activate", {}, solution_path="s.sln")
            missing, _ = run_shell_step("activate", {})
        self.assertFalse(result["dispatched"])
        self.assertNotIn("outcomeUnknown", result)
        self.assertFalse(missing["dispatched"])
        self.assertEqual(["ensure-solution"], [c.args[0] for c in call.call_args_list])

    def test_restart_waits_for_its_own_ready_line(self):
        host = ShellHost(Path("unused.exe"))
        host._ready_info = {"type": "ready"}  # left by a worker that died without cleanup
        proc = Mock()
        proc.poll.return_value = None
        with (
            patch("twincat_mcp.host.subprocess.Popen", return_value=proc),
            patch("twincat_mcp.host.threading.Thread"),
            patch.object(ShellHost, "READY_TIMEOUT_SEC", 0.2),
        ):
            with self.assertRaises(HostError):
                host._start_locked()

    def test_confirmed_solution_is_cached(self):
        host = ShellHost(Path("unused.exe"))
        reply = {"loaded": True, "solutionPath": os.path.abspath("sample.sln")}
        with (
            patch.object(host, "is_alive", return_value=True),
            patch.object(host, "_call_raw_locked", return_value=reply) as call,
        ):
            host.ensure_solution("sample.sln", None)
            cached = host.ensure_solution("sample.sln", None)
        call.assert_called_once()
        self.assertTrue(cached["cached"])

    def test_restarted_host_forgets_cached_solution(self):
        host = ShellHost(Path("unused.exe"))
        host._current_solution = "sample.sln"
        host._current_tc_version = None
        with patch("twincat_mcp.host.subprocess.Popen", side_effect=OSError("no worker")):
            with self.assertRaises(HostError):
                host.ensure_solution("sample.sln", None)
        self.assertIsNone(host._current_solution)

    def test_explicit_executable_never_falls_back(self):
        with tempfile.TemporaryDirectory() as folder:
            exe = Path(folder) / "isolated.exe"
            with patch.dict(os.environ, TWINCAT_AUTOMATION_EXE=str(exe)):
                with self.assertRaises(FileNotFoundError):
                    find_tc_automation_exe()
                exe.touch()
                self.assertEqual(exe.resolve(), find_tc_automation_exe())

    def test_source_hash_and_online_counter_required(self):
        catalog = Catalog()
        for id, required in [
            ("engineering.edit_plc_source", "expectedSha256"),
            ("engineering.online_change", "expectedOnlineChangeCount"),
        ]:
            self.assertIn(required, catalog.get(id)["inputSchema"]["required"])
        self.assertIn(
            "configuration", catalog.get("engineering.matching_login")["inputSchema"]["required"]
        )
        self.assertIn(
            "platform", catalog.get("engineering.matching_login")["inputSchema"]["required"]
        )
        self.assertIn(
            "contextFile", catalog.get("engineering.online_change")["inputSchema"]["required"]
        )

    def test_online_change_receipt_preserves_dispatch_and_verification(self):
        backend = Backend()
        ctx = dict(solutionPath="sample.sln", amsNetId="1.2.3.4.1.1", plcName="PLC")
        with patch(
            "twincat_mcp.backend.run_shell_step",
            return_value=({"Success": False, "Dispatched": True, "RuntimeVerified": False}, []),
        ) as run:
            result = backend.run(Catalog().get("engineering.online_change"), {}, ctx)
        self.assertFalse(result["runtimeVerified"])
        self.assertTrue(result["dispatched"])
        self.assertEqual("1.2.3.4.1.1", run.call_args.args[1]["amsNetId"])

    def test_batch_variable_adapters_preserve_symbol_keys(self):
        backend = Backend()
        with patch(
            "twincat_mcp.backend.run_shell_step",
            return_value=({"Success": True, "Values": {"MAIN.X": "1"}}, []),
        ) as run:
            result = backend.run(
                Catalog().get("runtime.write_var_list"),
                dict(amsNetId="1.2.3.4.1.1", variables={"MAIN.X": "1"}),
            )
        self.assertEqual({"MAIN.X": "1"}, json.loads(run.call_args.args[1]["variables"]))
        self.assertEqual({"MAIN.X": "1"}, result["values"])

    def test_scope_status_does_not_start_process(self):
        session = ScopeSession()
        with patch("twincat_mcp.scope.subprocess.Popen") as proc:
            with self.assertRaises(RuntimeError):
                session.send_command({"command": "status"})
        proc.assert_not_called()

    def test_scope_stop_preserves_all_receipt_fields(self):
        backend = Backend()
        receipt = dict(
            success=True, dataPath="C:/trace.csv", elapsedSeconds=2.5, samplesCollected=42
        )
        backend.scope = Mock()
        backend.scope.send_command.return_value = receipt
        result = backend.run(Catalog().get("scope.stop"), dict(recordingHandle="record"))
        self.assertEqual(receipt, result)
        backend.scope.close.assert_called_once()

    def test_route_backend_uses_fixed_beckhoff_adapter_actions(self):
        backend = Backend()
        catalog = Catalog()

        with patch(
            "twincat_mcp.backend.run_route_action",
            return_value={"success": True, "routes": []},
        ) as run:
            backend.run(catalog.get("system.routes"), {})
            run.assert_called_once_with("Get", {})

        arguments = {
            "amsNetId": "1.2.3.4.1.1",
            "ipOrHostName": "1.2.3.4",
            "name": "test-target",
            "credentialPath": "C:/secure/target.credential.xml",
            "fingerprint": "a" * 64,
            "verifyPort": 10000,
            "unidirectional": False,
        }
        with patch(
            "twincat_mcp.backend.run_route_action",
            return_value={"success": True},
        ) as run:
            backend.run(catalog.get("system.route_upsert"), arguments)
            run.assert_called_once_with("Upsert", arguments)

    def test_route_adapter_parses_json_without_exposing_credential(self):
        receipt = {
            "success": True,
            "route": {"amsNetId": "1.2.3.4.1.1", "secure": True},
        }
        completed = Mock(returncode=0, stdout=json.dumps(receipt), stderr="")

        with tempfile.TemporaryDirectory() as folder:
            credential = Path(folder) / "credential.xml"
            credential.write_text("protected", encoding="utf-8")
            with (
                patch("twincat_mcp.routes._powershell_executable", return_value="powershell.exe"),
                patch("twincat_mcp.routes.subprocess.run", return_value=completed) as run,
            ):
                result = run_route_action(
                    "Upsert",
                    {
                        "credentialPath": str(credential.resolve()),
                        "amsNetId": "1.2.3.4.1.1",
                    },
                )

        self.assertEqual(receipt, result)
        invocation = run.call_args
        self.assertNotIn("protected", " ".join(invocation.args[0]))
        self.assertNotIn("credential.xml", completed.stdout)

    def test_route_adapter_does_not_replay_timeout(self):
        with (
            patch("twincat_mcp.routes._powershell_executable", return_value="powershell.exe"),
            patch(
                "twincat_mcp.routes.subprocess.run",
                side_effect=subprocess.TimeoutExpired("powershell", 1),
            ) as run,
        ):
            result = run_route_action("Remove", {"amsNetId": "1.2.3.4.1.1"}, 1)

        self.assertFalse(result["success"])
        self.assertTrue(result["outcomeUnknown"])
        run.assert_called_once()


class CatalogTests(unittest.TestCase):
    def test_discovery_is_bounded_and_deterministic(self):
        c = Catalog()
        self.assertEqual(c.search(""), c.search(""))
        self.assertEqual(8, len(c.search("")["matches"]))
        ids = []
        offset = 0
        while True:
            page = c.search("", offset=offset)
            ids.extend(e["operation"] for e in page["matches"])
            if page["nextOffset"] is None:
                break
            offset = page["nextOffset"]
        self.assertEqual(
            sum(e.get("available") is not False for e in c.entries.values()), len(set(ids))
        )

    def test_search_finds_workflows_and_granular_operations(self):
        c = Catalog()
        self.assertIn("workflow.test", [e["operation"] for e in c.search("tests")["matches"]])
        self.assertIn("engineering.build", [e["operation"] for e in c.search("compile")["matches"]])
        self.assertIn("workflow.sequence", [e["operation"] for e in c.search("batch")["matches"]])

    def test_catalog_has_no_implicit_target_defaults(self):
        for e in Catalog().entries.values():
            prop = e["inputSchema"]["properties"].get("amsNetId")
            if prop is not None and e["id"] != "safety.grant":
                self.assertIn("amsNetId", e["inputSchema"]["required"])
                self.assertNotIn("default", prop)

    def test_every_catalog_schema_is_strict(self):
        for e in Catalog().entries.values():
            self.assertFalse(e["inputSchema"]["additionalProperties"])

    def test_route_mutations_require_scoped_grant_and_confirmation(self):
        catalog = Catalog()
        for operation in ("system.route_upsert", "system.route_remove"):
            entry = catalog.get(operation)
            self.assertTrue(entry["requiresGrant"])
            self.assertFalse(entry["readOnly"])
            self.assertIn("amsNetId", entry["inputSchema"]["required"])
            self.assertIn("grantHandle", entry["inputSchema"]["required"])
            self.assertIn("confirm", entry["inputSchema"]["required"])

        with self.assertRaisesRegex(OperationError, "credentialPath"):
            catalog.validate(
                "system.route_remove",
                {
                    "amsNetId": "1.2.3.4.1.1",
                    "removeRemote": True,
                    "confirm": "CONFIRM",
                    "grantHandle": "grant",
                },
            )


class ListResultNormalizeTests(unittest.TestCase):
    def test_ordered_items_keep_order_duplicates_and_symbols(self):
        raw = {
            "Items": [
                {"Index": 0, "Symbol": "GVL.A", "Result": {"Success": True, "Value": "1"}},
                {"Index": 1, "Symbol": "MAIN.Cnt", "Result": {"Success": True, "Value": "2"}},
                {"Index": 2, "Symbol": "GVL.A", "Result": {"Success": False, "ErrorMessage": "x"}},
            ],
            "Results": {"GVL.A": {"Success": False}, "MAIN.Cnt": {"Success": True}},
            "SymbolCount": 3,
        }
        out = normalize(raw)
        self.assertEqual([i["symbol"] for i in out["items"]], ["GVL.A", "MAIN.Cnt", "GVL.A"])
        self.assertEqual([i["index"] for i in out["items"]], [0, 1, 2])
        self.assertEqual(out["items"][2]["result"], {"success": False, "errorMessage": "x"})
        self.assertEqual(set(out["results"]), {"GVL.A", "MAIN.Cnt"})
        self.assertEqual(out["symbolCount"], 3)


class ListWriteValueTests(unittest.TestCase):
    def test_backend_sends_json_numbers_and_booleans_to_the_worker(self):
        entry = Catalog().get("runtime.write_var_list")
        args = {"amsNetId": "192.0.2.1.1.1", "port": 851, "grantHandle": "g",
                "variables": {"GVL.n": 18446744073709551615, "GVL.r": 42.0, "GVL.b": True, "GVL.s": "3.14", "GVL.z": -0.0}}
        with patch("twincat_mcp.backend.run_shell_step", return_value=({"Success": True}, [])) as step:
            Backend().run(entry, dict(args), None)
        sent = step.call_args.args[1]["variables"]
        self.assertEqual(sent, '{"GVL.n": 18446744073709551615, "GVL.r": 42.0, "GVL.b": true, "GVL.s": "3.14", "GVL.z": -0.0}')
