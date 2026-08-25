import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
PATCHER = (
    ROOT
    / "noi-linux-official"
    / "rootfs"
    / "usr"
    / "local"
    / "bin"
    / "patch-novnc-contest-input.py"
)


def load_patcher():
    spec = importlib.util.spec_from_file_location("novnc_contest_input", PATCHER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class NoVNCContestInputPatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.patcher = load_patcher()

    def test_numpad_patch_uses_physical_keycode_and_is_idempotent(self):
        source = """export function getKey(evt) {
    // Are we getting a proper key value?
    return evt.key;
}
"""
        patched = self.patcher.patch_util(source)
        self.assertIn("const contestNumpadKeys", patched)
        self.assertIn("Numpad0: '0'", patched)
        self.assertIn("NumpadDivide: '/'", patched)
        self.assertIn("contestNumpadKeys[getKeycode(evt)]", patched)
        self.assertEqual(self.patcher.patch_util(patched), patched)

    def test_modifier_patch_releases_only_modifiers_not_physically_held(self):
        source = """export default class Keyboard {
    // ===== PUBLIC METHODS =====

    grab() {
    }

    ungrab() {
    }
}
"""
        patched = self.patcher.patch_keyboard(source)
        self.assertIn("_releaseStaleModifier(codes, pressed)", patched)
        self.assertIn("syncModifiers(e)", patched)
        self.assertIn("e.shiftKey", patched)
        self.assertIn("e.ctrlKey", patched)
        self.assertIn("e.altKey", patched)
        self.assertIn("e.metaKey", patched)
        self.assertEqual(self.patcher.patch_keyboard(patched), patched)

    def test_mouse_down_synchronizes_modifiers_before_forwarding_click(self):
        source = """        if ((ev.type === 'click') || (ev.type === 'contextmenu')) {
            return;
        }

        let pos = clientToElement(ev.clientX, ev.clientY,
                                  this._canvas);
"""
        patched = self.patcher.patch_rfb(source)
        sync = patched.index("this._keyboard.syncModifiers(ev)")
        position = patched.index("let pos = clientToElement")
        self.assertLess(sync, position)
        self.assertEqual(self.patcher.patch_rfb(patched), patched)

    def test_unfamiliar_source_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "unexpected noVNC"):
            self.patcher.patch_util("export function unknown() {}\n")

    def test_official_image_build_applies_and_labels_patch(self):
        dockerfile = (ROOT / "noi-linux-official" / "Dockerfile").read_text(
            encoding="utf-8"
        )
        for marker in (
            "patch-novnc-contest-input.py",
            "contestNumpadKeys",
            "syncModifiers(e)",
            "numpad-and-stale-modifier-v1",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, dockerfile)


if __name__ == "__main__":
    unittest.main()
