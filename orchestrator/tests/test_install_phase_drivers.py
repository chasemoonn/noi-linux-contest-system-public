import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
import sys
sys.path.insert(0, str(ROOT / "orchestrator"))

from services.install_phase_drivers import CleanFinalRollbackVerifier, CleanMaterialsDriver, ClosedFrontendDriver, ControllerDriver, ControllerQuiesceDriver, FinalRollbackVerifier, HydroIntegrationDriver, PostInstallVerificationDriver, SourceReleaseDriver, _trusted_executable
from services.install_transaction import InstallTransactionError, TransactionContext


TEMP_PARENT = "/root" if sys.platform.startswith("linux") and os.geteuid() == 0 else None


class TrustedExecutableTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith("linux") and os.geteuid() == 0,
                         "requires Linux root-owned path metadata")
    def test_preserves_a_validated_virtual_environment_entry_point(self):
        with tempfile.TemporaryDirectory(dir="/root") as raw:
            root = Path(raw)
            target = root / "python-real"
            target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            target.chmod(0o755)
            entry_point = root / "python"
            entry_point.symlink_to(target.name)

            trusted = _trusted_executable(entry_point)

            self.assertEqual(trusted, Path(os.path.abspath(entry_point)))
            self.assertNotEqual(trusted, trusted.resolve(strict=True))


class SourceReleaseDriverTests(unittest.TestCase):
    def driver(self, root: Path) -> SourceReleaseDriver:
        script = root / "stage.py"; script.write_text("pass\n", encoding="utf-8")
        return SourceReleaseDriver(
            candidate=root / "candidate", expected_manifest_sha256="1" * 64,
            install_root=root / "install", source_plan_id="2" * 64,
            transaction_script=script, python_executable=Path(__import__("sys").executable),
        )

    def context(self, root: Path) -> TransactionContext:
        return TransactionContext("3" * 64, "4" * 64, root)

    def test_apply_uses_exact_pins_and_returns_a_phase_receipt(self):
        with tempfile.TemporaryDirectory(dir=TEMP_PARENT) as raw:
            root = Path(raw); driver = self.driver(root)
            result = {"status": "committed", "changed": True, "plan_id": "2" * 64,
                      "release": "source-releases/" + "a" * 40 + "-" + "b" * 12,
                      "service_mutations": 0}
            with mock.patch("services.install_phase_drivers._run_json", return_value=result) as run:
                receipt = driver.apply(self.context(root))
            command = run.call_args.args[0]
            self.assertIn("--apply", command)
            self.assertEqual(command[command.index("--plan-id") + 1], "2" * 64)
            self.assertEqual(command[command.index("--expected-manifest-sha256") + 1], "1" * 64)
            self.assertEqual(receipt["phase"], "source_release")
            self.assertEqual(receipt["action"], "apply")
            self.assertRegex(receipt["evidence_sha256"], r"^[a-f0-9]{64}$")

    def test_uncertain_rollback_uses_owned_recovery(self):
        with tempfile.TemporaryDirectory(dir=TEMP_PARENT) as raw:
            root = Path(raw); driver = self.driver(root)
            result = {"status": "rollback_verified", "changed": False,
                      "plan_id": "2" * 64, "release": None, "service_mutations": 0}
            with mock.patch("services.install_phase_drivers._run_json", return_value=result) as run:
                receipt = driver.rollback(self.context(root), None)
            self.assertIn("--rollback-owned", run.call_args.args[0])
            self.assertEqual(receipt["action"], "rollback")

    def test_driver_rejects_malformed_subtransaction_evidence(self):
        with tempfile.TemporaryDirectory(dir=TEMP_PARENT) as raw:
            root = Path(raw); driver = self.driver(root)
            with mock.patch("services.install_phase_drivers._run_json",
                            return_value={"status": "committed"}), \
                    self.assertRaisesRegex(InstallTransactionError, "evidence differs"):
                driver.apply(self.context(root))


class HydroIntegrationDriverTests(unittest.TestCase):
    def driver(self, root: Path) -> HydroIntegrationDriver:
        release="a"*40+"-"+"b"*12; base=root/"source-releases"/release
        (base/"deploy").mkdir(parents=True); (base/"scripts").mkdir()
        for path in (base/"deploy/install-hydro-orchestrator-addon.sh",
                     base/"scripts/restore_v1_hydro_install_backup.py"):
            path.write_text("pass\n",encoding="utf-8")
        for path in (root/"python",root/"bash",root/"pm2",root/"node"):
            path.write_text("x",encoding="utf-8"); path.chmod(0o755)
        return HydroIntegrationDriver(install_root=root,source_release_name=release,
            backup_directory=root/"backup",python_executable=root/"python",bash_executable=root/"bash",
            pm2_bin=root/"pm2",node_bin=root/"node")

    def context(self, root: Path) -> TransactionContext:
        return TransactionContext("3"*64,"4"*64,root)

    def test_apply_uses_external_transaction_and_frozen_release(self):
        with tempfile.TemporaryDirectory(dir=TEMP_PARENT) as raw:
            root=Path(raw); driver=self.driver(root)
            result={"status":"verified","transaction":"external","source_release":driver.source_release_name,
                    "hydro":"online","routes":"submit-notify-problem-fileio-materials","other_pm2_mutations":0}
            with mock.patch("services.install_phase_drivers._run_json",return_value=result) as run:
                receipt=driver.apply(self.context(root))
            self.assertEqual(receipt["phase"],"hydro_integration")
            self.assertEqual(run.call_args.kwargs["extra_environment"]["EXTERNAL_INSTALL_TRANSACTION"],"1")
            self.assertIn(driver.source_release_name,run.call_args.kwargs["extra_environment"]["SOURCE_DIR"])

    def test_rollback_pins_backup_manifest_and_accepts_idempotence(self):
        with tempfile.TemporaryDirectory(dir=TEMP_PARENT) as raw:
            root=Path(raw); driver=self.driver(root)
            result={"status":"rollback_verified","plan_id":"3"*64,"backup_manifest_sha256":"4"*64,
                    "hydro":"online","other_pm2_mutations":0,"changed":False}
            with mock.patch("services.install_phase_drivers._run_json",return_value=result) as run:
                receipt=driver.rollback(self.context(root),None)
            command=run.call_args.args[0]
            self.assertEqual(command[command.index("--backup-manifest-sha256")+1],"4"*64)
            self.assertEqual(receipt["action"],"rollback")


class CleanMaterialsDriverTests(unittest.TestCase):
    def test_apply_and_rollback_accept_only_zero_service_mutation_receipts(self):
        with tempfile.TemporaryDirectory(dir=TEMP_PARENT) as raw:
            root=Path(raw);release="a"*40+"-"+"b"*12;base=root/"source-releases"/release/"scripts"
            base.mkdir(parents=True);(base/"prepare_v1_clean_install_materials.py").write_text("pass\n")
            python=root/"python";python.write_text("x");python.chmod(0o755)
            driver=CleanMaterialsDriver(root,release,root/"backup",root/"config",root/"env",
                root/"plugin-env",root/"token",python_executable=python)
            context=TransactionContext("3"*64,"4"*64,root)
            with mock.patch("services.install_phase_drivers._run_json",return_value={"status":"verified",
                    "plan_id":"3"*64,"changed":True,"service_mutations":0}) as run:
                applied=driver.apply(context)
            self.assertEqual(applied["phase"],"clean_materials");self.assertIn("--apply",run.call_args.args[0])
            with mock.patch("services.install_phase_drivers._run_json",return_value={"status":"rollback_verified",
                    "plan_id":"3"*64,"changed":False,"service_mutations":0}):
                rolled=driver.rollback(context,applied)
            self.assertEqual(rolled["phase"],"clean_materials");self.assertEqual(rolled["action"],"rollback")


class CleanFinalRollbackVerifierTests(unittest.TestCase):
    def test_accepts_only_exact_clean_terminal_receipt(self):
        with tempfile.TemporaryDirectory(dir=TEMP_PARENT) as raw:
            root=Path(raw);release="a"*40+"-"+"b"*12;base=root/"source-releases"/release/"scripts"
            base.mkdir(parents=True);(base/"verify_v1_clean_install_rollback.py").write_text("pass\n")
            python=root/"python";python.write_text("x");python.chmod(0o755)
            driver=CleanFinalRollbackVerifier(root,release,root/"backup",root/"contract",root/"definition",
                                               python_executable=python)
            context=TransactionContext("3"*64,"4"*64,root)
            value={"status":"rollback_verified","plan_id":"3"*64,"backup_manifest_sha256":"4"*64}
            with mock.patch("services.install_phase_drivers._run_json",return_value=value) as run:
                self.assertEqual(driver(context),value)
            self.assertIn("--desired-controller-definition",run.call_args.args[0])
            with mock.patch("services.install_phase_drivers._run_json",return_value={**value,"extra":1}), \
                    self.assertRaisesRegex(InstallTransactionError,"final clean"):
                driver(context)


class ClosedFrontendDriverTests(unittest.TestCase):
    def driver(self, root: Path) -> ClosedFrontendDriver:
        release="a"*40+"-"+"b"*12; base=root/"source-releases"/release/"scripts"
        base.mkdir(parents=True); (base/"apply_v1_closed_frontend.py").write_text("pass\n")
        python=root/"python"; python.write_text("x"); python.chmod(0o755)
        return ClosedFrontendDriver(install_root=root,source_release_name=release,
            backup_directory=root/"backup",caddyfile=root/"Caddyfile",snippet=root/"snippet",
            hydro_domain="oj.example.test",frontend_domain="exam.example.test",
            python_executable=python)

    def context(self, root: Path) -> TransactionContext:
        return TransactionContext("3"*64,"4"*64,root)

    def test_apply_binds_domains_and_accepts_only_exact_receipt(self):
        with tempfile.TemporaryDirectory(dir=TEMP_PARENT) as raw:
            root=Path(raw); driver=self.driver(root)
            value={"status":"verified","plan_id":"3"*64,"closed":True,
                   "hydro_route_hardened":True,"etag_used":True,
                   "active_sha256":"5"*64,"other_service_mutations":0}
            with mock.patch("services.install_phase_drivers._run_json",return_value=value) as run:
                receipt=driver.apply(self.context(root))
            command=run.call_args.args[0]
            self.assertEqual(command[command.index("--frontend-domain")+1],"exam.example.test")
            self.assertEqual(receipt["phase"],"closed_frontend")

    def test_uncertain_rollback_uses_exact_backup_pin(self):
        with tempfile.TemporaryDirectory(dir=TEMP_PARENT) as raw:
            root=Path(raw); driver=self.driver(root)
            value={"status":"rollback_verified","plan_id":"3"*64,
                   "backup_manifest_sha256":"4"*64,"changed":False,
                   "other_service_mutations":0}
            with mock.patch("services.install_phase_drivers._run_json",return_value=value) as run:
                receipt=driver.rollback(self.context(root),None)
            command=run.call_args.args[0]
            self.assertIn("--rollback",command)
            self.assertEqual(command[command.index("--backup-manifest-sha256")+1],"4"*64)
            self.assertEqual(receipt["action"],"rollback")


class ControllerDriverTests(unittest.TestCase):
    def driver(self, root: Path) -> ControllerDriver:
        release="a"*40+"-"+"b"*12;base=root/"source-releases"/release/"scripts"
        base.mkdir(parents=True);(base/"apply_v1_controller.py").write_text("pass\n")
        python=root/"python";python.write_text("x");python.chmod(0o755)
        return ControllerDriver(install_root=root,source_release_name=release,
            backup_directory=root/"backup",desired_definition=root/"desired.json",
            desired_config=root/"desired.yaml",desired_env=root/"desired.env",
            project_config=root/"config.yaml",project_env=root/".env",database=root/"data/db",
            python_executable=python)

    def context(self, root: Path) -> TransactionContext:
        return TransactionContext("3"*64,"4"*64,root)

    def test_apply_accepts_only_one_owned_healthy_controller(self):
        with tempfile.TemporaryDirectory(dir=TEMP_PARENT) as raw:
            root=Path(raw);driver=self.driver(root)
            value={"status":"verified","plan_id":"3"*64,"container_id":"5"*64,
                   "image_id":"sha256:"+"6"*64,"healthy":True,
                   "old_controller_retained":True,"other_container_mutations":0}
            with mock.patch("services.install_phase_drivers._run_json",return_value=value) as run:
                receipt=driver.apply(self.context(root))
            command=run.call_args.args[0];self.assertIn("--desired-definition",command)
            self.assertEqual(receipt["phase"],"controller")

    def test_uncertain_rollback_requires_exact_backup_identity(self):
        with tempfile.TemporaryDirectory(dir=TEMP_PARENT) as raw:
            root=Path(raw);driver=self.driver(root)
            value={"status":"rollback_verified","plan_id":"3"*64,
                   "backup_manifest_sha256":"4"*64,"controller_present":True,
                   "baseline_running":True,"controller_quiesced":True,
                   "changed":True,"other_container_mutations":0}
            with mock.patch("services.install_phase_drivers._run_json",return_value=value) as run:
                receipt=driver.rollback(self.context(root),None)
            self.assertIn("--rollback",run.call_args.args[0]);self.assertEqual(receipt["action"],"rollback")

    def test_commit_cleanup_removes_only_the_retained_old_controller(self):
        with tempfile.TemporaryDirectory(dir=TEMP_PARENT) as raw:
            root=Path(raw);driver=self.driver(root)
            value={"status":"cleanup_verified","plan_id":"3"*64,
                   "old_controller_removed":True,"other_container_mutations":0}
            with mock.patch("services.install_phase_drivers._run_json",return_value=value) as run:
                receipt=driver.commit_cleanup(self.context(root),{"phase":"controller"})
            self.assertIn("--commit-cleanup",run.call_args.args[0])
            self.assertEqual(receipt,{"phase":"controller","action":"commit_cleanup","status":"verified"})


class PostInstallVerificationDriverTests(unittest.TestCase):
    def driver(self, root: Path) -> PostInstallVerificationDriver:
        release="a"*40+"-"+"b"*12;base=root/"source-releases"/release/"scripts"
        base.mkdir(parents=True);(base/"verify_v1_post_install.py").write_text("pass\n")
        python=root/"python";python.write_text("x");python.chmod(0o755)
        return PostInstallVerificationDriver(install_root=root,source_release_name=release,
            backup_directory=root/"backup",expected_contract=root/"expected.json",
            python_executable=python)

    def context(self, root: Path) -> TransactionContext:
        return TransactionContext("3"*64,"4"*64,root)

    def test_apply_accepts_only_complete_read_only_gate(self):
        with tempfile.TemporaryDirectory(dir=TEMP_PARENT) as raw:
            root=Path(raw);driver=self.driver(root);context=self.context(root)
            value={"status":"verified","plan_id":context.plan_id,"controller_id":"5"*64,
                "controller_image_id":"sha256:"+"6"*64,"closed":True,"cloud_closed":True,
                "queues_quiet":True,"ordinary_oj_unchanged":True,"other_mutations":0}
            with mock.patch("services.install_phase_drivers._run_json",return_value=value):
                receipt=driver.apply(context)
            self.assertEqual(receipt["phase"],"post_install_verification")
            value["cloud_closed"]=False
            with mock.patch("services.install_phase_drivers._run_json",return_value=value), \
                    self.assertRaisesRegex(InstallTransactionError,"evidence"):
                driver.apply(context)

    def test_rollback_is_always_zero_mutation(self):
        with tempfile.TemporaryDirectory(dir=TEMP_PARENT) as raw:
            root=Path(raw);receipt=self.driver(root).rollback(self.context(root),None)
            self.assertEqual(receipt["phase"],"post_install_verification")
            self.assertEqual(receipt["action"],"rollback")


class ControllerQuiesceDriverTests(unittest.TestCase):
    def test_apply_and_rollback_require_exact_quiesce_evidence(self):
        with tempfile.TemporaryDirectory(dir=TEMP_PARENT) as raw:
            root=Path(raw);release="a"*40+"-"+"b"*12;base=root/"source-releases"/release/"scripts"
            base.mkdir(parents=True);(base/"quiesce_v1_controller.py").write_text("pass\n")
            python=root/"python";python.write_text("x");python.chmod(0o755)
            driver=ControllerQuiesceDriver(root,release,root/"backup",root/"config",root/"env",root/"db",
                                             root/"Caddyfile",root/"snippet",root/"pm2","https://oj.example.test",
                                             python_executable=python)
            context=TransactionContext("3"*64,"4"*64,root)
            value={"status":"verified","plan_id":context.plan_id,
                   "backup_manifest_sha256":context.backup_manifest_sha256,"controller_id":"5"*64,
                   "quiesced":True,"changed":True,"other_container_mutations":0}
            with mock.patch("services.install_phase_drivers._run_json",return_value=value):
                self.assertEqual(driver.apply(context)["phase"],"controller_quiesce")
            value["status"]="rollback_verified";value["changed"]=False
            with mock.patch("services.install_phase_drivers._run_json",return_value=value):
                self.assertEqual(driver.rollback(context,None)["phase"],"controller_quiesce")
            value["quiesced"]=False
            with mock.patch("services.install_phase_drivers._run_json",return_value=value), \
                    self.assertRaisesRegex(InstallTransactionError,"quiesce evidence"):
                driver.rollback(context,None)


class FinalRollbackVerifierTests(unittest.TestCase):
    def test_accepts_only_the_exact_terminal_live_receipt(self):
        with tempfile.TemporaryDirectory(dir=TEMP_PARENT) as raw:
            root=Path(raw);release="a"*40+"-"+"b"*12;base=root/"source-releases"/release/"scripts"
            base.mkdir(parents=True);(base/"verify_v1_live_install_rollback.py").write_text("pass\n")
            python=root/"python";python.write_text("x");python.chmod(0o755)
            verifier=FinalRollbackVerifier(root,release,root/"backup",root/"expected.json",python_executable=python)
            context=TransactionContext("3"*64,"4"*64,root);expected={"status":"rollback_verified",
                "plan_id":context.plan_id,"backup_manifest_sha256":context.backup_manifest_sha256}
            with mock.patch("services.install_phase_drivers._run_json",return_value=expected):
                self.assertEqual(verifier(context),expected)
            with mock.patch("services.install_phase_drivers._run_json",return_value={"status":"rollback_verified"}), \
                    self.assertRaisesRegex(InstallTransactionError,"final live"):
                verifier(context)


if __name__ == "__main__":
    unittest.main()
