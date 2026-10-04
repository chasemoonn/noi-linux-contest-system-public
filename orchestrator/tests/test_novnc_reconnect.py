"""Execute the deployed noVNC connection methods with deterministic timers."""
import importlib.util
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).with_name('fixtures') / 'novnc-1.0-connection.js'
PATCHER = ROOT / 'noi-linux-official/rootfs/usr/local/bin/patch-novnc-reconnect.py'


def patcher():
    spec = importlib.util.spec_from_file_location('reconnect_patch', PATCHER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReconnectPatchTests(unittest.TestCase):
    def test_idempotent(self):
        patch = patcher().patch_ui
        text = patch(FIXTURE.read_text(encoding='utf-8'))
        self.assertEqual(patch(text), text)

    def test_unknown_ui_rejected(self):
        with self.assertRaises(ValueError):
            patcher().patch_ui('var UI = {};')

    @unittest.skipUnless(shutil.which('node'), 'Node required for JS behavior')
    def test_retries_cancel_auth_limit_and_reset(self):
        source = patcher().patch_ui(FIXTURE.read_text(encoding='utf-8'))
        checks = """
const assert = require('node:assert/strict');
const _ = x => x;
let callback = null, delay = null, connects = 0;
const setTimeout = (fn, ms) => { callback = fn; delay = ms; return 1; };
const clearTimeout = () => { callback = null; };
const fire = () => { const fn=callback; callback=null; fn(); };
let settings = {reconnect:true,reconnect_delay:2000};
UI.getSetting = key => settings[key];
for (const name of ['updateVisualState','showStatus','openControlbar','openConnectPanel','closeAllPanels']) UI[name] = () => {};
UI.connect = () => { connects++; };
const drop = () => UI.disconnectFinished({detail:{clean:false}});
// Failed initial login does not start a loop.
drop(); assert.equal(callback,null);
// A successful login allows retries even if several attempts fail.
UI.rfb={focus(){}}; UI.connectFinished({});
drop(); assert.equal(delay,2000); fire(); assert.equal(connects,1);
drop(); assert.equal(typeof callback,'function'); fire(); assert.equal(connects,2);
// User cancellation and rejected credentials both stop retries.
UI.cancelReconnect(); drop(); assert.equal(callback,null);
UI.rfb={focus(){}}; UI.connectFinished({});
drop(); UI.securityFailed({detail:{}}); drop(); assert.equal(callback,null);
assert.equal(UI.reconnect_password,null);
// Explicit disconnect inhibits before synchronous disconnection events.
UI.rfb={focus(){},disconnect:drop}; UI.connectFinished({});
UI.disconnect(); assert.equal(callback,null);
// Retry count is finite; success resets it; delay cannot busy-spin.
UI.rfb={focus(){}}; UI.connectFinished({});
settings.reconnect_delay='invalid'; drop(); assert.equal(delay,5000);
settings.reconnect_delay=-1; drop(); assert.equal(delay,1000);
settings.reconnect_delay=999999; drop(); assert.equal(delay,30000);
UI.contestReconnectAttempts=60; callback=null; drop(); assert.equal(callback,null);
UI.rfb={focus(){}}; UI.connectFinished({}); assert.equal(UI.contestReconnectAttempts,0);
settings.reconnect=false; drop(); assert.equal(callback,null);
"""
        result = subprocess.run(['node', '-e', source + '\n' + checks], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
