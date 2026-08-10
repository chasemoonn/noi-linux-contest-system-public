from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALLER_PATH = (
    REPO_ROOT
    / "noi-linux-official"
    / "rootfs"
    / "usr"
    / "local"
    / "bin"
    / "install-firefox-cookie-policy.py"
)
RUNTIME_PROBE_PATH = (
    REPO_ROOT
    / "noi-linux-official"
    / "rootfs"
    / "usr"
    / "local"
    / "bin"
    / "verify-firefox-cookie-runtime.py"
)


def load_installer():
    spec = importlib.util.spec_from_file_location("noi_firefox_policy_installer", INSTALLER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load Firefox policy installer")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FirefoxCookiePolicyInstallerTests(unittest.TestCase):
    def setUp(self):
        self.module = load_installer()
        self.temporary = tempfile.TemporaryDirectory(prefix="noi-firefox-policy-test.")
        root = Path(self.temporary.name)
        self.system_policy = root / "etc" / "firefox" / "policies" / "policies.json"
        self.distribution_policy = root / "usr" / "lib" / "firefox" / "distribution" / "policies.json"
        self.module.POLICY_PATHS = (self.system_policy, self.distribution_policy)

    def tearDown(self):
        self.temporary.cleanup()

    def run_installer(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self.module.main(), 0)

    def test_creates_minimal_firefox_79_cookie_policy(self):
        self.run_installer()
        document = json.loads(self.system_policy.read_text(encoding="utf-8"))
        self.assertEqual(
            document,
            {
                "policies": {
                    "Cookies": {
                        "AcceptThirdParty": "never",
                        "Default": True,
                        "Locked": True,
                    }
                }
            },
        )
        self.assertFalse(self.distribution_policy.exists())

    def test_preserves_unrelated_policy_fields(self):
        self.system_policy.parent.mkdir(parents=True)
        self.system_policy.write_text(
            json.dumps(
                {
                    "policies": {
                        "DisableAppUpdate": True,
                        "Cookies": {
                            "Allow": ["https://example.invalid"],
                            "Default": True,
                        },
                    }
                }
            ),
            encoding="utf-8",
        )
        self.run_installer()
        document = json.loads(self.system_policy.read_text(encoding="utf-8"))
        self.assertIs(document["policies"]["DisableAppUpdate"], True)
        self.assertEqual(
            document["policies"]["Cookies"]["Allow"],
            ["https://example.invalid"],
        )
        self.assertEqual(document["policies"]["Cookies"]["AcceptThirdParty"], "never")
        self.assertIs(document["policies"]["Cookies"]["Locked"], True)

    def test_rejects_conflicting_cookie_policy(self):
        self.system_policy.parent.mkdir(parents=True)
        self.system_policy.write_text(
            json.dumps({"policies": {"Cookies": {"Default": False}}}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(SystemExit, r"conflicting policies\.Cookies\.Default"):
            self.module.main()

    def test_rejects_integer_in_place_of_boolean_policy(self):
        self.system_policy.parent.mkdir(parents=True)
        self.system_policy.write_text(
            json.dumps({"policies": {"Cookies": {"Default": 1}}}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(SystemExit, r"conflicting policies\.Cookies\.Default"):
            self.module.main()

    def test_rejects_multiple_policy_sources(self):
        for path in self.module.POLICY_PATHS:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"policies": {}}', encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, "multiple Firefox policy sources"):
            self.module.main()

    def test_rejects_invalid_json_without_overwriting_it(self):
        self.system_policy.parent.mkdir(parents=True)
        original = "{not-json"
        self.system_policy.write_text(original, encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, "cannot read valid JSON"):
            self.module.main()
        self.assertEqual(self.system_policy.read_text(encoding="utf-8"), original)

    def test_rejects_symlink_policy_path(self):
        target = Path(self.temporary.name) / "outside.json"
        target.write_text('{"policies": {}}', encoding="utf-8")
        self.system_policy.parent.mkdir(parents=True)
        try:
            self.system_policy.symlink_to(target)
        except (NotImplementedError, OSError):
            self.skipTest("symlink creation unavailable")
        with self.assertRaisesRegex(SystemExit, "policy path must not be a symlink"):
            self.module.main()


class FirefoxCookieReleaseGateTests(unittest.TestCase):
    def test_runtime_probe_exercises_real_cookie_boundaries(self):
        source = RUNTIME_PROBE_PATH.read_text(encoding="utf-8")
        for required in (
            "HttpOnly; SameSite=Strict",
            'self.send_response(303)',
            'user_pref("network.cookie.cookieBehavior", 2)',
            "first-party cookie roundtrip",
            "third-party cookie rejection",
            "cookie persistence after restart",
            'url=http://127.0.0.2:',
            '"--profile"',
        ):
            self.assertIn(required, source)

    def test_dockerfile_and_verifier_make_cookie_policy_mandatory(self):
        dockerfile = (REPO_ROOT / "noi-linux-official" / "Dockerfile").read_text(
            encoding="utf-8"
        )
        verifier = (REPO_ROOT / "deploy" / "verify-contest-image-local.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("install-firefox-cookie-policy.py", dockerfile)
        self.assertIn("python3 /usr/local/bin/install-firefox-cookie-policy.py", dockerfile)
        self.assertIn('"AcceptThirdParty": "never"', verifier)
        self.assertIn("verify-firefox-cookie-runtime.py", verifier)
        self.assertIn('firefox --version | grep -Fq "Mozilla Firefox 79."', verifier)


if __name__ == "__main__":
    unittest.main()
