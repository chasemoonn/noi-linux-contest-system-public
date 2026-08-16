from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "noictl.py"
SPEC = importlib.util.spec_from_file_location("noictl_install_apply_tests", SCRIPT)
noictl = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = noictl
SPEC.loader.exec_module(noictl)


class NoictlInstallApplyTests(unittest.TestCase):
    def completed(self, status="committed", returncode=0, stderr=b"", operation=None):
        payload = {"status": status, "plan_id": "1" * 64,
                   "backup_manifest_sha256": "2" * 64}
        if operation is not None: payload["operation"] = operation
        return mock.Mock(returncode=returncode, stdout=json.dumps(payload).encode(), stderr=stderr)

    def test_apply_refuses_before_spawning_outside_linux_root(self):
        with mock.patch.object(noictl, "_runtime_system", return_value="Windows"), \
                mock.patch.object(noictl.subprocess, "run") as run:
            result, code = noictl._install_apply("/root/plan.json", "3" * 64)
        self.assertEqual(code, 3)
        self.assertFalse(result["changed"])
        run.assert_not_called()

    def test_apply_maps_only_exact_committed_or_rollback_terminal(self):
        with mock.patch.object(noictl, "_runtime_system", return_value="Linux"), \
                mock.patch.object(noictl.os, "geteuid", return_value=0, create=True), \
                mock.patch.object(noictl, "_private_install_operation", return_value="upgrade"), \
                mock.patch.object(noictl.subprocess, "run", return_value=self.completed()):
            committed, code = noictl._install_apply("/root/plan.json", "3" * 64)
        self.assertEqual(code, 0)
        self.assertTrue(committed["changed"])
        self.assertEqual(committed["actions"][0]["status"], "committed")

        with mock.patch.object(noictl, "_runtime_system", return_value="Linux"), \
                mock.patch.object(noictl.os, "geteuid", return_value=0, create=True), \
                mock.patch.object(noictl, "_private_install_operation", return_value="upgrade"), \
                mock.patch.object(noictl.subprocess, "run",
                    return_value=self.completed("rollback_verified")):
            rolled_back, code = noictl._install_apply("/root/plan.json", "3" * 64)
        self.assertEqual(code, 4)
        self.assertFalse(rolled_back["changed"])
        self.assertEqual(rolled_back["status"], "rolled_back")

    def test_apply_failure_never_emits_executor_stderr(self):
        secret = b"PASSWORD=DO-NOT-LEAK"
        with mock.patch.object(noictl, "_runtime_system", return_value="Linux"), \
                mock.patch.object(noictl.os, "geteuid", return_value=0, create=True), \
                mock.patch.object(noictl, "_private_install_operation", return_value="upgrade"), \
                mock.patch.object(noictl.subprocess, "run",
                    return_value=self.completed(returncode=2, stderr=secret)):
            result, code = noictl._install_apply("/root/secret-plan.json", "3" * 64)
        self.assertEqual(code, 4)
        self.assertTrue(result["changed"])
        self.assertNotIn(secret.decode(), json.dumps(result, ensure_ascii=False))
        self.assertNotIn("secret-plan", json.dumps(result, ensure_ascii=False))

    def test_apply_dispatches_clean_plan_only_to_clean_executor(self):
        with mock.patch.object(noictl,"_runtime_system",return_value="Linux"), \
                mock.patch.object(noictl.os,"geteuid",return_value=0,create=True), \
                mock.patch.object(noictl,"_private_install_operation",return_value="clean-install"), \
                mock.patch.object(noictl.subprocess,"run",
                    return_value=self.completed(operation="clean-install")) as run:
            result,code=noictl._install_apply("/root/clean.json","3"*64)
        self.assertEqual(code,0);self.assertTrue(result["changed"])
        self.assertTrue(str(run.call_args.args[0][1]).endswith("apply_v1_clean_install.py"))
        self.assertEqual(result["actions"][0]["code"],"INSTALL_CLEAN_TRANSACTION")

    def test_apply_cli_does_not_resolve_or_read_config(self):
        terminal = noictl._base_result("install --apply", "ok", "done")
        with mock.patch.object(noictl, "_install_apply", return_value=(terminal, 0)) as apply, \
                mock.patch.object(noictl, "_default_config_path",
                    side_effect=AssertionError("config must not be touched")):
            output = io.StringIO()
            with redirect_stdout(output):
                code = noictl.main(["install", "--apply", "--private-plan", "/root/p.json",
                                    "--expected-plan-sha256", "3" * 64, "--json"])
        self.assertEqual(code, 0)
        apply.assert_called_once_with("/root/p.json", "3" * 64)
        self.assertEqual(json.loads(output.getvalue())["command"], "install --apply")

    def test_apply_cli_rejects_plan_arguments_and_config_before_execution(self):
        for extra in (("--candidate", "/tmp/candidate"),
                      ("--config", "/tmp/private-config.yaml")):
            output = io.StringIO()
            with mock.patch.object(noictl, "_install_apply") as apply, redirect_stdout(output):
                code = noictl.main(["install", "--apply", "--private-plan", "/root/p.json",
                    "--expected-plan-sha256", "3" * 64, *extra, "--json"])
            self.assertEqual(code, 2)
            apply.assert_not_called()
            payload = json.loads(output.getvalue())
            self.assertFalse(payload["changed"])
            self.assertEqual(payload["checks"][0]["code"], "CLI_ARGUMENTS_VALID")
            self.assertNotIn(extra[1], output.getvalue())


if __name__ == "__main__":
    unittest.main()
