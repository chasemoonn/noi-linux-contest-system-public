import unittest
from unittest.mock import patch

from services.hydro import Hydro


class FakeCollection:
    def __init__(self, rows):
        self.rows = rows
        self.last_query = None

    def find(self, query, projection):
        self.last_query = query
        return self.rows

    def find_one(self, query, projection=None):
        self.last_query = query
        return next(
            (
                row
                for row in self.rows
                if all(row.get(key) == value for key, value in query.items())
            ),
            None,
        )


class FakeDB:
    def __init__(self):
        self.collections = {
            "document.status": FakeCollection([{"uid": 8}]),
            "user": FakeCollection([{"_id": 8, "uname": "alice"}]),
            "document": FakeCollection(
                [
                    {
                        "domainId": "system",
                        "docType": 10,
                        "docId": 101,
                        "pid": "P1001",
                        "title": "test",
                    }
                ]
            ),
        }

    def __getitem__(self, name):
        return self.collections[name]


class HydroTests(unittest.TestCase):
    def test_roster_reads_document_status(self):
        hydro = Hydro.__new__(Hydro)
        hydro.domain_id = "system"
        hydro.db = FakeDB()
        rows = hydro.roster("0" * 24)
        self.assertEqual(rows, [{"uid": 8, "uname": "alice"}])
        query = hydro.db["document.status"].last_query
        self.assertEqual(query["docType"], 30)
        self.assertEqual(query["domainId"], "system")

    def test_get_problem_resolves_alias_for_registration_preflight(self):
        hydro = Hydro.__new__(Hydro)
        hydro.domain_id = "system"
        hydro.db = FakeDB()

        problem = hydro.get_problem("P1001")

        self.assertEqual(problem["docId"], 101)
        self.assertEqual(hydro.db["document"].last_query["docType"], 10)

    @patch("services.hydro.requests.post")
    def test_login_requires_redirect_and_sid(self, post):
        post.return_value.status_code = 302
        post.return_value.headers = {"set-cookie": "sid=abc; Path=/"}
        hydro = Hydro.__new__(Hydro)
        hydro.base_url = "https://example.test"
        self.assertTrue(hydro.verify_login("alice", "secret"))


if __name__ == "__main__":
    unittest.main()
