import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import os


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
SCRIPT = ROOT / "scripts" / "stage_v1_source_release.py"
spec = importlib.util.spec_from_file_location("source_release_transaction", SCRIPT)
module = importlib.util.module_from_spec(spec); assert spec.loader; spec.loader.exec_module(module)


class SourceReleaseTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.candidate = self.root / "candidate"; self.candidate.mkdir()
        self.archive = b"archive"
        self.manifest = {"source": {"tracked_file_count": 1, "files": []}}
        self.verified = {"revision": "1" * 40, "tree": "2" * 40,
                         "manifest_sha256": "3" * 64, "archive_sha256": "4" * 64,
                         "production_qualified": True}

    def tearDown(self):
        self.temporary.cleanup()

    def test_plan_is_deterministic_and_service_free(self):
        with mock.patch.object(module, "candidate_identity", return_value=(self.manifest, self.archive, self.verified)):
            first = module.plan(self.candidate, "3" * 64, self.root, qualification_lab=True)
            second = module.plan(self.candidate, "3" * 64, self.root, qualification_lab=True)
        self.assertEqual(first, second)
        self.assertRegex(first["plan_id"], r"^[a-f0-9]{64}$")
        self.assertEqual(first["service_mutations"], 0)

    def test_qualification_and_production_plans_cannot_share_a_plan_id(self):
        with mock.patch.object(module, "candidate_identity", return_value=(self.manifest, self.archive, self.verified)) as identity:
            lab = module.plan(self.candidate, "3" * 64, self.root, qualification_lab=True)
            production = module.plan(self.candidate, "3" * 64, self.root, qualification_lab=False)
        self.assertNotEqual(lab["plan_id"], production["plan_id"])
        self.assertEqual(lab["identity"]["scope"], "qualification-lab")
        self.assertEqual(production["identity"]["scope"], "production")
        self.assertEqual([call.kwargs["require_production"] for call in identity.call_args_list], [False, True])

    def test_different_service_transactions_cannot_share_a_source_plan(self):
        with mock.patch.object(module, "candidate_identity", return_value=(self.manifest, self.archive, self.verified)):
            first = module.plan(self.candidate, "3" * 64, self.root, qualification_lab=False,
                                owner_plan_id="a" * 64)
            second = module.plan(self.candidate, "3" * 64, self.root, qualification_lab=False,
                                 owner_plan_id="b" * 64)
        self.assertNotEqual(first["plan_id"], second["plan_id"])
        self.assertEqual(first["identity"]["owner_plan_id"], "a" * 64)

    def test_plan_accepts_an_absent_target_under_a_safe_parent_without_creating_it(self):
        target = self.root / "not-created"
        self.assertEqual(module.install_root_path(target, create=False), target)
        self.assertFalse(target.exists())

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux mode semantics required")
    def test_new_private_subdirectory_is_independent_of_restrictive_umask(self):
        previous = os.umask(0o077)
        try:
            created = module.private_subdirectory(self.root, "source-releases", 0o755)
        finally:
            os.umask(previous)
        self.assertEqual(created.stat().st_mode & 0o777, 0o755)

    def test_apply_commits_release_and_recovery_restores_previous_pointer(self):
        previous = "source-releases/" + "a" * 40 + "-" + "b" * 12
        (self.root / "source-releases").mkdir(mode=0o755)
        os.chmod(self.root / "source-releases", 0o755)
        (self.root / "source-releases" / previous.split("/", 1)[1]).mkdir()
        pointer = {"value": previous}
        get_pointer = lambda _root: pointer["value"]
        def set_pointer(_root, value): pointer["value"] = value
        planned = {"plan_id": "5" * 64, "release_name": "1" * 40 + "-" + "3" * 12,
                   "scope": "qualification-lab"}
        def extract(_manifest, _archive, staging):
            (staging / "README").write_text("trusted", encoding="utf-8")
        with mock.patch.object(module, "plan", return_value=planned), \
                mock.patch.object(module, "candidate_identity", return_value=(self.manifest, self.archive, self.verified)), \
                mock.patch.object(module, "extract_exact", side_effect=extract), \
                mock.patch.object(module, "pointer_target", side_effect=get_pointer), \
                mock.patch.object(module, "replace_pointer", side_effect=set_pointer):
            result = module.apply(self.candidate, "3" * 64, self.root, "5" * 64, qualification_lab=True)
        self.assertEqual(result["status"], "committed")
        self.assertEqual(pointer["value"], "source-releases/" + planned["release_name"])
        self.assertFalse((self.root / ".transactions/source-install.pending.json").exists())

        pending = {"schema_version": 1, "plan_id": "6" * 64, "owner_plan_id": None,
                   "scope": "qualification-lab", "phase": "pointer_committed",
                   "release_name": planned["release_name"], "previous_pointer": previous,
                   "new_pointer": "source-releases/" + planned["release_name"]}
        module.atomic_json(self.root / ".transactions/source-install.pending.json", pending)
        with mock.patch.object(module, "pointer_target", side_effect=get_pointer), \
                mock.patch.object(module, "replace_pointer", side_effect=set_pointer):
            recovered = module.recover(self.root, "6" * 64)
        self.assertEqual(recovered["status"], "rollback_verified")
        self.assertEqual(pointer["value"], previous)

    def test_recovery_refuses_external_pointer_change(self):
        transactions = self.root / ".transactions"; transactions.mkdir(mode=0o700)
        other = "source-releases/" + "c" * 40 + "-" + "d" * 12
        releases = self.root / "source-releases"; releases.mkdir(mode=0o755)
        os.chmod(releases, 0o755)
        (releases / other.split("/", 1)[1]).mkdir()
        pending = {"schema_version": 1, "plan_id": "7" * 64, "owner_plan_id": None,
                   "scope": "qualification-lab", "phase": "prepared",
                   "release_name": "1" * 40 + "-" + "3" * 12,
                   "previous_pointer": None, "new_pointer": "source-releases/" + "1" * 40 + "-" + "3" * 12}
        module.atomic_json(transactions / "source-install.pending.json", pending)
        with mock.patch.object(module, "pointer_target", return_value=other), \
                self.assertRaisesRegex(module.TransactionError, "outside"):
            module.recover(self.root, "7" * 64)

    def test_commit_receipt_failure_leaves_recoverable_pending_marker(self):
        (self.root / "source-releases").mkdir(mode=0o755)
        os.chmod(self.root / "source-releases", 0o755)
        pointer = {"value": None}
        planned = {"plan_id": "8" * 64, "release_name": "1" * 40 + "-" + "3" * 12,
                   "scope": "qualification-lab"}
        original_atomic_json = module.atomic_json
        def atomic(path, value):
            if path.name.startswith("source-install.committed-"):
                raise OSError("simulated power loss before durable receipt")
            return original_atomic_json(path, value)
        def extract(_manifest, _archive, staging):
            (staging / "README").write_text("trusted", encoding="utf-8")
        with mock.patch.object(module, "plan", return_value=planned), \
                mock.patch.object(module, "candidate_identity", return_value=(self.manifest, self.archive, self.verified)), \
                mock.patch.object(module, "extract_exact", side_effect=extract), \
                mock.patch.object(module, "pointer_target", side_effect=lambda _root: pointer["value"]), \
                mock.patch.object(module, "replace_pointer", side_effect=lambda _root, value: pointer.update(value=value)), \
                mock.patch.object(module, "atomic_json", side_effect=atomic), \
                self.assertRaisesRegex(OSError, "power loss"):
            module.apply(self.candidate, "3" * 64, self.root, "8" * 64, qualification_lab=True)
        pending = self.root / ".transactions/source-install.pending.json"
        self.assertTrue(pending.is_file())
        self.assertEqual(json.loads(pending.read_text())["phase"], "pointer_committed")
        with mock.patch.object(module, "pointer_target", side_effect=lambda _root: pointer["value"]), \
                mock.patch.object(module, "replace_pointer", side_effect=lambda _root, value: pointer.update(value=value)):
            recovered = module.recover(self.root, "8" * 64)
        self.assertEqual(recovered["status"], "rollback_verified")
        self.assertIsNone(pointer["value"])

    def test_apply_refuses_to_overwrite_a_terminal_receipt(self):
        transactions = self.root / ".transactions"; transactions.mkdir(mode=0o700)
        module.atomic_json(transactions / ("source-install.committed-" + "b" * 64 + ".json"),
                           {"evidence": "must-not-be-overwritten"})
        (self.root / "source-releases").mkdir(mode=0o755)
        os.chmod(self.root / "source-releases", 0o755)
        planned = {"plan_id": "b" * 64, "release_name": "1" * 40 + "-" + "3" * 12,
                   "scope": "production"}
        with mock.patch.object(module, "plan", return_value=planned), \
                self.assertRaisesRegex(module.TransactionError, "terminal receipt"):
            module.apply(self.candidate, "3" * 64, self.root, "b" * 64,
                         qualification_lab=False, owner_plan_id="0" * 64)

    def test_recovery_finishes_a_durably_committed_transaction(self):
        transactions = self.root / ".transactions"; transactions.mkdir(mode=0o700)
        new_pointer = "source-releases/" + "1" * 40 + "-" + "3" * 12
        row = {"schema_version": 1, "plan_id": "9" * 64, "owner_plan_id": None,
               "scope": "qualification-lab", "phase": "pointer_committed",
               "release_name": "1" * 40 + "-" + "3" * 12,
               "previous_pointer": None, "new_pointer": new_pointer}
        module.atomic_json(transactions / "source-install.pending.json", row)
        module.atomic_json(transactions / ("source-install.committed-" + "9" * 64 + ".json"),
                           {**row, "status": "committed"})
        with mock.patch.object(module, "pointer_target", return_value=new_pointer):
            result = module.recover(self.root, "9" * 64)
        self.assertEqual(result["status"], "commit_recovered")
        self.assertFalse((transactions / "source-install.pending.json").exists())

    def test_explicit_rollback_of_committed_release_is_idempotent(self):
        transactions = self.root / ".transactions"; transactions.mkdir(mode=0o700)
        previous = "source-releases/" + "a" * 40 + "-" + "b" * 12
        new_pointer = "source-releases/" + "1" * 40 + "-" + "3" * 12
        row = {"schema_version": 1, "plan_id": "c" * 64, "owner_plan_id": "0" * 64, "scope": "production",
               "phase": "pointer_committed", "release_name": "1" * 40 + "-" + "3" * 12,
               "previous_pointer": previous, "new_pointer": new_pointer, "status": "committed"}
        module.atomic_json(transactions / ("source-install.committed-" + "c" * 64 + ".json"), row)
        pointer = {"value": new_pointer}
        with mock.patch.object(module, "pointer_target", side_effect=lambda _root: pointer["value"]), \
                mock.patch.object(module, "replace_pointer", side_effect=lambda _root, value: pointer.update(value=value)):
            first = module.rollback_committed(self.root, "c" * 64)
            second = module.rollback_committed(self.root, "c" * 64)
        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        self.assertEqual(pointer["value"], previous)
        self.assertFalse((transactions / "source-rollback.pending.json").exists())

    def test_committed_rollback_refuses_an_external_pointer_change(self):
        transactions = self.root / ".transactions"; transactions.mkdir(mode=0o700)
        new_pointer = "source-releases/" + "1" * 40 + "-" + "3" * 12
        row = {"schema_version": 1, "plan_id": "d" * 64, "owner_plan_id": "0" * 64, "scope": "production",
               "phase": "pointer_committed", "release_name": "1" * 40 + "-" + "3" * 12,
               "previous_pointer": None, "new_pointer": new_pointer, "status": "committed"}
        module.atomic_json(transactions / ("source-install.committed-" + "d" * 64 + ".json"), row)
        external = "source-releases/" + "e" * 40 + "-" + "f" * 12
        with mock.patch.object(module, "pointer_target", return_value=external), \
                self.assertRaisesRegex(module.TransactionError, "no longer owns"):
            module.rollback_committed(self.root, "d" * 64)
        self.assertFalse((transactions / "source-rollback.pending.json").exists())

    def test_committed_rollback_recovers_after_pointer_swap(self):
        transactions = self.root / ".transactions"; transactions.mkdir(mode=0o700)
        new_pointer = "source-releases/" + "1" * 40 + "-" + "3" * 12
        row = {"schema_version": 1, "plan_id": "e" * 64, "owner_plan_id": "0" * 64,
               "scope": "qualification-lab",
               "phase": "pointer_committed", "release_name": "1" * 40 + "-" + "3" * 12,
               "previous_pointer": None, "new_pointer": new_pointer, "status": "committed"}
        module.atomic_json(transactions / ("source-install.committed-" + "e" * 64 + ".json"), row)
        pointer = {"value": new_pointer}
        original_atomic_json = module.atomic_json
        def fail_terminal(path, value):
            if path.name.startswith("source-install.committed-rollback-"):
                raise OSError("simulated power loss after pointer rollback")
            return original_atomic_json(path, value)
        with mock.patch.object(module, "pointer_target", side_effect=lambda _root: pointer["value"]), \
                mock.patch.object(module, "replace_pointer", side_effect=lambda _root, value: pointer.update(value=value)), \
                mock.patch.object(module, "atomic_json", side_effect=fail_terminal), \
                self.assertRaisesRegex(OSError, "power loss"):
            module.rollback_committed(self.root, "e" * 64)
        self.assertIsNone(pointer["value"])
        self.assertTrue((transactions / "source-rollback.pending.json").is_file())
        with mock.patch.object(module, "pointer_target", side_effect=lambda _root: pointer["value"]), \
                mock.patch.object(module, "replace_pointer", side_effect=lambda _root, value: pointer.update(value=value)):
            result = module.rollback_committed(self.root, "e" * 64)
        self.assertEqual(result["status"], "rollback_verified")
        self.assertFalse((transactions / "source-rollback.pending.json").exists())

    def test_owned_rollback_resolves_a_durable_commit_before_rolling_it_back(self):
        transactions = self.root / ".transactions"; transactions.mkdir(mode=0o700)
        new_pointer = "source-releases/" + "1" * 40 + "-" + "3" * 12
        row = {"schema_version": 1, "plan_id": "f" * 64, "owner_plan_id": "0" * 64, "scope": "production",
               "phase": "pointer_committed", "release_name": "1" * 40 + "-" + "3" * 12,
               "previous_pointer": None, "new_pointer": new_pointer}
        module.atomic_json(transactions / "source-install.pending.json", row)
        module.atomic_json(transactions / ("source-install.committed-" + "f" * 64 + ".json"),
                           {**row, "status": "committed"})
        pointer = {"value": new_pointer}
        with mock.patch.object(module, "pointer_target", side_effect=lambda _root: pointer["value"]), \
                mock.patch.object(module, "replace_pointer", side_effect=lambda _root, value: pointer.update(value=value)):
            result = module.rollback_owned(self.root, "f" * 64)
        self.assertEqual(result["status"], "rollback_verified")
        self.assertIsNone(pointer["value"])
        self.assertFalse((transactions / "source-install.pending.json").exists())

    def test_owned_rollback_reverifies_a_previous_interrupted_apply_rollback(self):
        transactions = self.root / ".transactions"; transactions.mkdir(mode=0o700)
        previous = "source-releases/" + "a" * 40 + "-" + "b" * 12
        row = {"schema_version": 1, "plan_id": "a" * 64, "owner_plan_id": "0" * 64,
               "scope": "qualification-lab",
               "phase": "rollback_verified", "release_name": "1" * 40 + "-" + "3" * 12,
               "previous_pointer": previous,
               "new_pointer": "source-releases/" + "1" * 40 + "-" + "3" * 12,
               "status": "rolled_back"}
        module.atomic_json(transactions / ("source-install.rollback-" + "a" * 64 + ".json"), row)
        with mock.patch.object(module, "pointer_target", return_value=previous):
            result = module.rollback_owned(self.root, "a" * 64)
        self.assertFalse(result["changed"])
        with mock.patch.object(module, "pointer_target", return_value=row["new_pointer"]), \
                self.assertRaisesRegex(module.TransactionError, "pointer differs"):
            module.rollback_owned(self.root, "a" * 64)


if __name__ == "__main__":
    unittest.main()
