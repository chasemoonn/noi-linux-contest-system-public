import hashlib
import importlib.util
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))


def load(name):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


session_tool = load("init_v1_single_seat_session.py")
fact_tool = load("collect_v1_single_seat_phase_fact.py")
component_tool = load("collect_v1_components.py")


class SingleSeatCollectorTests(unittest.TestCase):
    def setUp(self):
        self.source = {"revision": "a" * 40, "tree": "b" * 40}
        self.components = {
            "orchestrator_image_digest": "sha256:" + "1" * 64,
            "desktop_image_id": "sha256:" + "2" * 64,
            "desktop_source_revision": "a" * 40,
            "hydro_plugin_sha256": "3" * 64,
        }
        self.private = {
            "candidate_id": "999900000001",
            "contest_id": "synthetic-contest-id",
            "cutoff_at_ms": 1_900_000_000_000,
            "problem_slug": "apple",
            "seat_candidate": "CSP001",
            "seat_id": "synthetic-seat-id",
        }
        self.component_facts = {
            "control": "c" * 64,
            "desktop": "d" * 64,
            "oj": "e" * 64,
        }

    def test_session_hashes_private_identifiers(self):
        document = session_tool.build_session(
            session_id="4" * 64,
            source=self.source,
            components=self.components,
            private_context=self.private,
            component_facts=self.component_facts,
            created_at="2026-08-12T00:00:00Z",
        )
        encoded = str(document)
        self.assertNotIn("synthetic-contest-id", encoded)
        self.assertNotIn("synthetic-seat-id", encoded)
        self.assertEqual(document["context"]["candidate_id"], "999900000001")
        self.assertEqual(document["context"]["seat_candidate"], "CSP001")

    def test_clean_git_status_is_a_valid_empty_result(self):
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(session_tool.subprocess, "run", return_value=completed):
            self.assertEqual(
                session_tool.git("status", "--porcelain=v1", "--untracked-files=no"),
                "",
            )

    def test_non_reserved_candidate_number_is_rejected(self):
        private = dict(self.private, candidate_id="202608120001")
        with self.assertRaisesRegex(session_tool.SessionError, "candidate_id"):
            session_tool.build_session(
                session_id="4" * 64,
                source=self.source,
                components=self.components,
                private_context=private,
                component_facts=self.component_facts,
                created_at="2026-08-12T00:00:00Z",
            )

    def test_non_runtime_seat_candidate_is_rejected(self):
        private = dict(self.private, seat_candidate="999900000001")
        with self.assertRaisesRegex(session_tool.SessionError, "seat_candidate"):
            session_tool.build_session(
                session_id="4" * 64,
                source=self.source,
                components=self.components,
                private_context=private,
                component_facts=self.component_facts,
                created_at="2026-08-12T00:00:00Z",
            )

    def test_component_plugin_digest_binds_names_and_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in component_tool.PLUGIN_FILES:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(relative, encoding="utf-8")
                path.chmod(0o600)
            first = component_tool.plugin_digest(root)
            (root / "index.js").write_text("changed", encoding="utf-8")
            second = component_tool.plugin_digest(root)
            self.assertNotEqual(first, second)

    def test_component_plugin_digest_rejects_extra_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in component_tool.PLUGIN_FILES:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(relative, encoding="utf-8")
                path.chmod(0o600)
            (root / "extra.js").write_text("extra", encoding="utf-8")
            with self.assertRaisesRegex(component_tool.ComponentError, "file set"):
                component_tool.plugin_digest(root)

    def test_deployed_plugin_tree_is_exactly_two_files(self):
        self.assertEqual(component_tool.PLUGIN_FILES, ("index.js", "package.json"))

    def test_component_collector_rejects_unsafe_container_name(self):
        self.assertIsNone(component_tool.CONTAINER_NAME.fullmatch("seat;docker-stop"))
        self.assertIsNotNone(component_tool.CONTAINER_NAME.fullmatch("noi-seat-001"))

    def test_role_component_facts_merge_into_session_components(self):
        merged = session_tool.merge_role_components(
            {"role": "control", "observed_at": "2026-08-12T00:00:00Z", "orchestrator_image_digest": self.components["orchestrator_image_digest"]},
            {
                "role": "desktop",
                "observed_at": "2026-08-12T00:00:00Z",
                "desktop_contract": "finalizer-status-v1",
                "desktop_image_id": self.components["desktop_image_id"],
                "desktop_source_revision": self.components["desktop_source_revision"],
            },
            {"role": "oj", "observed_at": "2026-08-12T00:00:00Z", "hydro_plugin_sha256": self.components["hydro_plugin_sha256"]},
        )
        self.assertEqual(merged, self.components)

    def test_session_rejects_stale_component_facts(self):
        rows = [
            {"observed_at": "2026-08-12T00:00:00Z"},
            {"observed_at": "2026-08-12T00:00:10Z"},
            {"observed_at": "2026-08-12T00:00:20Z"},
        ]
        with self.assertRaisesRegex(session_tool.SessionError, "at most 120 seconds"):
            session_tool.require_fresh_component_facts(
                rows, datetime(2026, 8, 12, 0, 3, tzinfo=timezone.utc)
            )

    def test_session_schema_binds_component_fact_digests(self):
        document = session_tool.build_session(
            session_id="4" * 64,
            source=self.source,
            components=self.components,
            private_context=self.private,
            component_facts=self.component_facts,
            created_at="2026-08-12T00:00:00Z",
        )
        self.assertEqual(
            document["component_facts"]["desktop"],
            {"reference": "components/desktop.json", "sha256": "d" * 64},
        )

    def test_phase_checks_only_its_role_component_identity(self):
        observed = {
            "role": "desktop",
            "observed_at": "2026-08-12T00:00:00Z",
            "desktop_contract": "finalizer-status-v1",
            "desktop_image_id": self.components["desktop_image_id"],
            "desktop_source_revision": self.components["desktop_source_revision"],
        }
        self.assertTrue(fact_tool.role_matches_session("desktop", observed, self.components))
        observed["desktop_image_id"] = "sha256:" + "f" * 64
        self.assertFalse(fact_tool.role_matches_session("desktop", observed, self.components))

    def test_phase_fact_uses_frozen_session_identity(self):
        session = session_tool.build_session(
            session_id="4" * 64,
            source=self.source,
            components=self.components,
            private_context=self.private,
            component_facts=self.component_facts,
            created_at="2026-08-12T00:00:00Z",
        )
        observations = {
            "desktop_paper_sha256": "5" * 64,
            "material_manifest_sha256": "6" * 64,
            "oj_publication_receipt_sha256": "7" * 64,
            "paper_sha256": "5" * 64,
            "practice_pairs": [
                {"group": 1, "input_sha256": "8" * 64, "output_sha256": "9" * 64},
                {"group": 2, "input_sha256": "a" * 64, "output_sha256": "b" * 64},
            ],
        }
        ordinary = {
            "homepage_status": 200,
            "login_status": 200,
            "prep_health_ok": True,
            "prep_database_ok": True,
            "errors": 0,
            "restarts": 0,
            "pm2_fingerprint_sha256": "c" * 64,
            "observed_at": "2026-08-12T00:00:30Z",
        }
        fact = fact_tool.create_fact(
            phase="materials",
            role="control",
            session=session,
            host_id="d" * 64,
            observed_at="2026-08-12T00:01:00Z",
            ordinary_oj=ordinary,
            observations=observations,
            artifacts=[{"reference": "materials/capture.json", "sha256": "e" * 64}],
        )
        self.assertEqual(fact["source"], self.source)
        self.assertEqual(fact["components"], self.components)

    def test_artifact_copy_digest_matches_exact_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.json"
            destination = root / "copy.json"
            payload = b'{"sanitized":true}\n'
            source.write_bytes(payload)
            source.chmod(0o600)
            digest = fact_tool.copy_artifact(source, destination)
            self.assertEqual(digest, hashlib.sha256(payload).hexdigest())
            self.assertEqual(destination.read_bytes(), payload)


if __name__ == "__main__":
    unittest.main()
