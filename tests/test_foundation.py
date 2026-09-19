"""Acceptance tests on temporary synthetic projects; no credentials or API."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from directologist.contracts import ContractError, load_project
from directologist.storage import StateError, Store, restore


class FoundationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.profile = {"schema_version": 1, "project_id": "1pi-pro", "display_name": "Synthetic",
                        "timezone": "Europe/Moscow", "binding_version": 1,
                        "bindings": {"direct": {"connection_id": "fake-direct",
                                               "resources": {"campaign_ids": ["fake-1"]}}}}
        self.write_profile()
        # Any accidental direct socket use in this test process fails immediately.
        self.network = patch("socket.socket", side_effect=AssertionError("Network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def write_profile(self, data=None):
        data = self.profile if data is None else data
        folder = self.root / "projects" / data["project_id"]
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "profile.json").write_text(json.dumps(data), encoding="utf-8")
        return folder

    def context(self, name="1pi-pro"):
        return load_project(self.root, name)

    def cli(self, *args):
        # Run in-process CLI with socket/child-process guards also active in child.
        runner = "import sys,socket,subprocess; from unittest.mock import patch; sys.path.insert(0,sys.argv.pop(1)); from directologist.cli import main\nwith patch('socket.socket',side_effect=RuntimeError('offline')), patch('subprocess.Popen',side_effect=RuntimeError('no child')): raise SystemExit(main())"
        return subprocess.run([sys.executable, "-c", runner, str(ROOT / "scripts"),
                               "--workspace", str(self.root), "--project", "1pi-pro", *args],
                              capture_output=True, text=True, timeout=15)

    def test_profile_contract_rejects_unknown_and_secret_fields(self):
        for mutation in ({"schema_version": 2}, {"schema_version": True},
                         {"token": "SYNTHETIC-SENTINEL"}, {"binding_version": 0},
                         {"timezone": "../bad"}, {"bindings": {"other": {}}}):
            with self.subTest(mutation=list(mutation)):
                data = {**self.profile, **mutation}
                self.write_profile(data)
                with self.assertRaises(ContractError):
                    self.context()

    def test_malformed_json_does_not_echo_content(self):
        path = self.root / "projects/1pi-pro/profile.json"
        for content in ('{"x":1,"x":2}', '{"x":NaN}', 'SYNTHETIC-SENTINEL', '[]'):
            path.write_text(content)
            result = self.cli("validate")
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("SYNTHETIC-SENTINEL", result.stdout + result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def test_resource_contract(self):
        for value in ([], ["same", "same"], [1], "not-list", ["bad\n"]):
            data = copy.deepcopy(self.profile)
            data["bindings"]["direct"]["resources"]["campaign_ids"] = value
            self.write_profile(data)
            with self.assertRaises(ContractError):
                self.context()

    def test_project_traversal_mismatch_and_symlink(self):
        for name in ("../outside", "/tmp", "Upper", "", "a/b"):
            with self.assertRaises(ContractError):
                self.context(name)
        profile_path = self.root / "projects/1pi-pro/profile.json"
        data = {**self.profile, "project_id": "different"}
        profile_path.write_text(json.dumps(data))
        with self.assertRaises(ContractError):
            self.context()
        profile_path.unlink()
        target = self.root / "outside.json"
        target.write_text(json.dumps(self.profile))
        profile_path.symlink_to(target)
        with self.assertRaises(ContractError):
            self.context()

    def test_context_is_immutable_and_changes_are_detected(self):
        old = self.context()
        old.profile["bindings"].clear()
        self.assertTrue(old.profile["bindings"])
        with Store(old, create=True):
            pass
        self.profile["bindings"]["direct"]["resources"]["campaign_ids"] = ["fake-2"]
        self.write_profile()
        with self.assertRaises(StateError):
            Store(self.context())

    def test_two_projects_are_isolated(self):
        other = copy.deepcopy(self.profile)
        other["project_id"] = "second"
        other["bindings"]["direct"]["resources"]["campaign_ids"] = ["fake-other"]
        self.write_profile(other)
        first, second = self.context(), self.context("second")
        with Store(first, create=True) as a, Store(second, create=True) as b:
            one = a.create_run("same-request", "analysis", ["collect"])
            two = b.create_run("same-request", "analysis", ["collect"])
            self.assertNotEqual(one["run_id"], two["run_id"])
            with self.assertRaises(StateError):
                b.run(one["run_id"])
        second.database.unlink()
        shutil.copyfile(first.database, second.database)
        with self.assertRaises(StateError):
            Store(second)

    def test_status_does_not_initialize(self):
        result = self.cli("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["result"]["initialized"])
        self.assertFalse(self.context().database.exists())
        self.assertEqual(self.cli("validate").returncode, 0)

    def test_init_does_not_overwrite_corrupt_or_unknown_database(self):
        ctx = self.context()
        ctx.database.write_bytes(b"synthetic malformed database")
        before = ctx.database.read_bytes()
        self.assertNotEqual(self.cli("init").returncode, 0)
        self.assertEqual(ctx.database.read_bytes(), before)
        ctx.database.unlink()
        with Store(ctx, create=True) as store:
            store.connection.execute("UPDATE metadata SET value='999' WHERE key='schema_version'")
        with self.assertRaises(StateError):
            Store(ctx, create=True)

    def test_database_and_journal_symlinks_rejected(self):
        ctx = self.context()
        outside = self.root / "outside"
        outside.write_bytes(b"untouched")
        for suffix in ("", "-journal", "-wal", "-shm"):
            path = Path(str(ctx.directory / "state.sqlite3") + suffix)
            path.symlink_to(outside)
            with self.assertRaises(ContractError):
                Store(ctx, create=True)
            path.unlink()
        self.assertEqual(outside.read_bytes(), b"untouched")

    def test_idempotency_and_conflicting_request(self):
        with Store(self.context(), create=True) as store:
            run = store.create_run("request", "analysis", ["collect", "analyze"])
            same = store.create_run("request", "analysis", ["collect", "analyze"])
            self.assertEqual(run, same)
            self.assertEqual(store.connection.execute("SELECT count(*) FROM events").fetchone()[0], 1)
            for kind, steps in (("planning", ["collect", "analyze"]), ("analysis", ["analyze", "collect"])):
                with self.assertRaises(StateError):
                    store.create_run("request", kind, steps)

    def test_concurrent_duplicate_request_creates_one_run(self):
        ctx = self.context()
        with Store(ctx, create=True):
            pass
        def create(_):
            with Store(ctx) as store:
                return store.create_run("request", "analysis", ["collect"])["run_id"]
        with ThreadPoolExecutor(max_workers=4) as pool:
            runs = list(pool.map(create, range(8)))
        self.assertEqual(len(set(runs)), 1)
        with Store(ctx) as store:
            self.assertEqual(store.connection.execute("SELECT count(*) FROM events").fetchone()[0], 1)

    def test_step_order_completion_and_repeated_calls(self):
        with Store(self.context(), create=True) as store:
            rid = store.create_run("request", "analysis", ["collect", "analyze"])["run_id"]
            with self.assertRaises(StateError):
                store.change_step(rid, "collect", "STARTED")
            store.start_run(rid)
            with self.assertRaises(StateError):
                store.change_step(rid, "analyze", "STARTED")
            with self.assertRaises(StateError):
                store.finish_run(rid)
            for step in ("collect", "analyze"):
                store.change_step(rid, step, "STARTED")
                store.change_step(rid, step, "STARTED")
                store.change_step(rid, step, "COMPLETE")
            self.assertEqual(store.finish_run(rid)["status"], "COMPLETE")
            store.finish_run(rid)
            self.assertEqual(store.connection.execute("SELECT count(*) FROM events").fetchone()[0], 7)

    def test_abrupt_process_exit_preserves_started_step(self):
        with Store(self.context(), create=True) as store:
            rid = store.create_run("request", "analysis", ["collect"])["run_id"]
        code = "import os,sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); from directologist.contracts import load_project; from directologist.storage import Store; s=Store(load_project(Path(sys.argv[2]),'1pi-pro')); s.start_run(sys.argv[3]); s.change_step(sys.argv[3],'collect','STARTED'); os._exit(17)"
        result = subprocess.run([sys.executable, "-c", code, str(ROOT / "scripts"), str(self.root), rid], timeout=15)
        self.assertEqual(result.returncode, 17)
        with Store(self.context()) as store:
            run = store.create_run("request", "analysis", ["collect"])
            self.assertEqual(run["run_id"], rid)
            self.assertEqual(run["steps"][0]["status"], "STARTED")
            self.assertTrue(run["requires_reconciliation"])
            store.change_step(rid, "collect", "UNKNOWN")
            with self.assertRaises(StateError):
                store.change_step(rid, "collect", "STARTED")
            with self.assertRaises(StateError):
                store.change_step(rid, "collect", "COMPLETE")

    def test_failed_transaction_rolls_back(self):
        with Store(self.context(), create=True) as store:
            with self.assertRaises(RuntimeError):
                with store.transaction():
                    store.connection.execute("UPDATE metadata SET value='999' WHERE key='schema_version'")
                    raise RuntimeError("synthetic failure")
            self.assertEqual(store.connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0], "1")

    def make_backup(self):
        ctx = self.context()
        with Store(ctx, create=True) as store:
            run = store.create_run("request", "analysis", ["collect"])
            backup = store.backup()
            store.start_run(run["run_id"])
        return ctx, run, backup

    def test_backup_snapshot_restore_and_no_overwrite(self):
        ctx, run, backup = self.make_backup()
        path = ctx.directory / "backups" / backup["backup_id"] / "state.sqlite3"
        original_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaises(StateError):
            restore(ctx, backup["backup_id"])
        ctx.database.unlink()  # Synthetic database only.
        result = restore(ctx, backup["backup_id"])
        self.assertTrue(result["restored_requires_review"])
        self.assertFalse(result["autonomous_writes"])
        with Store(ctx) as store:
            self.assertEqual(store.run(run["run_id"])["status"], "PENDING")
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), original_hash)
        if sys.platform != "win32":
            self.assertEqual(ctx.database.stat().st_mode & 0o777, 0o600)

    def test_restore_rejects_corruption_and_foreign_manifest(self):
        ctx, _, backup = self.make_backup()
        ctx.database.unlink()
        folder = ctx.directory / "backups" / backup["backup_id"]
        manifest = json.loads((folder / "manifest.json").read_text())
        altered = {**manifest, "project_id": "foreign"}
        (folder / "manifest.json").write_text(json.dumps(altered))
        with self.assertRaises(StateError):
            restore(ctx, backup["backup_id"])
        (folder / "manifest.json").write_text(json.dumps(manifest))
        with (folder / "state.sqlite3").open("ab") as stream:
            stream.write(b"changed")
        with self.assertRaises(StateError):
            restore(ctx, backup["backup_id"])
        self.assertFalse(ctx.database.exists())

    def test_restore_race_does_not_replace_new_database(self):
        ctx, _, backup = self.make_backup()
        ctx.database.unlink()
        real_link = os.link
        def race(source, target):
            Path(target).write_bytes(b"synthetic new owner")
            return real_link(source, target)
        with patch("directologist.storage.os.link", side_effect=race):
            with self.assertRaises(FileExistsError):
                restore(ctx, backup["backup_id"])
        self.assertEqual(ctx.database.read_bytes(), b"synthetic new owner")
        self.assertEqual(list(ctx.directory.glob(".restore-*")), [])

    def test_backup_path_symlink_rejected(self):
        ctx = self.context()
        outside = self.root / "outside"
        outside.mkdir()
        (ctx.directory / "backups").symlink_to(outside, target_is_directory=True)
        with Store(ctx, create=True) as store:
            with self.assertRaises(ContractError):
                store.backup()
        self.assertEqual(list(outside.iterdir()), [])

    def test_read_only_status_does_not_modify_database(self):
        ctx = self.context()
        with Store(ctx, create=True):
            pass
        before = ctx.database.read_bytes()
        with Store(ctx, read_only=True) as store:
            self.assertFalse(store.status()["autonomous_writes"])
            with self.assertRaises(StateError):
                store.create_run("request", "analysis", ["collect"])
        self.assertEqual(before, ctx.database.read_bytes())

    def test_cli_rejects_accidental_secret_and_has_no_apply(self):
        for args in (("apply", "SYNTHETIC-SENTINEL"), ("init", "--token", "SYNTHETIC-SENTINEL")):
            result = self.cli(*args)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("SYNTHETIC-SENTINEL", result.stdout + result.stderr)

    def test_cli_end_to_end(self):
        self.assertEqual(self.cli("init").returncode, 0)
        result = self.cli("run", "create", "--request-id", "request", "--kind", "analysis", "--step", "collect")
        self.assertEqual(result.returncode, 0, result.stderr)
        rid = json.loads(result.stdout)["result"]["run_id"]
        for args in (("run", "start", "--run-id", rid),
                     ("step", "start", "--run-id", rid, "--step-id", "collect"),
                     ("step", "finish", "--run-id", rid, "--step-id", "collect"),
                     ("run", "finish", "--run-id", rid), ("backup",)):
            result = self.cli(*args)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
