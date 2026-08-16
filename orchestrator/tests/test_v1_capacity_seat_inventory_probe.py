import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
SCRIPT = ROOT / "scripts" / "v1_capacity_seat_inventory_probe.py"
BUILDER = ROOT / "scripts" / "build_v1_capacity_seat_inventory_probe.py"

spec = importlib.util.spec_from_file_location("v1_capacity_seat_inventory_probe", SCRIPT)
probe = importlib.util.module_from_spec(spec); spec.loader.exec_module(probe)
builder_spec = importlib.util.spec_from_file_location("build_v1_capacity_seat_inventory_probe", BUILDER)
builder = importlib.util.module_from_spec(builder_spec); builder_spec.loader.exec_module(builder)


def config():
    ids = [f"{index:064x}" for index in range(1, 18)]
    return {
        "schema_version": 1,
        "tid": "a" * 24,
        "docker_socket": "/var/run/docker.sock",
        "desktop_image_id": "sha256:" + "b" * 64,
        "network_name": "noi-seats",
        "network_id": "c" * 64,
        "formal_container_ids": ids[:15],
        "spare_container_ids": ids[15:],
        "planned_restart": {"container_id": ids[0], "restart_count_delta": 1},
        "fault_replacement": {
            "baseline_container_id": ids[1], "slot_no": 2,
            "replacement_spare_container_id": ids[15], "replacement_slot_no": 16,
        },
        "baselines": [
            {"container_id": cid, "slot_no": index, "pid": 1000 + index,
             "restart_count": 0, "started_at": f"2026-08-13T00:00:{index:02d}Z"}
            for index, cid in enumerate(ids, start=1)
        ],
    }


def container_document(row, cid):
    replacement_id = "f" * 64
    baseline_id = (
        row["fault_replacement"]["baseline_container_id"]
        if cid == replacement_id else cid
    )
    baseline = next(item for item in row["baselines"] if item["container_id"] == baseline_id)
    planned = cid == row["planned_restart"]["container_id"]
    replaced = cid == replacement_id
    return {
        "Id": cid, "Image": row["desktop_image_id"],
        "RestartCount": 0 if replaced else baseline["restart_count"] + (1 if planned else 0),
        "State": {"Running": True, "Restarting": False,
                  "Pid": baseline["pid"] + (10000 if planned or replaced else 0),
                  "StartedAt": "2026-08-13T00:10:00Z" if planned or replaced else baseline["started_at"]},
        "Config": {"Labels": {"noi.contest": row["tid"], "noi.slot": str(baseline["slot_no"])}},
        "NetworkSettings": {"Networks": {row["network_name"]: {
            "NetworkID": row["network_id"], "IPAddress": f"10.20.0.{baseline['slot_no'] + 1}",
            "MacAddress": f"02:42:ac:11:00:{baseline['slot_no']:02x}"
        }}},
    }


class CapacitySeatInventoryProbeTests(unittest.TestCase):
    def test_config_binds_formal_and_spare_slots(self):
        self.assertEqual(len(probe.validate_config(config())["baselines"]), 17)
        wrong = config()
        wrong["formal_container_ids"][0], wrong["spare_container_ids"][0] = (
            wrong["spare_container_ids"][0], wrong["formal_container_ids"][0]
        )
        with self.assertRaisesRegex(probe.SeatProbeError, "slot roles"):
            probe.validate_config(wrong)

    def test_collect_derives_inventory_from_all_seventeen_live_containers(self):
        row = config()
        replacement_id = "f" * 64
        current_ids = [replacement_id if cid == row["fault_replacement"]["baseline_container_id"] else cid
                       for cid in row["formal_container_ids"] + row["spare_container_ids"]]
        by_id = {cid: container_document(row, cid) for cid in current_ids}
        network = {
            "Id": row["network_id"], "Name": row["network_name"], "Driver": "bridge",
            "Internal": True, "Attachable": False,
            "Options": {"com.docker.network.bridge.enable_icc": "false"},
            "Containers": {cid: {"Name": f"seat-{index}"} for index, cid in enumerate(by_id, start=1)},
        }

        def get(_socket, resource, _limit=4 * 1024 * 1024):
            if resource.startswith("/containers/"):
                return by_id[resource.split("/")[2]]
            return network

        with (
            mock.patch.object(probe, "docker_get", side_effect=get),
            mock.patch.object(probe, "probe_novnc") as novnc,
        ):
            result = probe.collect(row)
        self.assertEqual(len(result["verified_container_ids"]), 17)
        self.assertEqual(novnc.call_count, 17)
        self.assertEqual(result["unexpected_restart_events"], 0)
        self.assertIn(row["fault_replacement"]["replacement_spare_container_id"], result["formal_container_ids"])
        self.assertIn(replacement_id, result["spare_container_ids"])
        self.assertNotIn(row["fault_replacement"]["baseline_container_id"], result["verified_container_ids"])

    def test_collect_rejects_restart_or_extra_network_member(self):
        row = config()
        replacement_id = "f" * 64
        current_ids = [replacement_id if cid == row["fault_replacement"]["baseline_container_id"] else cid
                       for cid in row["formal_container_ids"] + row["spare_container_ids"]]
        with mock.patch.object(probe, "docker_get", return_value={
            "Id": row["network_id"], "Name": row["network_name"], "Driver": "bridge",
            "Internal": True, "Attachable": False,
            "Options": {"com.docker.network.bridge.enable_icc": "false"},
            "Containers": {cid: {} for cid in current_ids} | {"e" * 64: {}},
        }), mock.patch.object(probe, "probe_novnc"):
            with self.assertRaisesRegex(probe.SeatProbeError, "replacement set differs"):
                probe.collect(row)

    def test_planned_restart_must_change_lifecycle_exactly_once(self):
        row = config()
        target = row["planned_restart"]["container_id"]
        document = container_document(row, target)
        document["RestartCount"] = 0
        replacement_id = "f" * 64
        current_ids = [replacement_id if cid == row["fault_replacement"]["baseline_container_id"] else cid
                       for cid in row["formal_container_ids"] + row["spare_container_ids"]]
        network = {"Id": row["network_id"], "Name": row["network_name"], "Driver": "bridge",
                   "Internal": True, "Attachable": False,
                   "Options": {"com.docker.network.bridge.enable_icc": "false"},
                   "Containers": {cid: {} for cid in current_ids}}
        def get(_socket, resource, _limit=0):
            if resource.startswith("/networks/"): return network
            cid = resource.split("/")[2]
            return document if cid == target else container_document(row, cid)
        with mock.patch.object(probe, "docker_get", side_effect=get), mock.patch.object(probe, "probe_novnc"):
            with self.assertRaisesRegex(probe.SeatProbeError, "did not recover exactly once"):
                probe.collect(row)

    def test_builder_embeds_and_compiles_the_exact_config(self):
        raw = builder.render(probe.validate_config(config()))
        text = raw.decode()
        self.assertNotIn(builder.MARKER, text)
        self.assertIn("formal_container_ids", text)
        compile(text, "<seat-probe-test>", "exec")


if __name__ == "__main__":
    unittest.main()
