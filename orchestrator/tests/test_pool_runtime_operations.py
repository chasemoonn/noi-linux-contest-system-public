import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import MagicMock

from services.pipeline import Pipeline
from services.seat_pool import SeatPoolState, TeacherApprovalRequiredError
from services.store import Store


class RunningCVM:
    def status(self):
        return "RUNNING", "198.51.100.10"


class RecordingRemote:
    def __init__(self):
        self.commands = []
        self.contents = []

    def wait_ssh(self, timeout=180):
        return True

    def run(self, command, timeout=300):
        self.commands.append(command)
        if "docker inspect -f '{{.State.Status}}'" in command:
            return "running\n"
        return ""

    def put_content(self, content, remote_path):
        self.contents.append((content, remote_path))


def config(root: Path) -> dict:
    return {
        "contest_server": {
            "ssh_user": "root",
            "ssh_key": "/keys/contest",
            "strict_host_key": True,
            "host_key_sha256": "SHA256:" + "A" * 43,
            "seats_root": "/data/seats",
            "docker_image": "noi-linux-sim:latest",
            "docker_network": "seats",
            "memory": "1536m",
            "cpus": "1.0",
            "pids_limit": 512,
            "shm_size": "1g",
            "gateway_listen": 80,
            "submit_proxy_port": 18082,
        },
        "hydro": {"submit_enabled": False},
        "orchestrator": {
            "materials_dir": str(root / "materials"),
            "collected_dir": str(root / "collected"),
            "deployment_lock": str(root / "runtime" / "deploy-image.lock"),
            "public_base_url": "https://exam.example.test",
            "seat_pool_maximum": 30,
            "seat_pool_total_maximum": 40,
        },
    }


class PoolRuntimeOperationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(str(self.root / "state.db"))
        self.tid = "a" * 24
        self.store.upsert_contest(
            self.tid,
            "pool test",
            ["apple"],
            {"apple": "P1"},
            max_participants=2,
            spare_seats=1,
        )
        self.store.set_state(self.tid, "ready", "running")
        pool = SeatPoolState.create(
            self.tid,
            max_participants=2,
            spare_count=1,
            begin_at_ms=int(time.time() * 1000) + 3_600_000,
        )
        for seat in pool.seats:
            previous = pool.revision
            pool = pool.mark_warming(
                seat.slot_no,
                now_ms=1,
                command_id=f"warm:{seat.slot_no}",
                expected_revision=previous,
            ).state
            previous = pool.revision
            pool = pool.mark_verified(
                seat.slot_no,
                container_ref=f"container-{seat.slot_no}",
                image_digest="sha256:image",
                material_digest="sha256:material",
                now_ms=2,
                command_id=f"verify:{seat.slot_no}",
                expected_revision=previous,
            ).state
        for uid, uname in ((7, "alice"), (8, "bob")):
            previous = pool.revision
            pool = pool.reserve(
                uid,
                uname,
                now_ms=3,
                command_id=f"reserve:{uid}",
                expected_revision=previous,
            ).state
        self.pool = pool
        self.store.put_seat_pool(self.tid, None, pool.to_dict())
        for seat in pool.seats:
            self.store.put_seat_pool_resource(
                self.tid,
                seat.slot_no,
                token=f"seatTokenABCDEF{seat.slot_no:02d}",
                vnc_pass=f"pass-{seat.slot_no}",
                submit_token=f"submit-{seat.slot_no}",
                candidate=f"CSP{seat.slot_no:03d}",
                container=f"container-{seat.slot_no}",
                cip=f"172.18.0.{seat.slot_no + 1}",
                image_digest="sha256:image",
                material_digest="sha256:material",
            )
        self.store.bind_pool_seat(self.tid, 7, "alice", 1)
        self.store.bind_pool_seat(self.tid, 8, "bob", 2)
        self.remote = RecordingRemote()
        self.pipe = Pipeline(
            config(self.root), RunningCVM(), MagicMock(), self.store, MagicMock()
        )
        self.pipe._remote = lambda _: self.remote
        self.pipe._acquire_deployment_lock = MagicMock(return_value=None)
        self.pipe._pool_runtime_context = MagicMock(
            side_effect=lambda _remote, _contest, _pool: {
                "resources": self.store.seat_pool_resources(self.tid),
                "image_digest": "sha256:image",
                "material_digest": "sha256:material",
                "network": "seats",
                "network_gateway": "172.18.0.1",
                "seats_root": "/data/seats",
                "remote_materials": f"/data/seats/{self.tid}/materials",
                "remote_testdata": f"/data/seats/{self.tid}/testdata",
                "mode": "folder",
                "web_enabled": False,
                "public_base": "https://exam.example.test",
                "origin_host": "exam.example.test",
                "image_contract": "finalizer-status-v1",
            }
        )

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    @staticmethod
    def _resource(spec: dict) -> dict:
        result = dict(spec)
        result.pop("home", None)
        result.update(
            {
                "cip": f"172.18.0.{int(spec['slot_no']) + 1}",
                "image_digest": "sha256:image",
                "material_digest": "sha256:material",
            }
        )
        return result

    def test_growth_only_builds_new_slots_and_preserves_assignments(self):
        old_alice = self.store.seat_pool_assignment(self.tid, 7)
        old_bob = self.store.seat_pool_assignment(self.tid, 8)
        self.pipe._provision_pool_slot = MagicMock(
            side_effect=lambda _r, _c, _x, spec, _ips: self._resource(spec)
        )

        result = self.pipe.grow_pool(
            self.tid,
            additional_main=1,
            additional_spares=1,
            expected_revision=self.pool.revision,
        )

        self.assertFalse(result["replayed"])
        self.assertEqual(result["added"], [4, 5])
        self.assertEqual(
            [call.args[3]["slot_no"] for call in self.pipe._provision_pool_slot.call_args_list],
            [4, 5],
        )
        self.assertEqual(self.store.seat_pool_assignment(self.tid, 7)["slot_no"], 1)
        self.assertEqual(self.store.seat_pool_assignment(self.tid, 8)["slot_no"], 2)
        self.assertEqual(old_alice["resource"]["token"], "seatTokenABCDEF01")
        self.assertEqual(old_bob["resource"]["token"], "seatTokenABCDEF02")
        self.assertEqual(len(self.store.seat_pool_resources(self.tid)), 5)
        contest = self.store.get_contest(self.tid)
        self.assertEqual(contest["max_participants"], 3)
        self.assertEqual(contest["spare_seats"], 2)

        replay = self.pipe.grow_pool(
            self.tid,
            additional_main=1,
            additional_spares=1,
            expected_revision=self.pool.revision,
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(self.pipe._provision_pool_slot.call_count, 2)
        self.assertEqual(len(self.store.seat_pool_resources(self.tid)), 5)

    def test_growth_enforces_participant_and_total_container_limits(self):
        self.pipe.cfg["orchestrator"]["seat_pool_maximum"] = 2
        self.pipe.cfg["orchestrator"]["seat_pool_total_maximum"] = 5
        with self.assertRaisesRegex(RuntimeError, "正式参赛人数"):
            self.pipe.grow_pool(
                self.tid,
                additional_main=1,
                additional_spares=0,
                expected_revision=self.pool.revision,
            )

        self.pipe.cfg["orchestrator"]["seat_pool_maximum"] = 3
        self.pipe.cfg["orchestrator"]["seat_pool_total_maximum"] = 3
        with self.assertRaisesRegex(RuntimeError, "座位总数"):
            self.pipe.grow_pool(
                self.tid,
                additional_main=0,
                additional_spares=1,
                expected_revision=self.pool.revision,
            )
        self.assertEqual(self.store.seat_pool(self.tid)["revision"], self.pool.revision)

    def test_partial_provision_failure_leaves_old_pool_and_mapping_intact(self):
        before_pool = self.store.seat_pool(self.tid)
        before_resources = self.store.seat_pool_resources(self.tid)
        calls = 0

        def provision(_remote, _contest, _context, spec, _ips):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("second slot failed validation")
            return self._resource(spec)

        self.pipe._provision_pool_slot = MagicMock(side_effect=provision)
        with self.assertRaisesRegex(RuntimeError, "failed validation"):
            self.pipe.grow_pool(
                self.tid,
                additional_main=2,
                additional_spares=0,
                expected_revision=self.pool.revision,
            )

        self.assertEqual(self.store.seat_pool(self.tid)["state"], before_pool["state"])
        self.assertEqual(self.store.seat_pool_resources(self.tid), before_resources)
        self.assertEqual(self.store.seat_pool_assignment(self.tid, 7)["slot_no"], 1)
        cleanup = "\n".join(
            command for command in self.remote.commands if "docker rm -f" in command
        )
        self.assertIn("slot-004", cleanup)
        self.assertIn("slot-005", cleanup)
        self.assertNotIn("container-1", cleanup)
        self.assertNotIn("container-2", cleanup)

    def test_failed_assigned_seat_moves_to_verified_spare(self):
        bob_before = self.store.seat_pool_assignment(self.tid, 8)
        self.pipe._provision_pool_slot = MagicMock(
            side_effect=lambda _r, _c, _x, spec, _ips: self._resource(spec)
        )

        result = self.pipe.replace_failed_seat(
            self.tid,
            1,
            reason="VNC health check failed",
            expected_revision=self.pool.revision,
            teacher_approved=True,
        )

        self.assertEqual(result["replacement"]["slot_no"], 3)
        self.assertEqual(result["credential_revision"], 2)
        alice = self.store.seat_pool_assignment(self.tid, 7)
        self.assertEqual(alice["slot_no"], 3)
        self.assertEqual(alice["resource"]["container"], "container-3")
        self.assertEqual(alice["resource"]["credential_revision"], 2)
        self.assertTrue(result["capacity_recovered"])
        self.assertIsNotNone(self.store.seat_pool_resource(self.tid, 1))
        restored = self.store.seat_pool(self.tid)["state"]["seats"][0]
        self.assertEqual(restored["state"], "verified")
        self.assertEqual(restored["failure_count"], 1)
        self.assertEqual(restored["role"], "spare")
        self.assertIsNone(restored["uid"])
        replacement = self.store.seat_pool(self.tid)["state"]["seats"][2]
        self.assertEqual(replacement["role"], "primary")
        bob = self.store.seat_pool_assignment(self.tid, 8)
        self.assertEqual(bob["slot_no"], 2)
        self.assertEqual(bob["resource"]["token"], bob_before["resource"]["token"])
        commands = "\n".join(self.remote.commands)
        self.assertIn("docker pause container-1", commands)
        self.assertIn("cp -a --reflink=auto", commands)
        self.assertIn("docker rm -f container-1", commands)
        self.assertIn("rm -rf -- /data/seats/", commands)

    def test_failed_seat_cutover_stays_committed_when_capacity_repair_fails(self):
        self.pipe._provision_pool_slot = MagicMock(
            side_effect=RuntimeError("fresh spare did not pass validation")
        )

        result = self.pipe.replace_failed_seat(
            self.tid,
            1,
            reason="desktop became unhealthy",
            expected_revision=self.pool.revision,
            teacher_approved=True,
        )

        self.assertFalse(result["capacity_recovered"])
        self.assertEqual(self.store.seat_pool_assignment(self.tid, 7)["slot_no"], 3)
        self.assertIsNone(self.store.seat_pool_resource(self.tid, 1))
        failed = self.store.seat_pool(self.tid)["state"]["seats"][0]
        self.assertEqual(failed["state"], "planned")
        self.assertIn("备用容量恢复失败", self.store.get_contest(self.tid)["message"])

    def test_capacity_repair_retry_restores_only_the_isolated_slot(self):
        self.pipe._provision_pool_slot = MagicMock(
            side_effect=RuntimeError("first capacity repair fails")
        )
        first = self.pipe.replace_failed_seat(
            self.tid,
            1,
            reason="desktop became unhealthy",
            expected_revision=self.pool.revision,
            teacher_approved=True,
        )
        self.assertFalse(first["capacity_recovered"])
        degraded = self.store.seat_pool(self.tid)
        alice = self.store.seat_pool_assignment(self.tid, 7)
        self.pipe._provision_pool_slot = MagicMock(
            side_effect=lambda _r, _c, _x, spec, _ips: self._resource(spec)
        )

        result = self.pipe.repair_pool_capacity(
            self.tid, 1, expected_revision=degraded["revision"]
        )

        self.assertTrue(result["recovered"])
        self.assertEqual(self.store.seat_pool_assignment(self.tid, 7)["slot_no"], 3)
        self.assertEqual(
            self.store.seat_pool_assignment(self.tid, 7)["resource"]["token"],
            alice["resource"]["token"],
        )
        restored = self.store.seat_pool(self.tid)["state"]["seats"][0]
        self.assertEqual(restored["state"], "verified")
        self.assertEqual(restored["failure_count"], 1)
        self.assertEqual(restored["role"], "spare")

    def test_replacement_commit_failure_restores_gateway_and_resumes_old_seat(self):
        old_assignment = self.store.seat_pool_assignment(self.tid, 7)
        self.store.commit_pool_replacement = MagicMock(
            side_effect=RuntimeError("database commit failed")
        )

        with self.assertRaisesRegex(RuntimeError, "database commit failed"):
            self.pipe.replace_failed_seat(
                self.tid,
                1,
                reason="temporary VNC fault",
                expected_revision=self.pool.revision,
                teacher_approved=True,
            )

        current = self.store.seat_pool_assignment(self.tid, 7)
        self.assertEqual(current["slot_no"], 1)
        self.assertEqual(
            current["resource"]["token"], old_assignment["resource"]["token"]
        )
        commands = "\n".join(self.remote.commands)
        self.assertIn("docker pause container-1", commands)
        self.assertIn("docker unpause container-1", commands)
        self.assertNotIn("docker rm -f container-1", commands)

    def test_replacement_gateway_failure_resumes_old_seat(self):
        self.pipe._stage_pool_gateway = MagicMock(
            side_effect=RuntimeError("gateway reload failed after rollback")
        )

        with self.assertRaisesRegex(RuntimeError, "gateway reload failed"):
            self.pipe.replace_failed_seat(
                self.tid,
                1,
                reason="temporary gateway fault",
                expected_revision=self.pool.revision,
                teacher_approved=True,
            )

        current = self.store.seat_pool_assignment(self.tid, 7)
        self.assertEqual(current["slot_no"], 1)
        commands = "\n".join(self.remote.commands)
        self.assertIn("docker pause container-1", commands)
        self.assertIn("docker unpause container-1", commands)
        self.assertNotIn("docker rm -f container-1", commands)

    def test_gateway_stage_command_restores_old_config_on_reload_failure(self):
        remote = RecordingRemote()

        backup = self.pipe._stage_pool_gateway(
            remote, self.tid, self.pool.revision + 1, "server { listen 80; }"
        )

        command = remote.commands[-1]
        self.assertIn("else rc=$?", command)
        self.assertIn(f"sudo cp -f {backup}", command)
        self.assertIn("sudo systemctl reload-or-restart nginx", command)
        self.assertIn("exit $rc", command)

    def test_replacement_requires_explicit_teacher_approval(self):
        with self.assertRaises(TeacherApprovalRequiredError):
            self.pipe.replace_failed_seat(
                self.tid,
                1,
                reason="bad desktop",
                expected_revision=self.pool.revision,
                teacher_approved=False,
            )
        self.assertEqual(self.store.seat_pool_assignment(self.tid, 7)["slot_no"], 1)
        self.assertEqual(self.remote.commands, [])


if __name__ == "__main__":
    unittest.main()
