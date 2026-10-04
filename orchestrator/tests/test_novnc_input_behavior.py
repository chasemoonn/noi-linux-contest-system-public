"""Execute injected keyboard behavior, not just checks for marker strings.

These JS fixtures isolate our patch and model noVNC's legacy/modern key list
contracts. They do not replace full upstream-source or real browser tests.
"""
import shutil
import subprocess
import unittest

from tests.test_novnc_contest_input_patch import load_patcher


MODERN = """class Keyboard {
    constructor() { this._keyDownList = {}; this.events = []; }
    _sendKeyEvent(keysym, code, down) {
        if (down) { this._keyDownList[code] = keysym; }
        else { delete this._keyDownList[code]; }
        this.events.push({keysym,code,down});
    }
    // ===== PUBLIC METHODS =====

    grab() {
    }

    ungrab() {
    }
}
"""
LEGACY = """function Keyboard() { this._keyDownList = {}; this.events = []; }
Keyboard.prototype = {
    _sendKeyEvent: function(keysym, code, down) { this.events.push({keysym,code,down}); },
    // ===== PUBLIC METHODS =====

    grab: function () {
    },

    ungrab: function () {
    },
};
"""


@unittest.skipUnless(shutil.which('node'), 'Node.js required for injected JS behavior')
class NoVNCInputBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.patcher = load_patcher()

    def execute(self, source, checks):
        setup = """const assert = require('node:assert/strict');
const KeyTable = {XK_Control_L:65507};
let clearedTimer = null;
const clearTimeout = value => { clearedTimer = value; };
const noModifiers = {shiftKey:false,ctrlKey:false,altKey:false,metaKey:false};
"""
        result = subprocess.run(['node','-e',setup+source+'\n'+checks], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)

    def keyboard(self, checks):
        for style,source in (('legacy',LEGACY),('modern-without-altgr-helper',MODERN)):
            with self.subTest(style=style):
                self.execute(self.patcher.patch_keyboard(source),checks)

    def test_normal_mouse_click_clears_all_stale_modifiers_but_not_other_keys(self):
        self.keyboard("""const keyboard = new Keyboard();
keyboard._keyDownList = {ShiftLeft:1,ShiftRight:2,ControlLeft:3,ControlRight:4,
    AltLeft:5,AltRight:6,MetaLeft:7,MetaRight:8,KeyA:97};
keyboard.syncModifiers(noModifiers);
assert.deepEqual(keyboard._keyDownList,{KeyA:97});
assert.equal(keyboard.events.length,8);
assert.ok(keyboard.events.every(event => event.down === false));
keyboard.syncModifiers(noModifiers);
assert.equal(keyboard.events.length,8);
""")

    def test_intentional_shift_click_keeps_shift_and_releases_stale_ctrl(self):
        self.keyboard("""const keyboard = new Keyboard();
keyboard._keyDownList = {ShiftLeft:1,ControlLeft:2};
keyboard.syncModifiers({...noModifiers,shiftKey:true});
assert.deepEqual(keyboard._keyDownList,{ShiftLeft:1});
assert.deepEqual(keyboard.events,[{keysym:2,code:'ControlLeft',down:false}]);
""")

    def test_held_ctrl_alt_meta_are_not_released_by_mouse_click(self):
        self.keyboard("""const keyboard = new Keyboard();
keyboard._keyDownList = {ControlLeft:1,AltRight:2,MetaLeft:3};
keyboard.syncModifiers({...noModifiers,ctrlKey:true,altKey:true,metaKey:true});
assert.equal(keyboard.events.length,0);
assert.equal(Object.keys(keyboard._keyDownList).length,3);
""")

    def test_pending_control_timer_is_cancelled_after_host_ctrl_was_released(self):
        self.execute(self.patcher.patch_keyboard(MODERN),"""const keyboard = new Keyboard();
keyboard._altGrArmed = true; keyboard._altGrTimeout = 42;
keyboard.syncModifiers(noModifiers);
assert.equal(clearedTimer,42);
assert.equal(keyboard._altGrArmed,false);
assert.deepEqual(keyboard._keyDownList,{});
assert.equal(keyboard.events.length,0);
""")

    def test_pending_control_is_sent_before_intentional_ctrl_click(self):
        self.execute(self.patcher.patch_keyboard(MODERN),"""const keyboard = new Keyboard();
keyboard._altGrArmed = true; keyboard._altGrTimeout = 42;
keyboard.syncModifiers({...noModifiers,ctrlKey:true});
assert.equal(clearedTimer,42);
assert.equal(keyboard._altGrArmed,false);
assert.deepEqual(keyboard._keyDownList,{ControlLeft:65507});
assert.deepEqual(keyboard.events,[{keysym:65507,code:'ControlLeft',down:true}]);
keyboard.syncModifiers({...noModifiers,ctrlKey:true});
assert.equal(keyboard.events.length,1);
""")

    def test_existing_upstream_altgr_helper_is_used_when_available(self):
        self.execute(self.patcher.patch_keyboard(MODERN),"""const keyboard = new Keyboard();
let called = 0;
keyboard._interruptAltGrSequence = () => { called++; keyboard._sendKeyEvent(65507,'ControlLeft',true); };
keyboard.syncModifiers(noModifiers);
assert.equal(called,1);
assert.deepEqual(keyboard._keyDownList,{});
assert.deepEqual(keyboard.events.map(event=>event.down),[true,false]);
""")

    def test_old_installed_modern_patch_is_upgraded_once(self):
        updated = self.patcher.patch_keyboard(MODERN)
        start = updated.index('    syncModifiers(e) {')
        end = updated.index('    ungrab() {',start)
        old_method = """    syncModifiers(e) {
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

"""
        old = updated[:start]+old_method+updated[end:]
        migrated = self.patcher.patch_keyboard(old)
        self.assertEqual(migrated,updated)
        self.assertEqual(self.patcher.patch_keyboard(migrated),migrated)
        self.execute(migrated,"""const keyboard = new Keyboard();
keyboard._keyDownList = {ShiftLeft:1};
keyboard.syncModifiers(noModifiers);
assert.deepEqual(keyboard._keyDownList,{});
""")

    def test_numpad_digits_and_operators_do_not_modify_normal_keyboard_keys(self):
        source = """function getKeycode(evt) { return evt.code; }
export function getKey(evt) {
    // Are we getting a proper key value?
    return evt.key;
}
"""
        self.execute(self.patcher.patch_util(source).replace('export function','function'),"""
for(let number=0;number<=9;number++) {
    assert.equal(getKey({code:'Numpad'+number,key:'Insert'}),String(number));
}
for(const [code,value] of Object.entries({NumpadDecimal:'.',NumpadAdd:'+',NumpadSubtract:'-',NumpadMultiply:'*',NumpadDivide:'/'})) {
    assert.equal(getKey({code,key:'Delete'}),value);
}
for(const [code,key] of [['Minus','_'],['Tab','Tab'],['Enter','Enter'],['NumpadEnter','Enter'],['KeyA','a'],['Insert','Insert'],['NumLock','NumLock']]) {
    assert.equal(getKey({code,key}),key);
}
""")
