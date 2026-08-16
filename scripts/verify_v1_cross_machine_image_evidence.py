#!/usr/bin/env python3
"""Verify export/import/promotion/rollback facts from two Linux hosts."""
from __future__ import annotations

from datetime import datetime
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import stat


HEX40 = re.compile(r"^[a-f0-9]{40}$")
HEX64 = re.compile(r"^[a-f0-9]{64}$")
IMAGE_ID = re.compile(r"^sha256:[a-f0-9]{64}$")
PHASES = ["export", "imported", "promoted", "rolled_back", "repromoted", "restored"]


class EvidenceError(ValueError):
    pass


def exact(value, keys: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise EvidenceError(f"{label} shape differs")
    return value


def timestamp(value, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise EvidenceError(f"{label} is invalid")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceError(f"{label} is invalid") from exc


def validate_fact(document: object, expected_phase: str) -> dict:
    root = exact(
        document,
        {"$schema", "schema_version", "phase", "session_id", "observed_at", "host", "source", "bundle", "state"},
        "fact",
    )
    if root["$schema"] != "v1-image-host-fact.schema.json" or root["schema_version"] != 1:
        raise EvidenceError("unsupported image host fact")
    if root["phase"] != expected_phase:
        raise EvidenceError(f"expected phase {expected_phase}")
    if not HEX64.fullmatch(str(root["session_id"])):
        raise EvidenceError("session ID is invalid")
    timestamp(root["observed_at"], "observed_at")
    host = exact(root["host"], {"anonymous_id", "architecture", "docker_server", "kernel"}, "host")
    if not HEX64.fullmatch(str(host["anonymous_id"])):
        raise EvidenceError("anonymous host ID is invalid")
    for name in ("architecture", "docker_server", "kernel"):
        if not isinstance(host[name], str) or not host[name] or len(host[name]) > 200:
            raise EvidenceError(f"host.{name} is invalid")
    source = exact(root["source"], {"revision", "tree"}, "source")
    if not HEX40.fullmatch(str(source["revision"])) or not HEX40.fullmatch(str(source["tree"])):
        raise EvidenceError("source identity is invalid")
    bundle = exact(
        root["bundle"],
        {
            "archive_sha256", "bundle_checksums_sha256", "bundle_manifest_sha256",
            "contract", "image_id", "image_tag", "iso_sha256",
            "release_manifest_sha256", "source_revision",
        },
        "bundle",
    )
    for name in ("archive_sha256", "bundle_checksums_sha256", "bundle_manifest_sha256", "iso_sha256", "release_manifest_sha256"):
        if not HEX64.fullmatch(str(bundle[name])):
            raise EvidenceError(f"bundle.{name} is invalid")
    if not IMAGE_ID.fullmatch(str(bundle["image_id"])) or not HEX40.fullmatch(str(bundle["source_revision"])):
        raise EvidenceError("bundle image or source identity is invalid")
    if bundle["contract"] != "finalizer-status-v1" or source["revision"] != bundle["source_revision"]:
        raise EvidenceError("source and bundle contract differ")
    if not isinstance(bundle["image_tag"], str) or not bundle["image_tag"] or bundle["image_tag"].endswith(":latest"):
        raise EvidenceError("bundle image tag is invalid")
    state = exact(
        root["state"],
        {
            "candidate_tag_image_id", "current_promoted_image_id",
            "current_rollback_image_id", "current_rollback_source_target",
            "current_source_revision", "current_source_target", "formal_image_id",
            "pending_transaction", "running_contest_seats",
        },
        "state",
    )
    if state["candidate_tag_image_id"] != bundle["image_id"]:
        raise EvidenceError("candidate tag differs from bundle")
    if state["pending_transaction"] is not False or state["running_contest_seats"] != 0:
        raise EvidenceError("host is not quiescent")
    for name in ("current_promoted_image_id", "current_rollback_image_id", "formal_image_id"):
        if state[name] is not None and not IMAGE_ID.fullmatch(str(state[name])):
            raise EvidenceError(f"state.{name} is invalid")
    if state["current_source_revision"] is not None and not HEX40.fullmatch(str(state["current_source_revision"])):
        raise EvidenceError("state.current_source_revision is invalid")
    for name in ("current_source_target", "current_rollback_source_target"):
        value = state[name]
        if value is not None and not re.fullmatch(r"image-releases/[A-Za-z0-9TZ-]+", str(value)):
            raise EvidenceError(f"state.{name} is invalid")
    if expected_phase == "export" and any(
        state[name] is not None
        for name in (
            "current_promoted_image_id", "current_rollback_image_id",
            "current_rollback_source_target", "current_source_revision",
            "current_source_target", "formal_image_id",
        )
    ):
        raise EvidenceError("export fact must not claim import-host promotion state")
    return root


def load_fact(path: Path, phase: str) -> tuple[dict, str]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise EvidenceError(f"cannot open {phase} fact safely: {exc}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1024 * 1024:
            raise EvidenceError(f"{phase} fact must be a bounded single-link regular file")
        raw = b""
        while len(raw) <= 1024 * 1024:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            raw += chunk
        if len(raw) != info.st_size:
            raise EvidenceError(f"{phase} fact changed while reading")
    finally:
        os.close(descriptor)
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"{phase} fact is not strict UTF-8 JSON: {exc}") from exc
    return validate_fact(document, phase), hashlib.sha256(raw).hexdigest()


def atomic_json(path: Path, document: dict) -> str:
    requested = Path(os.path.abspath(path))
    if os.path.lexists(requested):
        raise EvidenceError("combined evidence output must not already exist")
    requested.parent.mkdir(parents=True, exist_ok=True)
    path = requested.parent.resolve() / requested.name
    raw = (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".v1-cross-machine-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return hashlib.sha256(raw).hexdigest()


def combine(facts: dict[str, dict], digests: dict[str, str], expected_revision: str) -> dict:
    if set(facts) != set(PHASES) or set(digests) != set(PHASES):
        raise EvidenceError("exactly six named phase facts are required")
    if any(not HEX64.fullmatch(str(digests[phase])) for phase in PHASES):
        raise EvidenceError("fact digest is invalid")
    times = [timestamp(facts[phase]["observed_at"], f"{phase}.observed_at") for phase in PHASES]
    if any(current <= previous for previous, current in zip(times, times[1:])):
        raise EvidenceError("fact times are not strictly ordered")
    sessions = {fact["session_id"] for fact in facts.values()}
    sources = {json.dumps(fact["source"], sort_keys=True) for fact in facts.values()}
    bundles = {json.dumps(fact["bundle"], sort_keys=True) for fact in facts.values()}
    if len(sessions) != 1 or len(sources) != 1 or len(bundles) != 1:
        raise EvidenceError("facts do not describe one session, source, and bundle")
    if facts["export"]["source"]["revision"] != expected_revision:
        raise EvidenceError("source revision differs")
    export_host = facts["export"]["host"]["anonymous_id"]
    import_hosts = {facts[phase]["host"]["anonymous_id"] for phase in PHASES[1:]}
    if len(import_hosts) != 1 or export_host in import_hosts:
        raise EvidenceError("export and import must use two distinct stable hosts")
    candidate = facts["export"]["bundle"]["image_id"]
    imported = facts["imported"]["state"]
    baseline_image = imported["formal_image_id"]
    baseline_source = imported["current_source_target"]
    baseline_revision = imported["current_source_revision"]
    if baseline_image is None or baseline_image == candidate or baseline_source is None:
        raise EvidenceError("imported phase lacks a distinct formal baseline pair")
    if imported["current_promoted_image_id"] != baseline_image:
        raise EvidenceError("imported baseline image/source pair is inconsistent")
    promoted = facts["promoted"]["state"]
    if promoted["formal_image_id"] != candidate or promoted["current_promoted_image_id"] != candidate:
        raise EvidenceError("candidate was not promoted")
    if promoted["current_source_revision"] != expected_revision:
        raise EvidenceError("promoted source revision differs")
    candidate_source = promoted["current_source_target"]
    if candidate_source is None:
        raise EvidenceError("promoted source target is missing")
    if promoted["current_rollback_image_id"] != baseline_image or promoted["current_rollback_source_target"] != baseline_source:
        raise EvidenceError("promoted rollback pair differs from the imported baseline")
    rolled = facts["rolled_back"]["state"]
    if rolled["formal_image_id"] != baseline_image or rolled["current_promoted_image_id"] != baseline_image or rolled["current_source_target"] != baseline_source or rolled["current_source_revision"] != baseline_revision:
        raise EvidenceError("rollback did not restore the baseline pair")
    repromoted = facts["repromoted"]["state"]
    if repromoted["formal_image_id"] != candidate or repromoted["current_promoted_image_id"] != candidate:
        raise EvidenceError("candidate was not promoted again")
    if repromoted["current_source_revision"] != expected_revision or repromoted["current_source_target"] is None or repromoted["current_rollback_image_id"] != baseline_image or repromoted["current_rollback_source_target"] != baseline_source:
        raise EvidenceError("second promotion does not retain the same baseline rollback pair")
    restored = facts["restored"]["state"]
    if restored["formal_image_id"] != baseline_image or restored["current_promoted_image_id"] != baseline_image or restored["current_source_target"] != baseline_source or restored["current_source_revision"] != baseline_revision:
        raise EvidenceError("final restoration did not return to the original baseline")
    return {
        "$schema": "v1-cross-machine-image-evidence.schema.json",
        "schema_version": 1,
        "status": "passed",
        "session_id": next(iter(sessions)),
        "source": facts["export"]["source"],
        "bundle": facts["export"]["bundle"],
        "hosts": {"export": export_host, "import": next(iter(import_hosts))},
        "transitions": {
            "baseline_image_id": baseline_image,
            "baseline_source_target": baseline_source,
            "candidate_image_id": candidate,
            "facts": [{"phase": phase, "sha256": digests[phase]} for phase in PHASES],
        },
    }


def validate_combined(document: object, *, expected_revision: str | None = None,
                      expected_tree: str | None = None,
                      expected_image_id: str | None = None) -> dict:
    evidence = exact(document, {
        "$schema", "schema_version", "status", "session_id", "source", "bundle",
        "hosts", "transitions",
    }, "combined image evidence")
    if evidence["$schema"] != "v1-cross-machine-image-evidence.schema.json" or \
            evidence["schema_version"] != 1 or evidence["status"] != "passed" or \
            not HEX64.fullmatch(str(evidence["session_id"])):
        raise EvidenceError("combined image evidence identity differs")
    source = exact(evidence["source"], {"revision", "tree"}, "combined source")
    if not HEX40.fullmatch(str(source["revision"])) or not HEX40.fullmatch(str(source["tree"])):
        raise EvidenceError("combined source identity is invalid")
    if expected_revision is not None and source["revision"] != expected_revision:
        raise EvidenceError("combined source revision differs")
    if expected_tree is not None and source["tree"] != expected_tree:
        raise EvidenceError("combined source tree differs")
    bundle = exact(evidence["bundle"], {
        "archive_sha256", "bundle_checksums_sha256", "bundle_manifest_sha256",
        "contract", "image_id", "image_tag", "iso_sha256", "release_manifest_sha256",
        "source_revision",
    }, "combined bundle")
    for name in ("archive_sha256", "bundle_checksums_sha256", "bundle_manifest_sha256",
                 "iso_sha256", "release_manifest_sha256"):
        if not HEX64.fullmatch(str(bundle[name])):
            raise EvidenceError(f"combined bundle {name} is invalid")
    if bundle["contract"] != "finalizer-status-v1" or \
            not IMAGE_ID.fullmatch(str(bundle["image_id"])) or \
            bundle["source_revision"] != source["revision"] or \
            not isinstance(bundle["image_tag"], str) or not bundle["image_tag"] or \
            bundle["image_tag"].endswith(":latest"):
        raise EvidenceError("combined bundle identity differs")
    if expected_image_id is not None and bundle["image_id"] != expected_image_id:
        raise EvidenceError("combined desktop image differs")
    hosts = exact(evidence["hosts"], {"export", "import"}, "combined hosts")
    if any(not HEX64.fullmatch(str(value)) for value in hosts.values()) or \
            hosts["export"] == hosts["import"]:
        raise EvidenceError("combined hosts do not prove two machines")
    transitions = exact(evidence["transitions"], {
        "baseline_image_id", "baseline_source_target", "candidate_image_id", "facts",
    }, "combined transitions")
    if not IMAGE_ID.fullmatch(str(transitions["baseline_image_id"])) or \
            not re.fullmatch(r"image-releases/[A-Za-z0-9TZ-]+", str(transitions["baseline_source_target"])) or \
            transitions["candidate_image_id"] != bundle["image_id"] or \
            transitions["baseline_image_id"] == transitions["candidate_image_id"]:
        raise EvidenceError("combined transition identity differs")
    facts = transitions["facts"]
    if not isinstance(facts, list) or len(facts) != len(PHASES):
        raise EvidenceError("combined transition fact count differs")
    for phase, value in zip(PHASES, facts):
        row = exact(value, {"phase", "sha256"}, f"combined fact {phase}")
        if row["phase"] != phase or not HEX64.fullmatch(str(row["sha256"])):
            raise EvidenceError("combined transition fact identity differs")
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    for phase in PHASES:
        parser.add_argument(f"--{phase.replace('_', '-')}", type=Path, required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if not HEX40.fullmatch(args.expected_revision):
            raise EvidenceError("expected revision is invalid")
        facts = {}
        digests = {}
        for phase in PHASES:
            fact, digest = load_fact(getattr(args, phase), phase)
            facts[phase] = fact
            digests[phase] = digest
        document = combine(facts, digests, args.expected_revision)
        validate_combined(document, expected_revision=args.expected_revision)
        digest = atomic_json(args.output, document)
        print(json.dumps({"evidence_sha256": digest, "status": "passed"}, sort_keys=True))
        return 0
    except (EvidenceError, OSError) as exc:
        print(f"NO_GO: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
