import base64, copy, importlib.util, json, platform, sys
from pathlib import Path
import unittest
from unittest import mock
ROOT=Path(__file__).resolve().parents[2]; SCRIPT=ROOT/"scripts"/"verify_v1_independent_teacher_install.py"
spec=importlib.util.spec_from_file_location("verify_v1_independent_teacher_install",SCRIPT); verifier=importlib.util.module_from_spec(spec); assert spec.loader; spec.loader.exec_module(verifier)
def evidence():
    revision="a"*40
    return {"$schema":"v1-independent-teacher-install-evidence.schema.json","schema_version":1,
        "source":{"revision":revision,"tree":"b"*40},"components":{"orchestrator_image_digest":"sha256:"+"c"*64,
        "desktop_image_id":"sha256:"+"d"*64,"desktop_source_revision":revision,"hydro_plugin_sha256":"e"*64},
        "candidate":{"manifest_sha256":"f"*64,"archive_sha256":"1"*64},"observed_at":"2026-08-13T00:00:00Z",
        "host":{"anonymous_id":"2"*64,"architecture":"x86_64","kernel":"6.8","os_release_sha256":"3"*64},
        "teacher":{"qualification_marker":"NOI-V1-TEACHER-1234567890ABCDEF","independent":True,"operator_id_sha256":"4"*64},
        "checks":{"candidate_verified":True,"clean_target":True,"root_only_staging":True,"closed_frontend":True,
        "controller_healthy":True,"active_seats":0,"managed_rules":0,"cloud_state":"STOPPED","ordinary_oj_errors":0,
        "ordinary_oj_restarts":0,"ordinary_oj_pid_changes":0,"rollback_verified":True,"pending_markers":0},
        "artifacts":{"install_log_sha256":"5"*64,"rollback_receipt_sha256":"6"*64,
        "ordinary_oj_before_sha256":"7"*64,"ordinary_oj_after_sha256":"7"*64,
        "clean_install_rehearsal_sha256":"8"*64},"signer":"teacher-agent",
        "signing_public_key":"ssh-ed25519 "+"A"*68,"signature":base64.b64encode(b"signed").decode()}
class TeacherInstallEvidenceTests(unittest.TestCase):
    def test_accepts_exact_closed_install_and_rollback(self):
        row=evidence(); self.assertIs(verifier.validate(row),row)
    def test_rejects_oj_restart_or_unclosed_cloud(self):
        row=evidence(); row["checks"]["ordinary_oj_restarts"]=1
        with self.assertRaisesRegex(verifier.EvidenceError,"must equal zero"): verifier.validate(row)
        row=evidence(); row["checks"]["cloud_state"]="RUNNING"
        with self.assertRaisesRegex(verifier.EvidenceError,"cloud state"): verifier.validate(row)
    def test_signature_is_over_every_field(self):
        row=evidence()
        ssh_keygen=Path("/usr/bin/ssh-keygen") if platform.system().lower()=="linux" else Path(sys.executable)
        with mock.patch.object(verifier.subprocess,"run",return_value=mock.Mock(returncode=0)) as run:
            verifier.validate(row,ssh_keygen=ssh_keygen)
        self.assertEqual(run.call_args.kwargs["input"],verifier.canonical({k:v for k,v in row.items() if k!="signature"}))
    def test_candidate_archive_is_bound_when_requested(self):
        row=evidence()
        self.assertIs(verifier.validate(row,expected_archive_sha256="1"*64),row)
        with self.assertRaisesRegex(verifier.EvidenceError,"candidate archive differs"):
            verifier.validate(row,expected_archive_sha256="9"*64)
if __name__=="__main__": unittest.main()
