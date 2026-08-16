import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


def script(name: str) -> str:
    return (ROOT / "deploy" / name).read_text(encoding="utf-8")


class ReleaseScriptSafetyTests(unittest.TestCase):
    def test_iso_digest_label_is_canonical_lowercase_across_release_scripts(self):
        expected = "c8824240736352e5e4aaf3f6532b40961f75fa9f23d670bb78881355a49d5878"
        for name in (
            "build-noi-official-image.sh",
            "verify-contest-image-local.sh",
            "deploy-contest-image-from-oj.sh",
            "attest-cached-noi-rootfs-once.sh",
        ):
            source = script(name)
            self.assertIn(expected, source)
            self.assertNotIn(expected.upper(), source)

    def test_image_deploy_packages_the_invoked_release_source(self):
        source = script("deploy-contest-image-from-oj.sh")
        self.assertIn('${BASH_SOURCE[0]}', source)
        self.assertIn('source_root="$(cd -- "${script_dir}/.." && pwd -P)"', source)
        self.assertIn('tar czf - -C "${source_root}"', source)
        self.assertNotIn('tar czf - -C "${app}"', source)
        permission_gate = source.index('find "${source_root}" -xdev -perm /022')
        readability_gate = source.index('type f ! -perm -0444')
        archive = source.index('tar czf - -C "${source_root}"')
        self.assertLess(permission_gate, archive)
        self.assertLess(readability_gate, archive)
        self.assertIn('key="${app}/secrets/contest.pem"', source)
        self.assertIn('exec 9>"${app}/orchestrator/runtime/deploy-image.lock"', source)

    def test_desktop_image_contract_is_built_and_verified_by_immutable_id(self):
        dockerfile = (ROOT / "noi-linux-official" / "Dockerfile").read_text(
            encoding="utf-8"
        )
        build = script("build-noi-official-image.sh")
        verify = script("verify-contest-image-local.sh")
        self.assertIn(
            'LABEL org.noi.desktop.contract="finalizer-status-v1"', dockerfile
        )
        for source in (build, verify):
            self.assertIn("org.noi.desktop.contract", source)
            self.assertIn("finalizer-status-v1", source)
            self.assertIn('docker image inspect "${image_id}"', source)

    def test_desktop_image_binds_explicit_source_revision_end_to_end(self):
        dockerfile = (ROOT / "noi-linux-official" / "Dockerfile").read_text(
            encoding="utf-8"
        )
        build = script("build-noi-official-image.sh")
        deploy = script("deploy-contest-image-from-oj.sh")
        verify = script("verify-contest-image-local.sh")

        self.assertIn("ARG NOI_SOURCE_REVISION", dockerfile)
        self.assertIn("ARG NOI_ISO_SHA256", dockerfile)
        self.assertIn(
            '[[ "${NOI_SOURCE_REVISION}" =~ ^[0-9a-f]{40}$ ]]', dockerfile
        )
        self.assertIn(
            '[[ "${NOI_ISO_SHA256}" =~ ^[0-9a-f]{64}$ ]]', dockerfile
        )
        self.assertIn(
            'org.opencontainers.image.revision="${NOI_SOURCE_REVISION}"',
            dockerfile,
        )
        self.assertIn('org.noi.iso.sha256="${NOI_ISO_SHA256}"', dockerfile)

        self.assertIn('SOURCE_REVISION="${3:-}"', build)
        self.assertIn('"${SOURCE_REVISION}" =~ ^[0-9a-f]{40}$', build)
        self.assertIn('--build-arg "NOI_SOURCE_REVISION=${SOURCE_REVISION}"', build)
        self.assertIn('--build-arg "NOI_ISO_SHA256=${EXPECTED_SHA256}"', build)
        self.assertIn(
            '{{index .Config.Labels "org.opencontainers.image.revision"}}', build
        )
        self.assertIn(
            '"${image_source_revision}" != "${SOURCE_REVISION}"', build
        )

        self.assertIn('${NOI_SOURCE_REVISION:?', deploy)
        self.assertIn('"${source_revision}" =~ ^[0-9a-f]{40}$', deploy)
        self.assertIn("'${source_revision}'\" <<'REMOTE'", deploy)
        self.assertIn(
            '--build-arg "NOI_SOURCE_REVISION=${source_revision}"', deploy
        )
        self.assertIn(
            '"${iso_path}" "${candidate}" "${source_revision}"', deploy
        )
        self.assertIn(
            '"${candidate_source_revision}" != "${source_revision}"', deploy
        )
        self.assertIn(
            '"${candidate_image_id}" "${source_revision}"', deploy
        )
        self.assertNotIn("git rev-parse HEAD", deploy)

        self.assertIn('expected_source_revision="${2:-}"', verify)
        self.assertIn(
            '"${image_source_revision}" =~ ^[0-9a-f]{40}$', verify
        )
        self.assertIn(
            '"${image_source_revision}" != "${expected_source_revision}"', verify
        )
        self.assertIn('docker image inspect "${image_id}"', verify)

    def test_desktop_finalizer_publishes_transitional_and_failed_states(self):
        finalizer = (
            ROOT
            / "noi-linux-official"
            / "rootfs"
            / "usr"
            / "local"
            / "bin"
            / "finalize-contest-session.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("printf 'starting\\n'", finalizer)
        self.assertIn("trap finalizer_exit EXIT", finalizer)
        self.assertIn("printf 'failed:%s\\n'", finalizer)
        self.assertIn("printf 'ready\\n'", finalizer)

    @staticmethod
    def _bash() -> str | None:
        candidates = []
        if os.name == "nt":
            for root in filter(None, (os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA"))):
                candidates.append(Path(root) / "Git" / "bin" / "bash.exe")
        discovered = shutil.which("bash")
        if discovered:
            candidates.append(Path(discovered))
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
        return None

    @staticmethod
    def _shell_path(path: Path) -> str:
        return path.resolve().as_posix()

    @staticmethod
    def _write_baseline_source(
        root: Path, marker: str, *, build: bool = True, verify: bool = True
    ) -> None:
        image_root = root / "noi-linux-official"
        deploy_root = root / "deploy"
        image_root.mkdir(parents=True)
        deploy_root.mkdir(parents=True)
        (image_root / "origin.txt").write_text(marker, encoding="utf-8")
        if build:
            (deploy_root / "build-noi-official-image.sh").write_text(
                f"# {marker} build\n", encoding="utf-8"
            )
        if verify:
            (deploy_root / "verify-contest-image-local.sh").write_text(
                f"# {marker} verify\n", encoding="utf-8"
            )

    def _run_baseline_seed(self, app: Path, stage: Path) -> subprocess.CompletedProcess:
        bash = self._bash()
        if not bash:
            self.skipTest("bash is required for the release-script behavior test")
        source = script("deploy-contest-image-from-oj.sh")
        start = source.index("complete_baseline_source()")
        end = source.index(
            'if [[ ! "${stage}"', source.index("seed_existing_baseline()")
        )
        functions = source[start:end]
        harness = f"""
set -euo pipefail
PATH="/usr/bin:/bin:${{PATH:-}}"
app="$1"
stage="$2"
release_id=release-test
current_link="${{app}}/current-image-source"
{functions}
install() {{
  [[ "$1" == "-d" && "$2" == "-m" && "$3" == "0755" ]]
  mkdir -p -- "$4"
}}
assert_current_pair() {{
  local expected_source="$1"
  local expected_image="$2"
  test -e "${{current_link}}"
  grep -Fqx -- "SOURCE_TARGET=${{expected_source}}" \
    "${{app}}/${{expected_source}}/promotion.env"
  grep -Fqx -- "PROMOTED_IMAGE_ID=${{expected_image}}" \
    "${{app}}/${{expected_source}}/promotion.env"
}}
seed_existing_baseline "sha256:{'a' * 64}"
"""
        return subprocess.run(
            [
                bash,
                "-c",
                harness,
                "baseline-seed-test",
                self._shell_path(app),
                self._shell_path(stage),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_addon_installer_exports_nix_path_before_pm2_restart(self):
        addon = script("install-hydro-orchestrator-addon.sh")
        path_export = (
            'export PATH="/root/.nix-profile/bin:'
            '/nix/var/nix/profiles/default/bin:'
        )
        self.assertIn(path_export, addon)
        self.assertLess(addon.index(path_export), addon.index('"${pm2}" restart'))

    def test_addon_installer_uses_only_a_frozen_source_release_tree(self):
        addon = script("install-hydro-orchestrator-addon.sh")
        self.assertIn('source_dir="${SOURCE_DIR:-/opt/noi-linux-contest-system/current-source/hydro-plugin-orchestrator}"', addon)
        self.assertIn('EXPECTED_SOURCE_RELEASE:?set EXPECTED_SOURCE_RELEASE', addon)
        self.assertIn('resolved_source_dir="$(realpath -e -- "${source_dir}")"', addon)
        self.assertIn('SOURCE_DIR does not resolve to EXPECTED_SOURCE_RELEASE', addon)
        self.assertIn("source-releases/[a-f0-9]{40}-[a-f0-9]{12}", addon)
        self.assertIn('relative not in expected', addon)
        self.assertIn('observed != expected', addon)
        self.assertLess(addon.index('SOURCE_DIR does not resolve'), addon.index('observed != expected'))
        self.assertLess(addon.index('observed != expected'), addon.index('"${node}" --check'))

    def test_addon_installer_has_one_exact_external_transaction_mode(self):
        addon = script("install-hydro-orchestrator-addon.sh")
        self.assertIn('external_transaction="${EXTERNAL_INSTALL_TRANSACTION:-0}"', addon)
        self.assertIn('EXTERNAL_INSTALL_TRANSACTION must be 0 or 1', addon)
        self.assertIn('if [[ "${external_transaction}" = 0 ]]; then\n  trap \'rollback\' ERR', addon)
        self.assertIn('"transaction":"external"', addon)
        self.assertIn('compgen -A variable ORCHESTRATOR_', addon)
        self.assertLess(addon.index('compgen -A variable ORCHESTRATOR_'), addon.index('source "${plugin_env}"'))
        self.assertIn('nested_prefix!=desired', addon)
        self.assertIn('not set(top_prefix).issubset(desired)', addon)
        self.assertIn('"ORCHESTRATOR_TOKEN" in desired', addon)

    def test_caddy_children_support_candidate_only_mode(self):
        harden = script("harden-hydro-submit-route.sh")
        configure = script("configure-hydro-caddy.sh")
        for content, marker in (
            (harden, "hydro_submit_route_candidate_ready"),
            (configure, "caddy_exam_candidate_ready"),
        ):
            self.assertIn("NO_CADDY_LOAD", content)
            self.assertIn('if [[ "${no_caddy_load}" = 1 ]]', content)
            self.assertIn(marker, content)
            self.assertLess(content.index(marker), content.index("curl -fsS -X POST", content.index(marker)))

    def test_addon_rollback_supports_legacy_token_and_waits_for_hydro(self):
        addon = script("install-hydro-orchestrator-addon.sh")
        rollback = addon[addon.index("rollback() {") : addon.index("trap 'rollback' ERR")]
        self.assertIn(
            'legacy_token="$(tr -d \'\\r\\n\' < "${token_file}")"', rollback
        )
        self.assertIn('ORCHESTRATOR_TOKEN="${legacy_token}"', rollback)
        self.assertIn('"${pm2}" restart hydrooj --update-env', rollback)
        self.assertIn("wait_for_hydro 120", rollback)
        self.assertIn("unset ORCHESTRATOR_TOKEN", addon)
        self.assertNotIn('echo "${legacy_token}"', rollback)

    def test_addon_post_install_probes_accept_only_explicit_client_errors(self):
        addon = script("install-hydro-orchestrator-addon.sh")
        probes = addon[
            addon.index(
                "for endpoint in '' '/notify' '/problem-fileio' '/materials'; do"
            ) :
            addon.index("trap - ERR")
        ]
        self.assertIn('case "${status}" in', probes)
        self.assertIn("400|409|413|422)", probes)
        self.assertIn("*)", probes)
        default_branch = probes[probes.index("*)") : probes.index("esac")]
        self.assertIn("exit 1", default_branch)
        self.assertNotIn('"${status}" == "404"', probes)
        self.assertNotIn('"${status}" == "000"', probes)

    def test_installers_mount_deploy_for_container_tests(self):
        expectations = {
            "install-hydro-host.sh": "\n".join(
                (
                    '"${compose[@]}" run --rm --no-deps \\',
                    '    --volume "${app}/deploy:/deploy:ro" \\',
                    '    orchestrator \\',
                    "    python -m unittest discover -s tests -v",
                )
            ),
        }
        for name, expected in expectations.items():
            with self.subTest(installer=name):
                self.assertIn(expected, script(name))

    def test_installers_publish_active_caddy_snippet_transactionally(self):
        helper = script("publish-caddy-exam-snippet.sh")
        load = helper[
            helper.index("load_full_config() {") : helper.index(
                "restore_previous_snippet() {"
            )
        ]
        self.assertLess(
            load.index('"${caddy}" validate --config "${caddyfile}"'),
            load.index('curl -fsS -X POST'),
        )
        self.assertIn('http://127.0.0.1:2019/load', helper)
        self.assertIn('mv -Tf -- "${temporary}" "${snippet}"', helper)

        rollback = helper[
            helper.index("restore_previous_snippet() {") : helper.index(
                'if ! grep -Fqx -- "${import_line}"'
            )
        ]
        self.assertIn('install -m 0644 "${backup}" "${temporary}"', rollback)
        self.assertIn("load_full_config", rollback)
        failure = helper[helper.index("if ! load_full_config; then") :]
        self.assertIn("restore_previous_snippet", failure)

        installer = script("install-hydro-host.sh")
        self.assertIn("caddy_candidate=$(mktemp", installer)
        self.assertIn("publish-caddy-exam-snippet.sh", installer)
        self.assertIn("caddy-exam.conf.before-install", installer)
        self.assertLess(
            installer.index("publish-caddy-exam-snippet.sh"),
            installer.index('"${compose[@]}" up -d'),
        )

    def test_health_check_detects_caddy_live_disk_drift(self):
        health = script("health-check.sh")
        self.assertIn('"${caddy_bin}" validate --config "${caddyfile}"', health)
        self.assertNotIn('"${caddy_bin}" adapt --config "${caddyfile}"', health)
        self.assertIn("adapted_json_fingerprint", health)
        self.assertIn('envelope.get(\"result\")', health)
        self.assertIn("-H 'Content-Type: text/caddyfile'", health)
        self.assertIn('--data-binary "@${caddyfile}"', health)
        self.assertIn('"${CADDY_ADMIN_URL%/}/adapt"', health)
        self.assertIn('"${CADDY_ADMIN_URL%/}/config/"', health)
        self.assertIn("disk_caddy_fingerprint", health)
        self.assertIn("live_caddy_fingerprint", health)
        self.assertIn("Caddy live/disk drift detected", health)
        self.assertIn("Caddy is serving stale live configuration", health)

    def test_plugin_journals_are_resolved_from_runtime_configuration(self):
        variables = (
            "ORCHESTRATOR_IDEMPOTENCY_FILE",
            "ORCHESTRATOR_NOTIFICATION_IDEMPOTENCY_FILE",
            "ORCHESTRATOR_PROBLEM_DRAFT_IDEMPOTENCY_FILE",
            "ORCHESTRATOR_MATERIAL_IDEMPOTENCY_FILE",
        )
        health = script("health-check.sh")
        self.assertIn("orchestrator-plugin.env", health)
        self.assertIn("unset ORCHESTRATOR_IDEMPOTENCY_FILE", health)
        self.assertNotIn("/root/.hydro/orchestrator-idempotency.json", health)
        self.assertIn('if [[ ! -w "${path}" ]]', health)
        for variable in variables:
            self.assertIn(variable, health)

        addon = script("install-hydro-orchestrator-addon.sh")
        self.assertIn("unset ORCHESTRATOR_TOKEN_FILE", addon)
        self.assertIn('if [[ ! -w "${state_file}" ]]', addon)
        for variable in variables:
            self.assertIn(f'"${{{variable}}}"', addon)

        compose = script("hydro-compose-snippet.yml")
        host_installer = script("install-hydro-host.sh")
        for variable in variables:
            self.assertIn(f"{variable}:", compose)
            self.assertIn(f"'{variable}':", host_installer)

    def test_operational_scripts_require_explicit_site_and_mutation_inputs(self):
        health = script("health-check.sh")
        for variable in ("EXAM_URL", "HYDRO_URL", "EXPECTED_OJ_CIDR"):
            self.assertIn(f'${{{variable}:?', health)
        self.assertNotIn("DEPLOYMENT_PROFILE", health)
        self.assertIn("CONFIRM_PUBLIC_DESKTOP_CIDR", health)

        harden = script("harden-hydro-submit-route.sh")
        for variable in (
            "CADDYFILE",
            "HYDRO_DOMAIN",
            "CONFIRM_HARDEN_SUBMIT_ROUTE",
        ):
            self.assertIn(f'${{{variable}:?', harden)

        exercise = script("exercise-dual-from-oj.sh")
        for variable in (
            "EXAM_URL",
            "TEST_SEAT_CONTAINER",
            "WEB_PROBLEM_SLUG",
            "FOLDER_PROBLEM_SLUG",
            "ORCHESTRATOR_ADMIN_FILE",
            "SSH_PRIVATE_KEY",
            "SSH_KNOWN_HOSTS",
            "CONFIRM_DESTRUCTIVE_SMOKE_TEST",
        ):
            self.assertIn(f'${{{variable}:?', exercise)

    def test_promotion_is_guarded_by_persistent_marker_and_signal_traps(self):
        source = script("deploy-contest-image-from-oj.sh")
        marker_commit = source.index(
            'mv -Tf -- "${transaction_temp}" "${pending_transaction}"'
        )
        promotion = source.index(
            'docker tag "${candidate_image_id}" noi-linux-official:2.0'
        )
        final_check = source.index(
            'assert_current_pair "${new_source_target}" "${candidate_image_id}"'
        )
        marker_remove = source.index('rm -f -- "${pending_transaction}"', final_check)
        self.assertLess(marker_commit, promotion)
        self.assertLess(promotion, final_check)
        self.assertLess(final_check, marker_remove)
        for signal in ("HUP", "INT", "TERM"):
            self.assertIn(f"trap 'rollback_promotion ", source)
            self.assertIn(signal, source)

    def test_interrupted_promotion_has_an_explicit_fail_closed_recovery(self):
        source = script("recover-image-promotion-local.sh")
        self.assertIn("--expected-marker-sha256", source)
        self.assertIn("NOI_IMAGE_RECOVERY_LOCK_HELD", source)
        self.assertIn("NOI_IMAGE_RECOVERY_DEPLOYMENT_LOCK_HELD", source)
        self.assertIn("O_NOFOLLOW", source)
        self.assertIn("label=noi.contest", source)
        self.assertIn("Docker seat inventory cannot be read", source)
        self.assertIn("docker info", source)
        self.assertIn("pending marker SHA256 differs", source)
        self.assertIn("assert_recorded_pair", source)
        self.assertIn("assert_current_old_state", source)
        self.assertIn("STATUS=rolled_back_to_old_pair", source)
        receipt = source.index('mv -Tf -- "${receipt_temp}" "${receipt}"')
        receipt_sync = source.index('sync -f "${app}"', receipt)
        marker_remove = source.index('rm -f -- "${pending}"', receipt_sync)
        self.assertLess(receipt, receipt_sync)
        self.assertLess(receipt_sync, marker_remove)
        self.assertNotIn("docker start", source)
        self.assertNotIn("docker run", source)
        self.assertNotIn("docker rm -f", source)

    def test_imported_promotion_has_a_default_off_durable_crash_boundary(self):
        source = script("promote-imported-contest-image-local.sh")
        marker = source.index('mv -Tf -- "${transaction_temp}" "${pending}"')
        ready = source.index('"phase": "marker_durable_before_mutation"')
        stopped = source.index('kill -STOP "$$"')
        mutation = source.index('docker tag "${candidate_id}" noi-linux-official:2.0')
        self.assertLess(marker, ready)
        self.assertLess(ready, stopped)
        self.assertLess(stopped, mutation)
        self.assertIn("NOI_V1_QUALIFICATION_MARKER", source)
        self.assertIn("NOI_V1_POWER_LOSS_READY_PATH", source)
        self.assertIn("qualification power-loss process resumed unexpectedly", source)

    def test_existing_image_baseline_is_snapshot_not_new_gate(self):
        source = script("deploy-contest-image-from-oj.sh")
        baseline = source[
            source.index("seed_existing_baseline()") : source.index(
                'if [[ ! "${stage}"', source.index("seed_existing_baseline()")
            )
        ]
        self.assertIn("BASELINE_UNVERIFIED=1", baseline)
        self.assertIn('baseline_source="existing-app"', baseline)
        self.assertIn('complete_baseline_source "${stage}"', baseline)
        self.assertIn('baseline_source="current-stage-fallback"', baseline)
        self.assertIn("BASELINE_SOURCE=%s", baseline)
        self.assertNotIn("bash \"${baseline_stage}/deploy/verify", baseline)

    def test_first_managed_baseline_prefers_existing_source_then_falls_back(self):
        temp_root = ROOT / "tmp"
        temp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="baseline-source-test-", dir=temp_root
        ) as temporary:
            cases = (
                ("existing-complete", True, "existing-app", "old-source"),
                (
                    "existing-incomplete",
                    False,
                    "current-stage-fallback",
                    "current-stage",
                ),
            )
            for name, existing_build, expected_source, expected_marker in cases:
                with self.subTest(case=name):
                    case_root = Path(temporary) / name
                    app = case_root / "app"
                    stage = case_root / "stage"
                    self._write_baseline_source(
                        app, "old-source", build=existing_build
                    )
                    self._write_baseline_source(stage, "current-stage")
                    (app / "image-releases").mkdir()

                    result = self._run_baseline_seed(app, stage)

                    self.assertEqual(
                        result.returncode, 0, result.stdout + result.stderr
                    )
                    release = app / "image-releases" / "baseline-release-test"
                    self.assertEqual(
                        (release / "noi-linux-official" / "origin.txt").read_text(
                            encoding="utf-8"
                        ),
                        expected_marker,
                    )
                    metadata = (release / "promotion.env").read_text(
                        encoding="utf-8"
                    )
                    self.assertIn("BASELINE_UNVERIFIED=1\n", metadata)
                    self.assertIn(f"BASELINE_SOURCE={expected_source}\n", metadata)

    def test_first_managed_baseline_fails_if_stage_is_also_incomplete(self):
        temp_root = ROOT / "tmp"
        temp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="baseline-incomplete-test-", dir=temp_root
        ) as temporary:
            app = Path(temporary) / "app"
            stage = Path(temporary) / "stage"
            self._write_baseline_source(app, "old-source", build=False)
            self._write_baseline_source(stage, "current-stage", verify=False)
            (app / "image-releases").mkdir()

            result = self._run_baseline_seed(app, stage)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("both lack a complete required source set", result.stderr)
            self.assertFalse((app / "current-image-source").exists())
            self.assertEqual(list((app / "image-releases").iterdir()), [])

    def test_verifier_fails_closed_on_pair_or_pending_mismatch(self):
        source = script("verify-contest-image-from-oj.sh")
        self.assertIn("image-promotion.pending", source)
        self.assertIn('"${formal_image_id}" != "${promoted_image_id}"', source)
        self.assertIn('promoted_source_revision="$(read_value SOURCE_REVISION)"', source)
        self.assertIn(
            '! "${promoted_source_revision}" =~ ^[0-9a-f]{40}$', source
        )
        self.assertIn(
            '"${promoted_image_id}" "${promoted_source_revision}"', source
        )

    def test_oj_rollback_wrapper_locks_before_ssh(self):
        source = script("rollback-contest-image-from-oj.sh")
        self.assertLess(source.index("flock -n 9"), source.index('ssh "${ssh_opts[@]}"'))
        local = script("rollback-contest-image-local.sh")
        self.assertIn("image-promotion.pending", local)
        self.assertIn("rollback image and source snapshot are not a recorded pair", local)


if __name__ == "__main__":
    unittest.main()
