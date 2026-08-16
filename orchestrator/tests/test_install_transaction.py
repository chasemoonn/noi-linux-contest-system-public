import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "orchestrator"))

from services import install_transaction as tx


def receipt(phase, action):
    return {"phase": phase, "action": action, "status": "verified",
            "evidence_sha256": hashlib.sha256(f"{phase}:{action}".encode()).hexdigest()}


class Driver:
    def __init__(self, phase, events, fail=False): self.phase=phase; self.events=events; self.fail=fail
    def apply(self, context):
        self.events.append("apply:"+self.phase)
        if self.fail: raise RuntimeError("fail")
        return receipt(self.phase, "apply")
    def rollback(self, context, previous):
        self.events.append("rollback:"+self.phase+(":uncertain" if previous is None else "")); return receipt(self.phase, "rollback")
    def commit_cleanup(self, context, previous):
        self.events.append("cleanup:"+self.phase)
        return {"phase":self.phase,"action":"commit_cleanup","status":"verified"}


class InstallTransactionTests(unittest.TestCase):
    def drivers(self, events, fail=None): return {phase: Driver(phase,events,phase==fail) for phase in tx.PHASES}
    def verify(self, context):
        return {"status":"rollback_verified","plan_id":context.plan_id,
                "backup_manifest_sha256":context.backup_manifest_sha256}

    def test_success_is_exact_order_and_durable_commit(self):
        with tempfile.TemporaryDirectory() as raw:
            events=[]; result=tx.run(Path(raw),"1"*64,"2"*64,self.drivers(events),self.verify)
            self.assertEqual(events,["apply:"+phase for phase in tx.PHASES]+["cleanup:"+phase for phase in tx.PHASES])
            self.assertEqual(result["status"],"committed")
            self.assertFalse((Path(raw)/"service-install.pending.json").exists())
            self.assertTrue((Path(raw)/("service-install.committed-"+"1"*64+".json")).exists())
            again=tx.run(Path(raw),"1"*64,"2"*64,self.drivers([]),self.verify)
            self.assertEqual(again["status"],"committed")

    def test_clean_install_has_its_own_exact_apply_and_safe_rollback_order(self):
        with tempfile.TemporaryDirectory() as raw:
            events=[];drivers={phase:Driver(phase,events,phase=="controller") for phase in tx.CLEAN_PHASES}
            result=tx.run_clean(Path(raw),"1"*64,"2"*64,drivers,self.verify)
            self.assertEqual(events,["apply:"+phase for phase in tx.CLEAN_PHASES[:5]]+[
                "rollback:controller:uncertain","rollback:hydro_integration",
                "rollback:closed_frontend","rollback:clean_materials","rollback:source_release"])
            self.assertEqual(result["status"],"rollback_verified")

    def test_cleanup_failure_never_rolls_back_a_durable_commit_and_is_retried(self):
        class CleanupFailsOnce(Driver):
            failed=False
            def commit_cleanup(self,context,previous):
                self.events.append("cleanup:"+self.phase)
                if not self.failed:
                    self.failed=True;raise RuntimeError("cleanup interrupted")
                return {"phase":self.phase,"action":"commit_cleanup","status":"verified"}
        with tempfile.TemporaryDirectory() as raw:
            events=[];drivers=self.drivers(events);drivers["controller"]=CleanupFailsOnce("controller",events)
            with self.assertRaisesRegex(RuntimeError,"cleanup interrupted"):
                tx.run(Path(raw),"1"*64,"2"*64,drivers,self.verify)
            root=Path(raw);self.assertTrue((root/("service-install.committed-"+"1"*64+".json")).exists())
            self.assertFalse((root/"service-install.pending.json").exists())
            result=tx.run(root,"1"*64,"2"*64,drivers,self.verify)
            self.assertEqual(result["status"],"committed")
            self.assertTrue((root/("service-install.cleanup-"+"1"*64+".json")).exists())

    def test_failure_rolls_back_only_completed_phases_in_reverse(self):
        with tempfile.TemporaryDirectory() as raw:
            events=[]; result=tx.run(Path(raw),"1"*64,"2"*64,self.drivers(events,"closed_frontend"),self.verify)
            self.assertEqual(events,["apply:source_release","apply:controller_quiesce","apply:hydro_integration","apply:closed_frontend",
                                     "rollback:hydro_integration","rollback:closed_frontend:uncertain","rollback:controller_quiesce","rollback:source_release"])
            self.assertEqual(result["status"],"rollback_verified")
            self.assertFalse((Path(raw)/"service-install.pending.json").exists())
            self.assertTrue((Path(raw)/("service-install.rollback_verified-"+"1"*64+".json")).exists())

    def test_controller_failure_restores_hydro_before_public_caddy_baseline(self):
        with tempfile.TemporaryDirectory() as raw:
            events=[]; result=tx.run(Path(raw),"1"*64,"2"*64,self.drivers(events,"controller"),self.verify)
            self.assertEqual(events,["apply:source_release","apply:controller_quiesce","apply:hydro_integration","apply:closed_frontend",
                                     "apply:controller","rollback:controller:uncertain",
                                     "rollback:hydro_integration","rollback:closed_frontend",
                                     "rollback:controller_quiesce","rollback:source_release"])
            self.assertEqual(result["status"],"rollback_verified")

    def test_existing_applying_journal_never_continues_forward(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); journal=tx.initial_journal("1"*64,"2"*64)
            journal["completed"]=["source_release"]; journal["receipts"]={"source_release":receipt("source_release","apply")}; journal["next_phase"]="controller_quiesce"
            tx._atomic_json(root/"service-install.pending.json",journal); events=[]
            result=tx.run(root,"1"*64,"2"*64,self.drivers(events),self.verify)
            self.assertEqual(events,["rollback:source_release"]); self.assertEqual(result["failure"],"interrupted_apply")

    def test_existing_in_progress_intent_is_rolled_back_before_completed_phase(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); journal=tx.initial_journal("1"*64,"2"*64)
            journal["completed"]=["source_release"]; journal["receipts"]={"source_release":receipt("source_release","apply")}
            journal["next_phase"]="controller_quiesce"; journal["in_progress"]="controller_quiesce"
            tx._atomic_json(root/"service-install.pending.json",journal); events=[]
            result=tx.run(root,"1"*64,"2"*64,self.drivers(events),self.verify)
            self.assertEqual(events,["rollback:controller_quiesce:uncertain","rollback:source_release"])
            self.assertEqual(result["status"],"rollback_verified")
            # Reopening a completed rollback is stable and never invokes a
            # second uncertain-phase rollback.
            events.clear(); again=tx.run(root,"1"*64,"2"*64,self.drivers(events),self.verify)
            self.assertEqual(again["status"],"rollback_verified"); self.assertEqual(events,[])

    def test_rollback_failure_is_manual_intervention(self):
        class Bad(Driver):
            def rollback(self, context, previous): raise RuntimeError("rollback failed")
        with tempfile.TemporaryDirectory() as raw:
            events=[]; drivers=self.drivers(events,"hydro_integration"); drivers["source_release"]=Bad("source_release",events)
            with self.assertRaisesRegex(RuntimeError,"rollback failed"):
                tx.run(Path(raw),"1"*64,"2"*64,drivers,self.verify)
            row=tx._read_exact(Path(raw)/"service-install.pending.json")
            self.assertEqual(row["status"],"manual_intervention")

    def test_clean_rollback_cleanup_is_retried_without_repeating_live_verification(self):
        class Final:
            verifies=0;cleanups=0
            def __call__(self,context):
                self.verifies+=1;return {"status":"rollback_verified","plan_id":context.plan_id,
                    "backup_manifest_sha256":context.backup_manifest_sha256}
            def rollback_cleanup(self,context):
                self.cleanups+=1
                if self.cleanups==1:raise RuntimeError("cleanup interrupted")
                return {"phase":"final_rollback","action":"cleanup","status":"verified"}
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);events=[];drivers={phase:Driver(phase,events,phase=="controller") for phase in tx.CLEAN_PHASES}
            final=Final()
            with self.assertRaisesRegex(RuntimeError,"cleanup interrupted"):
                tx.run_clean(root,"1"*64,"2"*64,drivers,final)
            self.assertEqual(final.verifies,1);self.assertEqual(final.cleanups,1)
            self.assertEqual(tx._read_exact(root/"service-install.pending.json")["status"],"rolling_back")
            result=tx.run_clean(root,"1"*64,"2"*64,drivers,final)
            self.assertEqual(result["status"],"rollback_verified")
            self.assertEqual(final.verifies,1);self.assertEqual(final.cleanups,2)
            self.assertTrue((root/("service-install.rollback-cleanup-"+"1"*64+".json")).exists())

    def test_clean_qualification_hook_runs_only_after_durable_phase_receipt(self):
        class Injected(RuntimeError): pass
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);events=[];drivers={phase:Driver(phase,events) for phase in tx.CLEAN_PHASES}
            observed=[]
            def inject(context,phase,receipt):
                journal=tx._read_exact(root/"service-install.pending.json")
                observed.append((phase,receipt,journal["completed"][:],journal["in_progress"]))
                if phase=="hydro_integration":raise Injected("qualification failure")
            result=tx.run_clean(root,"1"*64,"2"*64,drivers,self.verify,after_phase_committed=inject)
            self.assertEqual(result["status"],"rollback_verified")
            self.assertEqual([row[0] for row in observed],["source_release","clean_materials","hydro_integration"])
            self.assertEqual(observed[-1][2],["source_release","clean_materials","hydro_integration"])
            self.assertIsNone(observed[-1][3])
            self.assertNotIn("apply:closed_frontend",events)

    def test_crash_after_final_receipt_only_clears_matching_pending(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); journal=tx.initial_journal("1"*64,"2"*64)
            journal["completed"]=list(tx.PHASES)
            journal["receipts"]={phase:receipt(phase,"apply") for phase in tx.PHASES}
            journal["next_phase"]=None; journal["status"]="committed"
            tx._atomic_json(root/"service-install.pending.json",journal)
            tx._atomic_json(root/("service-install.committed-"+"1"*64+".json"),journal)
            events=[]; result=tx.run(root,"1"*64,"2"*64,self.drivers(events),self.verify)
            self.assertEqual(result["status"],"committed")
            self.assertEqual(events,["cleanup:"+phase for phase in tx.PHASES])
            self.assertFalse((root/"service-install.pending.json").exists())

    def test_finalization_seals_pending_before_publishing_final_receipt(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); pending=root/"service-install.pending.json"
            journal=tx.initial_journal("1"*64,"2"*64)
            tx._atomic_json(pending,journal); original=tx._atomic_json; calls=[]
            def record(path,value):
                calls.append((path.name,value["status"])); return original(path,value)
            with mock.patch.object(tx,"_atomic_json",side_effect=record):
                tx._finalize(root,pending,journal,"committed")
            self.assertEqual(calls[0],("service-install.pending.json","committed"))
            self.assertEqual(calls[1],("service-install.committed-"+"1"*64+".json","committed"))


if __name__=="__main__": unittest.main()
