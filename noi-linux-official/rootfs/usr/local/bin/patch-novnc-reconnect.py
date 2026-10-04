"""Bounded retry after a previously authenticated noVNC 1.0 connection drops.

Never retry an initial login failure, rejected credentials, or a user cancel.
Only the packaged browser client changes, not VNC auth or the contest deadline.
"""
from pathlib import Path


MARKER = "contestBoundedReconnectV1"


def patch_ui(text: str) -> str:
    if MARKER in text:
        if text.count(MARKER) != 1 or "UI.contestReconnectAttempts < 60" not in text:
            raise ValueError("incomplete noVNC reconnect patch")
        return text
    old_retry = """        } else if (UI.getSetting('reconnect', false) === true && !UI.inhibit_reconnect) {
            UI.updateVisualState('reconnecting');

            var delay = parseInt(UI.getSetting('reconnect_delay'));
            UI.reconnect_callback = setTimeout(UI.reconnect, delay);
            return;
"""
    replacements = [
        ("    inhibit_reconnect: true,", "    inhibit_reconnect: true,\n    contestReconnectAttempts: 0,"),
        ("    disconnect: function() {", "    disconnect: function() {\n        UI.inhibit_reconnect = true;\n        clearTimeout(UI.reconnect_callback);\n        UI.reconnect_callback = null;"),
        ("    cancelReconnect: function() {", "    cancelReconnect: function() {\n        UI.inhibit_reconnect = true;"),
        ("        UI.inhibit_reconnect = false;", "        UI.inhibit_reconnect = false;\n        UI.contestReconnectAttempts = 0;"),
        ("    securityFailed: function (e) {", "    securityFailed: function (e) {\n        UI.inhibit_reconnect = true;\n        UI.reconnect_password = null;\n        clearTimeout(UI.reconnect_callback);\n        UI.reconnect_callback = null;"),
        (old_retry, ""),
        ("        if (!e.detail.clean) {", """        // contestBoundedReconnectV1: a failed retry is still a network outage.
        // Authentication failures and explicit user actions inhibit retries.
        if (UI.getSetting('reconnect', false) === true && !UI.inhibit_reconnect &&
            UI.contestReconnectAttempts < 60) {
            UI.contestReconnectAttempts += 1;
            UI.updateVisualState('reconnecting');
            var delay = parseInt(UI.getSetting('reconnect_delay'), 10);
            if (!Number.isFinite(delay)) delay = 5000;
            delay = Math.max(1000, Math.min(30000, delay));
            clearTimeout(UI.reconnect_callback);
            UI.reconnect_callback = setTimeout(UI.reconnect, delay);
            return;
        }

        if (!e.detail.clean) {"""),
    ]
    for before, _ in replacements:
        if text.count(before) != 1:
            raise ValueError("unexpected noVNC UI; review connection implementation")
    for before, after in replacements:
        text = text.replace(before, after, 1)
    return text


def main():
    path = Path('/usr/share/novnc/app/ui.js')
    if path.is_symlink() or not path.is_file():
        raise ValueError('expected regular packaged noVNC UI')
    original = path.read_bytes()
    updated = patch_ui(original.decode('utf-8')).encode('utf-8')
    if updated != original:
        path.write_bytes(updated)


if __name__ == '__main__':
    main()
