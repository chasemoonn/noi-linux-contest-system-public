import unittest

from services.problem_mapping import ProblemMappingError, auto_problem_mapping


class ProblemMappingTests(unittest.TestCase):
    def test_uses_hydro_order_and_filename_then_public_pid_then_fallback(self):
        documents = {
            11: {"docId": 11, "pid": "P1001", "config": "filename: books.in\n"},
            12: {"docId": 12, "pid": "study", "config": {}},
            13: {"docId": 13, "pid": "中文题号", "config": {}},
        }
        files, pids, details = auto_problem_mapping(
            {"pids": [13, 11, 12]}, documents.get
        )
        self.assertEqual(files, ["problem1", "books", "study"])
        self.assertEqual(
            pids,
            {"problem1": "中文题号", "books": "P1001", "study": "study"},
        )
        self.assertEqual([item["doc_id"] for item in details], [13, 11, 12])
        self.assertEqual(details[1]["input_filename"], "books.in")
        self.assertEqual(details[1]["source"], "config.filename")

    def test_duplicate_filename_and_pid_conflicts_fall_back_stably(self):
        documents = {
            1: {"docId": 1, "pid": "same", "config": {"filename": "dup"}},
            2: {"docId": 2, "pid": "same", "config": {"filename": "dup.out"}},
            3: {"docId": 3, "pid": "problem1", "config": {}},
        }
        files, pids, details = auto_problem_mapping(
            {"pids": [1, 2, 3]}, documents.get
        )
        # Duplicate filename and duplicate public pid cannot silently collide;
        # the valid unique public pid problem1 keeps priority.
        self.assertEqual(files, ["problem2", "problem3", "problem1"])
        self.assertEqual(len(set(files)), 3)
        self.assertEqual([item["source"] for item in details], ["fallback", "fallback", "pid"])
        self.assertEqual(list(pids), files)

    def test_each_document_must_match_the_contest_doc_id(self):
        with self.assertRaisesRegex(ProblemMappingError, "不属于本场"):
            auto_problem_mapping(
                {"pids": [7]},
                lambda _: {"docId": 8, "pid": "apple", "config": {}},
            )


if __name__ == "__main__":
    unittest.main()
