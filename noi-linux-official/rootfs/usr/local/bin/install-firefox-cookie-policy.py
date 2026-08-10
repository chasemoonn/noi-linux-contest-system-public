#!/usr/bin/env python3
"""Merge the Firefox 79 cookie policy without replacing other policies."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile


POLICY_PATHS = (
    Path("/etc/firefox/policies/policies.json"),
    Path("/usr/lib/firefox/distribution/policies.json"),
)
REQUIRED_COOKIES = {
    "Default": True,
    "AcceptThirdParty": "never",
    "Locked": True,
}


def fail(message: str) -> "NoReturn":
    raise SystemExit(f"Firefox cookie policy install failed: {message}")


def load_policy(path: Path) -> dict:
    if path.is_symlink():
        fail(f"policy path must not be a symlink: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"cannot read valid JSON from {path}: {exc}")
    if not isinstance(value, dict):
        fail(f"policy root must be an object: {path}")
    policies = value.get("policies")
    if policies is None:
        value["policies"] = {}
    elif not isinstance(policies, dict):
        fail(f"policies must be an object: {path}")
    return value


def main() -> int:
    existing = [path for path in POLICY_PATHS if path.exists() or path.is_symlink()]
    if len(existing) > 1:
        fail("multiple Firefox policy sources found: " + ", ".join(map(str, existing)))

    target = existing[0] if existing else POLICY_PATHS[0]
    document = load_policy(target) if existing else {"policies": {}}
    policies = document["policies"]
    cookies = policies.get("Cookies")
    if cookies is None:
        cookies = {}
        policies["Cookies"] = cookies
    elif not isinstance(cookies, dict):
        fail("policies.Cookies must be an object")

    for key, required in REQUIRED_COOKIES.items():
        if key in cookies and (
            type(cookies[key]) is not type(required) or cookies[key] != required
        ):
            fail(
                f"conflicting policies.Cookies.{key}: "
                f"found {cookies[key]!r}, required {required!r}"
            )
        cookies[key] = required

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=".policies.json.", dir=str(target.parent), text=True
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o644)
        os.replace(temporary_name, target)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass

    print(target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
