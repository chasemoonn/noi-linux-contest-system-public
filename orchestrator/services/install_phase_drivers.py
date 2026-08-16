"""Concrete, externally pinned phase drivers for the V1 install transaction."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys

from .install_transaction import InstallTransactionError, TransactionContext, canonical


HEX64 = re.compile(r"^[a-f0-9]{64}$")
SOURCE_POINTER = re.compile(r"^source-releases/[a-f0-9]{40}-[a-f0-9]{12}$")


def _trusted_script(path: Path) -> Path:
    requested = Path(os.path.abspath(path))
    resolved = requested.resolve(strict=True)
    metadata = os.lstat(requested)
    if requested != resolved or stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) \
            or metadata.st_nlink != 1:
        raise InstallTransactionError("phase driver script metadata is unsafe")
    if platform.system().lower() == "linux" and (
            metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022):
        raise InstallTransactionError("phase driver script must be root-owned and not group/world writable")
    return resolved


def _trusted_executable(path: Path) -> Path:
    requested = Path(os.path.abspath(path)); resolved = requested.resolve(strict=True)
    metadata = os.lstat(resolved)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or not os.access(resolved, os.X_OK):
        raise InstallTransactionError("phase driver executable metadata is unsafe")
    if platform.system().lower() == "linux":
        if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
            raise InstallTransactionError("phase driver executable must be root-owned and not group/world writable")
        current = requested.parent
        while True:
            parent_metadata = os.lstat(current)
            if parent_metadata.st_uid != 0 or (not stat.S_ISLNK(parent_metadata.st_mode) and (
                    not stat.S_ISDIR(parent_metadata.st_mode)
                    or stat.S_IMODE(parent_metadata.st_mode) & 0o022)):
                raise InstallTransactionError("phase driver executable ancestor is unsafe")
            if current.parent == current: break
            current = current.parent
    # Execute the trusted entry point exactly as configured.  In particular, a
    # virtual-environment Python is normally a symlink; replacing it with the
    # resolved system interpreter silently drops the venv site-packages.
    return requested


def _run_json(command: list[str], timeout_seconds: int,
              extra_environment: dict[str, str] | None = None) -> dict:
    environment = {
        "HOME": "/root",
        "USER": "root",
        "LOGNAME": "root",
        "SHELL": "/bin/bash",
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    if extra_environment:
        if any(not isinstance(key, str) or not isinstance(value, str) or "\x00" in key + value
               for key, value in extra_environment.items()):
            raise InstallTransactionError("phase driver environment differs")
        environment.update(extra_environment)
    try:
        completed = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=environment, timeout=timeout_seconds,
                                   check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstallTransactionError("source release phase command did not complete") from exc
    if completed.returncode != 0:
        raise InstallTransactionError("source release phase command failed")
    if not 0 < len(completed.stdout) <= 1024 * 1024:
        raise InstallTransactionError("source release phase output size differs")
    try:
        value = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallTransactionError("source release phase output is invalid") from exc
    if not isinstance(value, dict):
        raise InstallTransactionError("source release phase output is not an object")
    return value


def _phase_receipt(action: str, evidence: dict, phase: str = "source_release") -> dict:
    return {
        "phase": phase,
        "action": action,
        "status": "verified",
        "evidence_sha256": hashlib.sha256(canonical(evidence)).hexdigest(),
    }


@dataclass(frozen=True)
class SourceReleaseDriver:
    candidate: Path
    expected_manifest_sha256: str
    install_root: Path
    source_plan_id: str
    transaction_script: Path
    expected_source_release: str | None = None
    qualification_lab: bool = False
    timeout_seconds: int = 300
    python_executable: Path = Path(sys.executable)

    def __post_init__(self) -> None:
        if not HEX64.fullmatch(self.expected_manifest_sha256) or not HEX64.fullmatch(self.source_plan_id):
            raise InstallTransactionError("source release driver identity is invalid")
        if self.expected_source_release is not None and not re.fullmatch(
                r"[a-f0-9]{40}-[a-f0-9]{12}", self.expected_source_release):
            raise InstallTransactionError("expected source release identity differs")
        if not isinstance(self.timeout_seconds, int) or not 1 <= self.timeout_seconds <= 1800:
            raise InstallTransactionError("source release driver timeout differs")
        object.__setattr__(self, "transaction_script", _trusted_script(self.transaction_script))
        python = Path(os.path.abspath(self.python_executable)).resolve(strict=True)
        if not python.is_file():
            raise InstallTransactionError("source release Python executable differs")
        object.__setattr__(self, "python_executable", python)

    def _base_command(self) -> list[str]:
        return [str(self.python_executable), str(self.transaction_script)]

    def apply(self, context: TransactionContext) -> dict:
        command = self._base_command() + [
            "--apply", "--candidate", str(self.candidate),
            "--expected-manifest-sha256", self.expected_manifest_sha256,
            "--install-root", str(self.install_root), "--plan-id", self.source_plan_id,
            "--owner-plan-id", context.plan_id,
        ]
        if self.qualification_lab:
            command.append("--qualification-lab")
        value = _run_json(command, self.timeout_seconds)
        required = {"status", "changed", "plan_id", "release", "service_mutations"}
        if set(value) != required or value["status"] != "committed" or value["changed"] is not True \
                or value["plan_id"] != self.source_plan_id or value["service_mutations"] != 0 \
                or not SOURCE_POINTER.fullmatch(str(value["release"])) \
                or (self.expected_source_release is not None and
                    value["release"] != f"source-releases/{self.expected_source_release}"):
            raise InstallTransactionError("source release apply evidence differs")
        evidence = {"service_plan_id": context.plan_id, "source_plan_id": self.source_plan_id,
                    "result": value}
        return _phase_receipt("apply", evidence)

    def rollback(self, context: TransactionContext, receipt: dict | None) -> dict:
        command = self._base_command() + [
            "--rollback-owned", "--install-root", str(self.install_root),
            "--plan-id", self.source_plan_id,
        ]
        value = _run_json(command, self.timeout_seconds)
        required = {"status", "changed", "plan_id", "release", "service_mutations"}
        if set(value) != required or value["status"] != "rollback_verified" \
                or not isinstance(value["changed"], bool) or value["plan_id"] != self.source_plan_id \
                or value["service_mutations"] != 0 or (
                    value["release"] is not None and not SOURCE_POINTER.fullmatch(str(value["release"]))) \
                or (self.expected_source_release is not None and value["release"] not in
                    {None, f"source-releases/{self.expected_source_release}"}):
            raise InstallTransactionError("source release rollback evidence differs")
        evidence = {"service_plan_id": context.plan_id, "source_plan_id": self.source_plan_id,
                    "prior_phase_receipt": receipt, "result": value}
        return _phase_receipt("rollback", evidence)


@dataclass(frozen=True)
class CleanMaterialsDriver:
    install_root: Path
    source_release_name: str
    backup_directory: Path
    desired_config: Path
    desired_env: Path
    desired_plugin_env: Path
    desired_plugin_token: Path
    timeout_seconds: int = 120
    python_executable: Path = Path(sys.executable)

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-f0-9]{40}-[a-f0-9]{12}", self.source_release_name) \
                or not isinstance(self.timeout_seconds, int) or not 1 <= self.timeout_seconds <= 300:
            raise InstallTransactionError("clean materials driver identity differs")

    def _base(self, context: TransactionContext) -> list[str]:
        release=(Path(os.path.abspath(self.install_root))/"source-releases"/self.source_release_name).resolve(strict=True)
        script=_trusted_script(release/"scripts/prepare_v1_clean_install_materials.py")
        if script.parent!=(release/"scripts").resolve(strict=True):
            raise InstallTransactionError("clean materials script escaped the frozen source release")
        python=_trusted_executable(self.python_executable)
        return [str(python),str(script),"--backup-directory",str(self.backup_directory),
                "--transaction-directory",str(context.transaction_directory),"--plan-id",context.plan_id,
                "--backup-manifest-sha256",context.backup_manifest_sha256,"--install-root",str(self.install_root),
                "--desired-config",str(self.desired_config),"--desired-env",str(self.desired_env),
                "--desired-plugin-env",str(self.desired_plugin_env),
                "--desired-plugin-token",str(self.desired_plugin_token)]

    def _run(self, context: TransactionContext, action: str) -> dict:
        value=_run_json(self._base(context)+[f"--{action}"],self.timeout_seconds)
        expected_status="verified" if action=="apply" else "rollback_verified"
        if set(value)!={"status","plan_id","changed","service_mutations"} \
                or value["status"]!=expected_status or value["plan_id"]!=context.plan_id \
                or not isinstance(value["changed"],bool) or value["service_mutations"]!=0:
            raise InstallTransactionError("clean materials phase evidence differs")
        return value

    def apply(self,context:TransactionContext)->dict:
        return _phase_receipt("apply",{"service_plan_id":context.plan_id,"result":self._run(context,"apply")},"clean_materials")

    def rollback(self,context:TransactionContext,receipt:dict|None)->dict:
        return _phase_receipt("rollback",{"service_plan_id":context.plan_id,"prior_phase_receipt":receipt,
                                           "result":self._run(context,"rollback")},"clean_materials")


@dataclass(frozen=True)
class HydroIntegrationDriver:
    install_root: Path
    source_release_name: str
    backup_directory: Path
    installer_timeout_seconds: int = 300
    rollback_timeout_seconds: int = 300
    pm2_bin: Path = Path("/root/.nix-profile/bin/pm2")
    node_bin: Path = Path("/root/.nix-profile/bin/node")
    python_executable: Path = Path(sys.executable)
    bash_executable: Path = Path("/bin/bash")

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-f0-9]{40}-[a-f0-9]{12}", self.source_release_name):
            raise InstallTransactionError("Hydro source release identity differs")
        for value in (self.installer_timeout_seconds, self.rollback_timeout_seconds):
            if not isinstance(value, int) or not 1 <= value <= 1800:
                raise InstallTransactionError("Hydro phase timeout differs")

    def _release_root(self) -> Path:
        return Path(os.path.abspath(self.install_root)) / "source-releases" / self.source_release_name

    def _script(self, relative: str) -> Path:
        release = self._release_root().resolve(strict=True)
        script = _trusted_script(release / relative)
        if script.parent != (release / Path(relative).parent).resolve(strict=True):
            raise InstallTransactionError("Hydro phase script escaped the frozen source release")
        return script

    def _executables(self) -> tuple[Path, Path, Path, Path, Path, Path]:
        installer = self._script("deploy/install-hydro-orchestrator-addon.sh")
        restore = self._script("scripts/restore_v1_hydro_install_backup.py")
        python = _trusted_executable(self.python_executable)
        bash = _trusted_executable(self.bash_executable)
        pm2 = _trusted_executable(self.pm2_bin)
        node = _trusted_executable(self.node_bin)
        return installer, restore, python, bash, pm2, node

    def apply(self, context: TransactionContext) -> dict:
        installer, _, _, bash, pm2, node = self._executables()
        release = self._release_root().resolve(strict=True)
        extra = {
            "SOURCE_DIR": str(release / "hydro-plugin-orchestrator"),
            "EXPECTED_SOURCE_RELEASE": self.source_release_name,
            "EXTERNAL_INSTALL_TRANSACTION": "1",
            "PM2_BIN": str(pm2),
            "NODE_BIN": str(node),
        }
        value = _run_json([str(bash), str(installer)], self.installer_timeout_seconds,
                          extra_environment=extra)
        expected = {"status": "verified", "transaction": "external",
                    "source_release": self.source_release_name, "hydro": "online",
                    "routes": "submit-notify-problem-fileio-materials",
                    "other_pm2_mutations": 0}
        if value != expected:
            raise InstallTransactionError("Hydro integration apply evidence differs")
        evidence = {"service_plan_id": context.plan_id, "result": value}
        return _phase_receipt("apply", evidence, "hydro_integration")

    def rollback(self, context: TransactionContext, receipt: dict | None) -> dict:
        _, restore, python, _, pm2, _ = self._executables()
        value = _run_json([
            str(python), str(restore), "--backup-directory", str(self.backup_directory),
            "--transaction-directory", str(context.transaction_directory),
            "--plan-id", context.plan_id,
            "--backup-manifest-sha256", context.backup_manifest_sha256,
            "--pm2-bin", str(pm2),
        ], self.rollback_timeout_seconds)
        required = {"status", "plan_id", "backup_manifest_sha256", "hydro",
                    "other_pm2_mutations", "changed"}
        if set(value) != required or value["status"] != "rollback_verified" \
                or value["plan_id"] != context.plan_id \
                or value["backup_manifest_sha256"] != context.backup_manifest_sha256 \
                or value["hydro"] != "online" or value["other_pm2_mutations"] != 0 \
                or not isinstance(value["changed"], bool):
            raise InstallTransactionError("Hydro integration rollback evidence differs")
        evidence = {"service_plan_id": context.plan_id, "prior_phase_receipt": receipt,
                    "result": value}
        return _phase_receipt("rollback", evidence, "hydro_integration")


@dataclass(frozen=True)
class ClosedFrontendDriver:
    install_root: Path
    source_release_name: str
    backup_directory: Path
    caddyfile: Path
    snippet: Path
    hydro_domain: str
    frontend_domain: str
    orchestrator_upstream: str = "http://127.0.0.1:8600"
    timeout_seconds: int = 300
    python_executable: Path = Path(sys.executable)

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-f0-9]{40}-[a-f0-9]{12}", self.source_release_name):
            raise InstallTransactionError("closed frontend source release identity differs")
        if not isinstance(self.timeout_seconds, int) or not 1 <= self.timeout_seconds <= 600:
            raise InstallTransactionError("closed frontend timeout differs")

    def _script(self) -> tuple[Path, Path]:
        release = (Path(os.path.abspath(self.install_root)) / "source-releases" /
                   self.source_release_name).resolve(strict=True)
        script = _trusted_script(release / "scripts/apply_v1_closed_frontend.py")
        if script.parent != (release / "scripts").resolve(strict=True):
            raise InstallTransactionError("closed frontend script escaped the frozen source release")
        return script, _trusted_executable(self.python_executable)

    def _base(self, context: TransactionContext) -> list[str]:
        script, python = self._script()
        return [str(python), str(script), "--backup-directory", str(self.backup_directory),
                "--transaction-directory", str(context.transaction_directory),
                "--plan-id", context.plan_id, "--backup-manifest-sha256",
                context.backup_manifest_sha256, "--caddyfile", str(self.caddyfile),
                "--snippet", str(self.snippet)]

    def apply(self, context: TransactionContext) -> dict:
        value = _run_json(self._base(context) + [
            "--apply", "--hydro-domain", self.hydro_domain,
            "--frontend-domain", self.frontend_domain,
            "--orchestrator-upstream", self.orchestrator_upstream,
        ], self.timeout_seconds)
        required = {"status", "plan_id", "closed", "hydro_route_hardened",
                    "etag_used", "active_sha256", "other_service_mutations"}
        if set(value) != required or value["status"] != "verified" \
                or value["plan_id"] != context.plan_id or value["closed"] is not True \
                or value["hydro_route_hardened"] is not True or value["etag_used"] is not True \
                or not HEX64.fullmatch(str(value["active_sha256"])) \
                or value["other_service_mutations"] != 0:
            raise InstallTransactionError("closed frontend apply evidence differs")
        return _phase_receipt("apply", {"service_plan_id": context.plan_id,
                                        "result": value}, "closed_frontend")

    def rollback(self, context: TransactionContext, receipt: dict | None) -> dict:
        value = _run_json(self._base(context) + ["--rollback"], self.timeout_seconds)
        required = {"status", "plan_id", "backup_manifest_sha256", "changed",
                    "other_service_mutations"}
        if set(value) != required or value["status"] != "rollback_verified" \
                or value["plan_id"] != context.plan_id \
                or value["backup_manifest_sha256"] != context.backup_manifest_sha256 \
                or not isinstance(value["changed"], bool) or value["other_service_mutations"] != 0:
            raise InstallTransactionError("closed frontend rollback evidence differs")
        return _phase_receipt("rollback", {"service_plan_id": context.plan_id,
                                           "prior_phase_receipt": receipt,
                                           "result": value}, "closed_frontend")


@dataclass(frozen=True)
class ControllerQuiesceDriver:
    install_root: Path
    source_release_name: str
    backup_directory: Path
    project_config: Path
    project_env: Path
    database: Path
    caddyfile: Path
    snippet: Path
    pm2_bin: Path
    oj_origin: str
    docker_socket: Path = Path("/var/run/docker.sock")
    timeout_seconds: int = 120
    python_executable: Path = Path(sys.executable)

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-f0-9]{40}-[a-f0-9]{12}", self.source_release_name):
            raise InstallTransactionError("controller quiesce source release identity differs")
        if not isinstance(self.timeout_seconds, int) or not 1 <= self.timeout_seconds <= 300:
            raise InstallTransactionError("controller quiesce timeout differs")

    def _base(self, context: TransactionContext) -> list[str]:
        release=(Path(os.path.abspath(self.install_root))/"source-releases"/self.source_release_name).resolve(strict=True)
        script=_trusted_script(release/"scripts/quiesce_v1_controller.py")
        if script.parent!=(release/"scripts").resolve(strict=True):
            raise InstallTransactionError("controller quiesce script escaped the frozen source release")
        python=_trusted_executable(self.python_executable)
        return ([str(python),str(script),"--backup-directory",str(self.backup_directory),
                "--plan-id",context.plan_id,"--backup-manifest-sha256",context.backup_manifest_sha256,
                "--docker-socket",str(self.docker_socket),"--project-config",str(self.project_config),
                "--project-env",str(self.project_env),"--database",str(self.database)]
                +["--caddyfile",str(self.caddyfile),"--snippet",str(self.snippet),"--pm2-bin",str(self.pm2_bin),
                  "--oj-origin",self.oj_origin])

    @staticmethod
    def _validate(value:dict,context:TransactionContext,status:str)->dict:
        required={"status","plan_id","backup_manifest_sha256","controller_id","quiesced","changed","other_container_mutations"}
        if set(value)!=required or value["status"]!=status or value["plan_id"]!=context.plan_id \
                or value["backup_manifest_sha256"]!=context.backup_manifest_sha256 \
                or not HEX64.fullmatch(str(value["controller_id"])) or value["quiesced"] is not True \
                or not isinstance(value["changed"],bool) or value["other_container_mutations"]!=0:
            raise InstallTransactionError("controller quiesce evidence differs")
        return value

    def apply(self,context:TransactionContext)->dict:
        value=self._validate(_run_json(self._base(context)+["--apply"],self.timeout_seconds),context,"verified")
        return _phase_receipt("apply",{"service_plan_id":context.plan_id,"result":value},"controller_quiesce")

    def rollback(self,context:TransactionContext,receipt:dict|None)->dict:
        # Rollback deliberately keeps the sealed controller stopped.  The
        # terminal verifier restarts it only after source, Hydro, Caddy, DB and
        # cloud have all been proven restored.
        value=self._validate(_run_json(self._base(context)+["--rollback"],self.timeout_seconds),context,"rollback_verified")
        return _phase_receipt("rollback",{"service_plan_id":context.plan_id,"prior_phase_receipt":receipt,
                                          "result":value},"controller_quiesce")


@dataclass(frozen=True)
class ControllerDriver:
    install_root: Path
    source_release_name: str
    backup_directory: Path
    desired_definition: Path
    desired_config: Path
    desired_env: Path
    project_config: Path
    project_env: Path
    database: Path
    docker_socket: Path = Path("/var/run/docker.sock")
    timeout_seconds: int = 240
    python_executable: Path = Path(sys.executable)

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-f0-9]{40}-[a-f0-9]{12}", self.source_release_name):
            raise InstallTransactionError("controller source release identity differs")
        if not isinstance(self.timeout_seconds, int) or not 1 <= self.timeout_seconds <= 600:
            raise InstallTransactionError("controller phase timeout differs")

    def _script(self) -> tuple[Path, Path]:
        release = (Path(os.path.abspath(self.install_root)) / "source-releases" /
                   self.source_release_name).resolve(strict=True)
        script = _trusted_script(release / "scripts/apply_v1_controller.py")
        if script.parent != (release / "scripts").resolve(strict=True):
            raise InstallTransactionError("controller script escaped the frozen source release")
        return script, _trusted_executable(self.python_executable)

    def _base(self, context: TransactionContext) -> list[str]:
        script, python = self._script()
        return [str(python), str(script), "--backup-directory", str(self.backup_directory),
                "--plan-id", context.plan_id, "--backup-manifest-sha256",
                context.backup_manifest_sha256, "--source-release", self.source_release_name,
                "--docker-socket", str(self.docker_socket), "--project-config",
                str(self.project_config), "--project-env", str(self.project_env),
                "--database", str(self.database)]

    def apply(self, context: TransactionContext) -> dict:
        value = _run_json(self._base(context) + ["--apply", "--desired-definition",
            str(self.desired_definition), "--desired-config", str(self.desired_config),
            "--desired-env", str(self.desired_env)], self.timeout_seconds)
        required = {"status", "plan_id", "container_id", "image_id", "healthy",
                    "old_controller_retained", "other_container_mutations"}
        if set(value) != required or value["status"] != "verified" \
                or value["plan_id"] != context.plan_id \
                or not HEX64.fullmatch(str(value["container_id"])) \
                or not re.fullmatch(r"sha256:[a-f0-9]{64}", str(value["image_id"])) \
                or value["healthy"] is not True or not isinstance(value["old_controller_retained"], bool) \
                or value["other_container_mutations"] != 0:
            raise InstallTransactionError("controller apply evidence differs")
        return _phase_receipt("apply", {"service_plan_id": context.plan_id,
                                        "result": value}, "controller")

    def rollback(self, context: TransactionContext, receipt: dict | None) -> dict:
        value = _run_json(self._base(context) + ["--rollback"], self.timeout_seconds)
        required = {"status", "plan_id", "backup_manifest_sha256", "controller_present",
                    "baseline_running", "controller_quiesced", "changed", "other_container_mutations"}
        if set(value) != required or value["status"] != "rollback_verified" \
                or value["plan_id"] != context.plan_id \
                or value["backup_manifest_sha256"] != context.backup_manifest_sha256 \
                or not isinstance(value["controller_present"], bool) \
                or not isinstance(value["baseline_running"], bool) \
                or value["controller_quiesced"] is not True \
                or not isinstance(value["changed"], bool) or value["other_container_mutations"] != 0:
            raise InstallTransactionError("controller rollback evidence differs")
        return _phase_receipt("rollback", {"service_plan_id": context.plan_id,
                                           "prior_phase_receipt": receipt,
                                           "result": value}, "controller")

    def commit_cleanup(self, context: TransactionContext, receipt: dict) -> dict:
        value = _run_json(self._base(context) + ["--commit-cleanup"], self.timeout_seconds)
        expected = {"status": "cleanup_verified", "plan_id": context.plan_id,
                    "old_controller_removed": True, "other_container_mutations": 0}
        if value != expected:
            raise InstallTransactionError("controller commit cleanup evidence differs")
        return {"phase": "controller", "action": "commit_cleanup", "status": "verified"}


@dataclass(frozen=True)
class PostInstallVerificationDriver:
    install_root: Path
    source_release_name: str
    backup_directory: Path
    expected_contract: Path
    timeout_seconds: int = 180
    python_executable: Path = Path(sys.executable)

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-f0-9]{40}-[a-f0-9]{12}", self.source_release_name):
            raise InstallTransactionError("post-install source release identity differs")
        if not isinstance(self.timeout_seconds, int) or not 1 <= self.timeout_seconds <= 600:
            raise InstallTransactionError("post-install timeout differs")

    def _command(self, context: TransactionContext) -> list[str]:
        release = (Path(os.path.abspath(self.install_root)) / "source-releases" /
                   self.source_release_name).resolve(strict=True)
        script = _trusted_script(release / "scripts/verify_v1_post_install.py")
        if script.parent != (release / "scripts").resolve(strict=True):
            raise InstallTransactionError("post-install verifier escaped the frozen source release")
        python = _trusted_executable(self.python_executable)
        return [str(python), str(script), "--verify", "--backup-directory",
                str(self.backup_directory), "--transaction-directory",
                str(context.transaction_directory), "--plan-id", context.plan_id,
                "--backup-manifest-sha256", context.backup_manifest_sha256,
                "--source-release", self.source_release_name,
                "--expected-contract", str(self.expected_contract)]

    def apply(self, context: TransactionContext) -> dict:
        value = _run_json(self._command(context), self.timeout_seconds)
        required = {"status", "plan_id", "controller_id", "controller_image_id", "closed",
                    "cloud_closed", "queues_quiet", "ordinary_oj_unchanged", "other_mutations"}
        if set(value) != required or value["status"] != "verified" \
                or value["plan_id"] != context.plan_id \
                or not HEX64.fullmatch(str(value["controller_id"])) \
                or not re.fullmatch(r"sha256:[a-f0-9]{64}", str(value["controller_image_id"])) \
                or any(value[key] is not True for key in
                       ("closed", "cloud_closed", "queues_quiet", "ordinary_oj_unchanged")) \
                or value["other_mutations"] != 0:
            raise InstallTransactionError("post-install verification evidence differs")
        return _phase_receipt("apply", {"service_plan_id": context.plan_id,
                                        "result": value}, "post_install_verification")

    def rollback(self, context: TransactionContext, receipt: dict | None) -> dict:
        # This phase is strictly read-only, including an uncertain response.
        return _phase_receipt("rollback", {"service_plan_id": context.plan_id,
                                            "prior_phase_receipt": receipt,
                                            "mutations": 0}, "post_install_verification")


@dataclass(frozen=True)
class FinalRollbackVerifier:
    install_root: Path
    source_release_name: str
    backup_directory: Path
    expected_contract: Path
    timeout_seconds: int = 240
    python_executable: Path = Path(sys.executable)

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-f0-9]{40}-[a-f0-9]{12}", self.source_release_name):
            raise InstallTransactionError("rollback verifier source release identity differs")
        if not isinstance(self.timeout_seconds, int) or not 1 <= self.timeout_seconds <= 600:
            raise InstallTransactionError("rollback verifier timeout differs")

    def __call__(self, context: TransactionContext) -> dict:
        release=(Path(os.path.abspath(self.install_root))/"source-releases"/self.source_release_name).resolve(strict=True)
        script=_trusted_script(release/"scripts/verify_v1_live_install_rollback.py")
        if script.parent!=(release/"scripts").resolve(strict=True):
            raise InstallTransactionError("live rollback verifier escaped the frozen source release")
        python=_trusted_executable(self.python_executable)
        value=_run_json([str(python),str(script),"--verify","--backup-directory",str(self.backup_directory),
            "--plan-id",context.plan_id,"--backup-manifest-sha256",context.backup_manifest_sha256,
            "--source-release",self.source_release_name,"--expected-contract",str(self.expected_contract)],self.timeout_seconds)
        expected={"status":"rollback_verified","plan_id":context.plan_id,
                  "backup_manifest_sha256":context.backup_manifest_sha256}
        if value!=expected:raise InstallTransactionError("final live rollback verification differs")
        return value


@dataclass(frozen=True)
class CleanFinalRollbackVerifier:
    install_root: Path
    source_release_name: str
    backup_directory: Path
    expected_contract: Path
    desired_controller_definition: Path
    timeout_seconds: int = 240
    python_executable: Path = Path(sys.executable)

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-f0-9]{40}-[a-f0-9]{12}",self.source_release_name):
            raise InstallTransactionError("clean rollback verifier source release identity differs")
        if not isinstance(self.timeout_seconds,int) or not 1<=self.timeout_seconds<=600:
            raise InstallTransactionError("clean rollback verifier timeout differs")

    def __call__(self,context:TransactionContext)->dict:
        release=(Path(os.path.abspath(self.install_root))/"source-releases"/self.source_release_name).resolve(strict=True)
        script=_trusted_script(release/"scripts/verify_v1_clean_install_rollback.py")
        if script.parent!=(release/"scripts").resolve(strict=True):
            raise InstallTransactionError("clean rollback verifier escaped the frozen source release")
        python=_trusted_executable(self.python_executable)
        value=_run_json([str(python),str(script),"--verify","--backup-directory",str(self.backup_directory),
            "--plan-id",context.plan_id,"--backup-manifest-sha256",context.backup_manifest_sha256,
            "--source-release",self.source_release_name,"--expected-contract",str(self.expected_contract),
            "--desired-controller-definition",str(self.desired_controller_definition)],self.timeout_seconds)
        expected={"status":"rollback_verified","plan_id":context.plan_id,
                  "backup_manifest_sha256":context.backup_manifest_sha256}
        if value!=expected:raise InstallTransactionError("final clean rollback verification differs")
        return value
