import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_v1_product_contract.py"
SPEC = importlib.util.spec_from_file_location("check_v1_product_contract", SCRIPT)
contract = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(contract)


SAFE_PLUGIN = """
const rid = await RecordModel.add(
    DOMAIN, pdoc.docId, uid, lang, normalized, true,
    { contest: tdoc.docId, type: 'judge' },
);
"""


class V1OjAppendOnlyTests(unittest.TestCase):
    def test_current_plugin_is_append_only(self):
        plugin = (ROOT / "hydro-plugin-orchestrator" / "index.js").read_text(
            encoding="utf-8"
        )
        self.assertEqual(contract.append_only_record_failures(plugin), [])

    def test_rejudge_update_and_delete_are_each_rejected(self):
        mutations = (
            "RecordModel.reset(DOMAIN, rid, true);",
            "RecordModel.update(DOMAIN, rid, { score: 100 });",
            "RecordModel.coll.updateOne({ _id: rid }, { $set: { score: 100 } });",
            "RecordModel.coll.deleteOne({ _id: rid });",
            "const request = { operation: 'rejudge' };",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                failures = contract.append_only_record_failures(
                    SAFE_PLUGIN + "\n" + mutation
                )
                self.assertTrue(failures)
                self.assertTrue(
                    any("may not mutate existing OJ records" in item for item in failures)
                )

    def test_second_record_creation_site_is_rejected(self):
        failures = contract.append_only_record_failures(
            SAFE_PLUGIN + "\n" + SAFE_PLUGIN
        )
        self.assertIn(
            "Hydro plugin must have exactly one append-only record creation site",
            failures,
        )


if __name__ == "__main__":
    unittest.main()
