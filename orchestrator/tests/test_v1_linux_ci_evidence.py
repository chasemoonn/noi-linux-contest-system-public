import copy
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


def load_verifier():
    path = ROOT / "scripts" / "verify_v1_linux_ci_evidence.py"
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


verifier = load_verifier()


def load_runner():
    path = ROOT / "scripts" / "run_v1_linux_ci.py"
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


runner = load_runner()


class V1LinuxCiEvidenceTests(unittest.TestCase):
    def evidence(self):
        gates = []
        for name in verifier.EXPECTED_GATES:
            gates.append(
                {
                    "duration_ms": 1,
                    "name": name,
                    "status": "passed",
                    "stderr_file": f"{len(gates) + 1:02d}-{name}.stderr.log",
                    "stderr_sha256": "1" * 64,
                    "stdout_file": f"{len(gates) + 1:02d}-{name}.stdout.log",
                    "stdout_sha256": "2" * 64,
                }
            )
        return {
            "$schema": "v1-linux-ci-evidence.schema.json",
            "schema_version": 1,
            "status": "passed",
            "source": {"revision": "a" * 40, "tree": "b" * 40},
            "environment": {
                "architecture": "x86_64",
                "effective_uid": 0,
                "kernel": "6.8.0",
                "node": "v22.0.0",
                "python": "3.12.0",
                "system": "linux",
            },
            "started_at": "2026-08-12T01:00:00Z",
            "finished_at": "2026-08-12T01:01:00Z",
            "gates": gates,
        }

    def test_exact_linux_evidence_is_accepted(self):
        validated = verifier.validate(self.evidence(), "a" * 40)
        self.assertEqual(validated["status"], "passed")
        self.assertEqual(len(validated["gates"]), 10)

    def test_non_linux_evidence_is_rejected(self):
        document = self.evidence()
        document["environment"]["system"] = "windows"
        with self.assertRaisesRegex(verifier.EvidenceError, "not produced on Linux"):
            verifier.validate(document)

    def test_non_root_linux_evidence_is_rejected(self):
        document = self.evidence()
        document["environment"]["effective_uid"] = 1000
        with self.assertRaisesRegex(verifier.EvidenceError, "Linux root"):
            verifier.validate(document)

    def test_gate_environment_overrides_all_temporary_directory_variables(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            environment = runner.gate_environment(temporary)
        self.assertEqual(environment["TMPDIR"], str(temporary))
        self.assertEqual(environment["TMP"], str(temporary))
        self.assertEqual(environment["TEMP"], str(temporary))

    def test_missing_or_reordered_gate_is_rejected(self):
        missing = self.evidence()
        missing["gates"].pop()
        with self.assertRaisesRegex(verifier.EvidenceError, "gate count differs"):
            verifier.validate(missing)

        reordered = self.evidence()
        reordered["gates"][0], reordered["gates"][1] = (
            reordered["gates"][1],
            reordered["gates"][0],
        )
        with self.assertRaisesRegex(verifier.EvidenceError, "invalid|names or order differ"):
            verifier.validate(reordered)

    def test_revision_stream_digest_and_time_are_bound(self):
        with self.assertRaisesRegex(verifier.EvidenceError, "revision differs"):
            verifier.validate(self.evidence(), "c" * 40)

        digest = self.evidence()
        digest["gates"][0]["stdout_sha256"] = "not-a-digest"
        with self.assertRaisesRegex(verifier.EvidenceError, "stdout_sha256"):
            verifier.validate(digest)

        time_reversed = self.evidence()
        time_reversed["finished_at"] = "2026-08-12T00:59:59Z"
        with self.assertRaisesRegex(verifier.EvidenceError, "precedes"):
            verifier.validate(time_reversed)

    def test_extra_fields_cannot_be_smuggled_into_evidence(self):
        document = copy.deepcopy(self.evidence())
        document["secret"] = "must not be accepted"
        with self.assertRaisesRegex(verifier.EvidenceError, "shape differs"):
            verifier.validate(document)

    def test_log_bytes_are_exactly_bound(self):
        document = self.evidence()
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            for row in document["gates"]:
                for stream in ("stderr", "stdout"):
                    raw = f"{row['name']}:{stream}\n".encode("utf-8")
                    (directory / row[f"{stream}_file"]).write_bytes(raw)
                    row[f"{stream}_sha256"] = hashlib.sha256(raw).hexdigest()
            validated = verifier.validate(document)
            verifier.verify_logs(validated, directory)

            changed = directory / document["gates"][0]["stdout_file"]
            changed.write_bytes(b"changed\n")
            with self.assertRaisesRegex(verifier.EvidenceError, "digest differs"):
                verifier.verify_logs(validated, directory)


if __name__ == "__main__":
    unittest.main()
