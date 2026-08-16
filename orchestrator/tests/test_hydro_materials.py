import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock

import requests

from services.hydro_materials import HydroMaterialPublisher


class HydroMaterialPublisherTests(unittest.TestCase):
    def client(self) -> HydroMaterialPublisher:
        client = HydroMaterialPublisher(
            "http://127.0.0.1:8888",
            "test-token-that-is-at-least-thirty-two-characters",
        )
        client.session.post = MagicMock()
        return client

    def test_rejects_plain_http_to_a_non_loopback_host(self):
        with self.assertRaisesRegex(ValueError, "loopback"):
            HydroMaterialPublisher(
                "http://oj.example.test",
                "test-token-that-is-at-least-thirty-two-characters",
            )

    def test_publishes_only_the_fixed_attachment_set_and_validates_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paper = b"%PDF-1.7\nfixture"
            testdata = b"fixture tar gzip bytes"
            paper_path = root / "paper.pdf"
            testdata_path = root / "testdata.tar.gz"
            paper_path.write_bytes(paper)
            testdata_path.write_bytes(testdata)
            client = self.client()

            def respond(*_args, **kwargs):
                request = kwargs["json"]
                body = {
                    "status": "published",
                    "publication_id": request["publication_id"],
                    "tid": request["tid"],
                    "revision": request["revision"],
                    "attachments": [
                        {
                            "name": item["name"],
                            "sha256": item["sha256"],
                            "size": len(__import__("base64").b64decode(item["content_base64"])),
                        }
                        for item in request["attachments"]
                    ],
                }
                response = MagicMock()
                response.ok = True
                response.status_code = 200
                response.json.return_value = body
                return response

            client.session.post.side_effect = respond
            result = client.publish(
                tid="1234567890abcdef12345678",
                revision="release-1",
                paper_path=paper_path,
                paper_sha256=hashlib.sha256(paper).hexdigest(),
                testdata_path=testdata_path,
                testdata_sha256=hashlib.sha256(testdata).hexdigest(),
            )

            self.assertTrue(result["ok"])
            self.assertRegex(result["publication_id"], r"^[0-9a-f]{64}$")
            self.assertRegex(result["receipt_sha256"], r"^[0-9a-f]{64}$")
            call = client.session.post.call_args
            self.assertFalse(call.kwargs["allow_redirects"])
            self.assertEqual(
                [item["name"] for item in call.kwargs["json"]["attachments"]],
                ["01_比赛题面.pdf", "02_辅助自测数据.tar.gz"],
            )
            self.assertFalse(client.session.trust_env)

    def test_rejects_changed_local_bytes_before_network_io(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paper_path = root / "paper.pdf"
            data_path = root / "testdata.tar.gz"
            paper_path.write_bytes(b"paper")
            data_path.write_bytes(b"data")
            client = self.client()
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                client.publish(
                    tid="1234567890abcdef12345678",
                    revision="release-1",
                    paper_path=paper_path,
                    paper_sha256="0" * 64,
                    testdata_path=data_path,
                    testdata_sha256=hashlib.sha256(b"data").hexdigest(),
                )
            client.session.post.assert_not_called()

    def test_transport_failure_is_retryable_without_exposing_material_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paper = b"paper-secret-bytes"
            data = b"data-secret-bytes"
            paper_path = root / "paper.pdf"
            data_path = root / "testdata.tar.gz"
            paper_path.write_bytes(paper)
            data_path.write_bytes(data)
            client = self.client()
            client.session.post.side_effect = requests.Timeout("fixture timeout")

            result = client.publish(
                tid="1234567890abcdef12345678",
                revision="release-1",
                paper_path=paper_path,
                paper_sha256=hashlib.sha256(paper).hexdigest(),
                testdata_path=data_path,
                testdata_sha256=hashlib.sha256(data).hexdigest(),
            )

            self.assertEqual(result["ok"], False)
            self.assertEqual(result["retryable"], True)
            self.assertNotIn("paper-secret-bytes", str(result))
            self.assertNotIn("data-secret-bytes", str(result))


if __name__ == "__main__":
    unittest.main()
