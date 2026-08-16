#!/usr/bin/env python3
"""Bind a Docker save archive to an inspect image ID on old and new engines.

Classic Docker uses the image-config digest as ``docker image inspect .Id``.
Docker with the containerd image store instead reports an OCI manifest or
manifest-list digest while ``manifest.json`` still points at the config blob.
This verifier accepts both representations, but only after proving the complete
small-object digest chain from the expected image ID to that config blob.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tarfile
from pathlib import Path


HEX = re.compile(r"[a-f0-9]{64}")
IMAGE_ID = re.compile(r"sha256:[a-f0-9]{64}")
MAX_JSON = 4 * 1024 * 1024
OCI_INDEX_MEDIA = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
OCI_MANIFEST_MEDIA = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}


class ArchiveIdentityError(RuntimeError):
    pass


def canonical_member(name: str) -> str:
    if name.startswith("./"):
        name = name[2:]
    if not name or name.startswith("/") or "\\" in name:
        raise ArchiveIdentityError("Docker archive contains an unsafe member name")
    parts = name.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ArchiveIdentityError("Docker archive contains an unsafe member name")
    return name


def load_json(raw: bytes, label: str) -> object:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArchiveIdentityError(f"{label} is not valid UTF-8 JSON") from exc


def digest_path(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.startswith("sha256:") \
            or not HEX.fullmatch(value[7:]):
        raise ArchiveIdentityError(f"{label} digest is invalid")
    return value[7:]


def descriptor_size(value: dict, raw: bytes, label: str) -> None:
    size = value.get("size")
    if not isinstance(size, int) or size != len(raw):
        raise ArchiveIdentityError(f"{label} size differs")


def collect_small_members(archive: str) -> dict[str, bytes]:
    mode = "r|*" if archive == "-" else "r:*"
    source = sys.stdin.buffer if archive == "-" else archive
    result: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=source if archive == "-" else None,
                          name=None if archive == "-" else str(source), mode=mode) as handle:
            for member in handle:
                name = canonical_member(member.name)
                if member.isdir():
                    continue
                if not member.isfile() or member.issym() or member.islnk():
                    raise ArchiveIdentityError("Docker archive contains a non-regular payload")
                wanted = name in {"manifest.json", "index.json", "oci-layout"} \
                    or re.fullmatch(r"(?:blobs/sha256/)?[a-f0-9]{64}(?:\.json)?", name)
                if not wanted or member.size > MAX_JSON:
                    continue
                if name in result:
                    raise ArchiveIdentityError("Docker archive contains duplicate identity metadata")
                stream = handle.extractfile(member)
                if stream is None:
                    raise ArchiveIdentityError("Docker archive identity metadata is unreadable")
                raw = stream.read(MAX_JSON + 1)
                if len(raw) != member.size or len(raw) > MAX_JSON:
                    raise ArchiveIdentityError("Docker archive identity metadata changed while reading")
                result[name] = raw
    except (tarfile.TarError, OSError) as exc:
        raise ArchiveIdentityError(f"cannot read Docker archive: {exc}") from exc
    return result


def required_blob(members: dict[str, bytes], digest: str, label: str) -> bytes:
    name = f"blobs/sha256/{digest}"
    raw = members.get(name)
    if raw is None or hashlib.sha256(raw).hexdigest() != digest:
        raise ArchiveIdentityError(f"{label} blob is missing or has the wrong digest")
    return raw


def config_record(members: dict[str, bytes], expected_tag: str) -> tuple[str, bytes]:
    raw = members.get("manifest.json")
    if raw is None:
        raise ArchiveIdentityError("Docker archive is missing manifest.json")
    document = load_json(raw, "Docker archive manifest.json")
    if not isinstance(document, list) or len(document) != 1 or not isinstance(document[0], dict):
        raise ArchiveIdentityError("Docker archive must contain exactly one image record")
    record = document[0]
    if record.get("RepoTags") != [expected_tag]:
        raise ArchiveIdentityError("Docker archive does not contain exactly the requested tag")
    config_path = record.get("Config")
    if not isinstance(config_path, str):
        raise ArchiveIdentityError("Docker archive config path is missing")
    config_path = canonical_member(config_path)
    match = re.fullmatch(r"(?:blobs/sha256/)?([a-f0-9]{64})(?:\.json)?", config_path)
    if match is None:
        raise ArchiveIdentityError("Docker archive config path is invalid")
    config = members.get(config_path)
    if config is None or hashlib.sha256(config).hexdigest() != match.group(1):
        raise ArchiveIdentityError("Docker archive config content digest differs")
    loaded = load_json(config, "Docker archive image config")
    if not isinstance(loaded, dict):
        raise ArchiveIdentityError("Docker archive image config must be an object")
    return match.group(1), config


def bind_modern_identity(members: dict[str, bytes], expected_digest: str,
                         config_digest: str) -> str:
    layout = load_json(members.get("oci-layout", b""), "Docker archive oci-layout")
    if layout != {"imageLayoutVersion": "1.0.0"}:
        raise ArchiveIdentityError("Docker archive OCI layout differs")
    outer = load_json(members.get("index.json", b""), "Docker archive index.json")
    if not isinstance(outer, dict) or outer.get("schemaVersion") != 2:
        raise ArchiveIdentityError("Docker archive outer OCI index differs")
    descriptors = outer.get("manifests")
    if not isinstance(descriptors, list) or len(descriptors) != 1 \
            or not isinstance(descriptors[0], dict):
        raise ArchiveIdentityError("Docker archive must bind exactly one OCI image reference")
    descriptor = descriptors[0]
    if digest_path(descriptor.get("digest"), "outer OCI descriptor") != expected_digest:
        raise ArchiveIdentityError("Docker archive outer OCI descriptor does not match image ID")
    identity_raw = required_blob(members, expected_digest, "expected image identity")
    descriptor_size(descriptor, identity_raw, "outer OCI descriptor")
    identity = load_json(identity_raw, "expected image identity blob")
    if not isinstance(identity, dict) or identity.get("schemaVersion") != 2:
        raise ArchiveIdentityError("expected image identity blob has an invalid shape")
    media = identity.get("mediaType") or descriptor.get("mediaType")
    if media in OCI_MANIFEST_MEDIA:
        manifest = identity
        mode = "oci-manifest"
    elif media in OCI_INDEX_MEDIA:
        candidates = []
        for row in identity.get("manifests", []):
            if not isinstance(row, dict):
                raise ArchiveIdentityError("inner OCI index descriptor is invalid")
            annotations = row.get("annotations") or {}
            platform = row.get("platform") or {}
            is_attestation = annotations.get("vnd.docker.reference.type") == "attestation-manifest" \
                or (platform.get("os") == "unknown" and platform.get("architecture") == "unknown")
            if not is_attestation and row.get("mediaType") in OCI_MANIFEST_MEDIA:
                candidates.append(row)
        if len(candidates) != 1:
            raise ArchiveIdentityError("inner OCI index does not contain exactly one runnable manifest")
        row = candidates[0]
        manifest_digest = digest_path(row.get("digest"), "runnable OCI manifest")
        manifest_raw = required_blob(members, manifest_digest, "runnable OCI manifest")
        descriptor_size(row, manifest_raw, "runnable OCI manifest")
        manifest = load_json(manifest_raw, "runnable OCI manifest")
        mode = "oci-index"
    else:
        raise ArchiveIdentityError("expected image identity uses an unsupported OCI media type")
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 2:
        raise ArchiveIdentityError("runnable OCI manifest has an invalid shape")
    config = manifest.get("config")
    if not isinstance(config, dict) \
            or digest_path(config.get("digest"), "OCI image config") != config_digest:
        raise ArchiveIdentityError("OCI identity graph does not bind the Docker image config")
    config_raw = required_blob(members, config_digest, "OCI image config")
    descriptor_size(config, config_raw, "OCI image config")
    return mode


def verify(archive: str, expected_tag: str, expected_id: str) -> tuple[dict, bytes]:
    if not expected_tag or expected_tag.endswith(":latest") or ":" not in expected_tag:
        raise ArchiveIdentityError("expected image tag is not fixed")
    if not IMAGE_ID.fullmatch(expected_id):
        raise ArchiveIdentityError("expected image ID is invalid")
    members = collect_small_members(archive)
    config_digest, config = config_record(members, expected_tag)
    expected_digest = expected_id[7:]
    if config_digest == expected_digest:
        mode = "legacy-config"
    else:
        mode = bind_modern_identity(members, expected_digest, config_digest)
    return {
        "config_sha256": config_digest,
        "identity_mode": mode,
        "image_id": expected_id,
        "image_tag": expected_tag,
        "status": "verified",
    }, config


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", required=True)
    parser.add_argument("--expected-tag", required=True)
    parser.add_argument("--expected-image-id", required=True)
    parser.add_argument("--config-output")
    args = parser.parse_args()
    try:
        result, config = verify(args.archive, args.expected_tag, args.expected_image_id)
        if args.config_output:
            output = Path(args.config_output)
            with output.open("xb") as handle:
                handle.write(config)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ArchiveIdentityError, OSError) as exc:
        print(f"NO_GO: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
