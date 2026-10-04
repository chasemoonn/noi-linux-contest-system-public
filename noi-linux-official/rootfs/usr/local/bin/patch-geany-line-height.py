"""Prevent underscore clipping without replacing the official editor/font.

Change the packaged default, not a student's existing configuration. The
official Geany 1.36 desktop was visually checked with Monospace 10 and 2px
extra descent. Unexpected package defaults must be reviewed, not overwritten.
"""
from pathlib import Path
import re


def patched_config(text: str) -> str:
    lines = text.splitlines(keepends=True)
    section = ""
    matches = []
    for index, line in enumerate(lines):
        heading = re.fullmatch(r"\s*\[([^\]]+)\]\s*", line.strip())
        if heading:
            section = heading[1]
        if section == "styling" and re.match(r"^\s*line_height\s*=", line):
            matches.append(index)
    if len(matches) != 1:
        raise ValueError("expected exactly one Geany styling.line_height default")
    index = matches[0]
    match = re.fullmatch(r"(\s*line_height\s*=\s*)0;([02]);([ \t]*(?:\r?\n)?)", lines[index])
    if match is None:
        raise ValueError("unexpected Geany line_height; review package before patching")
    lines[index] = match[1] + "0;2;" + match[3]
    return "".join(lines)


def main():
    path = Path("/usr/share/geany/filedefs/filetypes.common")
    if path.is_symlink() or not path.is_file():
        raise ValueError("expected a regular packaged Geany filetypes.common")
    original = path.read_bytes()
    updated = patched_config(original.decode("utf-8")).encode("utf-8")
    if updated != original:
        path.write_bytes(updated)


if __name__ == "__main__":
    main()
