#!/usr/bin/env python3
"""Apply the small contest keyboard fixes to Ubuntu Focal's noVNC files.

The official NOI Linux desktop remains unchanged. This patch only normalizes
browser keyboard events before noVNC forwards them to TigerVNC:

* physical numeric-keypad keys always enter their numeric/operator value;
* a mouse click releases modifiers whose key-up event was lost by the browser.

Every edit is anchored to the packaged noVNC source and fails closed when that
source is unfamiliar. Re-running the patch is safe.
"""

from __future__ import annotations

from pathlib import Path
import sys


def _replace_once(text: str, old: str, new: str, marker: str, label: str) -> str:
    if marker in text:
        return text
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"unexpected noVNC {label} source: anchor count={count}")
    return text.replace(old, new, 1)


def patch_util(text: str) -> str:
    old = """export function getKey(evt) {
    // Are we getting a proper key value?
"""
    new = """export function getKey(evt) {
    // Contest workstations use the numeric keypad for entering numbers and
    // operators. Some host browser/OS combinations report navigation keys
    // here when Num Lock is on (or report digits when it is off), causing
    // noVNC to invert the keypad. Prefer the physical Numpad code so the
    // keypad remains numeric regardless of the host Num Lock state.
    const contestNumpadKeys = {
        Numpad0: '0',
        Numpad1: '1',
        Numpad2: '2',
        Numpad3: '3',
        Numpad4: '4',
        Numpad5: '5',
        Numpad6: '6',
        Numpad7: '7',
        Numpad8: '8',
        Numpad9: '9',
        NumpadDecimal: '.',
        NumpadAdd: '+',
        NumpadSubtract: '-',
        NumpadMultiply: '*',
        NumpadDivide: '/',
    };
    const contestNumpadKey = contestNumpadKeys[getKeycode(evt)];
    if (contestNumpadKey !== undefined) {
        return contestNumpadKey;
    }

    // Are we getting a proper key value?
"""
    return _replace_once(text, old, new, "contestNumpadKeys", "util.js")


def patch_keyboard(text: str) -> str:
    modern_private = """    // ===== PUBLIC METHODS =====

    grab() {
"""
    modern_private_patched = """    _releaseStaleModifier(codes, pressed) {
        if (pressed) {
            return;
        }
        for (const code of codes) {
            if (code in this._keyDownList) {
                this._sendKeyEvent(this._keyDownList[code], code, false);
                delete this._keyDownList[code];
            }
        }
    }

    // ===== PUBLIC METHODS =====

    grab() {
"""
    legacy_private = """    // ===== PUBLIC METHODS =====

    grab: function () {
"""
    legacy_private_patched = """    _releaseStaleModifier: function (codes, pressed) {
        if (pressed) {
            return;
        }
        for (var i = 0; i < codes.length; i++) {
            var code = codes[i];
            if (code in this._keyDownList) {
                this._sendKeyEvent(this._keyDownList[code], code, false);
                delete this._keyDownList[code];
            }
        }
    },

    // ===== PUBLIC METHODS =====

    grab: function () {
"""
    if "_releaseStaleModifier(codes, pressed)" not in text \
            and "_releaseStaleModifier: function (codes, pressed)" not in text:
        modern_count = text.count(modern_private)
        legacy_count = text.count(legacy_private)
        if modern_count + legacy_count != 1:
            raise RuntimeError(
                "unexpected noVNC keyboard.js private helper source: "
                f"modern={modern_count} legacy={legacy_count}"
            )
        if modern_count == 1:
            text = text.replace(modern_private, modern_private_patched, 1)
        else:
            text = text.replace(legacy_private, legacy_private_patched, 1)

    modern_public = """    ungrab() {
"""
    modern_public_patched = """    syncModifiers(e) {
        // contestSyncModifiers: release only stale browser modifiers.
        // A browser can lose a keyup while focus or IME state is changing.
        // Mouse events still carry the host's current modifier state, so use
        // them to release only modifiers that are no longer physically held.
        // Intentional Shift/Ctrl/Alt/Meta + click therefore keeps working.
        this._interruptAltGrSequence();
        this._releaseStaleModifier(['ShiftLeft', 'ShiftRight'], e.shiftKey);
        this._releaseStaleModifier(['ControlLeft', 'ControlRight'], e.ctrlKey);
        this._releaseStaleModifier(['AltLeft', 'AltRight'], e.altKey);
        this._releaseStaleModifier(['MetaLeft', 'MetaRight'], e.metaKey);
    }

    ungrab() {
"""
    legacy_public = """    ungrab: function () {
"""
    legacy_public_patched = """    syncModifiers: function (e) {
        // contestSyncModifiers: release only stale browser modifiers.
        // This noVNC generation has no pending AltGr timer to interrupt.
        this._releaseStaleModifier(['ShiftLeft', 'ShiftRight'], e.shiftKey);
        this._releaseStaleModifier(['ControlLeft', 'ControlRight'], e.ctrlKey);
        this._releaseStaleModifier(['AltLeft', 'AltRight'], e.altKey);
        this._releaseStaleModifier(['MetaLeft', 'MetaRight'], e.metaKey);
    },

    ungrab: function () {
"""
    if "contestSyncModifiers" in text:
        return text
    modern_count = text.count(modern_public)
    legacy_count = text.count(legacy_public)
    if modern_count + legacy_count != 1:
        raise RuntimeError(
            "unexpected noVNC keyboard.js public method source: "
            f"modern={modern_count} legacy={legacy_count}"
        )
    if modern_count == 1:
        return text.replace(modern_public, modern_public_patched, 1)
    return text.replace(legacy_public, legacy_public_patched, 1)


def patch_rfb(text: str) -> str:
    modern = """        if ((ev.type === 'click') || (ev.type === 'contextmenu')) {
            return;
        }

        let pos = clientToElement(ev.clientX, ev.clientY,
"""
    modern_patched = """        if ((ev.type === 'click') || (ev.type === 'contextmenu')) {
            return;
        }

        if (ev.type === 'mousedown') {
            // contestSyncModifiersFromMouse
            this._keyboard.syncModifiers(ev);
        }

        let pos = clientToElement(ev.clientX, ev.clientY,
"""
    legacy = """    _handleMouseButton: function (x, y, down, bmask) {
"""
    legacy_patched = """    _handleMouseButton: function (x, y, down, bmask, e) {
        // contestSyncModifiersFromMouse
        // contestLegacyModifierEvent: Mouse forwards the original DOM event.
        if (down && e) {
            this._keyboard.syncModifiers(e);
        }
"""
    if "contestSyncModifiersFromMouse" in text:
        return text
    modern_count = text.count(modern)
    legacy_count = text.count(legacy)
    if modern_count + legacy_count != 1:
        raise RuntimeError(
            "unexpected noVNC rfb.js source: "
            f"modern={modern_count} legacy={legacy_count}"
        )
    if modern_count == 1:
        return text.replace(modern, modern_patched, 1)
    return text.replace(legacy, legacy_patched, 1)


def patch_legacy_mouse(text: str) -> str:
    old = """        this.onmousebutton(pos.x, pos.y, down, bmask);
"""
    new = """        // contestModifierEvent: preserve modifier state for RFB.
        this.onmousebutton(pos.x, pos.y, down, bmask, e);
"""
    return _replace_once(
        text,
        old,
        new,
        "contestModifierEvent",
        "mouse.js",
    )


def patch_tree(root: Path) -> None:
    targets = (
        (root / "usr/share/novnc/core/input/util.js", patch_util),
        (root / "usr/share/novnc/core/input/keyboard.js", patch_keyboard),
        (root / "usr/share/novnc/core/rfb.js", patch_rfb),
    )
    for path, transform in targets:
        original = path.read_text(encoding="utf-8")
        patched = transform(original)
        if patched != original:
            with path.open("w", encoding="utf-8", newline="\n") as stream:
                stream.write(patched)
        if path.name == "rfb.js" and "contestLegacyModifierEvent" in patched:
            mouse_path = root / "usr/share/novnc/core/input/mouse.js"
            mouse_original = mouse_path.read_text(encoding="utf-8")
            mouse_patched = patch_legacy_mouse(mouse_original)
            if mouse_patched != mouse_original:
                with mouse_path.open(
                    "w", encoding="utf-8", newline="\n"
                ) as stream:
                    stream.write(mouse_patched)


def main(argv: list[str]) -> int:
    root = Path(argv[1]).resolve() if len(argv) == 2 else Path("/")
    if len(argv) > 2:
        raise RuntimeError("usage: patch-novnc-contest-input.py [root]")
    patch_tree(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
