"""Validate a root-owned PM2 entry point, including Hydro's Nix profile."""
from __future__ import annotations

import os
from pathlib import Path
import stat


def trusted_pm2_entrypoint(path: Path) -> Path:
    """Return the canonical CLI after checking both link and target ancestry.

    Nix's root-owned store normally has mode 1775. Its sticky bit protects
    existing root-owned package entries from the build group. The exception
    is limited to that exact directory and mode; package contents and every
    other ancestor still must be root-owned and not group/world writable.
    This is PM2-specific: do not use it to canonicalize a venv interpreter.
    """
    if not path.is_absolute():
        raise ValueError("PM2 executable must be an absolute path")
    requested = Path(os.path.abspath(path))
    resolved = requested.resolve(strict=True)
    info = os.lstat(resolved)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o022
            or not os.access(resolved, os.X_OK)):
        raise ValueError("PM2 executable metadata is unsafe")
    entry = os.lstat(requested)
    if entry.st_uid != 0:
        raise ValueError("PM2 entry point is not root-owned")

    checked = set()

    def ancestors(current: Path, *, links: bool) -> None:
        while current not in checked:
            metadata = os.lstat(current)
            mode = stat.S_IMODE(metadata.st_mode)
            if metadata.st_uid != 0:
                raise ValueError("PM2 executable ancestor is not root-owned")
            if stat.S_ISLNK(metadata.st_mode) and links:
                ancestors(current.resolve(strict=True), links=False)
            else:
                nix_store = current == Path('/nix/store').absolute() and mode == 0o1775
                if not stat.S_ISDIR(metadata.st_mode) or (mode & 0o022 and not nix_store):
                    raise ValueError("PM2 executable ancestor is unsafe")
                # Even an ordinary directory may have been reached through
                # an earlier profile symlink. Check that resolved chain too.
                canonical = current.resolve(strict=True)
                if canonical != current:
                    ancestors(canonical, links=False)
            checked.add(current)
            if current.parent == current:
                break
            current = current.parent

    ancestors(resolved.parent, links=False)
    ancestors(requested.parent, links=True)
    return resolved
