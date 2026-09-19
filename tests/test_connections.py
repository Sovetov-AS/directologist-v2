"""No real credentials/network/Keychain: unit/integration tests use fixtures; PTY uses random synthetic input."""
import copy
import io
import json
import os
from pathlib import Path
try:
    import pty
except ImportError:
    pty = None
import select
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import uuid
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from directologist import cli
from directologist.adapters import Adapter, Probe, ProbeError, Transport, NoRedirect, origin
from directologist.contracts import ContractError, load_project
from directologist.secrets import Credential, SecretStore, SecretError
from directologist.setup import (configure, commit_connection, wizard, Terminal, PublicationPending,
                                  recover_connections, connection_status, setup_lock)
from directologist.storage import Store, StateError


class MemorySecrets:
    def __init__(self, project_id):
        self.project_id = project_id
        self.items = {}

    def put(self, provider, connection_id, credential):
        self.items[provider, connection_id] = credential

    def delete(self, provider, connection_id):
        self.items.pop((provider, connection_id), None)


class QueueTransport:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def request(self, url, credential, **kwargs):
        self.calls.append((url, kwargs))
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return value


class ConnectionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        folder = self.root / "projects/fixture"
        folder.mkdir(parents=True)
        self.profile = {"schema_version": 1, "project_id": "fixture", "display_name": "Fixture",
                        "timezone": "Europe/Moscow", "binding_version": 0, "bindings": {}}
        (folder / "profile.json").write_text(json.dumps(self.profile))
        self.ctx = load_project(self.root, "fixture")
        self.secret = Credential(uuid.uuid4().hex + uuid.uuid4().hex)
        self.secrets = MemorySecrets("fixture")
        self.net = patch("socket.socket", side_effect=AssertionError("Network forbidden"))
        self.net.start()
        self.addCleanup(self.net.stop)

    def configure(self, provider="metrika", probe=None, config=None):
        adapter = Mock()
        adapter.probe.return_value = probe or Probe("CHECKED", {"counter_id": "12", "goal_ids": ["34"]})
        return configure(self.ctx, provider, self.secret, config or {}, lambda label, options, multi: options[-1:], self.secrets, adapter)

    def test_credential_repr_never_contains_value(self):
        self.assertNotIn(self.secret.value, str(self.secret))
        for invalid in ("", "abc\ndef", "abc def", "я", "x" * 8193):
            with self.assertRaises(ContractError):
                Credential(invalid)

    def test_no_tty_fails_before_keychain_and_leaves_no_database(self):
        with patch("sys.stdin.isatty", return_value=False), patch("directologist.setup.SecretStore") as store:
            with self.assertRaisesRegex(ContractError, "TTY"):
                wizard(self.ctx)
            store.assert_not_called()
        self.assertFalse(self.ctx.database.exists())

    def test_getpass_warning_never_falls_back(self):
        with patch("sys.stdin.isatty", return_value=True), patch("sys.stdout.isatty", return_value=True):
            term = Terminal()
        import getpass
        with patch("getpass.getpass", side_effect=getpass.GetPassWarning):
            with self.assertRaises(ContractError):
                term.secret()

    def test_explicit_selection_even_for_one_resource(self):
        terminal = object.__new__(Terminal)
        terminal.say = Mock()
        terminal.ask = Mock(return_value="")
        with self.assertRaises(ContractError):
            terminal.choose("One", ["12"], False)
        terminal.ask.return_value = "2,1"
        self.assertEqual(terminal.choose("Two", ["12", "34"], True), ["34", "12"])
        terminal.ask.return_value = "1,1"
        with self.assertRaises(ContractError):
            terminal.choose("Two", ["12", "34"], True)

    def test_keychain_namespace_and_sanitized_errors(self):
        # Construct only wrapper with fake backend; native acceptance is opt-in separate script.
        first = object.__new__(SecretStore)
        first.project_id = "fixture"
        first._backend = Mock()
        first._backend.get_password.return_value = self.secret.value
        first.put("direct", "connection", self.secret)
        service, account, _ = first._backend.set_password.call_args.args
        self.assertEqual(service, "pro.1pi.directologist.v2/fixture/direct")
        second = object.__new__(SecretStore)
        second.project_id = "second"
        second._backend = first._backend
        self.assertNotEqual(first._key("direct", "connection"), second._key("direct", "connection"))
        first._backend.set_password.side_effect = RuntimeError(self.secret.value)
        with self.assertRaises(SecretError) as error:
            first.put("direct", "connection", self.secret)
        self.assertNotIn(self.secret.value, str(error.exception))

    def test_binding_bump_invalidates_previous_runs_and_open_store(self):
        old = Store(self.ctx, create=True)
        self.addCleanup(old.connection.close)
        rid = old.create_run("previous", "analysis", ["collect"])["run_id"]
        old.start_run(rid)
        self.ctx, _ = self.configure()
        self.assertEqual(self.ctx.profile["binding_version"], 1)
        with self.assertRaises(StateError):
            old.change_step(rid, "collect", "STARTED")
        with self.assertRaises(StateError):
            old.backup()
        with Store(self.ctx) as fresh:
            self.assertTrue(fresh.run(rid)["context_stale"])
            for action in (lambda: fresh.start_run(rid), lambda: fresh.finish_run(rid),
                           lambda: fresh.create_run("previous", "analysis", ["collect"])):
                with self.assertRaises(StateError):
                    action()
            fresh.create_run("new", "analysis", ["collect"])

    def test_atomic_publication_recovery_keeps_secret_and_rejects_stale_context(self):
        with patch("directologist.setup.publish_profile", side_effect=OSError("synthetic")):
            with self.assertRaises(PublicationPending):
                self.configure()
        self.assertEqual(len(self.secrets.items), 1)
        self.assertEqual(load_project(self.root, "fixture").profile["binding_version"], 0)
        with self.assertRaises(StateError):
            Store(self.ctx)
        result = recover_connections(self.ctx)
        self.assertEqual(result["binding_version"], 1)
        with Store(load_project(self.root, "fixture")) as store:
            self.assertEqual(len(store.status()["runs"]), 0)

    def test_recovery_does_not_overwrite_manual_changes(self):
        with patch("directologist.setup.publish_profile", side_effect=OSError):
            with self.assertRaises(PublicationPending):
                self.configure()
        changed = {**self.profile, "display_name": "Owner edit"}
        path = self.ctx.directory / "profile.json"
        path.write_text(json.dumps(changed))
        before = path.read_bytes()
        with self.assertRaises(ContractError):
            recover_connections(load_project(self.root, "fixture"))
        self.assertEqual(before, path.read_bytes())

    def test_other_project_secret_store_rejected_before_save(self):
        self.secrets.project_id = "second"
        with self.assertRaises(ContractError):
            self.configure()
        self.assertEqual(self.secrets.items, {})

    def test_failure_of_one_provider_preserves_successful_other(self):
        self.ctx, _ = self.configure()
        self.ctx, state = self.configure("direct", Probe("INVALID_CREDENTIAL"), {"client_login": ""})
        self.assertEqual(state, "INVALID_CREDENTIAL")
        self.assertIn("metrika", self.ctx.profile["bindings"])
        self.assertNotIn("direct", self.ctx.profile["bindings"])
        states = {r["provider"]: r["state"] for r in connection_status(self.ctx)["connections"]}
        self.assertEqual(states["metrika"], "CHECKED")

    def test_secret_absent_from_project_files_and_public_status(self):
        self.ctx, _ = self.configure()
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(self.secret.value.encode(), path.read_bytes())
        self.assertNotIn(self.secret.value, json.dumps(connection_status(self.ctx)))

    def test_reflected_secret_in_resource_is_rejected_and_cleaned(self):
        with self.assertRaises(ProbeError):
            self.configure(probe=Probe("CHECKED", {"counter_id": self.secret.value, "goal_ids": ["3"]}))
        self.assertEqual(self.secrets.items, {})
        self.assertFalse(self.ctx.database.exists())

    def test_wordstat_deferred_without_transport_call(self):
        transport = Mock()
        result = Adapter(transport).probe("wordstat", self.secret, {"folder_id": "fixture"}, Mock())
        self.assertEqual(result.state, "AWAITING_COST_APPROVAL")
        transport.request.assert_not_called()

    def test_direct_error_codes_are_distinct(self):
        cases = {53: "INVALID_CREDENTIAL", 54: "FORBIDDEN", 58: "FORBIDDEN", 152: "QUOTA", 52: "NETWORK", 9999: "RESPONSE_ERROR"}
        for code, expected in cases.items():
            with self.subTest(code=code):
                transport = QueueTransport([{"error": {"error_code": code, "error_string": self.secret.value}}])
                result = Adapter(transport).probe("direct", self.secret, {}, Mock())
                self.assertEqual(result.state, expected)
                self.assertNotIn(self.secret.value, repr(result))

    def test_direct_pagination_and_account_choice(self):
        transport = QueueTransport([{"result": {"Clients": [{"Login": "first"}, {"Login": "second"}]}},
                                    {"result": {"Campaigns": [{"Id": 1}], "LimitedBy": 1}},
                                    {"result": {"Campaigns": [{"Id": 2}]}}])
        seen = []
        def choose(label, options, multiple):
            seen.append(options)
            return options[-1:]
        result = Adapter(transport).probe("direct", self.secret, {}, choose)
        self.assertEqual(result.resources, {"client_login": "second", "campaign_ids": ["2"]})
        self.assertEqual(transport.calls[1][1]["client_login"], "second")
        self.assertEqual(transport.calls[2][1]["body"]["params"]["Page"]["Offset"], 1)
        self.assertEqual(seen[1], ["1", "2"])

    def test_metrika_offsets_start_at_one_and_goals_follow_selected_counter(self):
        first_page = [{"id": index} for index in range(1, 1001)]
        transport = QueueTransport([{"rows": 1001, "counters": first_page},
                                    {"rows": 1001, "counters": [{"id": 1001}]}, {"goals": [{"id": 17}]}])
        result = Adapter(transport).probe("metrika", self.secret, {}, lambda label, options, multiple: options[-1:])
        self.assertEqual(result.resources, {"counter_id": "1001", "goal_ids": ["17"]})
        self.assertTrue(transport.calls[0][0].endswith("offset=1"))
        self.assertTrue(transport.calls[1][0].endswith("offset=1001"))
        self.assertTrue(transport.calls[2][0].endswith("counter/1001/goals"))

    def test_incomplete_pagination_is_not_ready(self):
        cases = [({"rows": 1, "counters": []}, "metrika"),
                 ({"result": {"Clients": []}}, "direct")]
        for response, provider in cases:
            result = Adapter(QueueTransport([response])).probe(provider, self.secret, {}, lambda *args: [])
            self.assertNotEqual(result.state, "CHECKED")

    def test_transport_http_errors_no_payload_leak_and_redirect_block(self):
        transport = Transport()
        for code, expected in ((401, "INVALID_CREDENTIAL"), (403, "FORBIDDEN"), (429, "QUOTA"), (503, "NETWORK"), (302, "RESPONSE_ERROR")):
            err = urllib.error.HTTPError("https://example.invalid/" + self.secret.value, code, self.secret.value, {}, io.BytesIO(self.secret.value.encode()))
            with patch.object(transport.opener, "open", side_effect=err):
                with self.assertRaises(ProbeError) as failure:
                    transport.request("https://api-metrika.yandex.net/management/v1/counters", self.secret)
                self.assertEqual(failure.exception.state, expected)
                self.assertNotIn(self.secret.value, str(failure.exception))
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.invalid"))

    def test_transport_rejects_write_unapproved_host_and_malformed_json(self):
        transport = Transport()
        with patch.object(transport.opener, "open") as opened:
            for url, body in (("https://evil.invalid/v1/portals", None),
                              ("https://api.direct.yandex.com/json/v5/campaigns", {"method": "update"})):
                with self.assertRaises(ProbeError):
                    transport.request(url, self.secret, body=body)
            opened.assert_not_called()
            opened.return_value.__enter__.return_value.read.return_value = b"not-json"
            with self.assertRaises(ProbeError) as failure:
                transport.request("https://api-metrika.yandex.net/management/v1/counters", self.secret)
            self.assertEqual(failure.exception.state, "RESPONSE_ERROR")

    def test_bridge_origin_constraints_and_missing_allowlist(self):
        for value in ("http://example.org", "https://user:pass@example.org", "https://example.org/?token=a", "https://example.org/path"):
            with self.assertRaises(ContractError):
                origin(value)
        with self.assertRaises(ContractError):
            self.configure("crm", Probe("CHECKED", {"bridge_id": "portal"}), {"origin": "https://example.org"})
        self.assertFalse(self.secrets.items)

    def test_setup_lock_prevents_second_wizard(self):
        with setup_lock(self.ctx):
            with self.assertRaises(ContractError):
                with setup_lock(self.ctx):
                    pass

    def test_invalid_resource_selection_does_not_replace_previous_binding(self):
        self.ctx, _ = self.configure()
        before = self.ctx.profile_json
        old_items = dict(self.secrets.items)
        adapter = Adapter(QueueTransport([{"rows": 1, "counters": [{"id": 99}]}]))
        with self.assertRaises(ContractError):
            configure(self.ctx, "metrika", self.secret, {}, lambda *args: ["not-listed"], self.secrets, adapter)
        self.assertEqual(load_project(self.root, "fixture").profile_json, before)
        self.assertEqual(self.secrets.items, old_items)

    def test_skip_retains_failed_saved_connection(self):
        self.ctx, _ = self.configure("direct", Probe("QUOTA"), {"client_login": ""})
        term = Mock()
        term.ask.return_value = ""
        result = wizard(self.ctx, term, self.secrets, Mock())
        self.assertEqual(result["connections"][0]["state"], "QUOTA")
        self.assertEqual(len(self.secrets.items), 1)

    def test_skip_unconfigured_providers_does_not_invalidate_runs(self):
        with Store(self.ctx, create=True) as store:
            rid = store.create_run("unchanged", "analysis", ["collect"])["run_id"]
        term = Mock()
        term.ask.return_value = ""
        result = wizard(self.ctx, term, self.secrets, Mock())
        self.assertEqual(result["binding_version"], 0)
        with Store(load_project(self.root, "fixture")) as store:
            self.assertEqual(store.start_run(rid)["status"], "RUNNING")

    def test_successful_bridge_selection_uses_only_allowlisted_origin(self):
        (self.ctx.directory / "allowed-services.json").write_text(json.dumps({"schema_version": 1, "crm_origins": ["https://bridge.example.org"]}))
        adapter = Adapter(QueueTransport([{"portals": [{"member_id": "first"}, {"member_id": "second"}]}]))
        self.ctx, state = configure(self.ctx, "crm", self.secret, {"origin": "https://bridge.example.org"},
                                    lambda label, choices, multi: choices[-1:], self.secrets, adapter)
        self.assertEqual(state, "CHECKED")
        self.assertEqual(self.ctx.profile["bindings"]["crm"]["resources"], {"bridge_id": "second"})

    def test_wizard_multiple_platforms_and_optional_skip(self):
        terminal = Mock()
        terminal.ask.side_effect = ["да", "", "да", "да", "Api-Key", "folder-test", ""]
        terminal.secret.side_effect = [self.secret, self.secret, self.secret]
        terminal.choose.side_effect = lambda label, options, multi: options[-1:]
        def factory(config):
            if "folder_id" in config:
                return Adapter(Mock())
            if "client_login" in config:
                return Adapter(QueueTransport([{"result": {"Clients": [{"Login": "fixture"}]}}, {"result": {"Campaigns": [{"Id": 1}]}}]))
            return Adapter(QueueTransport([{"rows": 1, "counters": [{"id": 12}]}, {"goals": [{"id": 34}]}]))
        result = wizard(self.ctx, terminal, self.secrets, factory)
        states = {r["provider"]: r["state"] for r in result["connections"]}
        self.assertEqual(states, {"direct": "CHECKED", "metrika": "CHECKED", "wordstat": "AWAITING_COST_APPROVAL", "crm": "SKIPPED_OPTIONAL"})
        self.assertEqual(len(self.secrets.items), 3)
        self.assertNotIn(self.secret.value, str(terminal.say.call_args_list))

    def test_cli_boundary_redacts_unexpected_exception(self):
        output = io.StringIO()
        with patch("directologist.cli.execute", side_effect=RuntimeError(self.secret.value)), redirect_stderr(output):
            code = cli.main([])
        self.assertNotEqual(code, 0)
        self.assertNotIn(self.secret.value, output.getvalue())

    @unittest.skipIf(pty is None, "POSIX PTY unavailable; native Windows terminal needs separate acceptance")
    def test_real_pty_secret_has_no_echo(self):
        master, slave = pty.openpty()
        code = "import sys; sys.path.insert(0,sys.argv[1]); from directologist.setup import Terminal; t=Terminal(); c=t.secret(); print('INPUT_ACCEPTED',flush=True)"
        child = subprocess.Popen([sys.executable, "-B", "-c", code, str(ROOT / "scripts")], stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        captured = bytearray()
        sent = False
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        chunk = os.read(master, 4096)
                    except OSError:
                        break
                    captured.extend(chunk)
                    if b"(\xd1\x81\xd0\xba\xd1\x80\xd1\x8b\xd1\x82\xd1\x8b\xd0\xb9" in captured and not sent:
                        os.write(master, (self.secret.value + "\n").encode())
                        sent = True
                if child.poll() is not None:
                    break
            self.assertEqual(child.wait(timeout=2), 0)
            self.assertTrue(sent)
            self.assertIn(b"INPUT_ACCEPTED", captured)
            self.assertNotIn(self.secret.value.encode(), captured)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            os.close(master)


if __name__ == "__main__":
    unittest.main()
