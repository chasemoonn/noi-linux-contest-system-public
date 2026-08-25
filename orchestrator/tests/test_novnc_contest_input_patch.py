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
        self.assertIn("contestSyncModifiers", patched)
        self.assertIn("e.shiftKey", patched)
        self.assertIn("e.ctrlKey", patched)
        self.assertIn("e.altKey", patched)
        self.assertIn("e.metaKey", patched)
        self.assertIn("delete this._keyDownList[code]", patched)
        self.assertEqual(self.patcher.patch_keyboard(patched), patched)

    def test_modifier_patch_supports_official_focal_novnc_prototype_source(self):
        source = """Keyboard.prototype = {
    // ===== PRIVATE METHODS =====

    _allKeysUp: function () {
    },

    // ===== PUBLIC METHODS =====

    grab: function () {
    },

    ungrab: function () {
    },
};
"""
        patched = self.patcher.patch_keyboard(source)
        self.assertIn(
            "_releaseStaleModifier: function (codes, pressed)", patched
        )
        self.assertIn("syncModifiers: function (e)", patched)
        self.assertIn("contestSyncModifiers", patched)
        self.assertIn("delete this._keyDownList[code]", patched)
        self.assertNotIn("_interruptAltGrSequence", patched)
        self.assertEqual(self.patcher.patch_keyboard(patched), patched)

    def test_mouse_down_synchronizes_modifiers_before_forwarding_click(self):
        source = """        if ((ev.type === 'click') || (ev.type === 'contextmenu')) {
            return;
        }

        let pos = clientToElement(ev.clientX, ev.clientY,
                                  this._canvas);
"""
        patched = self.patcher.patch_rfb(source)
        sync = patched.index("contestSyncModifiersFromMouse")
        position = patched.index("let pos = clientToElement")
        self.assertLess(sync, position)
        self.assertEqual(self.patcher.patch_rfb(patched), patched)

    def test_official_focal_mouse_event_is_forwarded_to_legacy_rfb(self):
        rfb_source = """RFB.prototype = {
    _handleMouseButton: function (x, y, down, bmask) {
        this._mouse_buttonMask = bmask;
    },
};
"""
        mouse_source = """        Log.Debug("onmousebutton");
        this.onmousebutton(pos.x, pos.y, down, bmask);

        stopEvent(e);
"""
        patched_rfb = self.patcher.patch_rfb(rfb_source)
        patched_mouse = self.patcher.patch_legacy_mouse(mouse_source)
        self.assertIn("contestLegacyModifierEvent", patched_rfb)
        self.assertIn("down && e", patched_rfb)
        self.assertIn("this._keyboard.syncModifiers(e)", patched_rfb)
        self.assertIn("contestModifierEvent", patched_mouse)
        self.assertIn("down, bmask, e", patched_mouse)
        self.assertEqual(self.patcher.patch_rfb(patched_rfb), patched_rfb)
        self.assertEqual(
            self.patcher.patch_legacy_mouse(patched_mouse), patched_mouse
        )

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
            "contestSyncModifiers",
            "numpad-and-stale-modifier-v1",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, dockerfile)


if __name__ == "__main__":
    unittest.main()
