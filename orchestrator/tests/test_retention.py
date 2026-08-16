import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from services.retention import RetentionManager
from services.store import Store


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(str(self.root / "state.db"))
        self.artifacts = self.root / "artifacts"
        self.materials = self.root / "materials"
        self.collected = self.root / "collected"
        for path in (self.artifacts, self.materials, self.collected):
            path.mkdir()
        self.manager = RetentionManager(
            self.store,
            artifact_root=str(self.artifacts),
            materials_dir=str(self.materials),
            collected_dir=str(self.collected),
            workspace_retention_days=30,
            evidence_retention_days=180,
        )

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    @staticmethod
    def _publication(tid: str, revision: str) -> dict:
        receipt = {
            "publication_id": "5" * 64,
            "tid": tid,
            "revision": revision,
            "attachments": [
                {"name": "01_比赛题面.pdf", "sha256": "3" * 64, "size": 10},
                {
                    "name": "02_辅助自测数据.tar.gz",
                    "sha256": "4" * 64,
                    "size": 20,
                },
            ],
        }
        encoded = json.dumps(
            receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        return {
            "ok": True,
            **receipt,
            "receipt_sha256": hashlib.sha256(encoded).hexdigest(),
        }

    def _safe_ended_contest(self, tid: str, ended_at_ms: int = 1_000) -> None:
        self.store.upsert_contest(
            tid,
            "retention fixture",
            ["apple"],
            {"apple": "P1"},
            materials_mode="ai",
            material_state="pending",
        )
        contest_root = self.artifacts / tid
        for revision in ("r1", "draft2"):
            revision_root = contest_root / revision
            revision_root.mkdir(parents=True)
            (revision_root / "payload.txt").write_text(revision, encoding="utf-8")
            self.store.put_artifact_revision(
                tid,
                revision,
                state="review",
                source_sha256="1" * 64,
                root_path=str(revision_root),
                manifest_sha256="2" * 64,
                manifest={"status": "awaiting_teacher_approval"},
                paper_name="paper.pdf",
                paper_sha256="3" * 64,
                paper_size=10,
                testdata_name="testdata.tar.gz",
                testdata_sha256="4" * 64,
                testdata_size=20,
                testdata_files=4,
                testdata_expanded_size=40,
            )
        self.store.approve_artifact_with_publication(
            tid, "r1", "teacher", self._publication(tid, "r1")
        )
        for root in (self.materials, self.collected):
            target = root / tid
            target.mkdir()
            (target / "evidence.txt").write_text("keep", encoding="utf-8")
        self.store.set_state(tid, "collecting")
        self.assertTrue(
            self.store.enter_safe_wait(
                tid,
                run_id="run-1",
                collection_dir=str(self.collected / tid),
                receipt_sha256="6" * 64,
                completed_at_ms=ended_at_ms,
                shutdown_after_ms=ended_at_ms,
                message="safe",
            )
        )
        self.assertTrue(
            self.store.mark_safe_ended(
                tid, observed_at_ms=ended_at_ms, message="ended"
            )
        )

    def test_workspace_then_evidence_retention_preserves_oj_authority(self):
        tid = "a" * 24
        ended = 1_000
        self._safe_ended_contest(tid, ended)

        first = self.manager.sweep(now_ms=ended + 31 * 86_400_000)
        self.assertEqual(first, {"checked": 1, "workspace": 1, "evidence": 0})
        self.assertTrue((self.artifacts / tid / "r1").is_dir())
        self.assertFalse((self.artifacts / tid / "draft2").exists())
        self.assertTrue((self.materials / tid).is_dir())
        self.assertTrue((self.collected / tid).is_dir())
        self.assertIsNotNone(self.store.get_contest(tid))

        second = self.manager.sweep(now_ms=ended + 181 * 86_400_000)
        self.assertEqual(second, {"checked": 1, "workspace": 0, "evidence": 1})
        self.assertFalse((self.artifacts / tid).exists())
        self.assertFalse((self.materials / tid).exists())
        self.assertFalse((self.collected / tid).exists())
        contest = self.store.get_contest(tid)
        self.assertGreater(contest["workspace_purged_at_ms"], 0)
        self.assertGreater(contest["evidence_purged_at_ms"], 0)
        retention_events = [
            event
            for event in self.store.audit_events(tid)
            if event["action"] == "contest.retention.purge"
        ]
        self.assertEqual(
            {event["details"]["kind"] for event in retention_events},
            {"workspace", "evidence"},
        )

    def test_active_contest_and_untracked_workspace_fail_closed(self):
        active = "b" * 24
        self.store.upsert_contest(active, "active", ["apple"], {"apple": "P1"})
        (self.artifacts / active).mkdir()
        self.assertEqual(
            self.manager.sweep(now_ms=999 * 86_400_000),
            {"checked": 0, "workspace": 0, "evidence": 0},
        )
        self.assertTrue((self.artifacts / active).exists())

        ended = "c" * 24
        self._safe_ended_contest(ended)
        (self.artifacts / ended / "unexpected").mkdir()
        with self.assertRaisesRegex(RuntimeError, "untracked"):
            self.manager.sweep(now_ms=31 * 86_400_000)
        contest = self.store.get_contest(ended)
        self.assertEqual(contest["workspace_purged_at_ms"], 0)


if __name__ == "__main__":
    unittest.main()
