#!/usr/bin/env python3
"""Static release gate for the user-visible V1 product contract."""
from __future__ import annotations

import ast
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]


def require(condition: bool, message: str, failures: list[str]) -> None:
    if not condition:
        failures.append(message)


def source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def append_only_record_failures(plugin: str) -> list[str]:
    """Return violations of the Hydro append-only record boundary."""
    failures: list[str] = []
    require(
        plugin.count("RecordModel.add(") == 1,
        "Hydro plugin must have exactly one append-only record creation site",
        failures,
    )
    require(
        "contest: tdoc.docId" in plugin and "type: 'judge'" in plugin,
        "Hydro record creation must be a new contest judge submission",
        failures,
    )
    for reservation_guard in (
        "phase: 'reserved'",
        "if (!persisted.rid)",
        "OrchestratorSubmissionAmbiguousError",
        "fs.fsyncSync(handle)",
    ):
        require(
            reservation_guard in plugin,
            f"Hydro submission reservation misses: {reservation_guard}",
            failures,
        )
    for forbidden_record_api in (
        "RecordModel.reset(",
        "RecordModel.update(",
        "RecordModel.updateMulti(",
        "RecordModel.coll.update",
        "RecordModel.coll.replace",
        "RecordModel.coll.delete",
        "RecordModel.coll.bulkWrite",
        "RecordModel.collHistory",
        "type: 'rejudge'",
        'type: "rejudge"',
        "operation: 'rejudge'",
        'operation: "rejudge"',
    ):
        require(
            forbidden_record_api not in plugin,
            f"Hydro plugin may not mutate existing OJ records: {forbidden_record_api}",
            failures,
        )
    return failures


def main() -> int:
    failures: list[str] = []
    main_source = source("orchestrator/main.py")
    requirements = source("orchestrator/requirements.txt").splitlines()
    require(
        "tencentcloud-sdk-python-common==3.1.156" in requirements
        and "tencentcloud-sdk-python-cvm==3.1.156" in requirements
        and not any(line.startswith("tencentcloud-sdk-python-cvm>=") for line in requirements),
        "controller image must pin one compatible Tencent Cloud SDK pair",
        failures,
    )
    require(
        "alibabacloud-ecs20140526==7.9.6" in requirements
        and "alibabacloud-tea-openapi==0.4.5" in requirements
        and not any(line.startswith("alibabacloud-ecs20140526>=") for line in requirements)
        and not any(line.startswith("alibabacloud-tea-openapi>=") for line in requirements),
        "controller image must pin one compatible Alibaba Cloud SDK pair",
        failures,
    )
    tree = ast.parse(main_source, filename="orchestrator/main.py")
    sections = None
    routes: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "ADMIN_SECTIONS":
                    sections = ast.literal_eval(node.value)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                if (
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and isinstance(decorator.func.value, ast.Name)
                    and decorator.func.value.id == "app"
                    and decorator.func.attr in {"get", "post"}
                    and decorator.args
                    and isinstance(decorator.args[0], ast.Constant)
                    and isinstance(decorator.args[0].value, str)
                ):
                    routes.add((decorator.args[0].value, decorator.func.attr.upper()))

    require(
        sections
        == {
            "overview": "比赛总览",
            "materials": "比赛材料",
            "seats": "学生座位",
            "submissions": "递交与评测",
            "finish": "结束与归档",
        },
        "teacher console must expose exactly the five V1 pages",
        failures,
    )
    for forbidden in (
        ("/admin/boot", "POST"),
        ("/admin/shutdown", "POST"),
        ("/admin/sync-roster", "POST"),
        ("/admin/materials/{tid}/{revision}/manifest", "GET"),
    ):
        require(forbidden not in routes, f"forbidden teacher route: {forbidden}", failures)

    entrypoint = source(
        "noi-linux-official/rootfs/usr/local/bin/contest-entrypoint.sh"
    )
    capture_helper = source(
        "noi-linux-official/rootfs/usr/local/bin/capture-formal-source.py"
    )
    for name in (
        "01_比赛题面.pdf",
        "02_辅助自测数据",
        "03_开始答题.desktop",
        "03_答案文件夹",
        "04_CSP程序回收系统.html",
        "05_使用说明.txt",
    ):
        require(name in entrypoint, f"missing desktop object: {name}", failures)
    require(
        "V1 requires the fixed web-first and folder-fallback submission contract"
        in entrypoint,
        "desktop must reject legacy submission modes",
        failures,
    )
    pipeline = source("orchestrator/services/pipeline.py")
    for marker in (
        'source_name = "web_submit" if web_selected else "deadline_snapshot"',
        'user_web_rows[name] = latest if web_selected else None',
        'item["reuses_confirmed_submission"] = web_selected',
    ):
        require(
            marker in pipeline,
            f"per-problem web-first collection misses: {marker}",
            failures,
        )
    for descriptor_guard in (
        "os.O_NOFOLLOW",
        "dir_fd=",
        "follow_symlinks=False",
        "source_before.st_nlink != 1",
    ):
        require(
            descriptor_guard in capture_helper,
            f"formal source capture misses descriptor guard: {descriptor_guard}",
            failures,
        )

    config = source("orchestrator/config.example.yaml")
    require("workspace_retention_days: 30" in config, "missing 30-day policy", failures)
    require("evidence_retention_days: 180" in config, "missing 180-day policy", failures)

    product_docs = "\n".join(
        source(path)
        for path in ("README.md", "docs/PRODUCT.md", "docs/TEACHER_GUIDE.md")
    )
    for phrase in (
        "OJ 是唯一权威",
        "正式答案目录",
        "每次递交",
        "30 天",
        "180 天",
    ):
        require(phrase in product_docs, f"product documentation misses: {phrase}", failures)

    plugin = source("hydro-plugin-orchestrator/index.js")
    for endpoint in (
        "/orchestrator/submit",
        "/orchestrator/submit/status",
        "/orchestrator/submit/notify",
        "/orchestrator/submit/materials",
    ):
        require(endpoint in plugin, f"plugin endpoint missing: {endpoint}", failures)

    # OJ records are the production authority.  The NOI integration is an
    # append-only submission producer: it may create a new judge record and
    # update the contest status pointer/counters, but it must never rejudge,
    # rewrite, replace, or delete an existing RecordModel document.
    failures.extend(append_only_record_failures(plugin))

    production_python = "\n".join(
        source(path)
        for path in (
            "orchestrator/main.py",
            "orchestrator/services/hydro_submit.py",
            "orchestrator/services/realtime_judge.py",
            "orchestrator/services/pipeline.py",
        )
    ).lower()
    for forbidden_operation in (
        '"operation": "rejudge"',
        "'operation': 'rejudge'",
        "/record/",
        "record.history",
    ):
        require(
            forbidden_operation not in production_python,
            f"controller may not expose an OJ record mutation path: {forbidden_operation}",
            failures,
        )

    hydro_submit = source("orchestrator/services/hydro_submit.py")
    realtime_judge = source("orchestrator/services/realtime_judge.py")
    store = source("orchestrator/services/store.py")
    for ambiguity_guard, location in (
        ('error_name == "OrchestratorSubmissionAmbiguousError"', hydro_submit),
        ('state == "ambiguous"', realtime_judge),
        ('"ambiguous" if ambiguous else', store),
        ('target["judge_state"] == "ambiguous"', store),
        ("resolveAmbiguousSubmission", plugin),
        ("orchestratorSubmissionId", plugin),
        ("orchestratorPayloadSha256", plugin),
        ("files: {", plugin),
        ("record.judgeAt instanceof Date", plugin),
        ("finish_ambiguous_web_submission", realtime_judge),
        ('counts.get("ambiguous", 0)', main_source),
        ("系统不会自动重发不确定递交", main_source),
    ):
        require(
            ambiguity_guard in location,
            f"ambiguous OJ result guard missing: {ambiguity_guard}",
            failures,
        )

    fault_gate = source("scripts/run_v1_fault_injection.py")
    workflow = source(".github/workflows/ci.yml")
    builder = source("scripts/build_v1_candidate.py")
    linux_ci = source("scripts/run_v1_linux_ci.py")
    linux_ci_verifier = source("scripts/verify_v1_linux_ci_evidence.py")
    image_fact = source("scripts/collect_v1_image_host_fact.py")
    image_evidence = source("scripts/verify_v1_cross_machine_image_evidence.py")
    single_seat_evidence = source("scripts/verify_v1_single_seat_evidence.py")
    single_seat_components = source("scripts/collect_v1_components.py")
    single_seat_ordinary_oj = source("scripts/collect_v1_ordinary_oj_observation.py")
    single_seat_session = source("scripts/init_v1_single_seat_session.py")
    single_seat_phase = source("scripts/collect_v1_single_seat_phase_fact.py")
    test_cleanup_observation = source("scripts/collect_v1_test_cleanup_observation.py")
    imported_promotion = source("deploy/promote-imported-contest-image-local.sh")
    promotion_recovery = source("deploy/recover-image-promotion-local.sh")
    qualification_guide = source("deploy/V1_QUALIFICATION.md")
    clean_install_rehearsal_guide = source(
        "deploy/V1_CLEAN_INSTALL_REHEARSAL.md"
    )
    capacity_guide = source("deploy/V1_CAPACITY_REHEARSAL.md")
    qualification_validator = source("scripts/verify_v1_qualification.py")
    qualification_report_builder = source("scripts/build_v1_qualification_report.py")
    fault_evidence_verifier = source("scripts/verify_v1_fault_recovery_evidence.py")
    teacher_install_verifier = source("scripts/verify_v1_independent_teacher_install.py")
    teacher_install_builder = source("scripts/build_v1_independent_teacher_install_evidence.py")
    teacher_install_collector = source("scripts/collect_v1_independent_teacher_install_observation.py")
    clean_install_rehearsal = source("scripts/verify_v1_clean_install_rehearsal.py")
    clean_install_rehearsal_schema = source("release/v1-clean-install-rehearsal-matrix.schema.json")
    clean_install_scenario_runner = source("scripts/run_v1_clean_install_rehearsal_scenario.py")
    clean_install_power_loss_supervisor = source("scripts/run_v1_clean_install_power_loss_supervisor.py")
    clean_install_observation = source("scripts/collect_v1_clean_install_rehearsal_observation.py")
    clean_install_observation_schema = source("release/v1-clean-install-rehearsal-observation.schema.json")
    clean_install_matrix_builder = source("scripts/build_v1_clean_install_rehearsal_matrix.py")
    clean_install_case_runner = source("scripts/run_v1_clean_install_rehearsal_case.py")
    source_release_transaction = source("scripts/stage_v1_source_release.py")
    install_backup_verifier = source("scripts/verify_v1_install_backup.py")
    install_backup_builder = source("scripts/build_v1_install_backup_manifest.py")
    install_backup_collector = source("scripts/build_v1_install_backup.py")
    clean_backup_collector = source("scripts/build_v1_clean_install_backup.py")
    clean_backup_builder = source("scripts/build_v1_clean_install_backup_manifest.py")
    clean_backup_verifier = source("scripts/verify_v1_clean_install_backup.py")
    clean_backup_schema = source("release/v1-clean-install-backup-manifest.schema.json")
    clean_materials = source("scripts/prepare_v1_clean_install_materials.py")
    hydro_backup_verifier = source("scripts/verify_v1_hydro_install_backup.py")
    hydro_backup_builder = source("scripts/build_v1_hydro_install_backup.py")
    install_rollback_verifier = source("scripts/verify_v1_install_rollback.py")
    install_transaction = source("orchestrator/services/install_transaction.py")
    install_phase_drivers = source("orchestrator/services/install_phase_drivers.py")
    hydro_restore = source("scripts/restore_v1_hydro_install_backup.py")
    hydro_addon_installer = source("deploy/install-hydro-orchestrator-addon.sh")
    caddy_hardener = source("deploy/harden-hydro-submit-route.sh")
    caddy_configurer = source("deploy/configure-hydro-caddy.sh")
    caddy_conditional_commit = source("scripts/commit_v1_caddy_config.py")
    closed_frontend_phase = source("scripts/apply_v1_closed_frontend.py")
    controller_backup_builder = source("scripts/build_v1_controller_install_backup.py")
    controller_backup_verifier = source("scripts/verify_v1_controller_install_backup.py")
    controller_phase = source("scripts/apply_v1_controller.py")
    controller_quiesce = source("scripts/quiesce_v1_controller.py")
    cloud_backup_builder = source("scripts/build_v1_cloud_install_backup.py")
    cloud_backup_verifier = source("scripts/verify_v1_cloud_install_backup.py")
    post_install_verifier = source("scripts/verify_v1_post_install.py")
    live_rollback_verifier = source("scripts/verify_v1_live_install_rollback.py")
    install_apply = source("scripts/apply_v1_install.py")
    private_upgrade_plan_builder = source("scripts/build_v1_private_upgrade_plan.py")
    install_plan_schema = source("release/v1-private-install-plan.schema.json")
    private_clean_plan_builder = source("scripts/build_v1_private_clean_install_plan.py")
    clean_install_apply = source("scripts/apply_v1_clean_install.py")
    clean_live_rollback_verifier = source("scripts/verify_v1_clean_install_rollback.py")
    clean_install_plan_schema = source("release/v1-private-clean-install-plan.schema.json")
    readiness_reporter = source("scripts/report_v1_launch_readiness.py")
    noictl = source("scripts/noictl.py")
    capacity_collector = source("scripts/collect_v1_capacity_evidence.py")
    capacity_probe_builder = source("scripts/build_v1_capacity_probe.py")
    capacity_probe = source("scripts/v1_capacity_measurement_probe.py")
    capacity_browser_agent = source("capacity-browser-agent/agent.mjs")
    capacity_browser_library = source("capacity-browser-agent/lib.mjs")
    capacity_browser_package = source("capacity-browser-agent/package.json")
    capacity_browser_lock = source("capacity-browser-agent/package-lock.json")
    capacity_browser_publisher = source("capacity-browser-agent/publish.mjs")
    capacity_browser_schema = source("release/v1-capacity-browser-agent-config.schema.json")
    capacity_telemetry_installer = source("scripts/install_v1_capacity_telemetry.py")
    capacity_seat_probe = source("scripts/v1_capacity_seat_inventory_probe.py")
    capacity_seat_builder = source("scripts/build_v1_capacity_seat_inventory_probe.py")
    capacity_seat_schema = source("release/v1-capacity-seat-inventory-probe-config.schema.json")
    capacity_ordinary_agent = source("scripts/v1_capacity_ordinary_oj_agent.py")
    capacity_ordinary_builder = source("scripts/build_v1_capacity_ordinary_oj_agent.py")
    capacity_ordinary_schema = source("release/v1-capacity-ordinary-oj-agent-config.schema.json")
    capacity_ordinary_installer = source("scripts/install_v1_capacity_ordinary_oj_telemetry.py")
    capacity_ordinary_publisher = source("scripts/publish_v1_capacity_ordinary_oj_telemetry.py")
    capacity_shutdown_probe = source("scripts/v1_capacity_shutdown_probe.py")
    capacity_shutdown_builder = source("scripts/build_v1_capacity_shutdown_probe.py")
    capacity_shutdown_schema = source("release/v1-capacity-shutdown-probe-config.schema.json")
    capacity_workload_probe = source("scripts/v1_capacity_workload_probe.py")
    capacity_workload_builder = source("scripts/build_v1_capacity_workload_probe.py")
    capacity_workload_schema = source("release/v1-capacity-workload-probe-config.schema.json")
    capacity_workload_agent = source("scripts/v1_capacity_workload_action_agent.py")
    capacity_workload_agent_builder = source("scripts/build_v1_capacity_workload_action_agent.py")
    capacity_workload_agent_schema = source("release/v1-capacity-workload-action-agent-config.schema.json")
    capacity_fault_probe = source("scripts/v1_capacity_fault_probe.py")
    capacity_fault_builder = source("scripts/build_v1_capacity_fault_probe.py")
    capacity_fault_schema = source("release/v1-capacity-fault-probe-config.schema.json")
    capacity_network_agent = source("scripts/v1_capacity_network_fault_agent.py")
    capacity_network_builder = source("scripts/build_v1_capacity_network_fault_agent.py")
    capacity_network_schema = source("release/v1-capacity-network-fault-agent-config.schema.json")
    control_restart_agent = source("scripts/v1_control_restart_action_agent.py")
    control_restart_builder = source("scripts/build_v1_control_restart_action_agent.py")
    control_restart_schema = source("release/v1-control-restart-action-agent-config.schema.json")
    collection_retry_agent = source("scripts/v1_collection_retry_action_agent.py")
    collection_retry_builder = source("scripts/build_v1_collection_retry_action_agent.py")
    collection_retry_schema = source("release/v1-collection-retry-action-agent-config.schema.json")
    power_loss_agent = source("scripts/v1_power_loss_recovery_action_agent.py")
    power_loss_builder = source("scripts/build_v1_power_loss_recovery_action_agent.py")
    power_loss_schema = source("release/v1-power-loss-recovery-action-agent-config.schema.json")
    production_config_example = source("orchestrator/config.example.yaml")
    capacity_guard = source("scripts/v1_capacity_rehearsal_guard.py")
    capacity_guard_schema = source("release/v1-capacity-rehearsal-guard-config.schema.json")
    for marker in (
        "response_lost_after_commit",
        "plugin_restart_and_concurrent_resolution",
        "controller_restart",
        "resolution_network_failure",
        "concurrent_sqlite_claim",
    ):
        require(marker in fault_gate, f"fault-injection scenario missing: {marker}", failures)
    require(
        "Stage a root-owned qualification tree" in workflow
        and 'sudo cp -a "$GITHUB_WORKSPACE/." "$qualification_root/"' in workflow
        and 'sudo chown -R root:root "$qualification_root"' in workflow
        and 'exec "$2" scripts/run_v1_linux_ci.py' in workflow
        and '--output "$3/v1-linux-ci-evidence.json"' in workflow
        and '--log-directory "$3/v1-linux-ci-logs"' in workflow
        and "verify_v1_linux_ci_evidence.py v1-linux-ci-evidence.json" in workflow
        and "actions/upload-artifact@v7" in workflow,
        "Linux CI must generate qualification evidence from a root-owned tree, verify it, and upload it",
        failures,
    )
    require(
        '"operation": "clean-install"' in clean_backup_collector
        and "require_absent" in clean_backup_collector
        and "Caddyfile already contains NOI integration" in clean_backup_collector
        and "collect_hydro" in clean_backup_collector
        and "collect_controller" in clean_backup_collector
        and "service_mutations\": 0" in clean_backup_collector
        and "CLEAN_MUST_BE_ABSENT" in clean_backup_builder
        and "clean target contains an NOI controller" in clean_backup_builder
        and "clean target contains an NOI Hydro tree" in clean_backup_builder
        and "verify_clean_backup(root, plan_id)" in clean_backup_builder
        and "clean target contains an NOI controller" in clean_backup_verifier
        and "clean target contains an NOI Hydro tree" in clean_backup_verifier
        and "v1-clean-install-backup-manifest.schema.json" in clean_backup_schema,
        "clean install must seal and semantically verify explicit absence before any service mutation",
        failures,
    )
    require(
        '"prepare-clean-install-materials"' in clean_materials
        and "clean material target appeared before apply" in clean_materials
        and "owned private material changed outside transaction" in clean_materials
        and "owned clean install directory is not empty" in clean_materials
        and "clean-materials." in clean_materials
        and '"service_mutations":0' in clean_materials.replace(" ", ""),
        "clean private materials must be durable, idempotent, and removable only while exact",
        failures,
    )
    require(
        '"operation":"clean-install"' in private_clean_plan_builder.replace(" ", "")
        and "CLEAN_STAGING_FILES" in private_clean_plan_builder
        and "desired plugin token differs from site shared token" in private_clean_plan_builder
        and "desired plugin env contract differs" in private_clean_plan_builder
        and "private_artifact_sha256" in private_clean_plan_builder
        and "controller template bind set differs" in private_clean_plan_builder
        and "controller template isolation contract differs" in private_clean_plan_builder
        and "safe_docker_socket" in private_clean_plan_builder
        and '"operation": {"const": "clean-install"}' in clean_install_plan_schema
        and "def load_plan" in clean_install_apply
        and "private clean plan layout differs" in clean_install_apply
        and "private clean artifact content differs" in clean_install_apply
        and '"clean_materials":CleanMaterialsDriver' in clean_install_apply
        and "run_clean" in clean_install_apply
        and "CleanFinalRollbackVerifier" in clean_install_apply
        and "clean rollback Caddy state differs" in clean_live_rollback_verifier
        and "clean rollback Hydro state differs" in clean_live_rollback_verifier
        and "clean rollback left an NOI controller" in clean_live_rollback_verifier
        and "clean rollback cloud state differs" in clean_live_rollback_verifier,
        "clean install must have one strict private plan, exact six-phase executor, and independent terminal rollback proof",
        failures,
    )
    require(
        "def trusted_self" in install_apply
        and "safe_private_file(path,\"private install plan\"" in install_apply
        and "private install plan trust pin differs" in install_apply
        and "private install artifact content differs" in install_apply
        and "post-install contract differs from private install plan" in install_apply
        and '"source_release":SourceReleaseDriver' in install_apply
        and '"controller_quiesce":ControllerQuiesceDriver' in install_apply
        and '"post_install_verification":PostInstallVerificationDriver' in install_apply
        and "FinalRollbackVerifier" in install_apply
        and "v1-private-install-plan.schema.json" in install_plan_schema,
        "production apply must be one private-plan-pinned six-phase transaction with one final rollback verifier",
        failures,
    )
    require(
        '"controller_quiesce"' in install_transaction
        and "live_files_match_backup(args,backup,manifest)" in controller_quiesce
        and "docker.stop" in controller_quiesce
        and "controller quiesce evidence differs" in install_phase_drivers
        and install_transaction.find('"controller_quiesce"') < install_transaction.find('"hydro_integration"'),
        "the sealed controller must be quiesced and rebound before Hydro or Caddy mutation",
        failures,
    )
    require(
        "def collect(args)" in install_backup_collector
        and "backup_database" in install_backup_collector
        and "collect_hydro" in install_backup_collector
        and "collect_controller" in install_backup_collector
        and "build_cloud" in install_backup_collector
        and "seal_manifest" in install_backup_collector,
        "one read-only collector must durably build and seal the complete install backup",
        failures,
    )
    require(
        "class PostInstallVerificationDriver" in install_phase_drivers
        and "post-install verification evidence differs" in install_phase_drivers
        and "class FinalRollbackVerifier" in install_phase_drivers
        and "final live rollback verification differs" in install_phase_drivers
        and "desktop access is not exactly closed" in post_install_verifier
        and "closed Caddy disk/live state differs" in post_install_verifier
        and "compare_ordinary(baseline,ordinary)" in post_install_verifier
        and "realtime judge queue is not quiet" in post_install_verifier
        and "restored Hydro state differs" in live_rollback_verifier
        and "baseline controller identity differs after dependency restoration" in live_rollback_verifier
        and "restored cloud state differs from baseline" in live_rollback_verifier,
        "terminal install gates must prove closed cloud, exact Caddy/Hydro/OJ state, and dependency-ordered controller recovery",
        failures,
    )
    require(
        "controller health probe failed" in cloud_backup_builder
        and "cloud baseline did not stabilize" in cloud_backup_builder
        and "cloud baseline is not exactly closed" in cloud_backup_verifier
        and "production upgrade requires one running controller baseline" in install_backup_builder
        and "verify_cloud_backup" in install_backup_builder
        and "verify_cloud_backup" in install_backup_verifier,
        "production upgrade backup must bind one running controller to one exact closed cloud baseline",
        failures,
    )
    require(
        "Hydro install backup collection requires Linux root" in hydro_backup_builder
        and "PM2 live Hydro environment differs from persistent dump" in hydro_backup_builder
        and "PM2 live Hydro launch definition differs from persistent dump" in hydro_backup_builder
        and "Hydro backup tree contains a symlink" in hydro_backup_builder
        and "verify_tree_archive(raw,filename)" in hydro_backup_builder
        and "verify_pm2(dump_raw,raw)" in hydro_backup_builder,
        "Hydro backup collector must be deterministic, live-bound, and self-verifying",
        failures,
    )
    require(
        'admin.request("GET","/config/")' in caddy_conditional_commit
        and 'admin.request("POST","/adapt"' in caddy_conditional_commit
        and 'admin.request("POST","/config/"' in caddy_conditional_commit
        and '"If-Match":etag' in caddy_conditional_commit
        and "Caddy conditional commit lost an ETag race" in caddy_conditional_commit
        and "Caddy conditional rollback lost an ETag race" in caddy_conditional_commit
        and "refusing rollback overwrite" in caddy_conditional_commit
        and "Caddy candidate file changed before conditional commit" in caddy_conditional_commit
        and 'path=="/load"' not in caddy_conditional_commit,
        "Caddy production commit must be one ETag-protected native JSON transaction",
        failures,
    )
    require(
        "class ClosedFrontendDriver" in install_phase_drivers
        and "closed frontend apply evidence differs" in install_phase_drivers
        and "closed frontend rollback evidence differs" in install_phase_drivers
        and "live Caddyfile differs from backup baseline" in closed_frontend_phase
        and "backup Caddy disk and active config differ" in closed_frontend_phase
        and "frontend disk state changed outside this transaction" in closed_frontend_phase
        and "hydro_route_hardened" in closed_frontend_phase
        and "respond \"\u6bd4\u8d5b\u684c\u9762\u5c1a\u672a\u5f00\u653e\" 503" in closed_frontend_phase,
        "closed frontend phase must be frozen-source, baseline-bound, closed, and exactly restorable",
        failures,
    )
    require(
        "class ControllerDriver" in install_phase_drivers
        and "controller apply evidence differs" in install_phase_drivers
        and "controller rollback evidence differs" in install_phase_drivers
        and "controller commit cleanup evidence differs" in install_phase_drivers
        and "immutable_identity_sha256" in controller_backup_builder
        and "controller Docker socket is unsafe" in controller_backup_builder
        and "controller container identity differs" in controller_backup_verifier
        and "safe_private_file" in controller_phase
        and "controller image identity differs" in controller_phase
        and "Never restart the old controller here" in controller_phase
        and "committed controller identity differs during cleanup" in controller_phase,
        "controller phase must be immutable-image-bound, private-input-bound, quiesced on rollback, and cleanup-safe",
        failures,
    )
    require(
        all("NO_CADDY_LOAD" in content and 'if [[ "${no_caddy_load}" = 1 ]]' in content
            for content in (caddy_hardener, caddy_configurer))
        and "hydro_submit_route_candidate_ready" in caddy_hardener
        and "caddy_exam_candidate_ready" in caddy_configurer,
        "Caddy child transforms must support a no-load candidate transaction mode",
        failures,
    )
    require(
        "SOURCE_DIR:-/opt/noi-linux-contest-system/current-source/hydro-plugin-orchestrator" in hydro_addon_installer
        and "EXPECTED_SOURCE_RELEASE:?set EXPECTED_SOURCE_RELEASE" in hydro_addon_installer
        and "SOURCE_DIR does not resolve to EXPECTED_SOURCE_RELEASE" in hydro_addon_installer
        and "source-releases/[a-f0-9]{40}-[a-f0-9]{12}" in hydro_addon_installer
        and "plugin source tree contains an unexpected entry" in hydro_addon_installer
        and "plugin source tree is incomplete" in hydro_addon_installer
        and all(f'"tests/{name}"' in hydro_addon_installer for name in (
            "orchestrator-materials.test.js", "orchestrator-notify.test.js",
            "orchestrator-problem-fileio.test.js", "orchestrator-submit.test.js",
        )),
        "Hydro integration must execute only the exact frozen plugin tree",
        failures,
    )
    require(
        'external_transaction="${EXTERNAL_INSTALL_TRANSACTION:-0}"' in hydro_addon_installer
        and "EXTERNAL_INSTALL_TRANSACTION must be 0 or 1" in hydro_addon_installer
        and "compgen -A variable ORCHESTRATOR_" in hydro_addon_installer
        and '"transaction":"external"' in hydro_addon_installer
        and "nested_prefix!=desired" in hydro_addon_installer
        and "not set(top_prefix).issubset(desired)" in hydro_addon_installer,
        "Hydro integration must support exact-prefix external transaction ownership",
        failures,
    )
    require(
        "class HydroIntegrationDriver" in install_phase_drivers
        and '"EXTERNAL_INSTALL_TRANSACTION": "1"' in install_phase_drivers
        and "Hydro integration apply evidence differs" in install_phase_drivers
        and "Hydro integration rollback evidence differs" in install_phase_drivers
        and "--backup-manifest-sha256" in install_phase_drivers
        and "RENAME_EXCHANGE = 2" in hydro_restore
        and '["delete","hydrooj"]' in hydro_restore.replace(" ", "")
        and '["resurrect"]' in hydro_restore.replace(" ", "")
        and "PM2 Hydro process still exists after delete" in hydro_restore
        and "live_matches_backup" in hydro_restore
        and '"other_pm2_mutations":0' in hydro_restore.replace(" ", ""),
        "Hydro phase must be frozen-source, exact-backup, Hydro-only, and idempotently restorable",
        failures,
    )
    require(
        '"source_release"' in install_transaction
        and '"hydro_integration"' in install_transaction
        and '"closed_frontend"' in install_transaction
        and '"controller"' in install_transaction
        and '"post_install_verification"' in install_transaction
        and "ROLLBACK_ORDER = (" in install_transaction
        and install_transaction.index(
            '"controller",\n    "hydro_integration",\n    "closed_frontend"'
        ) > 0
        and "journal[\"in_progress\"] = phase" in install_transaction
        and "Never continue forward from a pre-existing journal" in install_transaction
        and 'journal["status"] = "manual_intervention"' in install_transaction
        and "another service install transaction is running" in install_transaction,
        "service install coordinator core must be durable, dependency-ordered, locked, and fail closed",
        failures,
    )
    require(
        "CLEAN_PHASES = (" in install_transaction
        and '"clean_materials"' in install_transaction
        and "CLEAN_ROLLBACK_ORDER = (" in install_transaction
        and "def run_clean" in install_transaction
        and "CLEAN_PHASES, CLEAN_ROLLBACK_ORDER" in install_transaction,
        "clean install must reuse the durable coordinator with a separate exact phase order",
        failures,
    )
    require(
        "service-install.rollback-verification-" in install_transaction
        and "service-install.rollback-cleanup-" in install_transaction
        and "_verify_and_cleanup_rollback" in install_transaction
        and "A cleanup failure after a durable live verification is retryable" in install_transaction,
        "rollback live verification must be durable before any retryable terminal cleanup",
        failures,
    )
    require(
        "--rollback-owned" in install_phase_drivers
        and "expected_manifest_sha256" in install_phase_drivers
        and "source_plan_id" in install_phase_drivers
        and "source release apply evidence differs" in install_phase_drivers
        and "source release rollback evidence differs" in install_phase_drivers
        and "rollback_committed" in source_release_transaction
        and "rollback_owned" in source_release_transaction
        and "committed source release no longer owns current-source" in source_release_transaction
        and "source-rollback.pending.json" in source_release_transaction,
        "source release phase must be externally pinned and support durable owned rollback",
        failures,
    )
    require(
        "restored artifact {name} differs from baseline" in install_rollback_verifier
        and "restored optional artifact {name} should be absent" in install_rollback_verifier
        and "install pending marker still exists" in install_rollback_verifier
        and '"ordinary_oj_unchanged": True' in install_rollback_verifier
        and '"pending_marker_cleared": True' in install_rollback_verifier
        and "rollback receipt already exists" in install_rollback_verifier,
        "rollback verified must mean exact artifact equality and no pending marker",
        failures,
    )
    require(
        "install backup sealing requires Linux root" in install_backup_builder
        and "backup manifest already exists" in install_backup_builder
        and "os.fsync(descriptor)" in install_backup_builder
        and "fsync_directory(root)" in install_backup_builder
        and "validate_manifest(value, root" in install_backup_builder,
        "install backup manifest must be machine-built, fsynced, and self-verified",
        failures,
    )
    require(
        "verify_hydro_backup(root, plan_id)" in install_backup_builder
        and "verify_hydro_backup(directory, value[\"plan_id\"])" in install_backup_verifier
        and "tree-state.json" in hydro_backup_verifier
        and "PM2 dump must contain exactly one Hydro definition" in hydro_backup_verifier
        and "PM2 Hydro definition does not match the dump" in hydro_backup_verifier
        and "contains an unexpected payload" in hydro_backup_verifier,
        "sealed install backups must have exact Hydro tree and PM2 semantics",
        failures,
    )
    require(
        "teacher install evidence must be built by root on Linux" in teacher_install_builder
        and "candidate did not pass complete verification" in teacher_install_builder
        and "ordinary OJ before/after artifact bytes differ" in teacher_install_builder
        and '"-Y", "sign"' in teacher_install_builder
        and "expected_archive_sha256" in teacher_install_builder
        and "--expected-manifest-sha256" in teacher_install_builder
        and "candidate archive contains an unexpected entry" in teacher_install_builder
        and "noi-v1-teacher-candidate-" in teacher_install_builder
        and "cwd=source_root" in teacher_install_builder
        and "--candidate-verifier" not in teacher_install_builder,
        "teacher install evidence must be machine built, candidate-bound, and externally signed",
        failures,
    )
    require(
        '"phase_failure", "power_loss"' in clean_install_rehearsal
        and '"source_release", "clean_materials", "hydro_integration"' in clean_install_rehearsal
        and '"closed_frontend", "controller", "post_install_verification"' in clean_install_rehearsal
        and "len(scenarios) != len(PHASES) * 2" in clean_install_rehearsal
        and "ordinary_oj_before_sha256" in clean_install_rehearsal
        and "ordinary_oj_after_sha256" in clean_install_rehearsal
        and "clean_install_rehearsal" in teacher_install_builder
        and "validate_clean_rehearsal" in teacher_install_builder
        and "qualification-lab plan" in clean_install_scenario_runner
        and "after_phase_committed=hook" in clean_install_scenario_runner
        and "ready_marker(marker, row, current)" in clean_install_scenario_runner
        and "kill_process(os.getpid(), SIGKILL)" in clean_install_scenario_runner
        and 'returncode != -SIGKILL' in clean_install_power_loss_supervisor
        and 'contain(child.pid)' in clean_install_power_loss_supervisor
        and '"resume"' in clean_install_power_loss_supervisor
        and '"terminal": "rollback_verified"' in clean_install_power_loss_supervisor
        and "post_install.verify(args)" in clean_install_observation
        and "rollback.verify(args)" in clean_install_observation
        and "validate_terminal_journal" in clean_install_observation
        and "ordinary_collector(row)" in clean_install_observation
        and "compare_ordinary(before, after)" in clean_install_observation
        and "ordinary_before_raw != ordinary_after_raw" not in clean_install_observation
        and "load_observations" in clean_install_matrix_builder
        and "candidate_identity" in clean_install_matrix_builder
        and "validate_matrix(document" in clean_install_matrix_builder
        and "power_supervise(row" in clean_install_case_runner
        and "collector(row" in clean_install_case_runner
        and "private_case_directory(output)" in clean_install_case_runner
        and '"private_plan_sha256"' in clean_install_rehearsal_schema
        and '"terminal_receipt"' in clean_install_observation_schema
        and '"minItems": 12' in clean_install_rehearsal_schema
        and '"maxItems": 12' in clean_install_rehearsal_schema,
        "independent teacher evidence must bind one success plus every phase failure and power-loss rollback",
        failures,
    )
    require(
        "每个场景都从 `clean-baseline` 的新克隆开始" in clean_install_rehearsal_guide
        and "不允许在同一未恢复 VM 中直接一次跑完整循环" in clean_install_rehearsal_guide
        and "run_v1_clean_install_rehearsal_case.py" in clean_install_rehearsal_guide
        and "phase_failure-$phase" in clean_install_rehearsal_guide
        and "power_loss-$phase" in clean_install_rehearsal_guide
        and "build_v1_clean_install_rehearsal_matrix.py" in clean_install_rehearsal_guide
        and "不能只保留组合 JSON" in clean_install_rehearsal_guide
        and "V1_CLEAN_INSTALL_REHEARSAL.md" in qualification_guide,
        "clean install qualification must document fresh-snapshot 13-case execution and evidence retention",
        failures,
    )
    require(
        "require_production_qualified=require_production" in source_release_transaction
        and '"qualification-lab" if qualification_lab else "production"' in source_release_transaction
        and "candidate differs from the external manifest trust pin" in source_release_transaction
        and "source-install.pending.json" in source_release_transaction
        and "source-install.committed-" in source_release_transaction
        and "rollback_verified" in source_release_transaction
        and '"service_mutations": 0' in source_release_transaction
        and "current-source changed outside the pending transaction" in source_release_transaction,
        "source release staging must be externally pinned, service-free, durable, and recoverable",
        failures,
    )
    require(
        "orchestrator_database_wal" in install_backup_verifier
        and "orchestrator_database_shm" in install_backup_verifier
        and "caddy_active" in install_backup_verifier
        and "hydro_plugin_state" in install_backup_verifier
        and "hydro_pm2_definition" in install_backup_verifier
        and "ordinary_oj_snapshot" in install_backup_verifier
        and "cloud_snapshot" in install_backup_verifier
        and "backup directory contains an unmanifested entry" in install_backup_verifier,
        "service mutation must require one exact, durable, complete backup set",
        failures,
    )
    require(
        "expected_failed_row_sha256" in collection_retry_schema
        and "qualification_failure_marker_path" in source("orchestrator/services/hydro_submit.py")
        and "block_until_removed" in source("orchestrator/services/hydro_submit.py")
        and "failed_submission" in collection_retry_agent
        and "one exact failed qualification row" in collection_retry_agent
        and "wait_contest(row, \"error\"" in collection_retry_agent
        and "remove_marker(marker)" in collection_retry_agent
        and "wait_contest(row, \"safe_wait\"" in collection_retry_agent
        and "collection_receipt_unique" in collection_retry_agent
        and "collection retry agent build requires Linux root" in collection_retry_builder
        and "qualification_failure_marker_path" not in production_config_example
        and "qualification_marker" not in production_config_example
        and "v1-collection-retry-action-agent-config.schema.json" in qualification_guide,
        "collection retry qualification must inject one scoped failure and prove one unique recovery",
        failures,
    )
    require(
        "controller_probe_target_sha256" in capacity_network_schema
        and "signing_public_key" in capacity_network_schema
        and '"--net"' in capacity_network_agent
        and '"OUTPUT"' in capacity_network_agent
        and '"REJECT"' in capacity_network_agent
        and "stale network fault state was recovered" in capacity_network_agent
        and "preflight-signature-check" in capacity_network_agent
        and "network fault controller lifecycle changed" in capacity_network_agent
        and "network fault frozen agent" in capacity_network_agent
        and "network_action_agent_sha256" in capacity_fault_schema
        and "network fault agent build requires Linux root" in capacity_network_builder
        and "v1-capacity-network-fault-agent-config.schema.json" in capacity_guide,
        "network fault execution must be controller-scoped, recoverable, and signature-verified",
        failures,
    )
    require(
        "expected_pending_set_sha256" in control_restart_schema
        and 'WHERE judge_state=\'pending\'' in control_restart_agent
        and "stopped = True; stop_controller(row)" in control_restart_agent
        and "preflight-signature-check" in control_restart_agent
        and "frozen = freeze_pending(row)" in control_restart_agent
        and "start_controller(row); health_ready(row)" in control_restart_agent
        and "verify_unique_records" in control_restart_agent
        and 'NAMESPACE = "noi-v1-fault-recovery-actions"' in control_restart_agent
        and "control restart agent build requires Linux root" in control_restart_builder
        and "v1-control-restart-action-agent-config.schema.json" in qualification_guide,
        "controller restart qualification must freeze pending work, preserve ordinary OJ, and sign unique recovery",
        failures,
    )
    require(
        "promotion_script_sha256" in power_loss_schema
        and "recovery_script_sha256" in power_loss_schema
        and "marker_durable_before_mutation" in imported_promotion
        and 'kill -STOP "$$"' in imported_promotion
        and "signal.SIGKILL" not in power_loss_agent
        and "os.kill(process.pid, SIGKILL)" in power_loss_agent
        and 'Path("/proc") / str(process.pid) / "status"' in power_loss_agent
        and "startup_blocked_pending" in power_loss_agent
        and "--expected-marker-sha256" in power_loss_agent
        and "run_program(recovery_command, clean_env); run_program(recovery_command, clean_env)" in power_loss_agent
        and "power loss agent build requires Linux root" in power_loss_builder
        and "v1-power-loss-recovery-action-agent-config.schema.json" in qualification_guide,
        "power-loss qualification must kill only at a durable pre-mutation marker and prove idempotent recovery",
        failures,
    )
    require(
        "formal_container_ids" in capacity_seat_schema
        and "spare_container_ids" in capacity_seat_schema
        and "probe_novnc" in capacity_seat_probe
        and 'connection.request("GET"' in capacity_seat_probe
        and 'network.get("Internal") is not True' in capacity_seat_probe
        and "seat probe build requires Linux root" in capacity_seat_builder
        and "v1-capacity-seat-inventory-probe-config.schema.json" in capacity_guide,
        "capacity seat inventory must be frozen and derived from all 15+2 live containers",
        failures,
    )
    require(
        "fault_replacement" in capacity_seat_schema
        and "baseline_container_id" in capacity_seat_probe
        and "failed seat was not rebuilt as one fresh container" in capacity_seat_probe
        and "commit_pool_repair" in source("orchestrator/services/store.py")
        and "repair_pool_capacity" in source("orchestrator/services/pipeline.py"),
        "failed-seat replacement must restore and re-verify the consumed spare capacity",
        failures,
    )
    require(
        "expected_pool_revision" in capacity_fault_schema
        and "controller_probe_target_sha256" in capacity_fault_schema
        and "capacity_session_dir" in capacity_fault_schema
        and "network fault occurred outside the capacity sample window" in capacity_fault_probe
        and "mode=ro" in capacity_fault_probe
        and "PRAGMA query_only=ON" in capacity_fault_probe
        and "repair:warm" in capacity_fault_probe
        and "repair:verify" in capacity_fault_probe
        and "controller-egress-deny" in capacity_fault_probe
        and "seat inventory probe SHA256 differs" in capacity_fault_probe
        and "fault probe build requires Linux root" in capacity_fault_builder
        and "v1-capacity-fault-probe-config.schema.json" in capacity_guide,
        "capacity fault evidence must bind pool receipts, live seats, and signed network recovery",
        failures,
    )
    require(
        '"submission_fault_injection"' in linux_ci
        and '"--require-linux"' in linux_ci
        and 'os.geteuid() != 0' in linux_ci
        and '"effective_uid": os.geteuid()' in linux_ci
        and 'private_linux_temp_root()' in linux_ci
        and '("TMPDIR", "TMP", "TEMP")' in linux_ci
        and 'shutil.rmtree(temporary)' in linux_ci,
        "complete Linux CI must include the required fault-injection gate",
        failures,
    )
    require(
        "EXPECTED_GATES" in linux_ci_verifier
        and 'environment["system"] != "linux"' in linux_ci_verifier
        and 'environment["effective_uid"] != 0' in linux_ci_verifier,
        "Linux CI evidence verifier must require the exact Linux gate set",
        failures,
    )
    require(
        "run_fault_injection_gate()" in builder
        and '"submission_fault_injection": "passed"' in builder,
        "candidate builder must run and record the fault-injection gate",
        failures,
    )
    require(
        "docker_image_labels" in image_fact
        and "running contest seat query" in image_fact
        and 'state["pending_transaction"]' in image_evidence,
        "cross-machine fact collector must bind imported labels and quiescent state",
        failures,
    )
    for phase in (
        "export", "imported", "promoted", "rolled_back", "repromoted", "restored"
    ):
        require(
            phase in image_evidence,
            f"cross-machine evidence verifier misses phase: {phase}",
            failures,
        )
    require(
        "--expected-image-id" in imported_promotion
        and "image-promotion.pending" in imported_promotion
        and "ROLLBACK_SOURCE_TARGET" in imported_promotion
        and "verify-contest-image-local.sh" in imported_promotion,
        "imported image promotion must be an immutable paired transaction",
        failures,
    )
    require(
        "--expected-marker-sha256" in promotion_recovery
        and "NOI_IMAGE_RECOVERY_LOCK_HELD" in promotion_recovery
        and "NOI_IMAGE_RECOVERY_DEPLOYMENT_LOCK_HELD" in promotion_recovery
        and "STATUS=rolled_back_to_old_pair" in promotion_recovery
        and 'mv -Tf -- "${receipt_temp}" "${receipt}"' in promotion_recovery
        and 'rm -f -- "${pending}"' in promotion_recovery,
        "interrupted image promotion must have explicit, confirmed, durable recovery",
        failures,
    )
    require(
        "v1-cross-machine-image-evidence.json" in qualification_guide
        and "六份原始事实" in qualification_guide,
        "qualification guide must retain reproducible cross-machine evidence",
        failures,
    )
    require(
        "v1-single-seat-evidence.json" in qualification_guide
        and "九份原始事实" in qualification_guide
        and '"manual_submit"' in single_seat_evidence
        and '"cutoff_submit"' in single_seat_evidence
        and '"test_cleanup"' in single_seat_evidence
        and "hydro_mongo_post_delete_absence" in test_cleanup_observation
        and "linked_record" in test_cleanup_observation
        and "ordinary OJ process fingerprint changed" in single_seat_evidence,
        "single-seat qualification must retain a linked nine-phase evidence chain",
        failures,
    )
    require(
        "--docker-bin" in single_seat_components
        and "observed_at" in single_seat_components
        and "ProxyHandler({})" in single_seat_ordinary_oj
        and "component_facts" in single_seat_session
        and "component observation must precede" in single_seat_phase
        and "V1_SINGLE_SEAT_REHEARSAL.md" in qualification_guide,
        "single-seat qualification must use fresh machine-collected role facts",
        failures,
    )
    require(
        "validate_single_seat_evidence" in builder
        and "--single-seat-evidence" in builder
        and "single-seat-evidence.json" in builder,
        "production candidate builder must verify and embed single-seat evidence",
        failures,
    )
    require(
        "validate_fault_recovery_evidence" in builder
        and "--fault-recovery-evidence" in builder
        and "v1-fault-recovery-evidence.json" in builder
        and 'fault_document["session_id"] != capacity_document["session_id"]' in builder
        and 'allowed.add("v1-fault-recovery-evidence.json")' in source("scripts/verify_v1_candidate.py")
        and '"actions"' in fault_evidence_verifier
        and "verify_signature" in fault_evidence_verifier,
        "production candidates must embed and reverify self-contained signed fault evidence",
        failures,
    )
    require(
        "validate_linux_ci_evidence" in builder
        and "verify_linux_ci_logs" in builder
        and "--linux-ci-evidence" in builder
        and "--linux-ci-log-directory" in builder
        and "validate_cross_machine_evidence" in builder
        and "--cross-machine-evidence" in builder
        and "v1-linux-ci-evidence.json" in qualification_validator
        and "v1-cross-machine-image-evidence.json" in qualification_validator,
        "production candidates must reverify Linux CI logs and cross-machine rollback evidence",
        failures,
    )
    require(
        "compile_report" in qualification_report_builder
        and "verify_logs" in qualification_report_builder
        and "validate_cross_machine" in qualification_report_builder
        and "validate_single_seat" in qualification_report_builder
        and "validate_capacity_evidence" in qualification_report_builder
        and "validate_fault_recovery" in qualification_report_builder
        and "validate_teacher_install" in qualification_report_builder
        and "--independent-teacher-install" in qualification_report_builder
        and "--fault-recovery" in qualification_report_builder
        and '"independent_teacher_install": teacher_row' in qualification_report_builder
        and "if teacher is None:" in qualification_report_builder
        and 'remaining.append("independent_teacher_install")' in qualification_report_builder
        and 'remaining.append("fault_recovery")' in qualification_report_builder,
        "qualification reports must verify fault evidence and keep every unproved gate pending",
        failures,
    )
    require(
        'NAMESPACE="noi-v1-independent-teacher-install"' in teacher_install_verifier
        and "candidate_verified" in teacher_install_verifier
        and "root_only_staging" in teacher_install_verifier
        and "rollback_verified" in teacher_install_verifier
        and 'checks["cloud_state"]!="STOPPED"' in teacher_install_verifier
        and "ordinary_oj_before_sha256" in teacher_install_verifier
        and "validate_machine_artifacts(loaded)" in teacher_install_builder
        and 'receipt["completed"] != list(transaction.CLEAN_PHASES)' in teacher_install_builder
        and 'execution != expected_execution' in teacher_install_builder
        and "validate_teacher_install_evidence" in builder
        and "validate_teacher_install_evidence" in source("scripts/verify_v1_candidate.py"),
        "independent teacher install must be externally signed and reverified by report and candidate paths",
        failures,
    )
    require(
        'CONFIRMATION = "INDEPENDENT-TEACHER-CLEAN-INSTALL-AND-ROLLBACK"' in teacher_install_collector
        and 'value["kind"] != "phase_failure"' in teacher_install_collector
        and 'value["phase"] != "post_install_verification"' in teacher_install_collector
        and 'receipt["completed"] != list(transaction.CLEAN_PHASES)' in teacher_install_collector
        and 'matrix_host == matrix["host"]["anonymous_id"]' in teacher_install_collector
        and 'evidence_builder.verify_candidate' in teacher_install_collector
        and 'artifact_sources["ordinary_oj_before"][1] != artifact_sources["ordinary_oj_after"][1]' in teacher_install_collector
        and "collect_v1_independent_teacher_install_observation.py" in qualification_guide
        and "不能复用矩阵机的私有计划" in qualification_guide
        and "phase_failure --phase post_install_verification" in qualification_guide,
        "independent teacher observation must be machine-collected on another host after full install rollback",
        failures,
    )
    require(
        '"production_install_apply_available": checked["production_qualified"] and delivery_complete' in readiness_reporter
        and '"production_install_plan_available": checked["production_qualified"]' in readiness_reporter
        and '"service_apply_coordinator", "passed"' in readiness_reporter
        and '"linux_clean_install_rehearsal_matrix"' in readiness_reporter
        and 'checked["evidence"]["independent_teacher_install"]["status"]' in readiness_reporter
        and '("capacity_15_plus_2", "15+2 一小时容量")' in readiness_reporter,
        "launch readiness must distinguish delivered coordination from the pending power-loss rehearsal",
        failures,
    )
    require(
        '"install --plan"' in noictl
        and "_verify_install_candidate" in noictl
        and "candidate archive digest differs" in noictl
        and "INSTALL_PLAN_READ_ONLY" in noictl
        and "--require-production-qualified" in noictl
        and "production qualification verifier identity differs" in noictl
        and "--expected-manifest-sha256" in noictl
        and "candidate directory" in noictl
        and "root-only plan file" in noictl
        and 'def _install_apply(' in noictl
        and '"--private-plan"' in noictl
        and '"--expected-plan-sha256"' in noictl
        and "stderr 已隐藏" in noictl
        and "private_staging" in private_upgrade_plan_builder
        and "public install plan identity differs" in private_upgrade_plan_builder
        and "controller image differs from qualification report" in private_upgrade_plan_builder
        and "live controller changed after backup" in private_upgrade_plan_builder
        and '"operation": "upgrade"' in private_upgrade_plan_builder,
        "installation planning must reverify qualified candidates and remain read-only",
        failures,
    )
    require(
        "def _private_install_operation" in noictl
        and '{"upgrade", "clean-install"}' in noictl
        and '"apply_v1_clean_install.py" if operation == "clean-install"' in noictl
        and "INSTALL_CLEAN_TRANSACTION" in noictl,
        "noictl apply must hash-pin and dispatch the private plan operation without reading site config",
        failures,
    )
    require(
        "noi-v1-fault-recovery-actions" in fault_evidence_verifier
        and "external trust root" in fault_evidence_verifier
        and "duplicate_oj_records" in fault_evidence_verifier
        and "final_source_mismatches" in fault_evidence_verifier
        and "ordinary_oj_restarts" in fault_evidence_verifier
        and "capacity evidence does not prove the three shared fault scenarios" in fault_evidence_verifier
        and "control_restart" in qualification_guide
        and "collection_retry" in qualification_guide
        and "power_loss_recovery" in qualification_guide
        and "完整故障组合证据必须" in qualification_guide,
        "fault qualification must require three signed actions and shared capacity evidence",
        failures,
    )
    require(
        "verify_v1_capacity_evidence.py" in qualification_guide
        and "capacity-evidence.json" in qualification_guide
        and "V1_CAPACITY_REHEARSAL.md" in qualification_guide
        and "--capacity-evidence" in builder
        and "--capacity-artifact-root" in builder
        and "validate_capacity_evidence" in builder
        and "unexpected_seat_restarts" in qualification_validator,
        "capacity qualification must use bound 15+2 machine-verifiable evidence",
        failures,
    )
    require(
        "trusted_probe" in capacity_collector
        and "manual_input" in capacity_collector
        and "capacity qualification samples require trusted probes" in capacity_collector
        and "threshold-sha256" in capacity_collector
        and "361" in capacity_guide
        and "30 分钟" in capacity_guide,
        "capacity collection must be append-only, probe-bound, and time-bounded",
        failures,
    )
    require(
        "os.link(temporary, path" in capacity_collector
        and "timestamp is not bound to this invocation" in capacity_collector
        and 'connection.request("GET"' in capacity_probe
        and "telemetry was replayed or moved backwards" in capacity_collector
        and "ssh-keygen" in capacity_probe
        and "v1-capacity-probe-config.schema.json" in capacity_guide
        and "build_v1_capacity_probe.py" in capacity_guide
        and "os.link(temporary, requested" in capacity_probe_builder,
        "capacity measurement must use frozen config, signed fresh telemetry, and read-only sampling",
        failures,
    )
    require(
        "playwright" in capacity_browser_package
        and '"playwright": "1.62.0"' in capacity_browser_lock
        and "page.on('websocket'" in capacity_browser_agent
        and "page.keyboard.press" in capacity_browser_agent
        and "seatSetSha256" in capacity_browser_library
        and "requireTrustedExecutable" in capacity_browser_library
        and "dedicated non-root Linux user" in capacity_browser_agent
        and "qualification_marker" in capacity_browser_schema
        and "seat_set_sha256" in capacity_browser_schema
        and "direct_http" in capacity_browser_schema
        and "formal_seat_count: 15" in capacity_browser_agent
        and "seat_urls" in capacity_browser_schema
        and "telemetry_transport_profile" in capacity_probe
        and "StrictHostKeyChecking=yes" in capacity_browser_publisher
        and "TELEMETRY_INSTALLED sequence=" in capacity_browser_publisher
        and "telemetry sequence did not advance" in capacity_telemetry_installer
        and "os.replace(temporary, output)" in capacity_telemetry_installer
        and "os.umask(previous_umask)" in capacity_telemetry_installer
        and "telemetry_seat_set_sha256" in capacity_guide,
        "capacity browser telemetry must be independently measured, qualification-bound, and signed",
        failures,
    )
    require(
        "noi-v1-capacity-ordinary-oj" in capacity_ordinary_agent
        and "ProxyHandler({})" in capacity_ordinary_agent
        and "pm2_baseline" in capacity_ordinary_schema
        and "credential_canary" in capacity_ordinary_schema
        and "result_canary" in capacity_ordinary_schema
        and "ordinary OJ capacity agent build requires Linux root" in capacity_ordinary_builder
        and "ordinary OJ telemetry sequence did not advance" in capacity_ordinary_installer
        and "os.replace(temporary, output)" in capacity_ordinary_installer
        and "os.umask(previous_umask)" in capacity_ordinary_installer
        and "os.umask(previous_umask)" in capacity_ordinary_agent
        and "StrictHostKeyChecking=yes" in capacity_ordinary_publisher
        and "ClearAllForwardings=yes" in capacity_ordinary_publisher
        and "verify_ordinary_oj" in capacity_probe
        and "ordinary OJ telemetry was replayed or moved backwards" in capacity_collector
        and "ordinary_oj" in capacity_guide,
        "capacity ordinary OJ continuity must be independently signed and bound to every sample",
        failures,
    )
    require(
        "controller_container_id" in capacity_shutdown_schema
        and "active_seats" in capacity_shutdown_probe
        and "instance_state" in capacity_shutdown_probe
        and "queue_counts" in capacity_shutdown_probe
        and "shutdown probe build requires Linux root" in capacity_shutdown_builder
        and "build_v1_capacity_shutdown_probe.py" in capacity_guide,
        "capacity shutdown fact must be machine-derived from controller, cloud, ingress, seats, and queues",
        failures,
    )
    require(
        "seat_bindings" in capacity_workload_schema
        and "action_public_key" in capacity_workload_schema
        and "capacity_session_dir" in capacity_workload_schema
        and "workload action occurred outside the capacity sample window" in capacity_workload_probe
        and "verify_action_envelope" in capacity_workload_probe
        and "mode=ro" in capacity_workload_probe
        and "PRAGMA query_only=ON" in capacity_workload_probe
        and "archive_manifest" in capacity_workload_probe
        and "web_submit" in capacity_workload_probe
        and "workload probe build requires Linux root" in capacity_workload_builder
        and "v1-capacity-workload-probe-config.schema.json" in capacity_guide,
        "capacity workload fact must cross-bind signed actions, SQLite deliveries, and collection evidence",
        failures,
    )
    require(
        "browser_envelope" in capacity_workload_agent_schema
        and '"docker_socket": {"const": "/var/run/docker.sock"}' in capacity_workload_agent_schema
        and "container_id" in capacity_workload_agent_schema
        and "operation_receipt_sha256" in capacity_workload_agent
        and "workload frozen action agent" in capacity_workload_agent
        and "browser telemetry does not prove current 15-seat login" in capacity_workload_agent
        and '"/usr/bin/g++"' in capacity_workload_agent
        and "compile_peak_concurrency" in capacity_workload_agent
        and "ThreadPoolExecutor(max_workers=15" in capacity_workload_agent
        and '"workload source compile"' in capacity_workload_agent
        and "workload action agent build requires Linux root" in capacity_workload_agent_builder
        and "action_receipt" in capacity_workload_schema
        and "action_agent_sha256" in capacity_workload_schema
        and "workload action receipt identity differs" in capacity_workload_probe
        and "v1-capacity-workload-action-agent-config.schema.json" in capacity_guide,
        "capacity workload actions must be machine-executed, lifecycle-bound, and receipt-signed",
        failures,
    )
    require(
        "PROBE_KINDS" in capacity_collector
        and "probe SHA256 differs from the frozen session" in capacity_collector
        and '"probes"' in capacity_guide,
        "capacity session must freeze all six probe digests before sampling",
        failures,
    )
    require(
        "action_agents" in capacity_guard_schema
        and "probe_paths" in capacity_guard_schema
        and "action output set is partial" in capacity_guard
        and "both runtime actions must complete inside the sample window" in capacity_guard
        and "terminal facts exist before runtime closeout" in capacity_guard
        and '"phase": "independent_verification"' in capacity_guard
        and "v1-capacity-rehearsal-guard-config.schema.json" in capacity_guide,
        "capacity rehearsal phases must be read-only, digest-bound, and fail-closed",
        failures,
    )

    if failures:
        for item in failures:
            print(f"V1_CONTRACT_FAIL {item}", file=sys.stderr)
        return 1
    print(
        "V1_CONTRACT_OK pages=5 source=web-per-problem-folder-fallback "
        "oj-records=append-only ambiguous=no-replay retention=30/180"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
