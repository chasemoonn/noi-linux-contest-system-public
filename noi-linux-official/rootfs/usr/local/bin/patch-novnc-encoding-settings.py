"""Honor generated quality/compression query settings in packaged noVNC 1.0.

The old client advertises fixed 6/2 levels, regardless of its URL. Keep these
defaults for absent/invalid values; do not upgrade keyboard or desktop code.
"""
from pathlib import Path


MARKER = "contestEncodingSettingsV1"
OLD = """        encs.push(encodings.pseudoEncodingQualityLevel0 + 6);
        encs.push(encodings.pseudoEncodingCompressLevel0 + 2);"""
NEW = """        // contestEncodingSettingsV1: generated URLs configure legacy noVNC.
        var contestQuery = new URLSearchParams(window.location.search);
        var contestLevel = function(name, fallback) {
            var values = contestQuery.getAll(name);
            return values.length === 1 && values[0].length === 1 && /^[0-9]$/.test(values[0]) ?
                Number(values[0]) : fallback;
        };
        // Expose what is actually advertised for diagnostics, not qualification.
        this._contestEncodingSettings = {
            quality: contestLevel('quality', 6),
            compression: contestLevel('compression', 2)
        };
        encs.push(encodings.pseudoEncodingQualityLevel0 + this._contestEncodingSettings.quality);
        encs.push(encodings.pseudoEncodingCompressLevel0 + this._contestEncodingSettings.compression);"""


def patch_rfb(text: str) -> str:
    if text.count('export default function RFB(') != 1 or text.count('_sendEncodings: function () {') != 1:
        raise ValueError('unexpected noVNC RFB version; review encoding settings')
    if MARKER in text:
        if text.count(MARKER) != 1 or text.count(NEW) != 1 or OLD in text:
            raise ValueError('incomplete noVNC encoding settings patch')
        return text
    if text.count(OLD) != 1 or '_contestEncodingSettings' in text:
        raise ValueError('unexpected noVNC encoding settings; review before patching')
    return text.replace(OLD, NEW, 1)


def main():
    path = Path('/usr/share/novnc/core/rfb.js')
    if path.is_symlink() or not path.is_file():
        raise ValueError('expected regular packaged noVNC RFB')
    original = path.read_bytes()
    updated = patch_rfb(original.decode('utf-8')).encode('utf-8')
    if updated != original:
        path.write_bytes(updated)


if __name__ == '__main__':
    main()
