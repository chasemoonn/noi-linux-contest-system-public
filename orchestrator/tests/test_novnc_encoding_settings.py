"""Execute the legacy RFB encoding negotiation, not just URL generation."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]
PATCHER = ROOT / 'noi-linux-official/rootfs/usr/local/bin/patch-novnc-encoding-settings.py'
# Minimal noVNC v1.0.0 negotiation anchors; full upstream checked separately.
FIXTURE = """export default function RFB() {}
RFB.prototype = {
    _sendEncodings: function () {
        var encs = [];
        encs.push(encodings.pseudoEncodingQualityLevel0 + 6);
        encs.push(encodings.pseudoEncodingCompressLevel0 + 2);
        RFB.messages.clientEncodings(this._sock, encs);
    }
};
"""


def patcher():
    spec = importlib.util.spec_from_file_location('encoding_patch', PATCHER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EncodingSettingsTests(unittest.TestCase):
    def test_idempotent(self):
        patch = patcher().patch_rfb
        result = patch(FIXTURE)
        self.assertEqual(patch(result), result)

    def test_unknown_or_partial_source_rejected(self):
        module = patcher()
        for source in [
            '', FIXTURE.replace('function RFB(', 'class RFB('),
            FIXTURE.replace('+ 6', '+ 5'), FIXTURE + module.OLD,
            FIXTURE + module.MARKER,
            module.patch_rfb(FIXTURE).replace("quality: contestLevel('quality', 6)", 'quality: 9'),
        ]:
            with self.subTest(source=source[:40]), self.assertRaises(ValueError):
                module.patch_rfb(source)

    @unittest.skipUnless(shutil.which('node'), 'Node required for encoding behavior')
    def test_actual_encoding_messages_and_reconnect(self):
        source = patcher().patch_rfb(FIXTURE).replace('export default ', '')
        cases = [
            ['', 6, 2], ['?quality=9&compression=2', 9, 2],
            ['?quality=0&compression=0', 0, 0], ['?quality=9&compression=9', 9, 9],
            ['?quality=3&compression=7', 3, 7], ['?quality=4', 4, 2],
            ['?compression=5', 6, 5], ['?quality=%39&compression=%30', 9, 0],
        ]
        for invalid in ['', '-1', '10', '1.5', '09', 'NaN', 'Infinity', 'true', '%20', '%0A9', '9%0A', '9%0D%0A', '9x']:
            cases.append([f'?quality={invalid}&compression={invalid}', 6, 2])
        cases += [['?quality=3&quality=9&compression=1&compression=1', 6, 2],
                  ['?quality=5&compression=nope', 5, 2]]
        checks = """
const assert = require('node:assert/strict');
const encodings = {pseudoEncodingQualityLevel0:-32, pseudoEncodingCompressLevel0:-256};
const window = {location:{search:''}};
let messages=[];
RFB.messages={clientEncodings:(sock, encs)=>messages.push(encs)};
for (const [query,quality,compression] of CASES) {
    window.location.search=query;
    const rfb=new RFB();
    rfb._sendEncodings();
    assert.deepEqual(messages.pop(),[-32+quality,-256+compression],query);
    assert.deepEqual(rfb._contestEncodingSettings,{quality,compression},query);
    // Re-negotiation and a new client must retain requested levels.
    rfb._sendEncodings();
    assert.deepEqual(messages.pop(),[-32+quality,-256+compression]);
    const reconnect=new RFB(); reconnect._sendEncodings();
    assert.deepEqual(messages.pop(),[-32+quality,-256+compression]);
}
""".replace('CASES', json.dumps(cases))
        result = subprocess.run(['node', '-e', source + checks], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_build_runs_patch(self):
        dockerfile = (ROOT / 'noi-linux-official/Dockerfile').read_text(encoding='utf-8')
        self.assertIn('RUN python3 /usr/local/bin/patch-novnc-encoding-settings.py', dockerfile)


if __name__ == '__main__':
    unittest.main()
