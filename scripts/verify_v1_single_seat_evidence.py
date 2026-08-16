#!/usr/bin/env python3
"""Verify nine privacy-safe facts from one real V1 single-seat rehearsal."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import stat
import sys
import tempfile


PHASES = [
    "materials",
    "desktop",
    "compile",
    "manual_submit",
    "cutoff_submit",
    "oj_record",
    "collection",
    "shutdown",
    "test_cleanup",
]
ROLES = {
    "materials": "control",
    "desktop": "desktop",
    "compile": "desktop",
    "manual_submit": "control",
    "cutoff_submit": "control",
    "oj_record": "oj",
    "collection": "control",
    "shutdown": "control",
    "test_cleanup": "oj",
}
OBSERVATION_KEYS = {
    "materials": {
        "desktop_paper_sha256",
        "material_manifest_sha256",
        "oj_publication_receipt_sha256",
        "paper_sha256",
        "practice_pairs",
    },
    "desktop": {
        "candidate_path",
        "desktop_contract",
        "entries",
        "page_status",
        "websocket_status",
    },
    "compile": {
        "actual_output_sha256",
        "binary_sha256",
        "exit_code",
        "expected_output_sha256",
        "input_sha256",
        "source_sha256",
    },
    "manual_submit": {
        "judge_state",
        "rid",
        "source_sha256",
        "submission_id",
    },
    "cutoff_submit": {
        "deadline_state",
        "final_rid",
        "final_submission_id",
        "frozen_source_sha256",
        "last_confirmed_source_sha256",
        "supplemental_submitted",
    },
    "oj_record": {
        "final_rid",
        "final_source_sha256",
        "final_score_source",
        "manual_rid",
        "manual_source_sha256",
        "record_count",
        "student_history_visible",
        "teacher_source_visible",
    },
    "collection": {
        "archive_manifest_sha256",
        "collection_receipt_sha256",
        "delivery_safe",
        "final_rid",
        "final_source_sha256",
        "state",
        "submit_failures",
        "submit_log_sha256",
    },
    "shutdown": {
        "cloud_state",
        "collection_receipt_sha256",
        "conflict_rules",
        "desktop_closed",
        "managed_rules",
        "running_seats",
        "shutdown_verified_at_ms",
    },
    "test_cleanup": {
        "cleanup_receipt_sha256",
        "cleanup_verified_at_ms",
        "contest_absent",
        "contest_id_sha256",
        "verification_method",
        "discussion_count",
        "linked_record_count",
        "registration_status_count",
        "scheduled_task_count",
    },
}
HEX24 = re.compile(r"^[a-f0-9]{24}$")
HEX40 = re.compile(r"^[a-f0-9]{40}$")
HEX64 = re.compile(r"^[a-f0-9]{64}$")
IMAGE_ID = re.compile(r"^sha256:[a-f0-9]{64}$")
SLUG = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
CANDIDATE = re.compile(r"^9999[0-9]{8}$")
SEAT_CANDIDATE = re.compile(r"^CSP[0-9]{3}$")
ARTIFACT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class EvidenceError(ValueError):
    pass


def exact(value, keys: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise EvidenceError(f"{label} shape differs")
    return value


def require_string(value, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise EvidenceError(f"{label} is invalid")
    return value


def require_bool(value, expected: bool, label: str) -> None:
    if value is not expected:
        raise EvidenceError(f"{label} must be {str(expected).lower()}")


def require_int(value, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise EvidenceError(f"{label} is invalid")
    return value


def timestamp(value, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise EvidenceError(f"{label} is invalid")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceError(f"{label} is invalid") from exc
    if result.tzinfo is None:
        raise EvidenceError(f"{label} is invalid")
    return result


def validate_source(value, label: str = "source") -> dict:
    source = exact(value, {"revision", "tree"}, label)
    require_string(source["revision"], HEX40, f"{label}.revision")
    require_string(source["tree"], HEX40, f"{label}.tree")
    return source


def validate_components(value, label: str = "components") -> dict:
    components = exact(
        value,
        {
            "desktop_image_id",
            "desktop_source_revision",
            "hydro_plugin_sha256",
            "orchestrator_image_digest",
        },
        label,
    )
    require_string(components["desktop_image_id"], IMAGE_ID, f"{label}.desktop_image_id")
    require_string(
        components["orchestrator_image_digest"],
        IMAGE_ID,
        f"{label}.orchestrator_image_digest",
    )
    require_string(
        components["desktop_source_revision"],
        HEX40,
        f"{label}.desktop_source_revision",
    )
    require_string(
        components["hydro_plugin_sha256"], HEX64, f"{label}.hydro_plugin_sha256"
    )
    return components


def validate_context(value, label: str = "context") -> dict:
    context = exact(
        value,
        {
            "candidate_id",
            "contest_id_sha256",
            "cutoff_at_ms",
            "problem_slug",
            "seat_candidate",
            "seat_id_sha256",
        },
        label,
    )
    require_string(context["candidate_id"], CANDIDATE, f"{label}.candidate_id")
    require_string(
        context["seat_candidate"], SEAT_CANDIDATE, f"{label}.seat_candidate"
    )
    require_string(context["contest_id_sha256"], HEX64, f"{label}.contest_id_sha256")
    require_string(context["seat_id_sha256"], HEX64, f"{label}.seat_id_sha256")
    require_string(context["problem_slug"], SLUG, f"{label}.problem_slug")
    require_int(context["cutoff_at_ms"], f"{label}.cutoff_at_ms", 1)
    return context


def validate_ordinary_oj(value, label: str = "ordinary_oj") -> dict:
    row = exact(
        value,
        {
            "errors",
            "homepage_status",
            "login_status",
            "observed_at",
            "pm2_fingerprint_sha256",
            "prep_database_ok",
            "prep_health_ok",
            "restarts",
        },
        label,
    )
    if row["homepage_status"] != 200 or row["login_status"] != 200:
        raise EvidenceError(f"{label} HTTP checks must both equal 200")
    require_bool(row["prep_health_ok"], True, f"{label}.prep_health_ok")
    require_bool(row["prep_database_ok"], True, f"{label}.prep_database_ok")
    if require_int(row["errors"], f"{label}.errors") != 0:
        raise EvidenceError(f"{label}.errors must be zero")
    if require_int(row["restarts"], f"{label}.restarts") != 0:
        raise EvidenceError(f"{label}.restarts must be zero")
    require_string(
        row["pm2_fingerprint_sha256"], HEX64, f"{label}.pm2_fingerprint_sha256"
    )
    timestamp(row["observed_at"], f"{label}.observed_at")
    return row


def validate_observations(phase: str, value, context: dict) -> dict:
    row = exact(value, OBSERVATION_KEYS[phase], f"{phase}.observations")
    slug = context["problem_slug"]
    candidate = context["seat_candidate"]
    if phase == "materials":
        for name in (
            "desktop_paper_sha256",
            "material_manifest_sha256",
            "oj_publication_receipt_sha256",
            "paper_sha256",
        ):
            require_string(row[name], HEX64, f"{phase}.{name}")
        if row["desktop_paper_sha256"] != row["paper_sha256"]:
            raise EvidenceError("desktop and published paper hashes differ")
        pairs = row["practice_pairs"]
        if not isinstance(pairs, list) or not 2 <= len(pairs) <= 4:
            raise EvidenceError("materials.practice_pairs must contain 2 to 4 groups")
        groups = []
        for index, pair_value in enumerate(pairs):
            pair = exact(pair_value, {"group", "input_sha256", "output_sha256"}, f"practice_pairs[{index}]")
            groups.append(require_int(pair["group"], f"practice_pairs[{index}].group", 1))
            require_string(pair["input_sha256"], HEX64, f"practice_pairs[{index}].input_sha256")
            require_string(pair["output_sha256"], HEX64, f"practice_pairs[{index}].output_sha256")
        if groups != list(range(1, len(groups) + 1)):
            raise EvidenceError("practice group numbers must be contiguous from one")
    elif phase == "desktop":
        if row["candidate_path"] != f"{candidate}/{slug}/{slug}.cpp":
            raise EvidenceError("desktop candidate path differs from the CSP contract")
        if row["desktop_contract"] != "finalizer-status-v1":
            raise EvidenceError("desktop contract differs")
        entries = exact(
            row["entries"],
            {"answer_directory", "instructions", "paper", "practice_data", "submission_portal"},
            "desktop.entries",
        )
        if any(value is not True for value in entries.values()):
            raise EvidenceError("all five desktop entries must be present")
        if row["page_status"] != 200 or row["websocket_status"] != 101:
            raise EvidenceError("desktop page/WS checks must equal 200/101")
    elif phase == "compile":
        for name in (
            "actual_output_sha256",
            "binary_sha256",
            "expected_output_sha256",
            "input_sha256",
            "source_sha256",
        ):
            require_string(row[name], HEX64, f"{phase}.{name}")
        if row["exit_code"] != 0 or row["actual_output_sha256"] != row["expected_output_sha256"]:
            raise EvidenceError("compile/self-test did not reproduce expected output")
    elif phase == "manual_submit":
        require_string(row["source_sha256"], HEX64, "manual_submit.source_sha256")
        require_string(row["submission_id"], HEX64, "manual_submit.submission_id")
        require_string(row["rid"], HEX24, "manual_submit.rid")
        if row["judge_state"] != "submitted":
            raise EvidenceError("manual submission is not confirmed submitted")
    elif phase == "cutoff_submit":
        for name in ("frozen_source_sha256", "last_confirmed_source_sha256", "final_submission_id"):
            require_string(row[name], HEX64, f"cutoff_submit.{name}")
        require_string(row["final_rid"], HEX24, "cutoff_submit.final_rid")
        if row["deadline_state"] != "frozen" or row["supplemental_submitted"] is not True:
            raise EvidenceError("cutoff must freeze and submit a changed final source")
        if row["frozen_source_sha256"] == row["last_confirmed_source_sha256"]:
            raise EvidenceError("cutoff rehearsal must exercise the changed-source supplement")
    elif phase == "oj_record":
        for name in ("manual_source_sha256", "final_source_sha256"):
            require_string(row[name], HEX64, f"oj_record.{name}")
        for name in ("manual_rid", "final_rid"):
            require_string(row[name], HEX24, f"oj_record.{name}")
        if require_int(row["record_count"], "oj_record.record_count", 2) < 2:
            raise EvidenceError("OJ must contain at least two native records")
        require_bool(row["teacher_source_visible"], True, "oj_record.teacher_source_visible")
        require_bool(row["student_history_visible"], True, "oj_record.student_history_visible")
        if row["final_score_source"] != "last_record":
            raise EvidenceError("OJ final score must use the last record")
    elif phase == "collection":
        for name in (
            "archive_manifest_sha256",
            "collection_receipt_sha256",
            "final_source_sha256",
            "submit_log_sha256",
        ):
            require_string(row[name], HEX64, f"collection.{name}")
        require_string(row["final_rid"], HEX24, "collection.final_rid")
        if row["state"] != "safe_wait" or row["delivery_safe"] is not True:
            raise EvidenceError("collection is not in its durable safe-wait state")
        if row["submit_failures"] != 0:
            raise EvidenceError("collection submit failures must be zero")
    elif phase == "shutdown":
        require_string(row["collection_receipt_sha256"], HEX64, "shutdown.collection_receipt_sha256")
        if row["cloud_state"] != "STOPPED" or row["desktop_closed"] is not True:
            raise EvidenceError("shutdown did not close the desktop and stop the cloud host")
        for name in ("managed_rules", "conflict_rules", "running_seats"):
            if row[name] != 0:
                raise EvidenceError(f"shutdown.{name} must be zero")
        require_int(row["shutdown_verified_at_ms"], "shutdown.shutdown_verified_at_ms", 1)
    else:
        require_string(row["cleanup_receipt_sha256"], HEX64, "test_cleanup.cleanup_receipt_sha256")
        require_string(row["contest_id_sha256"], HEX64, "test_cleanup.contest_id_sha256")
        if row["contest_id_sha256"] != context["contest_id_sha256"]:
            raise EvidenceError("test cleanup targets a different contest")
        if row["verification_method"] != "hydro_mongo_post_delete_absence":
            raise EvidenceError("test cleanup was not independently verified from Hydro state")
        require_bool(row["contest_absent"], True, "test_cleanup.contest_absent")
        for name in (
            "discussion_count",
            "linked_record_count",
            "registration_status_count",
            "scheduled_task_count",
        ):
            if require_int(row[name], f"test_cleanup.{name}") != 0:
                raise EvidenceError(f"test_cleanup.{name} must be zero")
        require_int(row["cleanup_verified_at_ms"], "test_cleanup.cleanup_verified_at_ms", 1)
        receipt_payload = dict(row)
        receipt_payload.pop("cleanup_receipt_sha256")
        expected_receipt = hashlib.sha256(
            json.dumps(receipt_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if row["cleanup_receipt_sha256"] != expected_receipt:
            raise EvidenceError("test cleanup receipt does not bind its observations")
    return row


def validate_fact(document, expected_phase: str) -> dict:
    fact = exact(
        document,
        {
            "$schema",
            "schema_version",
            "phase",
            "session_id",
            "observed_at",
            "collector",
            "source",
            "components",
            "context",
            "ordinary_oj",
            "observations",
            "artifacts",
        },
        "fact",
    )
    if fact["$schema"] != "v1-single-seat-phase-fact.schema.json" or fact["schema_version"] != 1:
        raise EvidenceError("unsupported single-seat phase fact")
    if fact["phase"] != expected_phase:
        raise EvidenceError(f"expected phase {expected_phase}")
    require_string(fact["session_id"], HEX64, "session_id")
    timestamp(fact["observed_at"], "observed_at")
    collector = exact(fact["collector"], {"anonymous_host_id", "role"}, "collector")
    require_string(collector["anonymous_host_id"], HEX64, "collector.anonymous_host_id")
    if collector["role"] != ROLES[expected_phase]:
        raise EvidenceError(f"{expected_phase} must be collected by the {ROLES[expected_phase]} role")
    source = validate_source(fact["source"])
    components = validate_components(fact["components"])
    if components["desktop_source_revision"] != source["revision"]:
        raise EvidenceError("desktop source revision differs from the fact source")
    context = validate_context(fact["context"])
    validate_ordinary_oj(fact["ordinary_oj"])
    fact_time = timestamp(fact["observed_at"], "observed_at")
    oj_time = timestamp(fact["ordinary_oj"]["observed_at"], "ordinary_oj.observed_at")
    age_seconds = (fact_time - oj_time).total_seconds()
    if age_seconds < 0 or age_seconds > 120:
        raise EvidenceError("ordinary OJ observation must precede the phase fact by at most 120 seconds")
    validate_observations(expected_phase, fact["observations"], context)
    artifacts = fact["artifacts"]
    if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= 16:
        raise EvidenceError(f"{expected_phase}.artifacts must contain 1 to 16 files")
    references = []
    for index, value in enumerate(artifacts):
        row = exact(value, {"reference", "sha256"}, f"{expected_phase}.artifacts[{index}]")
        reference = str(row["reference"])
        pure = PurePosixPath(reference)
        if (
            pure.is_absolute()
            or len(pure.parts) != 2
            or pure.parts[0] != expected_phase
            or not ARTIFACT_NAME.fullmatch(pure.parts[1])
        ):
            raise EvidenceError(f"{expected_phase}.artifacts[{index}].reference is unsafe")
        require_string(row["sha256"], HEX64, f"{expected_phase}.artifacts[{index}].sha256")
        references.append(reference)
    if len(set(references)) != len(references):
        raise EvidenceError(f"{expected_phase}.artifacts contain duplicate references")
    return fact


def verify_artifacts(fact: dict, artifact_directory: Path) -> None:
    root = artifact_directory.resolve()
    if not root.is_dir() or root.is_symlink():
        raise EvidenceError("artifact directory must be a real directory")
    for row in fact["artifacts"]:
        reference = PurePosixPath(row["reference"])
        path = root.joinpath(*reference.parts)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise EvidenceError(f"cannot open artifact safely: {row['reference']}") from exc
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 16 * 1024 * 1024:
                raise EvidenceError(f"artifact is not a bounded single-link regular file: {row['reference']}")
            digest = hashlib.sha256()
            total = 0
            while True:
                block = os.read(descriptor, 1024 * 1024)
                if not block:
                    break
                total += len(block)
                digest.update(block)
            if total != info.st_size or digest.hexdigest() != row["sha256"]:
                raise EvidenceError(f"artifact SHA256 differs: {row['reference']}")
        finally:
            os.close(descriptor)


def safe_load(path: Path, phase: str) -> tuple[dict, str]:
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
            block = os.read(descriptor, 65536)
            if not block:
                break
            raw += block
        if len(raw) != info.st_size:
            raise EvidenceError(f"{phase} fact changed while reading")
    finally:
        os.close(descriptor)
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"{phase} fact is not strict UTF-8 JSON") from exc
    return validate_fact(document, phase), hashlib.sha256(raw).hexdigest()


def combine(facts: dict[str, dict], digests: dict[str, str], expected_revision: str) -> dict:
    if set(facts) != set(PHASES) or set(digests) != set(PHASES):
        raise EvidenceError("exactly nine named phase facts are required")
    require_string(expected_revision, HEX40, "expected_revision")
    for phase in PHASES:
        require_string(digests[phase], HEX64, f"{phase}.sha256")
    times = [timestamp(facts[phase]["observed_at"], f"{phase}.observed_at") for phase in PHASES]
    if any(current <= previous for previous, current in zip(times, times[1:])):
        raise EvidenceError("phase facts are not strictly ordered")
    sessions = {fact["session_id"] for fact in facts.values()}
    sources = {json.dumps(fact["source"], sort_keys=True) for fact in facts.values()}
    components = {json.dumps(fact["components"], sort_keys=True) for fact in facts.values()}
    contexts = {json.dumps(fact["context"], sort_keys=True) for fact in facts.values()}
    if len(sessions) != 1 or len(sources) != 1 or len(components) != 1 or len(contexts) != 1:
        raise EvidenceError("facts do not describe one session, source, component set, and seat")
    source = facts["materials"]["source"]
    if source["revision"] != expected_revision:
        raise EvidenceError("source revision differs")
    control_hosts = {
        facts[phase]["collector"]["anonymous_host_id"]
        for phase in PHASES
        if ROLES[phase] == "control"
    }
    desktop_hosts = {
        facts[phase]["collector"]["anonymous_host_id"]
        for phase in PHASES
        if ROLES[phase] == "desktop"
    }
    if len(control_hosts) != 1 or len(desktop_hosts) != 1 or control_hosts == desktop_hosts:
        raise EvidenceError("control and desktop facts require two distinct stable hosts")
    oj_hosts = {
        facts[phase]["collector"]["anonymous_host_id"]
        for phase in PHASES
        if ROLES[phase] == "oj"
    }
    if len(oj_hosts) != 1:
        raise EvidenceError("OJ facts require one stable host")
    fingerprints = {fact["ordinary_oj"]["pm2_fingerprint_sha256"] for fact in facts.values()}
    if len(fingerprints) != 1:
        raise EvidenceError("ordinary OJ process fingerprint changed during the rehearsal")
    context = facts["materials"]["context"]
    cutoff_at = int(context["cutoff_at_ms"])
    observed_ms = [int(value.timestamp() * 1000) for value in times]
    if observed_ms[3] >= cutoff_at or observed_ms[4] < cutoff_at:
        raise EvidenceError("manual submission must precede cutoff and cutoff evidence must follow it")

    compile_row = facts["compile"]["observations"]
    manual = facts["manual_submit"]["observations"]
    cutoff = facts["cutoff_submit"]["observations"]
    oj = facts["oj_record"]["observations"]
    collection = facts["collection"]["observations"]
    shutdown = facts["shutdown"]["observations"]
    cleanup = facts["test_cleanup"]["observations"]
    if compile_row["source_sha256"] != manual["source_sha256"]:
        raise EvidenceError("compiled source differs from the manual submission")
    if cutoff["last_confirmed_source_sha256"] != manual["source_sha256"]:
        raise EvidenceError("cutoff evidence is not based on the confirmed manual submission")
    if (
        oj["manual_rid"] != manual["rid"]
        or oj["manual_source_sha256"] != manual["source_sha256"]
        or oj["final_rid"] != cutoff["final_rid"]
        or oj["final_source_sha256"] != cutoff["frozen_source_sha256"]
    ):
        raise EvidenceError("OJ records do not bind both submitted source versions")
    if (
        collection["final_rid"] != cutoff["final_rid"]
        or collection["final_source_sha256"] != cutoff["frozen_source_sha256"]
        or shutdown["collection_receipt_sha256"] != collection["collection_receipt_sha256"]
    ):
        raise EvidenceError("collection and shutdown do not bind the final source and receipt")
    if shutdown["shutdown_verified_at_ms"] < cutoff_at:
        raise EvidenceError("shutdown was verified before the contest cutoff")
    shutdown_at = shutdown["shutdown_verified_at_ms"]
    cleanup_at = cleanup["cleanup_verified_at_ms"]
    cleanup_fact_at = observed_ms[-1]
    if abs(cleanup_fact_at - cleanup_at) > 120 * 1000:
        raise EvidenceError("test cleanup observation is stale or future-dated")
    if cleanup_at < shutdown_at:
        raise EvidenceError("test contest cleanup was verified before shutdown")
    if cleanup_at - shutdown_at > 30 * 60 * 1000:
        raise EvidenceError("test contest cleanup exceeded the 30-minute boundary")
    return {
        "$schema": "v1-single-seat-evidence.schema.json",
        "schema_version": 1,
        "status": "passed",
        "session_id": next(iter(sessions)),
        "source": source,
        "components": facts["materials"]["components"],
        "context": context,
        "checks": {name: True for name in PHASES},
        "ordinary_oj_isolation": {
            "errors": 0,
            "pid_changes": 0,
            "restarts": 0,
            "pm2_fingerprint_sha256": next(iter(fingerprints)),
        },
        "facts": [{"phase": phase, "sha256": digests[phase]} for phase in PHASES],
    }


def validate_combined(document, *, expected_revision: str | None = None, expected_components: dict | None = None) -> dict:
    evidence = exact(
        document,
        {"$schema", "schema_version", "status", "session_id", "source", "components", "context", "checks", "ordinary_oj_isolation", "facts"},
        "single-seat evidence",
    )
    if evidence["$schema"] != "v1-single-seat-evidence.schema.json" or evidence["schema_version"] != 1 or evidence["status"] != "passed":
        raise EvidenceError("unsupported or non-passed single-seat evidence")
    require_string(evidence["session_id"], HEX64, "session_id")
    source = validate_source(evidence["source"])
    components = validate_components(evidence["components"])
    validate_context(evidence["context"])
    checks = exact(evidence["checks"], set(PHASES), "checks")
    if any(value is not True for value in checks.values()):
        raise EvidenceError("all single-seat checks must be true")
    isolation = exact(evidence["ordinary_oj_isolation"], {"errors", "pid_changes", "pm2_fingerprint_sha256", "restarts"}, "ordinary_oj_isolation")
    for name in ("errors", "pid_changes", "restarts"):
        if isolation[name] != 0:
            raise EvidenceError(f"ordinary_oj_isolation.{name} must be zero")
    require_string(isolation["pm2_fingerprint_sha256"], HEX64, "ordinary_oj_isolation.pm2_fingerprint_sha256")
    rows = evidence["facts"]
    if not isinstance(rows, list) or len(rows) != len(PHASES):
        raise EvidenceError("combined evidence must reference nine facts")
    for phase, value in zip(PHASES, rows):
        row = exact(value, {"phase", "sha256"}, f"facts.{phase}")
        if row["phase"] != phase:
            raise EvidenceError("combined fact order differs")
        require_string(row["sha256"], HEX64, f"facts.{phase}.sha256")
    if expected_revision is not None and source["revision"] != expected_revision:
        raise EvidenceError("single-seat evidence revision differs")
    if expected_components is not None and components != expected_components:
        raise EvidenceError("single-seat evidence components differ")
    return evidence


def atomic_json(path: Path, document: dict) -> str:
    requested = Path(os.path.abspath(path))
    if os.path.lexists(requested):
        raise EvidenceError("combined evidence output must not already exist")
    requested.parent.mkdir(parents=True, exist_ok=True)
    path = requested.parent.resolve() / requested.name
    raw = (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".v1-single-seat-", dir=path.parent)
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


def main() -> int:
    parser = argparse.ArgumentParser()
    for phase in PHASES:
        parser.add_argument(f"--{phase.replace('_', '-')}", type=Path, required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--artifact-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        facts = {}
        digests = {}
        for phase in PHASES:
            facts[phase], digests[phase] = safe_load(getattr(args, phase), phase)
            verify_artifacts(facts[phase], args.artifact_directory)
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
