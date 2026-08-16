import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "v1_capacity_rehearsal_guard.py"
spec = importlib.util.spec_from_file_location("capacity_guard", SCRIPT)
guard = importlib.util.module_from_spec(spec); assert spec.loader; spec.loader.exec_module(guard)


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    if isinstance(value, bytes): path.write_bytes(value)
    else: path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)


def fixture(root: Path):
    def posix(path: str) -> str: return "/root/guard-test" + path
    probes = {}
    probe_digests = {}
    for index, kind in enumerate(guard.PROBES, start=1):
        path = root / "probes" / kind; raw = f"probe-{index}\n".encode(); write(path, raw)
        probes[kind] = posix(f"/probes/{kind}"); probe_digests[kind] = hashlib.sha256(raw).hexdigest()
    agents = {}
    outputs = {}
    for kind in ("workload", "network"):
        path = root / "agents" / kind; raw = f"agent-{kind}\n".encode(); write(path, raw)
        agents[kind] = {"path": posix(f"/agents/{kind}"), "sha256": hashlib.sha256(raw).hexdigest()}
        outputs[f"{kind}_receipt"] = posix(f"/outputs/{kind}-receipt.json")
        outputs[f"{kind}_envelope"] = posix(f"/outputs/{kind}-envelope.json")
    identity = {"source": {"revision": "a" * 40, "tree": "b" * 40},
                "components": {"orchestrator_image_digest": "sha256:" + "c" * 64},
                "environment": {"profile": "test"}, "thresholds": {"limit": 1}, "probes": probe_digests}
    identity_path = root / "identity.json"; write(identity_path, identity)
    config = {"schema_version": 1, "identity_path": posix("/identity.json"),
              "session_dir": posix("/session"), "probe_paths": probes,
              "action_agents": agents, "action_outputs": outputs}
    row = guard.validate_config(config)
    mapping = {posix("/identity.json"): identity_path, posix("/session"): root / "session"}
    for kind in guard.PROBES: mapping[posix(f"/probes/{kind}")] = root / "probes" / kind
    for kind in ("workload", "network"):
        mapping[posix(f"/agents/{kind}")] = root / "agents" / kind
        mapping[posix(f"/outputs/{kind}-receipt.json")] = root / "outputs" / f"{kind}-receipt.json"
        mapping[posix(f"/outputs/{kind}-envelope.json")] = root / "outputs" / f"{kind}-envelope.json"
    return row, identity, mapping


def initialize(row: dict, identity: dict, samples=0):
    session = Path(row["session_dir"])
    (session / "samples").mkdir(parents=True, mode=0o700)
    (session / "raw").mkdir(mode=0o700)
    session.chmod(0o700); (session / "samples").chmod(0o700); (session / "raw").chmod(0o700)
    value = {**{key: identity[key] for key in ("source", "components", "environment", "thresholds", "probes")},
             "session_id": "f" * 64, "duration_seconds": 3600, "sample_interval_seconds": 60}
    write(session / "session.json", value)
    for index in range(1, samples + 1): write(session / "samples" / f"{index:06d}.json", {"sample": index})
    return value


def complete_action(row: dict, kind: str):
    receipt = {"agent_sha256": row["action_agents"][kind]["sha256"]}
    write(Path(row["action_outputs"][f"{kind}_receipt"]), receipt)
    write(Path(row["action_outputs"][f"{kind}_envelope"]), {"signed": True})


class CapacityRehearsalGuardTests(unittest.TestCase):
    def localize(self, row, mapping):
        row = copy.deepcopy(row)
        row["session_dir"] = mapping[row["session_dir"]].as_posix()
        row["identity_path"] = mapping[row["identity_path"]].as_posix()
        row["probe_paths"] = {key: mapping[value].as_posix() for key, value in row["probe_paths"].items()}
        row["action_agents"] = {key: {**value, "path": mapping[value["path"]].as_posix()}
                                for key, value in row["action_agents"].items()}
        row["action_outputs"] = {key: mapping[value].as_posix() for key, value in row["action_outputs"].items()}
        return row

    def inspect(self, row):
        with mock.patch.object(guard, "safe_ancestors"):
            return guard.inspect(row)

    def test_phases_progress_without_executing_mutations(self):
        with tempfile.TemporaryDirectory() as raw:
            row, identity, mapping = fixture(Path(raw))
            row = self.localize(row, mapping)
            self.assertEqual(self.inspect(row)["phase"], "initialize")
            initialize(row, identity, samples=1)
            sampling = self.inspect(row)
            self.assertEqual(sampling["phase"], "sampling_and_actions")
            self.assertEqual(sampling["missing_actions"], ["workload", "network"])
            session = Path(row["session_dir"])
            complete_action(row, "workload"); complete_action(row, "network")
            for index in range(2, 62): write(session / "samples" / f"{index:06d}.json", {"sample": index})
            self.assertEqual(self.inspect(row)["phase"], "runtime_facts")
            for kind in guard.RUNTIME_FACTS: write(session / "raw" / f"{kind}.json", {"fact": kind})
            self.assertEqual(self.inspect(row)["phase"], "terminal_facts")
            for kind in guard.TERMINAL_FACTS: write(session / "raw" / f"{kind}.json", {"fact": kind})
            self.assertEqual(self.inspect(row)["phase"], "finalize")
            write(session / "capacity-evidence.json", {"status": "passed", "session_id": "f" * 64})
            self.assertEqual(self.inspect(row)["phase"], "independent_verification")

    def test_probe_or_agent_drift_and_partial_output_fail_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            row, identity, mapping = fixture(Path(raw))
            row = self.localize(row, mapping); initialize(row, identity, samples=61)
            Path(row["probe_paths"]["measurement"]).write_text("changed\n")
            with self.assertRaisesRegex(guard.GuardError, "measurement probe SHA256"):
                self.inspect(row)
            Path(row["probe_paths"]["measurement"]).write_text("probe-1\n")
            write(Path(row["action_outputs"]["workload_receipt"]),
                  {"agent_sha256": row["action_agents"]["workload"]["sha256"]})
            with self.assertRaisesRegex(guard.GuardError, "partial"):
                self.inspect(row)

    def test_terminal_facts_cannot_appear_before_runtime_facts(self):
        with tempfile.TemporaryDirectory() as raw:
            row, identity, mapping = fixture(Path(raw)); row = self.localize(row, mapping)
            initialize(row, identity, samples=61)
            complete_action(row, "workload"); complete_action(row, "network")
            write(Path(row["session_dir"]) / "raw" / "shutdown_observation.json", {"fact": "early"})
            with self.assertRaisesRegex(guard.GuardError, "terminal facts"):
                self.inspect(row)

    def test_missing_action_at_end_of_window_is_no_go(self):
        with tempfile.TemporaryDirectory() as raw:
            row, identity, mapping = fixture(Path(raw)); row = self.localize(row, mapping)
            initialize(row, identity, samples=61)
            complete_action(row, "workload")
            with self.assertRaisesRegex(guard.GuardError, "inside the sample window"):
                self.inspect(row)


if __name__ == "__main__": unittest.main()
