from __future__ import annotations

from html.parser import HTMLParser
import importlib.util
import ipaddress
from pathlib import Path
import re
import struct
import tempfile
import unittest
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "build_demo.py"
DEMO_DIR = ROOT / "docs" / "demo"
SCREENSHOT_DIR = ROOT / "docs" / "screenshots"


def _load_builder():
    spec = importlib.util.spec_from_file_location("build_demo", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _png_chunk_types(data: bytes) -> tuple[bytes, ...]:
    offset = 8
    chunks: list[bytes] = []
    while offset + 12 <= len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        chunk_type = data[offset + 4 : offset + 8]
        chunks.append(chunk_type)
        offset += 12 + length
        if chunk_type == b"IEND":
            break
    return tuple(chunks)


class _AssetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.password_inputs: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        values = dict(attrs)
        if tag in {"a", "link"} and values.get("href"):
            self.links.append(values["href"])
        if tag == "input" and values.get("type") == "password":
            self.password_inputs.append(values)


class DemoAssetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.builder = _load_builder()
        cls.assets = dict(cls.builder.render_assets())

    def test_expected_static_states_are_generated(self):
        self.assertEqual(
            set(self.assets),
            {
                "collection-report.html",
                "demo.css",
                "index.html",
                "student-login.html",
                "teacher-status.html",
            },
        )
        for filename in (
            "teacher-status.html",
            "student-login.html",
            "collection-report.html",
        ):
            self.assertIn('name="demo-data" content="synthetic-only"', self.assets[filename])

    def test_checked_in_assets_match_deterministic_renderer(self):
        self.assertEqual(self.builder.check_assets(DEMO_DIR), ())

    def test_two_independent_builds_are_byte_identical(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            first_dir = Path(first)
            second_dir = Path(second)
            self.builder.write_assets(first_dir)
            self.builder.write_assets(second_dir)
            first_bytes = {
                path.name: path.read_bytes() for path in sorted(first_dir.iterdir())
            }
            second_bytes = {
                path.name: path.read_bytes() for path in sorted(second_dir.iterdir())
            }
        self.assertEqual(first_bytes, second_bytes)

    def test_demo_contains_no_known_site_identity_or_secret_material(self):
        text = "\n".join(self.assets.values())
        lowered = text.lower()
        # Assemble deployment sentinels at runtime so the public test source
        # does not itself preserve a production identifier verbatim.
        forbidden = (
            "quxi" + "nao",
            "xw" + "jedu",
            ".".join(("8", "210", "61", "7")),
            ".".join(("43", "129", "174", "220")),
            ".".join(("114", "55", "0", "198")),
            "i-" + "bp1fjgtm0njvcgwks2y3",
            "xudradma" + "koskanm+ejs5nnjfjx0zjcws43rrrksfec0",
            "github_pat_",
            "ghp_",
            "-----begin private key-----",
        )
        for value in forbidden:
            with self.subTest(value=value):
                self.assertNotIn(value, lowered)
        self.assertNotRegex(text, r"\b(?:LTAI|AKIA|ASIA)[A-Z0-9]{12,}\b")
        self.assertNotRegex(text, r"\b[a-fA-F0-9]{32,}\b")
        self.assertNotRegex(text, r"(?i)\btoken\b")

    def test_network_examples_use_only_reserved_publication_values(self):
        text = "\n".join(self.assets.values())
        allowed_networks = tuple(
            ipaddress.ip_network(cidr)
            for cidr in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
        )
        addresses = set(re.findall(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])", text))
        self.assertTrue(addresses)
        for value in addresses:
            address = ipaddress.ip_address(value)
            with self.subTest(address=value):
                self.assertTrue(any(address in network for network in allowed_networks))
        urls = re.findall(r"https?://[^\s<]+", text)
        self.assertTrue(urls)
        for url in urls:
            hostname = urlsplit(url.rstrip(".,;）)")).hostname
            with self.subTest(url=url):
                self.assertTrue(hostname == "example.test" or hostname.endswith(".example.test"))

    def test_login_demo_has_no_prefilled_credential(self):
        parser = _AssetParser()
        parser.feed(self.assets["student-login.html"])
        self.assertEqual(len(parser.password_inputs), 1)
        credential = parser.password_inputs[0]
        self.assertNotIn("value", credential)
        self.assertIn("disabled", credential)

    def test_every_local_html_link_resolves_inside_demo_directory(self):
        expected = set(self.assets)
        for filename, content in self.assets.items():
            if not filename.endswith(".html"):
                continue
            parser = _AssetParser()
            parser.feed(content)
            self.assertTrue(parser.links, filename)
            for href in parser.links:
                split = urlsplit(href)
                with self.subTest(page=filename, href=href):
                    self.assertFalse(split.scheme or split.netloc)
                    target = unquote(split.path) or filename
                    resolved = (Path(filename).parent / target).as_posix()
                    self.assertNotIn("..", Path(resolved).parts)
                    self.assertIn(resolved, expected)

    def test_screenshot_guide_links_resolve(self):
        guide = SCREENSHOT_DIR / "README.md"
        text = guide.read_text(encoding="utf-8")
        links = re.findall(r"\[[^]]+\]\(([^)]+)\)", text)
        self.assertTrue(links)
        for target in links:
            split = urlsplit(target)
            with self.subTest(target=target):
                self.assertFalse(split.scheme or split.netloc)
                resolved = (guide.parent / unquote(split.path)).resolve()
                resolved.relative_to(ROOT.resolve())
                self.assertTrue(resolved.exists(), resolved)

    def test_rendered_screenshots_are_real_fixed_viewport_pngs(self):
        for name in ("teacher-status", "student-login", "collection-report"):
            path = SCREENSHOT_DIR / f"{name}.png"
            data = path.read_bytes()
            with self.subTest(path=path.name):
                self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
                self.assertGreater(len(data), 10_000)
                self.assertEqual(struct.unpack(">II", data[16:24]), (1440, 900))
                chunks = _png_chunk_types(data)
                self.assertIn(b"IEND", chunks)
                self.assertTrue({b"tEXt", b"zTXt", b"iTXt"}.isdisjoint(chunks))


if __name__ == "__main__":
    unittest.main()
