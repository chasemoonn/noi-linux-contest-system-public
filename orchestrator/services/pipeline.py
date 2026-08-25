"""Prepare and collect the remote NOI Linux contest environment."""
from __future__ import annotations

import base64
import hashlib
import http.client
import ipaddress
import json
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import secrets
import shlex
import tarfile
import tempfile
import threading
import time
from urllib.parse import urlparse

try:
    import fcntl
except ImportError:  # pragma: no cover - production runs on Linux
    fcntl = None

from .frontend import make_frontend
from .hydro_submit import HydroSubmitter
from .materials import approved_material_paths, sha256_file
from .remote import Remote
from .seat_pool import SeatPoolState, TeacherApprovalRequiredError, desired_capacity
from .static_check import check_answer_tree, check_code, force_zero_code

_TID = re.compile(r"^[0-9a-fA-F]{24}$")
DESKTOP_IMAGE_CONTRACT_LABEL = "org.noi.desktop.contract"
DESKTOP_IMAGE_CONTRACT = "finalizer-status-v1"
SEAT_READINESS_TIMEOUT_SECONDS = 300


def _write_durable_json(path: Path, payload: dict) -> str:
    """Atomically persist one evidence document and return its byte digest."""
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(
        payload, ensure_ascii=False, indent=2, sort_keys=True
    ).encode("utf-8")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    return hashlib.sha256(encoded).hexdigest()


def _archive_tree_manifest(root: Path, *, excluded: set[str]) -> dict:
    """Hash the complete extracted evidence tree and reject special files."""
    files: dict[str, dict[str, object]] = {}
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        if relative in excluded:
            continue
        if path.is_symlink():
            raise RuntimeError(f"回收目录含符号链接: {relative}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise RuntimeError(f"回收目录含非普通文件: {relative}")
        files[relative] = {
            "sha256": sha256_file(path),
            "size": path.stat().st_size,
        }
    return {"schema_version": 1, "files": files}

NGINX_LOCATION = """    location = /s/{token} {{ return 302 {novnc_path}; }}
    location /s/{token}/ {{
        proxy_pass http://{cip}:6080/;
        proxy_http_version 1.1;
        proxy_buffering off;
        tcp_nodelay on;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }}
"""

NGINX_CONF = """# noi-contest: {tid}
server {{
    listen {listen} default_server;
    server_name _;
    server_tokens off;
{locations}
    location / {{ return 404; }}
}}
"""

SUBMIT_PROXY_CONF = """
server {{
    listen {gateway}:{port};
    server_name _;
    server_tokens off;
    location /submit/ {{
        proxy_pass {origin};
        proxy_set_header Host {origin_host};
        proxy_ssl_server_name on;
        proxy_ssl_name {origin_host};
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-NOI-Submit-Transport private-http;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 60s;
        client_max_body_size 256k;
    }}
    location / {{ return 404; }}
}}
"""


def _q(value) -> str:
    return shlex.quote(str(value))


def _seat_readiness_command(
    *,
    name: str,
    cip: str,
    paper_sha256: str,
    candidate: str,
    problems: list[str],
    testdata_files: int | None,
) -> str:
    """Build a bounded seat probe with only two docker execs per sample."""
    bundle_dir = "/run/contest-materials"
    root_checks = [
        # Bracket the first character so pgrep's regex cannot match the
        # readiness shell command that contains this probe text itself.
        "! pgrep -f '[g]nome-initial-setup' >/dev/null 2>&1",
        "pgrep -x systemd-logind >/dev/null 2>&1",
        "pgrep -f '/usr/libexec/gnome-session-binary' >/dev/null 2>&1",
        "pgrep -x gnome-shell >/dev/null 2>&1",
        "grep -Fqx ready /home/student/.contest-finalizer-status",
        "test -L /home/student/比赛资料（从这里开始）",
        "test -L /home/student/Desktop/比赛资料（从这里开始）",
        f"grep -Fqx schema=4 {bundle_dir}/.manifest",
        f"test -r {bundle_dir}/01_比赛题面.pdf",
        f"test -d {bundle_dir}/02_辅助自测数据",
        "test \"$(sha256sum /home/student/试题/paper.pdf | "
        f"awk '{{print $1}}')\" = {_q(paper_sha256)}",
        f"test -r {bundle_dir}/04_CSP程序回收系统.html",
        f"test -r {bundle_dir}/05_使用说明.txt",
        "test -L /home/student/Desktop/01_比赛题面.pdf",
        "test -L /home/student/Desktop/02_辅助自测数据",
        "test -L /home/student/Desktop/03_开始答题.desktop",
        "test -L /home/student/Desktop/03_答案文件夹",
        "test -L /home/student/Desktop/04_CSP程序回收系统.html",
        "test -L /home/student/Desktop/05_使用说明.txt",
    ]
    student_checks = [
        "test ! -w /home/student/试题/paper.pdf",
        f"test -w {bundle_dir}/03_答案文件夹",
    ]
    for problem in problems:
        problem_dir = f"/home/student/答案/{candidate}/{problem}"
        source_file = f"{problem_dir}/{problem}.cpp"
        student_checks.extend(
            (
                "test -w " + _q(problem_dir),
                "test -f " + _q(source_file),
                "test ! -L " + _q(source_file),
                "test -w " + _q(source_file),
            )
        )
    if testdata_files is not None:
        root_checks.append(
            "test \"$(find /home/student/测试数据 -type f | wc -l)\" "
            f"-eq {int(testdata_files)}"
        )
        student_checks.append("test ! -w /home/student/测试数据")
    sample = (
        f"docker exec {_q(name)} sh -lc {_q(' && '.join(root_checks))} && "
        f"docker exec -u student {_q(name)} sh -lc "
        f"{_q(' && '.join(student_checks))} && "
        f"curl -fsS --max-time 3 http://{_q(cip)}:6080/vnc.html "
        ">/dev/null 2>&1"
    )
    return (
        "ok=0; for _ in $(seq 1 45); do "
        f"if {sample}; then ok=$((ok + 1)); else ok=0; fi; "
        "if [ \"$ok\" -ge 3 ]; then exit 0; fi; sleep 2; done; "
        f"docker logs --tail 120 {_q(name)} >&2; exit 1"
    )


def _remote_readonly(remote: Remote, command: str, *, attempts: int = 3) -> str:
    """Retry an explicitly read-only remote query after an ambiguous SSH timeout.

    Mutating commands must continue to use ``Remote.run`` directly: replaying a
    create/delete request after a lost response would be unsafe.  Paramiko may
    surface a channel timeout as remote status ``-1``; only that ambiguous
    transport result (or a native timeout exception) is eligible here.
    """
    if attempts < 1:
        raise ValueError("attempts must be positive")
    for attempt in range(1, attempts + 1):
        try:
            return remote.run(command)
        except (TimeoutError, OSError) as exc:
            retryable = isinstance(exc, TimeoutError)
            if not retryable or attempt == attempts:
                raise
        except RuntimeError as exc:
            if "远程命令失败(-1):" not in str(exc) or attempt == attempts:
                raise
        time.sleep(float(attempt))
    raise RuntimeError("unreachable read-only retry state")


def novnc_path(token: str, quality: int, compression: int) -> str:
    """Return the one canonical student noVNC path used by UI and nginx."""
    return (
        f"/s/{token}/vnc.html?path=s/{token}/websockify"
        f"&autoconnect=true&view_only=false&resize=remote&quality={quality}"
        f"&compression={compression}"
        "&reconnect=true&reconnect_delay=5000"
    )


def gateway_base_url(server: dict, ip: str) -> str:
    """Resolve one root gateway URL and reject a stale configured IP."""
    configured = str(server.get("gateway_public_base_url", "")).rstrip("/")
    if configured:
        parsed = urlparse(configured)
        try:
            configured_ip = ipaddress.ip_address(str(parsed.hostname or ""))
        except ValueError:
            configured_ip = None
        if configured_ip is not None and ip and str(configured_ip) != str(ip):
            raise RuntimeError(
                f"桌面入口 IP {configured_ip} 与比赛 ECS EIP {ip} 不一致"
            )
        return configured
    scheme = str(server.get("gateway_scheme", "http")).lower()
    port = int(server.get("gateway_listen", 80))
    default_port = 80 if scheme == "http" else 443
    suffix = "" if port == default_port else f":{port}"
    return f"{scheme}://{ip}{suffix}"


def probe_novnc_gateway(
    host: str,
    port: int,
    token: str,
    *,
    quality: int,
    compression: int,
    timeout: float = 8.0,
) -> dict:
    """Prove the valid seat page and its WebSocket upgrade through the EIP."""
    page_path = novnc_path(token, quality, compression)
    page = http.client.HTTPConnection(str(host), int(port), timeout=timeout)
    try:
        page.request("GET", page_path, headers={"Host": str(host)})
        response = page.getresponse()
        response.read()
        if response.status != 200:
            raise RuntimeError(
                f"直连 noVNC 页面返回 HTTP {response.status}，期望 200"
            )
    finally:
        page.close()

    key = base64.b64encode(os.urandom(16)).decode("ascii")
    expected_accept = base64.b64encode(
        hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
        ).digest()
    ).decode("ascii")
    websocket = http.client.HTTPConnection(str(host), int(port), timeout=timeout)
    try:
        websocket.request(
            "GET",
            f"/s/{token}/websockify",
            headers={
                "Host": str(host),
                "Connection": "Upgrade",
                "Upgrade": "websocket",
                "Origin": f"http://{host}",
                "Sec-WebSocket-Key": key,
                "Sec-WebSocket-Version": "13",
                "Sec-WebSocket-Protocol": "binary",
            },
        )
        response = websocket.getresponse()
        if response.status != 101:
            response.read()
            raise RuntimeError(
                f"直连 noVNC WebSocket 返回 HTTP {response.status}，期望 101"
            )
        if response.getheader("Sec-WebSocket-Accept", "") != expected_accept:
            raise RuntimeError("noVNC WebSocket 握手响应无效")
    finally:
        websocket.close()
    return {"page_status": 200, "websocket_status": 101}


def rand_password(length: int = 8) -> str:
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def candidate_id(uname: str, uid: int) -> str:
    value = str(uname).strip()
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value):
        return value
    return f"U{uid}"


def safe_extract(archive: tarfile.TarFile, destination: Path) -> None:
    root = destination.resolve()
    for member in archive.getmembers():
        if member.issym() or member.islnk() or member.isdev():
            raise ValueError(f"收卷包含不允许的特殊文件: {member.name}")
        target = (root / member.name).resolve()
        if target != root and root not in target.parents:
            raise ValueError(f"收卷包路径越界: {member.name}")
    archive.extractall(root, filter="data")


def _datetime_ms(value) -> int:
    if not isinstance(value, datetime):
        raise ValueError("Hydro contest timestamp is not a datetime")
    current = value
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    return int(current.timestamp() * 1000)


class Pipeline:
    def __init__(self, cfg, cvm, hydro, store, log, realtime_judge=None):
        self.cfg = cfg
        self.cvm = cvm
        self.hydro = hydro
        self.store = store
        self.log = log
        self.realtime_judge = realtime_judge
        self.frontend = make_frontend(cfg.get("frontend_proxy"))
        self._guard = threading.Lock()
        self._active: set[str] = set()
        # Serialize frontend state changes with the cloud-state reconciler.
        # A stopped instance must never leave /s/* pointing at a dead upstream,
        # but repeatedly reloading Caddy on every scheduler poll is unnecessary.
        self._frontend_guard = threading.Lock()
        self._frontend_stopped_reconciled = False
        self._desktop_access_guard = threading.RLock()
        self._service_closing = False

    def begin_shutdown(self) -> None:
        """Latch the process into close-only mode before scheduler teardown."""
        with self._desktop_access_guard:
            self._service_closing = True

    def _enable_frontend(self, host: str, port: int) -> None:
        with self._frontend_guard:
            self.frontend.enable(host, port)
            self._frontend_stopped_reconciled = False

    def _disable_frontend(self, *, force: bool = False) -> bool:
        with self._frontend_guard:
            if self._frontend_stopped_reconciled and not force:
                return False
            self.frontend.disable()
            self._frontend_stopped_reconciled = True
            return True

    def reconcile_frontend(self, *, force: bool = False) -> bool:
        """Keep the HTTPS compatibility route aligned with contest state.

        ``force`` is used once at orchestrator startup so the in-memory Caddy
        configuration is reloaded even when the generated snippet already
        looks disabled.  Periodic calls are latched until the cloud is seen in
        a non-stopped state, avoiding needless Caddy reloads.
        """
        with self._frontend_guard:
            if self._direct_access_enabled():
                desired, _ = self._desired_desktop_access()
                state, ip = self.cvm.status()
                if (
                    desired is not None
                    and str(state).upper() == "RUNNING"
                    and ip
                ):
                    if not force and not self._frontend_stopped_reconciled:
                        return False
                    self.frontend.enable(
                        str(ip), int(self.cfg["contest_server"]["gateway_listen"])
                    )
                    self._frontend_stopped_reconciled = False
                    return True
                if self._frontend_stopped_reconciled and not force:
                    return False
                self.frontend.disable()
                self._frontend_stopped_reconciled = True
                return True
            state, _ = self.cvm.status()
            if str(state).upper() != "STOPPED":
                self._frontend_stopped_reconciled = False
                return False
            if self._frontend_stopped_reconciled and not force:
                return False
            self.frontend.disable()
            self._frontend_stopped_reconciled = True
            return True

    def shutdown_server(self) -> None:
        """Close both direct and fallback routes, then stop the contest VM."""
        errors: list[Exception] = []
        with self._desktop_access_guard:
            try:
                # Persist suppression before touching the rule. Otherwise the
                # 5-second reconciler would immediately re-open a ready contest
                # after an emergency teacher shutdown.
                for contest in self.store.contests():
                    if str(contest.get("state") or "") in {
                        "preparing",
                        "ready",
                        "collecting",
                    }:
                        self.store.set_state(
                            str(contest["tid"]),
                            "error",
                            "教师手动关闭比赛服务器；公网桌面入口已收回",
                        )
            except Exception as exc:
                errors.append(exc)
                self.log.exception("cannot persist manual desktop shutdown state")
            try:
                self._revoke_desktop_access()
            except Exception as exc:
                errors.append(exc)
                self.log.exception("desktop ingress revocation failed during shutdown")
        try:
            self._disable_frontend(force=True)
        except Exception as exc:
            errors.append(exc)
            self.log.exception("fallback desktop route shutdown failed")
        try:
            self._stop_server_best_effort()
        except Exception as exc:
            errors.append(exc)
            self.log.exception("contest VM shutdown failed")
        if errors:
            raise RuntimeError(
                "桌面关闭未完全收敛: "
                + "; ".join(str(error) for error in errors)
            ) from errors[0]

    def boot_server(self) -> bool:
        """Start only after proving no stale direct desktop rule remains."""
        with self._desktop_access_guard:
            if self._service_closing:
                raise RuntimeError("编排服务正在关闭，拒绝启动比赛服务器")
            state, _ = self.cvm.status()
            if str(state).upper() != "STOPPED":
                return False
            # A previous revoke failure followed by a manual boot must never
            # resurrect an old TCP/80 data plane.  A valid ready contest will
            # be re-opened by the reconciler using its current endAt.
            self._revoke_desktop_access()
            self.cvm.start()
            return True

    def _direct_access_enabled(self) -> bool:
        return getattr(self.cvm, "desktop_access_enabled", False) is True

    def _revoke_desktop_access(self) -> dict:
        if not self._direct_access_enabled():
            return {"enabled": False, "closed": True, "healthy": True}
        with self._desktop_access_guard:
            return self.cvm.revoke_desktop_access()

    def fail_closed_desktop_cleanup(self) -> dict:
        """Close direct ingress or make the contest VM unreachable.

        This is the process-lifecycle cleanup path.  An orderly orchestrator
        restart must not leave the temporary TCP/80 rule unattended merely
        because the final cloud read/revoke failed.  Stopping the dedicated
        contest VM is the independent safety barrier; ordinary OJ services run
        elsewhere and are not touched.
        """
        try:
            return self._revoke_desktop_access()
        except Exception as revoke_error:
            self.log.exception(
                "desktop ingress cleanup failed; stopping contest VM fail-closed"
            )
            try:
                self._stop_server_best_effort()
            except Exception as stop_error:
                raise RuntimeError(
                    "桌面公网入口未确认撤销，且比赛服务器停机失败: "
                    f"{stop_error}"
                ) from revoke_error
            raise RuntimeError(
                "桌面公网入口未确认撤销；已执行比赛服务器停机兜底"
            ) from revoke_error

    def _validate_ready_gateway(self) -> str:
        state, ip = self.cvm.status()
        if str(state).upper() != "RUNNING" or not ip:
            raise RuntimeError("比赛 ECS 未运行，拒绝开放桌面公网入口")
        gateway_base_url(self.cfg["contest_server"], str(ip))
        return str(ip)

    def _ensure_desktop_access(self, contest: dict) -> dict:
        if not self._direct_access_enabled():
            return {"enabled": False, "open": False, "healthy": True}
        with self._desktop_access_guard:
            if self._service_closing:
                raise RuntimeError("编排服务正在关闭，拒绝开放桌面公网入口")
            self._validate_ready_gateway()
            return self.cvm.ensure_desktop_access(
                tid=str(contest["tid"]),
                end_at_ms=int(contest.get("end_at_ms") or 0),
            )

    def _desired_desktop_access(self) -> tuple[dict | None, str]:
        now_ms = int(time.time() * 1000)
        ready = [
            contest
            for contest in self.store.contests()
            if str(contest.get("state") or "") == "ready"
        ]
        if len(ready) == 1 and int(ready[0].get("end_at_ms") or 0) > now_ms:
            return ready[0], "one active ready contest"
        if len(ready) == 1:
            return None, "ready contest is at or past deadline"
        if len(ready) > 1:
            return None, "multiple ready contests"
        return None, "no ready contest"

    def reconcile_desktop_access(self) -> dict:
        """Recover SG state after crashes and revoke it at/after the deadline."""
        if not self._direct_access_enabled():
            return {"enabled": False, "healthy": True, "desired_open": False}
        with self._desktop_access_guard:
            if self._service_closing:
                try:
                    status = self.cvm.revoke_desktop_access()
                except Exception:
                    try:
                        self._stop_server_best_effort()
                    except Exception:
                        self.log.exception(
                            "cannot stop contest VM during closing reconciliation"
                        )
                    raise
                status.update(
                    {
                        "desired_open": False,
                        "healthy": bool(status.get("closed")),
                        "reason": "orchestrator service is closing",
                    }
                )
                return status
            try:
                desired, reason = self._desired_desktop_access()
            except Exception as state_error:
                # Losing the persisted ownership/deadline proof can never mean
                # "leave whatever is currently public".
                try:
                    self.fail_closed_desktop_cleanup()
                except Exception as cleanup_error:
                    raise RuntimeError(
                        "无法读取桌面入口期望状态，且失败关闭未完全收敛: "
                        f"{cleanup_error}"
                    ) from state_error
                raise
            if desired is None:
                try:
                    status = self.cvm.revoke_desktop_access()
                except Exception:
                    # At/after the deadline or after state loss, leaving the
                    # contest VM running would keep a reachable data plane if
                    # the cloud rule cannot be proven closed.
                    try:
                        self._stop_server_best_effort()
                    except Exception:
                        self.log.exception(
                            "cannot stop contest VM after desktop rule revoke failure"
                        )
                    raise
                status.update(
                    {
                        "desired_open": False,
                        "healthy": (
                            bool(status.get("closed"))
                            and not status.get("conflict_count")
                            and bool(status.get("management_healthy"))
                            and reason == "no ready contest"
                        ),
                        "reason": reason,
                    }
                )
                return status
            try:
                self._validate_ready_gateway()
                status = self.cvm.ensure_desktop_access(
                    tid=str(desired["tid"]),
                    end_at_ms=int(desired["end_at_ms"]),
                )
            except Exception as open_error:
                # A once-valid rule can become unsafe when the EIP, SG
                # attachment, group membership, or ENI topology drifts.  Do
                # not merely make health red while leaving that rule public.
                try:
                    self.fail_closed_desktop_cleanup()
                except Exception as cleanup_error:
                    raise RuntimeError(
                        "桌面入口开放对账失败，且失败关闭未完全收敛: "
                        f"{cleanup_error}"
                    ) from open_error
                raise
            status.update(
                {
                    "desired_open": True,
                    "tid": str(desired["tid"]),
                    "end_at_ms": int(desired["end_at_ms"]),
                    "reason": reason,
                }
            )
            return status

    def desktop_access_health(self) -> dict:
        """Read-only desired/actual SG lifecycle health for /healthz."""
        if not self._direct_access_enabled():
            return {"enabled": False, "healthy": True, "desired_open": False}
        with self._desktop_access_guard:
            desired, reason = self._desired_desktop_access()
            if desired is None:
                status = self.cvm.desktop_access_status()
                healthy = (
                    bool(status.get("closed"))
                    and not status.get("conflict_count")
                    and bool(status.get("management_healthy"))
                    and reason == "no ready contest"
                )
                status.update(
                    {
                        "desired_open": False,
                        "healthy": healthy,
                        "reason": reason,
                    }
                )
                return status
            self._validate_ready_gateway()
            status = self.cvm.desktop_access_status(
                tid=str(desired["tid"]),
                end_at_ms=int(desired["end_at_ms"]),
            )
            healthy = (
                bool(status.get("open"))
                and not status.get("conflict_count")
                and bool(status.get("management_healthy"))
            )
            status.update(
                {
                    "desired_open": True,
                    "healthy": healthy,
                    "tid": str(desired["tid"]),
                    "end_at_ms": int(desired["end_at_ms"]),
                    "reason": reason,
                }
            )
            return status

    def _acquire_deployment_lock(self):
        default_path = "/app/runtime/deploy-image.lock" if fcntl is not None else ""
        path = str(
            self.cfg.get("orchestrator", {}).get(
                "deployment_lock", default_path
            )
        ).strip()
        if not path:
            return None
        if fcntl is None:
            raise RuntimeError("deployment_lock 仅支持 Linux 编排服务")
        lock_path = Path(path)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise RuntimeError("镜像部署或验收正在进行，暂不能备赛") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"prepare pid={os.getpid()}\n")
        handle.flush()
        return handle

    @staticmethod
    def _release_deployment_lock(handle) -> None:
        if handle is None:
            return
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()

    def _enter(self, tid: str) -> None:
        if not _TID.fullmatch(tid):
            raise ValueError("比赛 tid 必须是 24 位 ObjectId")
        with self._guard:
            if tid in self._active:
                raise RuntimeError(f"比赛 {tid} 已有流程在运行")
            self._active.add(tid)

    def _leave(self, tid: str) -> None:
        with self._guard:
            self._active.discard(tid)

    def is_active(self, tid: str) -> bool:
        with self._guard:
            return str(tid) in self._active

    def _remote(self, ip: str) -> Remote:
        server = self.cfg["contest_server"]
        return Remote(
            ip,
            server["ssh_user"],
            server["ssh_key"],
            server.get("known_hosts"),
            server.get("strict_host_key", True),
            server.get("host_key_sha256"),
        )

    @staticmethod
    def _put_remote_verified_file(
        remote: Remote,
        local_path: str | Path,
        remote_path: str,
        expected_sha256: str,
        *,
        mode: str = "0444",
    ) -> None:
        """Atomically replace one remote immutable artifact after attestation.

        A previous failed prepare may have already made the destination
        read-only.  Uploading directly to that inode makes every retry fail.
        A unique sibling also prevents a reader from observing partial bytes.
        """
        expected = str(expected_sha256).strip().lower()
        if not re.fullmatch(r"[a-f0-9]{64}", expected):
            raise RuntimeError("远程材料预期哈希无效")
        if not re.fullmatch(r"0[0-7]{3}", str(mode)):
            raise RuntimeError("远程材料权限无效")
        temporary = f"{remote_path}.upload-{secrets.token_hex(12)}"
        try:
            remote.put_file(str(local_path), temporary)
            actual = remote.run(
                f"sha256sum {_q(temporary)} | awk '{{print $1}}'"
            ).strip()
            if not secrets.compare_digest(actual, expected):
                raise RuntimeError("远程材料上传后哈希校验失败")
            remote.run(
                f"chmod {_q(str(mode))} {_q(temporary)} && "
                f"mv -f -- {_q(temporary)} {_q(remote_path)}"
            )
        except Exception:
            try:
                remote.run(f"rm -f -- {_q(temporary)}")
            except Exception:
                pass
            raise

    def _validate_contest_snapshot(self, contest: dict) -> None:
        """Fail before cloud startup if Hydro changed after registration."""
        begin_at = int(contest.get("begin_at_ms") or 0)
        end_at = int(contest.get("end_at_ms") or 0)
        if not begin_at or not end_at:
            # Existing registrations created before snapshot support remain
            # recoverable; re-registering upgrades them to strict validation.
            return
        document = self.hydro.get_contest(str(contest["tid"]))
        if not document:
            raise RuntimeError("Hydro 比赛不存在，请重新登记比赛")
        try:
            current_begin = _datetime_ms(document["beginAt"])
            current_end = _datetime_ms(document["endAt"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Hydro 比赛时间配置无效，请重新登记比赛") from exc
        current_rule = str(document.get("rule") or "")
        if (
            current_begin != begin_at
            or current_end != end_at
            or current_rule != str(contest.get("hydro_rule") or "")
        ):
            raise RuntimeError(
                "Hydro 比赛时间或赛制在登记后已修改；请重新登记再备赛"
            )
        if (
            str(contest.get("submission_mode") or "folder") in {"web", "both"}
            and current_rule != "oi"
        ):
            raise RuntimeError("网页实时评测只允许 Hydro OI 赛制")

    @staticmethod
    def _material_digest(contest: dict) -> str:
        manifest = str(contest.get("material_manifest_sha256") or "").strip()
        if manifest:
            return f"sha256:{manifest}"
        payload = "\0".join(
            (
                str(contest.get("active_material_revision") or "legacy-manual"),
                str(contest.get("paper_sha256") or ""),
                str(contest.get("testdata_sha256") or ""),
            )
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _inspect_desktop_image_contract(remote: Remote, image_ref: str) -> str:
        """Resolve and attest the desktop image before any seat is changed."""
        output = remote.run(
            "docker image inspect -f "
            + _q(
                "{{.Id}} {{index .Config.Labels \""
                + DESKTOP_IMAGE_CONTRACT_LABEL
                + "\"}}"
            )
            + " "
            + _q(image_ref)
        ).strip()
        try:
            image_id, contract = output.split()
        except ValueError as exc:
            raise RuntimeError("比赛镜像无法提供桌面就绪契约") from exc
        if contract != DESKTOP_IMAGE_CONTRACT:
            raise RuntimeError(
                "比赛镜像桌面就绪契约不匹配；禁止创建或切换座位"
            )
        if not image_id:
            raise RuntimeError("比赛镜像不存在或无法取得镜像 ID")
        return image_id

    def _save_pool_state(
        self, tid: str, previous_revision: int | None, state: SeatPoolState
    ) -> SeatPoolState:
        self.store.put_seat_pool(tid, previous_revision, state.to_dict())
        return state

    def _reserve_roster(
        self,
        contest: dict,
        pool: SeatPoolState,
        *,
        now_ms: int,
        roster: list[dict] | None = None,
    ) -> SeatPoolState:
        """Bind every new Hydro participant to a pre-verified seat."""
        participants = roster if roster is not None else self.hydro.roster(
            str(contest["tid"])
        )
        for participant in participants:
            uid = int(participant["uid"])
            uname = str(participant["uname"])
            existing = pool.assignment(uid)
            if existing:
                # The pool CAS and legacy-seat projection are intentionally
                # separate transactions.  A crash after the CAS must be
                # repairable on the next roster sync without moving the user
                # or rotating credentials.
                self.store.bind_pool_seat(
                    str(contest["tid"]), uid, uname, int(existing.slot_no)
                )
                continue
            previous = pool.revision
            result = pool.reserve(
                uid,
                uname,
                now_ms=now_ms,
                command_id=f"reserve:{contest['tid']}:{uid}",
                expected_revision=previous,
            )
            pool = self._save_pool_state(str(contest["tid"]), previous, result.state)
            self.store.bind_pool_seat(
                str(contest["tid"]), uid, uname, int(result.value["slot_no"])
            )
        return pool

    def _ensure_automatic_roster_capacity(
        self, tid: str, roster: list[dict]
    ) -> dict:
        """Append only the seats required by the current OJ roster target."""
        contest = self.store.get_contest(tid)
        stored = self.store.seat_pool(tid)
        if not contest or not stored:
            raise RuntimeError("比赛尚未建立座位池")
        if contest["state"] != "ready":
            return {"grown": False, "added": []}
        pool = SeatPoolState.from_dict(stored["state"])
        target_main, target_spares = desired_capacity(len(roster))
        additional_main = max(0, target_main - pool.max_participants)
        additional_spares = max(0, target_spares - pool.spare_count)
        if additional_main + additional_spares == 0:
            return {"grown": False, "added": []}
        result = self.grow_pool(
            tid,
            additional_main=additional_main,
            additional_spares=additional_spares,
            expected_revision=pool.revision,
        )
        return {"grown": True, **result}

    @staticmethod
    def _validate_roster(roster: list[dict]) -> list[dict]:
        """Reject malformed or duplicate OJ identities before allocating seats."""
        normalized: list[dict] = []
        seen_uids: set[int] = set()
        seen_names: set[str] = set()
        for participant in roster:
            uid = int(participant["uid"])
            uname = str(participant["uname"]).strip()
            if uid < 1 or not uname:
                raise RuntimeError("OJ 报名名单包含无效账号")
            folded = uname.casefold()
            if uid in seen_uids or folded in seen_names:
                raise RuntimeError("OJ 报名名单包含重复账号")
            seen_uids.add(uid)
            seen_names.add(folded)
            normalized.append({"uid": uid, "uname": uname})
        return normalized

    def sync_roster(self, tid: str) -> dict:
        """Assign late Hydro participants without rebuilding verified desktops."""
        roster = self._validate_roster(self.hydro.roster(tid))
        growth = self._ensure_automatic_roster_capacity(tid, roster)
        self._enter(tid)
        try:
            contest = self.store.get_contest(tid)
            stored = self.store.seat_pool(tid)
            if not contest or not stored:
                raise RuntimeError("比赛尚未建立座位池")
            if contest["state"] not in {"ready", "preparing"}:
                raise RuntimeError("当前比赛状态不允许同步名单")
            self._validate_contest_snapshot(contest)
            pool = SeatPoolState.from_dict(stored["state"])
            now_ms = int(time.time() * 1000)
            pool = self._reserve_roster(
                contest,
                pool,
                now_ms=now_ms,
                roster=roster,
            )
            released_uids: list[int] = []
            if now_ms >= pool.release_at_ms:
                # Do not replay a completed bulk-release command on every
                # scheduler poll. ``release_due`` fingerprints ``now_ms``;
                # reusing one command id five seconds later would therefore
                # turn an already successful T-5 release into a conflict and
                # prevent the notification retry that follows this method.
                # A persisted release has no reserved seats, so a restart can
                # safely skip it. A genuinely new reserved set gets its own
                # revision-scoped command.
                if any(seat.state == "reserved" for seat in pool.seats):
                    previous = pool.revision
                    released = pool.release_due(
                        now_ms=now_ms,
                        command_id=(
                            f"release:{tid}:{pool.release_at_ms}:r{previous}"
                        ),
                        expected_revision=previous,
                    )
                    if not released.replayed and released.state.revision != previous:
                        pool = self._save_pool_state(tid, previous, released.state)
                    else:
                        pool = released.state
                    released_uids = [
                        int(item["uid"])
                        for item in released.value.get("released", [])
                    ]
            return {
                "roster": len(roster),
                "assigned": sum(1 for seat in pool.seats if seat.uid is not None),
                "released": released_uids,
                "automatic_growth": growth,
                "counts": pool.state_counts(),
                "release_at_ms": pool.release_at_ms,
            }
        finally:
            self._leave(tid)

    def read_formal_source(
        self,
        tid: str,
        uid: int,
        problem: str,
        *,
        maximum_bytes: int,
    ) -> bytes:
        """Read one student's canonical CSP source from the released seat.

        This is deliberately read-only: the submission button never creates a
        second editable copy. Collection and explicit submissions therefore
        observe the same file in the same answer directory.
        """
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", str(problem)):
            raise ValueError("题目文件名无效")
        maximum = int(maximum_bytes)
        if maximum < 1:
            raise ValueError("源代码大小上限无效")
        self._enter(tid)
        try:
            contest = self.store.get_contest(tid)
            if not contest or str(contest.get("state") or "") != "ready":
                raise RuntimeError("比赛当前不接受递交")
            files = json.loads(contest.get("files") or "[]")
            if problem not in files:
                raise ValueError("题目名称不属于本场比赛")
            assignment = self.store.seat_pool_assignment(tid, int(uid))
            if not assignment or assignment.get("state") != "released":
                raise RuntimeError("座位尚未发放或分配已变化")
            resource = assignment.get("resource") or {}
            container = str(resource.get("container") or "")
            candidate = str(resource.get("candidate") or "")
            if str(assignment.get("container_ref") or "") != container:
                raise RuntimeError("座位容器验收记录与连接资源不一致")
            if not re.fullmatch(r"seat-[0-9a-f]{8}-slot-[0-9]{3}", container):
                raise RuntimeError("座位容器标识无效")
            if not re.fullmatch(r"CSP[0-9]{3}", candidate):
                raise RuntimeError("准考证号无效")
            cloud_state, ip = self.cvm.status()
            if str(cloud_state).upper() != "RUNNING" or not ip:
                raise RuntimeError("比赛服务器当前不可读")
            remote = self._remote(str(ip))
            if not remote.wait_ssh(20):
                raise RuntimeError("比赛服务器连接超时")
            # The image helper resolves every path component through an
            # already-open directory descriptor, then emits one hash-bound
            # source snapshot from an O_NOFOLLOW file descriptor.
            raw_snapshot = remote.run(
                f"docker inspect -f '{{{{.State.Running}}}}' {_q(container)} "
                f"| grep -Fx true >/dev/null && docker exec -u student "
                f"{_q(container)} /usr/local/bin/capture-formal-source.py "
                f"{_q('/home/student/答案')} {_q(candidate)} {_q(problem)} "
                f"{maximum}",
                timeout=30,
            ).strip()
            try:
                snapshot = json.loads(raw_snapshot)
                if (
                    snapshot.get("schema") != 1
                    or not re.fullmatch(
                        r"[0-9a-f]{64}", str(snapshot.get("sha256") or "")
                    )
                    or not isinstance(snapshot.get("size"), int)
                ):
                    raise ValueError("invalid snapshot envelope")
                payload = base64.b64decode(snapshot.get("base64", ""), validate=True)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError("正式源文件读取结果无效") from exc
            if (
                not payload
                or len(payload) > maximum
                or len(payload) != snapshot["size"]
                or hashlib.sha256(payload).hexdigest() != snapshot["sha256"]
            ):
                raise RuntimeError("正式源文件稳定快照校验失败")
            return payload
        finally:
            self._leave(tid)

    def sync_contest_schedule(
        self,
        tid: str,
        *,
        begin_at_ms: int,
        end_at_ms: int,
        hydro_rule: str,
        observed_at_ms: int,
    ) -> dict:
        """Follow an authoritative OJ time change without opening a second clock."""
        begin = int(begin_at_ms)
        end = int(end_at_ms)
        observed = int(observed_at_ms)
        if str(hydro_rule) != "oi" or begin <= 0 or end <= begin:
            raise RuntimeError("OJ 比赛时间或赛制无效")
        self._enter(tid)
        try:
            contest = self.store.get_contest(tid)
            stored_pool = self.store.seat_pool(tid)
            if not contest or str(contest.get("state") or "") != "ready":
                raise RuntimeError("比赛已不在可同步时间的状态")
            if not stored_pool:
                raise RuntimeError("比赛座位池不存在")
            pool = SeatPoolState.from_dict(stored_pool["state"])
            lead_ms = int(contest.get("release_lead_minutes") or 5) * 60 * 1000
            rescheduled = pool.reschedule(
                begin_at_ms=begin,
                release_lead_ms=lead_ms,
                command_id=f"schedule:{tid}:{begin}:{end}",
                expected_revision=pool.revision,
            )
            deadline_reached = end <= observed
            if not deadline_reached:
                cloud_state, ip = self.cvm.status()
                if str(cloud_state).upper() != "RUNNING" or not ip:
                    raise RuntimeError("比赛服务器未运行，无法同步新的截止时间")
                remote = self._remote(str(ip))
                if not remote.wait_ssh(20):
                    raise RuntimeError("比赛服务器不可达，无法同步新的截止时间")
                candidate = dict(contest)
                candidate.update(
                    {
                        "begin_at_ms": begin,
                        "end_at_ms": end,
                        "hydro_rule": str(hydro_rule),
                    }
                )
                self._install_freeze_watchdog(remote, candidate)
            updated = self.store.commit_schedule_sync(
                tid,
                expected_begin_at_ms=int(contest.get("begin_at_ms") or 0),
                expected_end_at_ms=int(contest.get("end_at_ms") or 0),
                begin_at_ms=begin,
                end_at_ms=end,
                hydro_rule=str(hydro_rule),
                observed_at_ms=observed,
                expected_pool_revision=int(stored_pool["revision"]),
                pool_state=rescheduled.state.to_dict(),
            )
            return {
                "changed": True,
                "deadline_reached": deadline_reached,
                "contest": updated,
                "pool_revision": rescheduled.state.revision,
            }
        finally:
            self._leave(tid)

    def _verified_pool_inventory(
        self, tid: str, pool: SeatPoolState
    ) -> tuple[list[dict], str, str]:
        """Return resources after proving they still match the pool evidence."""
        resources = self.store.seat_pool_resources(tid)
        by_slot = {int(item["slot_no"]): item for item in resources}
        if len(by_slot) != len(resources):
            raise RuntimeError("座位池连接资源编号重复")
        images: set[str] = set()
        materials: set[str] = set()
        active_slots: set[int] = set()
        for seat in pool.seats:
            if seat.state == "planned":
                if seat.slot_no in by_slot:
                    raise RuntimeError("待建座位仍残留旧连接资源，请先排障")
                continue
            resource = by_slot.get(seat.slot_no)
            if not resource:
                raise RuntimeError(f"座位 {seat.slot_no} 缺少连接资源")
            if (
                seat.container_ref != resource["container"]
                or seat.image_digest != resource["image_digest"]
                or seat.material_digest != resource["material_digest"]
            ):
                raise RuntimeError(f"座位 {seat.slot_no} 的验收证据不一致")
            active_slots.add(seat.slot_no)
            images.add(str(seat.image_digest))
            materials.add(str(seat.material_digest))
        if set(by_slot) != active_slots:
            raise RuntimeError("座位池存在未纳入状态机的连接资源")
        if len(images) != 1 or len(materials) != 1:
            raise RuntimeError("现有座位未使用同一镜像和材料版本，禁止现场变更")
        return resources, next(iter(images)), next(iter(materials))

    def _pool_runtime_context(
        self,
        remote: Remote,
        contest: dict,
        pool: SeatPoolState,
    ) -> dict:
        tid = str(contest["tid"])
        resources, image_digest, material_digest = self._verified_pool_inventory(
            tid, pool
        )
        expected_material = self._material_digest(contest)
        if material_digest != expected_material:
            raise RuntimeError("已批准材料版本与现有座位不一致，禁止现场变更")
        server = self.cfg["contest_server"]
        expected_image_id = self._inspect_desktop_image_contract(
            remote, str(server["docker_image"])
        )
        if not expected_image_id or expected_image_id != image_digest:
            raise RuntimeError("比赛镜像已变化，禁止把不同镜像混入现有座位池")
        network = str(server["docker_network"])
        network_gateway = remote.run(
            "docker network inspect -f "
            f"'{{{{(index .IPAM.Config 0).Gateway}}}}' {_q(network)}"
        ).strip()
        try:
            gateway_ip = ipaddress.ip_address(network_gateway)
        except ValueError as exc:
            raise RuntimeError("隔离网络网关地址无效") from exc
        if gateway_ip.version != 4 or not gateway_ip.is_private:
            raise RuntimeError("隔离网络必须使用内网 IPv4 网关")
        seats_root = str(server["seats_root"])
        remote_materials = f"{seats_root}/{tid}/materials"
        paper_remote = f"{remote_materials}/paper.pdf"
        paper_digest = remote.run(
            f"sha256sum {_q(paper_remote)} | awk '{{print $1}}'"
        ).strip()
        if paper_digest != str(contest.get("paper_sha256") or ""):
            raise RuntimeError("远端试题 PDF 与已批准版本不一致")
        mode = str(contest.get("submission_mode") or "folder")
        web_enabled = mode in {"web", "both"}
        public_base = str(
            self.cfg["orchestrator"].get("public_base_url", "")
        ).rstrip("/")
        parsed_public = urlparse(public_base)
        if web_enabled and (
            parsed_public.scheme != "https"
            or not parsed_public.hostname
            or parsed_public.path not in {"", "/"}
        ):
            raise RuntimeError("网页提交模式要求有效的 HTTPS public_base_url")
        return {
            "resources": resources,
            "image_digest": image_digest,
            "material_digest": material_digest,
            "network": network,
            "network_gateway": network_gateway,
            "seats_root": seats_root,
            "remote_materials": remote_materials,
            "remote_testdata": f"{seats_root}/{tid}/testdata",
            "mode": mode,
            "web_enabled": web_enabled,
            "public_base": public_base,
            "origin_host": parsed_public.netloc,
            "image_contract": DESKTOP_IMAGE_CONTRACT,
        }

    def _new_pool_resource_spec(self, tid: str, slot_no: int) -> dict:
        seats_root = str(self.cfg["contest_server"]["seats_root"])
        return {
            "slot_no": int(slot_no),
            "token": secrets.token_urlsafe(12),
            "vnc_pass": rand_password(),
            "submit_token": secrets.token_urlsafe(24),
            "candidate": f"CSP{int(slot_no):03d}",
            "container": f"seat-{tid[:8]}-slot-{int(slot_no):03d}",
            "home": f"{seats_root}/{tid}/slots/{int(slot_no):03d}",
            "credential_revision": 1,
        }

    def _provision_pool_slot(
        self,
        remote: Remote,
        contest: dict,
        context: dict,
        spec: dict,
        existing_cips: set[str],
    ) -> dict:
        """Create and independently validate one new, still-unassigned slot."""
        if context.get("image_contract") != DESKTOP_IMAGE_CONTRACT:
            raise RuntimeError("座位创建缺少已验证的桌面就绪契约")
        tid = str(contest["tid"])
        server = self.cfg["contest_server"]
        files = json.loads(contest["files"])
        slot_no = int(spec["slot_no"])
        name = str(spec["container"])
        home = str(spec["home"])
        candidate = str(spec["candidate"])
        answer_paths = [f"{home}/answers/{candidate}"] + [
            f"{home}/answers/{candidate}/{problem}" for problem in files
        ]
        remote.run("mkdir -p " + " ".join(_q(path) for path in answer_paths))
        submit_proxy_port = int(server.get("submit_proxy_port", 18082))
        web_submit_url = (
            f"http://{context['network_gateway']}:{submit_proxy_port}/submit/"
            f"{spec['submit_token']}"
            if context["web_enabled"]
            else ""
        )
        memory_swap = server.get("memory_swap")
        memory_swap_arg = (
            f"--memory-swap {_q(memory_swap)} " if memory_swap else ""
        )
        has_testdata = bool(contest.get("testdata_sha256"))
        testdata_mount = (
            f"-v {_q(context['remote_testdata'] + ':/home/student/测试数据:ro')} "
            if has_testdata
            else ""
        )
        remote.run(
            "docker run -d --restart unless-stopped "
            f"--network {_q(context['network'])} --name {_q(name)} --hostname noilinux "
            f"--memory {_q(server['memory'])} --cpus {_q(server['cpus'])} "
            f"{memory_swap_arg}"
            f"--pids-limit {_q(server['pids_limit'])} --shm-size {_q(server['shm_size'])} "
            f"--label {_q('noi.contest=' + tid)} --label {_q('noi.slot=' + str(slot_no))} "
            f"-e STUDENT_PASSWORD={_q(spec['vnc_pass'])} "
            f"-e VNC_PASSWORD={_q(spec['vnc_pass'])} "
            f"-e CANDIDATE_ID={_q(candidate)} "
            f"-e PROBLEM_NAMES={_q(','.join(files))} "
            f"-e SUBMISSION_MODE={_q(context['mode'])} "
            f"-e WEB_SUBMIT_URL={_q(web_submit_url)} "
            f"-e HAS_TEST_DATA={_q('1' if has_testdata else '0')} "
            f"-e RESOLUTION={_q(server.get('resolution', '1366x768'))} "
            f"-e FRAME_RATE={_q(server.get('frame_rate', 30))} "
            f"-v {_q(home + '/answers:/home/student/答案')} "
            f"-v {_q(context['remote_materials'] + ':/home/student/试题:ro')} "
            f"{testdata_mount}{_q(context['image_digest'])}"
        )
        actual_image_id = _remote_readonly(
            remote,
            "docker inspect -f '{{.Image}}' " + _q(name)
        ).strip()
        if actual_image_id != context["image_digest"]:
            raise RuntimeError(f"容器 {name} 使用了非预期镜像")
        cip = _remote_readonly(
            remote,
            "docker inspect -f "
            f"'{{{{with index .NetworkSettings.Networks \"{context['network']}\"}}}}"
            "{{.IPAddress}}{{end}}' "
            + _q(name)
        ).strip()
        try:
            address = ipaddress.ip_address(cip)
        except ValueError as exc:
            raise RuntimeError(f"容器 {name} 未取得有效内网 IP") from exc
        if address.version != 4 or not address.is_private or cip in existing_cips:
            raise RuntimeError(f"容器 {name} 的内网 IP 无效或重复")
        remote.run(
            _seat_readiness_command(
                name=name,
                cip=cip,
                paper_sha256=str(contest["paper_sha256"]),
                candidate=candidate,
                problems=files,
                testdata_files=(
                    int(contest["testdata_files"]) if has_testdata else None
                ),
            ),
            timeout=SEAT_READINESS_TIMEOUT_SECONDS,
        )
        result = dict(spec)
        result.pop("home", None)
        result.update(
            {
                "cip": cip,
                "image_digest": context["image_digest"],
                "material_digest": context["material_digest"],
            }
        )
        return result

    def _pool_gateway_conf(
        self, contest: dict, resources: list[dict], network_gateway: str
    ) -> str:
        server = self.cfg["contest_server"]
        locations = []
        tokens: set[str] = set()
        cips: set[str] = set()
        for resource in sorted(resources, key=lambda item: int(item["slot_no"])):
            token = str(resource["token"])
            cip = str(resource["cip"])
            if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", token):
                raise RuntimeError("座位网关 token 格式无效")
            try:
                address = ipaddress.ip_address(cip)
            except ValueError as exc:
                raise RuntimeError("座位网关内网 IP 无效") from exc
            if address.version != 4 or not address.is_private:
                raise RuntimeError("座位网关只允许隔离网络 IPv4 地址")
            if token in tokens or cip in cips:
                raise RuntimeError("座位网关存在重复 token 或内网 IP")
            tokens.add(token)
            cips.add(cip)
            locations.append(
                NGINX_LOCATION.format(
                    token=token,
                    cip=cip,
                    novnc_path=novnc_path(
                        token,
                        int(server.get("no_vnc_quality", 9)),
                        int(server.get("no_vnc_compression", 2)),
                    ),
                )
            )
        conf = NGINX_CONF.format(
            tid=contest["tid"],
            listen=(
                f"{server.get('gateway_bind_address', '0.0.0.0')}:"
                f"{int(server['gateway_listen'])}"
            ),
            locations="".join(locations),
        )
        mode = str(contest.get("submission_mode") or "folder")
        if mode in {"web", "both"}:
            public_base = str(
                self.cfg["orchestrator"].get("public_base_url", "")
            ).rstrip("/")
            parsed = urlparse(public_base)
            conf += SUBMIT_PROXY_CONF.format(
                gateway=network_gateway,
                port=int(server.get("submit_proxy_port", 18082)),
                origin=public_base,
                origin_host=parsed.netloc,
            )
        return conf

    @staticmethod
    def _stage_pool_gateway(
        remote: Remote, tid: str, revision: int, conf: str
    ) -> str:
        pending = f"/tmp/noi-seats-{tid}-r{revision}.conf"
        backup = f"/tmp/noi-seats-{tid}-r{revision}.before"
        live = "/etc/nginx/conf.d/noi-seats.conf"
        remote.put_content(conf, pending)
        remote.run(
            f"test -f {_q(live)} && sudo cp -f {_q(live)} {_q(backup)} && "
            f"if sudo install -m 0644 {_q(pending)} {_q(live)} && "
            "sudo nginx -t && sudo systemctl reload-or-restart nginx; then "
            f"rm -f -- {_q(pending)}; "
            "else rc=$?; "
            f"sudo cp -f {_q(backup)} {_q(live)} && "
            "sudo nginx -t && sudo systemctl reload-or-restart nginx; "
            f"rm -f -- {_q(pending)}; exit $rc; fi"
        )
        return backup

    @staticmethod
    def _restore_pool_gateway(remote: Remote, backup: str) -> None:
        live = "/etc/nginx/conf.d/noi-seats.conf"
        remote.run(
            f"sudo install -m 0644 {_q(backup)} {_q(live)} && "
            "sudo nginx -t && sudo systemctl reload-or-restart nginx"
        )

    @staticmethod
    def _cleanup_incremental_slots(remote: Remote, specs: list[dict]) -> None:
        for spec in specs:
            remote.run(
                f"docker rm -f {_q(spec['container'])} >/dev/null 2>&1 || true; "
                f"rm -rf -- {_q(spec['home'])}"
            )

    def grow_pool(
        self,
        tid: str,
        *,
        additional_main: int = 0,
        additional_spares: int = 1,
        expected_revision: int,
    ) -> dict:
        """Safely provision only newly appended slots and atomically publish them."""
        self._enter(tid)
        remote: Remote | None = None
        deployment_lock = None
        attempted: list[dict] = []
        staged_backup = ""
        committed = False
        try:
            deployment_lock = self._acquire_deployment_lock()
            contest = self.store.get_contest(tid)
            stored = self.store.seat_pool(tid)
            if not contest or not stored:
                raise RuntimeError("比赛尚未建立座位池")
            if contest["state"] != "ready":
                raise RuntimeError("只有运行中的比赛可以现场扩容")
            self._validate_contest_snapshot(contest)
            pool = SeatPoolState.from_dict(stored["state"])
            command_id = (
                f"grow:{tid}:r{int(expected_revision)}:"
                f"{int(additional_main)}:{int(additional_spares)}"
            )
            grown = pool.grow(
                additional_main=int(additional_main),
                additional_spares=int(additional_spares),
                command_id=command_id,
                expected_revision=int(expected_revision),
            )
            if grown.replayed:
                return {
                    "replayed": True,
                    "revision": grown.state.revision,
                    "added": grown.value.get("added", []),
                    "counts": grown.state.state_counts(),
                }
            participant_limit = int(
                self.cfg.get("orchestrator", {}).get("seat_pool_maximum", 30)
            )
            total_limit = int(
                self.cfg.get("orchestrator", {}).get(
                    "seat_pool_total_maximum", participant_limit
                )
            )
            if grown.state.max_participants > participant_limit:
                raise RuntimeError(
                    f"正式参赛人数不能超过安全上限 {participant_limit}"
                )
            if len(grown.state.seats) > total_limit:
                raise RuntimeError(f"座位总数不能超过安全上限 {total_limit}")
            state = grown.state
            added_slots = [int(item["slot_no"]) for item in grown.value["added"]]
            cloud_state, ip = self.cvm.status()
            if cloud_state.upper() != "RUNNING" or not ip:
                raise RuntimeError("比赛服务器未运行，禁止现场扩容")
            remote = self._remote(ip)
            if not remote.wait_ssh():
                raise RuntimeError("SSH 等待超时")
            context = self._pool_runtime_context(remote, contest, pool)
            existing_cips = {str(item["cip"]) for item in context["resources"]}
            new_resources: list[dict] = []
            for slot_no in added_slots:
                previous = state.revision
                warming = state.mark_warming(
                    slot_no,
                    now_ms=int(time.time() * 1000),
                    command_id=f"warm:grow:{tid}:r{expected_revision}:{slot_no}",
                    expected_revision=previous,
                )
                state = warming.state
                spec = self._new_pool_resource_spec(tid, slot_no)
                attempted.append(spec)
                resource = self._provision_pool_slot(
                    remote, contest, context, spec, existing_cips
                )
                existing_cips.add(str(resource["cip"]))
                previous = state.revision
                verified = state.mark_verified(
                    slot_no,
                    container_ref=resource["container"],
                    image_digest=context["image_digest"],
                    material_digest=context["material_digest"],
                    now_ms=int(time.time() * 1000),
                    command_id=f"verify:grow:{tid}:r{expected_revision}:{slot_no}",
                    expected_revision=previous,
                )
                state = verified.state
                new_resources.append(resource)
            all_resources = list(context["resources"]) + new_resources
            conf = self._pool_gateway_conf(
                contest, all_resources, context["network_gateway"]
            )
            staged_backup = self._stage_pool_gateway(
                remote, tid, state.revision, conf
            )
            try:
                self.store.commit_pool_expansion(
                    tid, int(expected_revision), state.to_dict(), new_resources
                )
            except Exception:
                self._restore_pool_gateway(remote, staged_backup)
                staged_backup = ""
                raise
            committed = True
            remote.run(
                f"rm -f -- {_q(staged_backup)} >/dev/null 2>&1 || true"
            )
            staged_backup = ""
            self.store.set_state(
                tid,
                "ready",
                f"自动扩容完成：新增 {len(new_resources)} 个座位，"
                f"座位池共 {len(state.seats)} 个",
            )
            return {
                "replayed": False,
                "revision": state.revision,
                "added": added_slots,
                "counts": state.state_counts(),
            }
        except Exception as exc:
            self.log.exception("seat pool expansion failed for %s", tid)
            if remote is not None and staged_backup:
                try:
                    self._restore_pool_gateway(remote, staged_backup)
                except Exception:
                    self.log.exception("cannot restore pool gateway for %s", tid)
            if remote is not None and attempted and not committed:
                try:
                    self._cleanup_incremental_slots(remote, attempted)
                except Exception:
                    self.log.exception("cannot clean incremental seats for %s", tid)
            contest = self.store.get_contest(tid)
            if contest and contest.get("state") == "ready":
                self.store.set_state(
                    tid, "ready", f"自动扩容失败，原座位池保持运行：{exc}"
                )
            raise
        finally:
            self._release_deployment_lock(deployment_lock)
            self._leave(tid)

    def replace_failed_seat(
        self,
        tid: str,
        slot_no: int,
        *,
        reason: str,
        expected_revision: int,
        teacher_approved: bool,
    ) -> dict:
        """Cut one failed token over to a verified spare without rebuilding peers."""
        if not teacher_approved:
            raise TeacherApprovalRequiredError("故障座位替换必须由教师明确确认")
        self._enter(tid)
        remote: Remote | None = None
        deployment_lock = None
        staged_backup = ""
        paused_by_us = False
        replacement_committed = False
        failed_container = ""
        try:
            deployment_lock = self._acquire_deployment_lock()
            contest = self.store.get_contest(tid)
            stored = self.store.seat_pool(tid)
            if not contest or not stored:
                raise RuntimeError("比赛尚未建立座位池")
            if contest["state"] != "ready":
                raise RuntimeError("只有运行中的比赛可以替换故障座位")
            self._validate_contest_snapshot(contest)
            pool = SeatPoolState.from_dict(stored["state"])
            command_id = f"replace:{tid}:r{int(expected_revision)}:{int(slot_no)}"
            replaced = pool.replace_failed(
                int(slot_no),
                reason=str(reason),
                now_ms=int(time.time() * 1000),
                teacher_approved=True,
                command_id=command_id,
                expected_revision=int(expected_revision),
            )
            if replaced.replayed:
                return {
                    "replayed": True,
                    "revision": replaced.state.revision,
                    **replaced.value,
                }
            cloud_state, ip = self.cvm.status()
            if cloud_state.upper() != "RUNNING" or not ip:
                raise RuntimeError("比赛服务器未运行，禁止替换故障座位")
            remote = self._remote(ip)
            if not remote.wait_ssh():
                raise RuntimeError("SSH 等待超时")
            context = self._pool_runtime_context(remote, contest, pool)
            failed_resource = next(
                (
                    item
                    for item in context["resources"]
                    if int(item["slot_no"]) == int(slot_no)
                ),
                None,
            )
            if not failed_resource:
                raise RuntimeError("故障座位连接资源不存在")
            failed_container = str(failed_resource["container"])
            failed_status = remote.run(
                "docker inspect -f '{{.State.Status}}' "
                + _q(failed_container)
            ).strip()
            if failed_status == "running":
                remote.run(
                    f"docker pause {_q(failed_container)} >/dev/null || "
                    f"test \"$(docker inspect -f '{{{{.State.Status}}}}' "
                    f"{_q(failed_container)})\" = paused"
                )
                paused_by_us = True
            elif failed_status not in {"paused", "exited", "dead"}:
                raise RuntimeError(
                    f"故障容器状态 {failed_status or 'unknown'} 不允许安全切换"
                )
            replacement = replaced.value.get("replacement")
            replacement_resource = None
            if replacement:
                replacement_resource = next(
                    (
                        item
                        for item in context["resources"]
                        if int(item["slot_no"])
                        == int(replacement["slot_no"])
                    ),
                    None,
                )
                if not replacement_resource:
                    raise RuntimeError("备用座位连接资源不存在")
                failed_home = (
                    f"{context['seats_root']}/{tid}/slots/{int(slot_no):03d}/answers/"
                    f"{failed_resource['candidate']}"
                )
                replacement_home = (
                    f"{context['seats_root']}/{tid}/slots/"
                    f"{int(replacement['slot_no']):03d}/answers/"
                    f"{replacement_resource['candidate']}"
                )
                remote.run(
                    f"mkdir -p {_q(replacement_home)} && "
                    f"if [ -d {_q(failed_home)} ]; then "
                    f"cp -a --reflink=auto {_q(failed_home + '/.')} "
                    f"{_q(replacement_home + '/')}; fi"
                )
            remaining = [
                item
                for item in context["resources"]
                if int(item["slot_no"]) != int(slot_no)
            ]
            conf = self._pool_gateway_conf(
                contest, remaining, context["network_gateway"]
            )
            staged_backup = self._stage_pool_gateway(
                remote, tid, replaced.state.revision, conf
            )
            try:
                bound = self.store.commit_pool_replacement(
                    tid,
                    int(expected_revision),
                    replaced.state.to_dict(),
                    failed_slot=int(slot_no),
                )
                replacement_committed = True
            except Exception:
                self._restore_pool_gateway(remote, staged_backup)
                staged_backup = ""
                raise
            remote.run(
                f"rm -f -- {_q(staged_backup)} >/dev/null 2>&1 || true"
            )
            staged_backup = ""
            try:
                remote.run(
                    f"docker update --restart=no {_q(failed_resource['container'])} "
                    ">/dev/null 2>&1 || true; "
                    f"docker rm -f {_q(failed_resource['container'])} >/dev/null"
                )
            except Exception:
                self.log.exception("cannot remove failed seat %s/%s", tid, slot_no)
            capacity_recovered = False
            capacity_revision = replaced.state.revision
            try:
                capacity_revision = self._repair_isolated_pool_slot(
                    tid,
                    int(slot_no),
                    expected_revision=replaced.state.revision,
                    remote=remote,
                    contest=contest,
                )
                capacity_recovered = True
            except Exception:
                # The student cutover is already committed and must never be
                # rolled back to the failed machine.  Keep the controller in a
                # visibly degraded ready state so the same slot can be repaired
                # with the idempotent capacity-repair operation.
                self.log.exception(
                    "cannot restore spare capacity after replacing %s/%s", tid, slot_no
                )
            self.store.set_state(
                tid,
                "ready",
                f"故障座位 {int(slot_no):03d} 已隔离"
                + (
                    f"，学生已切换到备用座位 {int(replacement['slot_no']):03d}"
                    if replacement
                    else ""
                )
                + (
                    "；原槽位已重建并恢复备用容量"
                    if capacity_recovered
                    else "；备用容量恢复失败，请立即执行容量修复"
                ),
            )
            return {
                "replayed": False,
                "revision": replaced.state.revision,
                "failed": replaced.value.get("failed"),
                "replacement": replacement,
                "credential_revision": (
                    int(bound["credential_revision"]) if bound else None
                ),
                "capacity_recovered": capacity_recovered,
                "capacity_revision": int(capacity_revision),
            }
        except Exception as exc:
            self.log.exception("failed-seat replacement failed for %s/%s", tid, slot_no)
            if remote is not None and staged_backup:
                try:
                    self._restore_pool_gateway(remote, staged_backup)
                except Exception:
                    self.log.exception("cannot restore pool gateway for %s", tid)
            if (
                remote is not None
                and failed_container
                and paused_by_us
                and not replacement_committed
            ):
                try:
                    remote.run(
                        f"state=\"$(docker inspect -f '{{{{.State.Status}}}}' "
                        f"{_q(failed_container)})\"; "
                        "if [ \"$state\" = paused ]; then "
                        f"docker unpause {_q(failed_container)} "
                        ">/dev/null; elif [ \"$state\" != running ]; then exit 1; fi"
                    )
                except Exception:
                    self.log.exception(
                        "cannot resume original failed seat %s/%s", tid, slot_no
                    )
            contest = self.store.get_contest(tid)
            if contest and contest.get("state") == "ready":
                self.store.set_state(
                    tid, "ready", f"故障座位替换失败，原连接保持不变：{exc}"
                )
            raise
        finally:
            self._release_deployment_lock(deployment_lock)
            self._leave(tid)

    def _repair_isolated_pool_slot(
        self,
        tid: str,
        slot_no: int,
        *,
        expected_revision: int,
        remote: Remote,
        contest: dict,
    ) -> int:
        """Recreate one planned failed slot without changing any assignment."""
        stored = self.store.seat_pool(tid)
        if not stored or int(stored["revision"]) != int(expected_revision):
            raise RuntimeError("容量修复前座位池已变化")
        pool = SeatPoolState.from_dict(stored["state"])
        failed = pool.seat(int(slot_no))
        if failed.state != "planned" or failed.uid is not None or failed.failure_count < 1:
            raise RuntimeError("目标座位不处于可恢复的隔离状态")
        context = self._pool_runtime_context(remote, contest, pool)
        spec = self._new_pool_resource_spec(tid, int(slot_no))
        # The student's last bytes were copied to the replacement before the
        # cutover committed.  A restored spare must start with an empty answer
        # tree and fresh credentials, never with another student's files.
        remote.run(f"rm -rf -- {_q(spec['home'])}")
        warming = pool.mark_warming(
            int(slot_no),
            now_ms=int(time.time() * 1000),
            command_id=(
                f"repair:warm:{tid}:{int(slot_no)}:failure:{int(failed.failure_count)}"
            ),
            expected_revision=pool.revision,
        )
        state = warming.state
        existing_cips = {str(item["cip"]) for item in context["resources"]}
        resource = None
        staged_backup = ""
        provision_attempted = False
        committed = False
        try:
            provision_attempted = True
            resource = self._provision_pool_slot(
                remote, contest, context, spec, existing_cips
            )
            verified = state.mark_verified(
                int(slot_no),
                container_ref=str(resource["container"]),
                image_digest=str(resource["image_digest"]),
                material_digest=str(resource["material_digest"]),
                now_ms=int(time.time() * 1000),
                command_id=(
                    f"repair:verify:{tid}:{int(slot_no)}:failure:"
                    f"{int(failed.failure_count)}"
                ),
                expected_revision=state.revision,
            )
            state = verified.state
            conf = self._pool_gateway_conf(
                contest,
                list(context["resources"]) + [resource],
                context["network_gateway"],
            )
            staged_backup = self._stage_pool_gateway(
                remote, tid, state.revision, conf
            )
            self.store.commit_pool_repair(
                tid,
                int(expected_revision),
                state.to_dict(),
                resource,
                repaired_slot=int(slot_no),
            )
            committed = True
            try:
                remote.run(f"rm -f -- {_q(staged_backup)} >/dev/null 2>&1 || true")
            except Exception:
                self.log.exception("cannot remove committed capacity repair backup")
            return state.revision
        except Exception:
            if staged_backup and not committed:
                try:
                    self._restore_pool_gateway(remote, staged_backup)
                except Exception:
                    self.log.exception("cannot restore gateway after capacity repair")
            if provision_attempted and not committed:
                try:
                    self._cleanup_incremental_slots(remote, [spec])
                except Exception:
                    self.log.exception("cannot remove failed capacity repair slot")
            raise

    def repair_pool_capacity(
        self, tid: str, slot_no: int, *, expected_revision: int
    ) -> dict:
        """Retry the fail-closed capacity restore for one isolated slot."""
        self._enter(tid)
        remote: Remote | None = None
        deployment_lock = None
        try:
            deployment_lock = self._acquire_deployment_lock()
            contest = self.store.get_contest(tid)
            if not contest or contest.get("state") != "ready":
                raise RuntimeError("只有运行中的比赛可以恢复备用容量")
            self._validate_contest_snapshot(contest)
            cloud_state, ip = self.cvm.status()
            if cloud_state.upper() != "RUNNING" or not ip:
                raise RuntimeError("比赛服务器未运行，禁止恢复备用容量")
            remote = self._remote(ip)
            if not remote.wait_ssh():
                raise RuntimeError("SSH 等待超时")
            revision = self._repair_isolated_pool_slot(
                tid,
                int(slot_no),
                expected_revision=int(expected_revision),
                remote=remote,
                contest=contest,
            )
            self.store.set_state(
                tid, "ready", f"座位 {int(slot_no):03d} 已重建，备用容量恢复"
            )
            return {"slot_no": int(slot_no), "revision": int(revision), "recovered": True}
        finally:
            self._release_deployment_lock(deployment_lock)
            self._leave(tid)

    def _ensure_server(self) -> str:
        state, ip = self.cvm.status()
        state = state.upper()
        if state == "RUNNING" and ip:
            return ip
        if state == "STOPPING":
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                state, _ = self.cvm.status()
                state = state.upper()
                if state == "STOPPED":
                    break
                time.sleep(5)
            else:
                raise TimeoutError("等待云服务器关机完成超时")
        if state == "STOPPED":
            self.cvm.start()
        elif state not in {"STARTING", "PENDING"}:
            raise RuntimeError(f"云服务器状态不可启动: {state}")
        return self.cvm.wait_running()

    def _stop_server_best_effort(self) -> None:
        """Request stop and prove the asynchronous operation reached STOPPED."""
        deadline = time.monotonic() + 120
        stop_requested = False
        last_state = "UNKNOWN"
        while time.monotonic() < deadline:
            state, _ = self.cvm.status()
            state = state.upper()
            last_state = state
            if state == "RUNNING":
                if not stop_requested:
                    self.cvm.stop()
                    stop_requested = True
                    # Confirm the transition immediately once before backing
                    # off; this keeps normal shutdown fast without hot-looping
                    # when the cloud state remains RUNNING.
                    continue
                time.sleep(3)
                continue
            if state == "STOPPED":
                return
            if state not in {"STARTING", "PENDING", "STOPPING"}:
                raise RuntimeError(f"云服务器状态不可安全停机: {state}")
            time.sleep(3)
        raise TimeoutError(f"等待云服务器完成停机超时: {last_state}")

    def _probe_direct_gateway(self, ip: str, token: str) -> dict:
        server = self.cfg["contest_server"]
        # Resolving the configured base first detects a stale hard-coded EIP
        # before a student rule is published.
        gateway_base_url(server, ip)
        return probe_novnc_gateway(
            ip,
            int(server.get("gateway_listen", 80)),
            token,
            quality=int(server.get("no_vnc_quality", 9)),
            compression=int(server.get("no_vnc_compression", 2)),
        )

    @staticmethod
    def _gateway_listener_source(bind_address: str, port: int) -> tuple[str, str]:
        """Return a scoped ss pipeline and one local address for HTTP probes."""
        address = ipaddress.ip_address(str(bind_address))
        if address.version != 4:
            raise ValueError("网关绑定地址必须是 IPv4")
        listen_port = int(port)
        if not 1 <= listen_port <= 65535:
            raise ValueError("网关监听端口无效")
        source = f"sudo ss -H -ltnp4 'sport = :{listen_port}'"
        probe_host = "127.0.0.1"
        if not address.is_unspecified:
            endpoint = f"{address}:{listen_port}"
            source += (
                " | awk '$4 == \"0.0.0.0:"
                f"{listen_port}\" || $4 == \"*:{listen_port}\" || "
                f"$4 == \"{endpoint}\"'"
            )
            probe_host = str(address)
        return source, probe_host

    @classmethod
    def _assert_gateway_port_available(
        cls,
        remote: Remote,
        port: int,
        bind_address: str = "0.0.0.0",
    ) -> None:
        """Reject a gateway port owned by anything except the managed nginx.

        ``systemctl reload nginx`` can return success even when new workers
        fail to bind.  Detecting an unrelated listener before creating any
        desktop keeps that late false-success from wasting a complete pool.
        An existing nginx listener is allowed because a previous fail-closed
        transaction may have left the service running without a live contest.
        """
        listen_port = int(port)
        source, _ = cls._gateway_listener_source(bind_address, listen_port)
        remote.run(
            f"listeners=\"$({source})\"; "
            "if [ -n \"$listeners\" ] && "
            "printf '%s\\n' \"$listeners\" | "
            "grep -Fv '\"nginx\"' | grep -q .; then "
            f"echo '网关端口 {listen_port} 已被非 nginx 进程占用' >&2; "
            "exit 1; fi"
        )

    @classmethod
    def _activate_pool_gateway(
        cls,
        remote: Remote,
        *,
        port: int,
        readiness_path: str,
        bind_address: str = "0.0.0.0",
    ) -> None:
        """Install the staged gateway and prove the new listener serves it."""
        listen_port = int(port)
        source, probe_host = cls._gateway_listener_source(
            bind_address, listen_port
        )
        path = str(readiness_path)
        if not path.startswith("/s/") or any(ch in path for ch in "\r\n"):
            raise ValueError("网关验收路径无效")
        remote.run(
            "sudo install -m 0644 /tmp/noi-seats.conf "
            "/etc/nginx/conf.d/noi-seats.conf && "
            "sudo nginx -t && sudo systemctl reload-or-restart nginx && "
            "ready=0; for attempt in $(seq 1 20); do "
            f"if {source} | grep -F '\"nginx\"' >/dev/null && "
            "curl -fsS --max-time 3 -o /dev/null "
            f"{_q('http://' + probe_host + ':' + str(listen_port) + path)}; "
            "then ready=1; break; fi; sleep 1; done; "
            "if [ \"$ready\" != 1 ]; then "
            "echo 'nginx 网关未在限定时间内监听并提供首个座位页面' >&2; "
            "exit 1; fi"
        )

    @staticmethod
    def _remove_gateway(remote: Remote, tid: str) -> None:
        marker = _q(f"# noi-contest: {tid}")
        conf = "/etc/nginx/conf.d/noi-seats.conf"
        remote.run(
            f"if [ -f {_q(conf)} ] && grep -Fqx -- {marker} {_q(conf)}; then "
            f"sudo rm -f {_q(conf)} && sudo nginx -t && "
            "sudo systemctl reload-or-restart nginx; "
            "fi"
        )

    @staticmethod
    def _freeze_unit(tid: str) -> str:
        return f"noi-contest-freeze-{tid}"

    def _remove_freeze_watchdog(self, remote: Remote, tid: str) -> None:
        unit = self._freeze_unit(tid)
        remote.run(
            f"sudo systemctl disable --now {_q(unit + '.timer')} "
            ">/dev/null 2>&1 || true; "
            f"sudo systemctl stop {_q(unit + '.service')} "
            ">/dev/null 2>&1 || true; "
            f"sudo rm -f {_q('/etc/systemd/system/' + unit + '.timer')} "
            f"{_q('/etc/systemd/system/' + unit + '.service')} "
            f"{_q('/usr/local/sbin/' + unit + '.sh')}; "
            "sudo systemctl daemon-reload; "
            f"sudo systemctl reset-failed {_q(unit + '.timer')} "
            f"{_q(unit + '.service')} >/dev/null 2>&1 || true"
        )

    def _install_freeze_watchdog(self, remote: Remote, contest: dict) -> None:
        """Close the local gateway and freeze seats at endAt without the OJ."""
        end_at_ms = int(contest.get("end_at_ms") or 0)
        if not end_at_ms:
            return
        if end_at_ms <= int(datetime.now(timezone.utc).timestamp() * 1000):
            raise RuntimeError("比赛已到截止时间，不能再进入备赛")
        tid = str(contest["tid"])
        unit = self._freeze_unit(tid)
        marker = f"{self.cfg['contest_server']['seats_root']}/{tid}/.frozen-at"
        label = _q(f"label=noi.contest={tid}")
        script_path = f"/usr/local/sbin/{unit}.sh"
        freeze_script = (
            "#!/bin/sh\n"
            "set -eu\n"
            f'ids="$(docker ps -aq --filter {label})"\n'
            '[ -n "$ids" ]\n'
            'docker update --restart=no $ids >/dev/null\n'
            'for c in $ids; do\n'
            '  state="$(docker inspect -f \'{{.State.Status}}\' "$c")"\n'
            '  if [ "$state" = running ]; then\n'
            '    docker pause "$c" >/dev/null || '
            '[ "$(docker inspect -f \'{{.State.Status}}\' "$c")" = paused ]\n'
            '  elif [ "$state" != paused ] && [ "$state" != exited ] '
            '&& [ "$state" != dead ] && [ "$state" != created ]; then\n'
            '    exit 1\n'
            '  fi\n'
            'done\n'
            # Freeze is the exact file cutoff and must not wait behind nginx's
            # graceful-stop timeout.  The Aliyun rule has no server-side TTL,
            # so then stop the local listener as an independent data-plane
            # barrier when the OJ/control plane is unavailable.  A failed
            # nginx stop makes systemd retry while the seats remain paused.
            "systemctl stop nginx\n"
            "! systemctl is-active --quiet nginx\n"
            f"date -u +%FT%TZ > {_q(marker)}\n"
        )
        # systemd's @UNIX form accepts integral seconds. Round upward so a
        # sub-second Hydro endAt is never frozen early.
        when = f"@{(end_at_ms + 999) // 1000}"
        service = (
            "[Unit]\n"
            f"Description=Freeze NOI contest seats {tid}\n"
            "After=docker.service\n"
            "Requires=docker.service\n\n"
            "[Service]\n"
            "Type=oneshot\n"
            f"ExecStart={script_path}\n"
            "Restart=on-failure\n"
            "RestartSec=1s\n"
        )
        timer = (
            "[Unit]\n"
            f"Description=Freeze NOI contest seats {tid} at Hydro endAt\n\n"
            "[Timer]\n"
            f"OnCalendar={when}\n"
            "AccuracySec=100ms\n"
            "Persistent=true\n"
            f"Unit={unit}.service\n\n"
            "[Install]\n"
            "WantedBy=timers.target\n"
        )
        remote.put_content(freeze_script, f"/tmp/{unit}.sh")
        remote.put_content(service, f"/tmp/{unit}.service")
        remote.put_content(timer, f"/tmp/{unit}.timer")
        remote.run(
            f"sudo install -m 0755 {_q('/tmp/' + unit + '.sh')} {_q(script_path)} && "
            f"sudo install -m 0644 {_q('/tmp/' + unit + '.service')} "
            f"{_q('/etc/systemd/system/' + unit + '.service')} && "
            f"sudo install -m 0644 {_q('/tmp/' + unit + '.timer')} "
            f"{_q('/etc/systemd/system/' + unit + '.timer')} && "
            "sudo systemctl daemon-reload && "
            f"sudo systemctl enable {_q(unit + '.timer')} >/dev/null && "
            f"sudo systemctl restart {_q(unit + '.timer')} && "
            f"sudo systemctl is-active --quiet {_q(unit + '.timer')}"
        )

    @staticmethod
    def _freeze_containers(remote: Remote, names: str) -> None:
        remote.run(
            f"docker update --restart=no {names} >/dev/null; "
            f"for c in {names}; do "
            "state=\"$(docker inspect -f '{{.State.Status}}' \"$c\")\" || exit 1; "
            "case \"$state\" in "
            "running) docker pause \"$c\" >/dev/null || "
            "test \"$(docker inspect -f '{{.State.Status}}' \"$c\")\" = paused ;; "
            "paused|exited|dead) : ;; "
            "*) echo \"unexpected container state $c=$state\" >&2; exit 1 ;; "
            "esac; done"
        )

    def _freeze_for_collection(self, remote: Remote, tid: str, names: str) -> None:
        """Prove seats frozen before removing the independent deadline timer."""
        # Removing the timer first creates a crash window with neither the
        # local watchdog nor frozen seats.  Duplicate/concurrent pause is safe,
        # so keep the timer armed until manual freeze has fully succeeded.
        self._freeze_containers(remote, names)
        self._remove_freeze_watchdog(remote, tid)

    def _cleanup_failed_prepare(self, remote: Remote, tid: str) -> None:
        self._remove_freeze_watchdog(remote, tid)
        label = _q(f"label=noi.contest={tid}")
        remote.run(
            f"ids=\"$(docker ps -aq --filter {label})\"; "
            'if [ -n "$ids" ]; then docker rm -f $ids >/dev/null; fi'
        )
        self._remove_gateway(remote, tid)

    def prepare(self, tid: str, claimed: bool = False) -> str:
        self._enter(tid)
        store = self.store
        owns_server = False
        frontend_attempted = False
        remote: Remote | None = None
        deployment_lock = None
        fresh_prepare_started = False
        direct_access_claimed = False
        try:
            deployment_lock = self._acquire_deployment_lock()
            if not claimed and not store.transition(
                tid, {"registered", "error"}, "preparing", "云服务器开机中"
            ):
                raise RuntimeError("当前比赛状态不允许备赛")
            store.set_state(tid, "preparing", "云服务器开机中")
            active = [
                contest
                for contest in store.contests()
                if contest["tid"] != tid
                and contest["state"]
                in {"preparing", "ready", "collecting", "safe_wait"}
            ]
            if active:
                raise RuntimeError(f"已有活动比赛 {active[0]['tid']}，暂不支持重叠办赛")
            contest = store.get_contest(tid)
            if not contest:
                raise RuntimeError("比赛未登记")
            self._validate_contest_snapshot(contest)
            if (
                store.seats(tid)
                or store.web_submission_count(tid)
                or store.seat_pool(tid)
            ):
                raise RuntimeError(
                    "本轮已有座位或学生递交（包括已建立的座位池），"
                    "不能重新备赛；请重试收卷，"
                    "如确需重开请在 Hydro 新建比赛"
                )
            roster = self._validate_roster(self.hydro.roster(tid))
            target_main, target_spares = desired_capacity(len(roster))
            participant_limit = int(
                self.cfg.get("orchestrator", {}).get("seat_pool_maximum", 30)
            )
            total_limit = int(
                self.cfg.get("orchestrator", {}).get(
                    "seat_pool_total_maximum", participant_limit
                )
            )
            if target_main > participant_limit:
                raise RuntimeError(
                    f"OJ 报名人数 {target_main} 超过安全上限 {participant_limit}"
                )
            if target_main + target_spares > total_limit:
                raise RuntimeError(
                    f"所需座位 {target_main + target_spares} 超过总安全上限 "
                    f"{total_limit}"
                )
            material_ready = str(contest.get("material_state") or "") == "approved"
            if not material_ready:
                raise RuntimeError("备赛材料尚未由教师批准并冻结")
            files = json.loads(contest["files"])
            if not contest.get("testdata_sha256"):
                raise RuntimeError("V1 必须为每题发布 2 到 4 组辅助自测数据")
            expected_testdata_files = (
                len(files) * int(contest.get("practice_groups") or 0) * 2
            )
            if int(contest.get("testdata_files") or 0) != expected_testdata_files:
                raise RuntimeError("辅助自测数据数量与已批准材料不一致")
            if str(contest.get("submission_mode") or "") != "both":
                raise RuntimeError("比赛未使用 V1 统一正式答案目录递交契约")
            active_revision = str(contest.get("active_material_revision") or "")
            publication = (
                store.material_publication(tid, active_revision)
                if active_revision
                else None
            )
            expected_publication_attachments = [
                {
                    "name": "01_比赛题面.pdf",
                    "sha256": str(contest.get("paper_sha256") or ""),
                    "size": int(contest.get("paper_size") or 0),
                },
                {
                    "name": "02_辅助自测数据.tar.gz",
                    "sha256": str(contest.get("testdata_sha256") or ""),
                    "size": int(contest.get("testdata_size") or 0),
                },
            ]
            if (
                not publication
                or publication.get("ok") is not True
                or publication.get("tid") != str(tid).lower()
                or publication.get("revision") != active_revision
                or publication.get("attachments")
                != expected_publication_attachments
            ):
                raise RuntimeError("OJ 与学生桌面的已批准材料尚未完成同字节发布")
            materials_dir = Path(
                self.cfg["orchestrator"].get("materials_dir", "/app/data/materials")
            )
            active_artifact = (
                store.artifact_revision(tid, active_revision)
                if active_revision
                else None
            )
            paper_local, testdata_local = approved_material_paths(
                materials_root=materials_dir,
                artifact_root=self.cfg["orchestrator"].get(
                    "artifact_root", "/app/data/artifacts"
                ),
                contest=contest,
                artifact=active_artifact,
            )
            if not contest.get("paper_sha256") or not paper_local.is_file():
                raise RuntimeError("比赛尚未上传试题 PDF")
            paper_digest = sha256_file(paper_local)
            if paper_digest != contest["paper_sha256"]:
                raise RuntimeError("试题 PDF 哈希与登记记录不一致")
            if testdata_local is None or not testdata_local.is_file():
                raise RuntimeError("测试数据归档文件丢失")
            if sha256_file(testdata_local) != contest["testdata_sha256"]:
                raise RuntimeError("测试数据哈希与登记记录不一致")

            # All read-only contest, roster, receipt and local-byte gates have
            # passed. Only now may this prepare transaction close a stale
            # desktop rule or start the dedicated contest server.
            direct_access_claimed = True
            store.set_state(tid, "preparing", "收回旧桌面公网入口")
            self._revoke_desktop_access()
            fresh_prepare_started = True
            initial_server_state, _ = self.cvm.status()
            owns_server = initial_server_state.upper() != "RUNNING"
            ip = self._ensure_server()
            remote = self._remote(ip)
            if not remote.wait_ssh():
                raise RuntimeError("SSH 等待超时")

            # This is intentionally the first remote prerequisite after SSH:
            # no pool state, slot directory, network, or container is created
            # until the mutable image tag resolves to the required contract.
            expected_image_id = self._inspect_desktop_image_contract(
                remote, str(self.cfg["contest_server"]["docker_image"])
            )

            server = self.cfg["contest_server"]
            self._assert_gateway_port_available(
                remote,
                int(server["gateway_listen"]),
                str(server.get("gateway_bind_address", "0.0.0.0")),
            )
            seats_root = server["seats_root"]
            network = server["docker_network"]
            mode = str(contest.get("submission_mode") or "folder")
            web_enabled = mode in {"web", "both"}
            old_resources = store.seat_pool_resources(tid)
            if old_resources:
                old_names = " ".join(
                    _q(seat["container"]) for seat in old_resources
                )
                remote.run(f"docker rm -f {old_names} >/dev/null 2>&1 || true")
            store.reset_seats(tid)
            store.delete_seat_pool(tid)
            pool = SeatPoolState.create(
                tid,
                max_participants=target_main,
                spare_count=target_spares,
                begin_at_ms=int(contest.get("begin_at_ms") or 0),
                release_lead_ms=int(contest.get("release_lead_minutes") or 5)
                * 60
                * 1000,
            )
            self._save_pool_state(tid, None, pool)

            store.set_state(tid, "preparing", "检查隔离网络")
            remote.run(
                f"mkdir -p {_q(f'{seats_root}/{tid}')} && "
                f"if docker network inspect {_q(network)} >/dev/null 2>&1; then "
                f"test \"$(docker network inspect -f '{{{{.Internal}}}}' {_q(network)})\" = true && "
                f"test \"$(docker network inspect -f '{{{{index .Options \"com.docker.network.bridge.enable_icc\"}}}}' {_q(network)})\" = false; "
                "else docker network create --internal "
                "--opt com.docker.network.bridge.enable_icc=false "
                f"{_q(network)}; fi"
            )
            network_gateway = remote.run(
                "docker network inspect -f "
                f"'{{{{(index .IPAM.Config 0).Gateway}}}}' {_q(network)}"
            ).strip()
            if not network_gateway:
                raise RuntimeError("隔离网络未取得宿主机网关地址")

            store.set_state(tid, "preparing", "下发只读试题 PDF")
            remote_materials = f"{seats_root}/{tid}/materials"
            remote_paper = f"{remote_materials}/paper.pdf"
            remote.run(f"mkdir -p {_q(remote_materials)}")
            self._put_remote_verified_file(
                remote,
                paper_local,
                remote_paper,
                str(contest["paper_sha256"]),
            )
            remote_testdata = f"{seats_root}/{tid}/testdata"
            if testdata_local:
                store.set_state(tid, "preparing", "下发只读自测数据")
                remote_testdata_archive = f"{remote_materials}/testdata.tar.gz"
                self._put_remote_verified_file(
                    remote,
                    testdata_local,
                    remote_testdata_archive,
                    str(contest["testdata_sha256"]),
                )
                remote.run(
                    f"if [ -e {_q(remote_testdata)} ]; then "
                    f"chmod -R u+w -- {_q(remote_testdata)}; fi && "
                    f"rm -rf -- {_q(remote_testdata)} && "
                    f"mkdir -p {_q(remote_testdata)} && "
                    # The normalized archive intentionally stores directories as
                    # 0555.  GNU tar applies those directory modes before later
                    # members unless restoration is delayed, which prevents the
                    # unprivileged contest SSH user from creating the files.
                    f"tar --delay-directory-restore -xzf {_q(remote_testdata_archive)} "
                    f"-C {_q(remote_testdata)} && "
                    f"find {_q(remote_testdata)} -type d -exec chmod 0555 {{}} + && "
                    f"find {_q(remote_testdata)} -type f -exec chmod 0444 {{}} + && "
                    f"rm -f {_q(remote_testdata_archive)}"
                )

            public_base = str(
                self.cfg["orchestrator"].get("public_base_url", "")
            ).rstrip("/")
            parsed_public = urlparse(public_base)
            if web_enabled and (
                parsed_public.scheme != "https"
                or not parsed_public.hostname
                or parsed_public.path not in {"", "/"}
            ):
                raise RuntimeError("网页提交模式要求有效的 HTTPS public_base_url")
            submit_proxy_port = int(server.get("submit_proxy_port", 18082))
            memory_swap = server.get("memory_swap")
            memory_swap_arg = (
                f"--memory-swap {_q(memory_swap)} " if memory_swap else ""
            )
            testdata_mount = (
                f"-v {_q(remote_testdata + ':/home/student/测试数据:ro')} "
                if testdata_local
                else ""
            )

            total_slots = len(pool.seats)
            store.set_state(
                tid,
                "preparing",
                f"预热并验收 {total_slots} 个座位（含 {pool.spare_count} 个备用）",
            )
            locations = []
            material_digest = self._material_digest(contest)
            for planned in pool.seats:
                slot_no = int(planned.slot_no)
                previous = pool.revision
                warming = pool.mark_warming(
                    slot_no,
                    now_ms=int(time.time() * 1000),
                    command_id=f"warm:{tid}:{slot_no}",
                    expected_revision=previous,
                )
                pool = self._save_pool_state(tid, previous, warming.state)
                token = secrets.token_urlsafe(12)
                submit_token = secrets.token_urlsafe(24)
                vnc_pass = rand_password()
                name = f"seat-{tid[:8]}-slot-{slot_no:03d}"
                home = f"{seats_root}/{tid}/slots/{slot_no:03d}"
                candidate = f"CSP{slot_no:03d}"
                answer_paths = [f"{home}/answers/{candidate}"] + [
                    f"{home}/answers/{candidate}/{problem}" for problem in files
                ]
                remote.run(
                    "mkdir -p "
                    + " ".join(_q(path) for path in answer_paths)
                )
                web_submit_url = (
                    f"http://{network_gateway}:{submit_proxy_port}/submit/{submit_token}"
                    if web_enabled
                    else ""
                )
                remote.run(
                    "docker run -d --restart unless-stopped "
                    f"--network {_q(network)} --name {_q(name)} --hostname noilinux "
                    f"--memory {_q(server['memory'])} --cpus {_q(server['cpus'])} "
                    f"{memory_swap_arg}"
                    f"--pids-limit {_q(server['pids_limit'])} --shm-size {_q(server['shm_size'])} "
                    f"--label {_q('noi.contest=' + tid)} "
                    f"--label {_q('noi.slot=' + str(slot_no))} "
                    f"-e STUDENT_PASSWORD={_q(vnc_pass)} -e VNC_PASSWORD={_q(vnc_pass)} "
                    f"-e CANDIDATE_ID={_q(candidate)} "
                    f"-e PROBLEM_NAMES={_q(','.join(files))} "
                    f"-e SUBMISSION_MODE={_q(mode)} "
                    f"-e WEB_SUBMIT_URL={_q(web_submit_url)} "
                    f"-e HAS_TEST_DATA={_q('1' if testdata_local else '0')} "
                    f"-e RESOLUTION={_q(server.get('resolution', '1366x768'))} "
                    f"-e FRAME_RATE={_q(server.get('frame_rate', 30))} "
                    f"-v {_q(home + '/answers:/home/student/答案')} "
                    f"-v {_q(remote_materials + ':/home/student/试题:ro')} "
                    f"{testdata_mount}"
                    f"{_q(expected_image_id)}"
                )
                actual_image_id = _remote_readonly(
                    remote,
                    "docker inspect -f '{{.Image}}' " f"{_q(name)}"
                ).strip()
                if actual_image_id != expected_image_id:
                    raise RuntimeError(f"容器 {name} 使用了非预期镜像")
                cip = _remote_readonly(
                    remote,
                    "docker inspect -f "
                    f"'{{{{with index .NetworkSettings.Networks \"{network}\"}}}}{{{{.IPAddress}}}}{{{{end}}}}' "
                    f"{_q(name)}"
                ).strip()
                if not cip:
                    raise RuntimeError(f"容器 {name} 未取得内网 IP")
                remote.run(
                    _seat_readiness_command(
                        name=name,
                        cip=cip,
                        paper_sha256=str(contest["paper_sha256"]),
                        candidate=candidate,
                        problems=files,
                        testdata_files=(
                            int(contest["testdata_files"])
                            if testdata_local
                            else None
                        ),
                    ),
                    timeout=SEAT_READINESS_TIMEOUT_SECONDS,
                )
                store.put_seat_pool_resource(
                    tid,
                    slot_no,
                    token=token,
                    vnc_pass=vnc_pass,
                    submit_token=submit_token,
                    candidate=candidate,
                    container=name,
                    cip=cip,
                    image_digest=expected_image_id,
                    material_digest=material_digest,
                )
                previous = pool.revision
                verified = pool.mark_verified(
                    slot_no,
                    container_ref=name,
                    image_digest=expected_image_id,
                    material_digest=material_digest,
                    now_ms=int(time.time() * 1000),
                    command_id=f"verify:{tid}:{slot_no}",
                    expected_revision=previous,
                )
                pool = self._save_pool_state(tid, previous, verified.state)
                if slot_no == 1:
                    store.set_state(
                        tid,
                        "preparing",
                        "教师测试座位已通过桌面、材料和程序回收检查；继续准备其余座位",
                    )
                locations.append(
                    NGINX_LOCATION.format(
                        token=token,
                        cip=cip,
                        novnc_path=novnc_path(
                            token,
                            int(server.get("no_vnc_quality", 9)),
                            int(server.get("no_vnc_compression", 2)),
                        ),
                    )
                )

            store.set_state(tid, "preparing", "下发 nginx 座位网关配置")
            conf = NGINX_CONF.format(
                tid=tid,
                listen=(
                    f"{server.get('gateway_bind_address', '0.0.0.0')}:"
                    f"{int(server['gateway_listen'])}"
                ),
                locations="".join(locations),
            )
            if web_enabled:
                conf += SUBMIT_PROXY_CONF.format(
                    gateway=network_gateway,
                    port=submit_proxy_port,
                    origin=public_base,
                    origin_host=parsed_public.netloc,
                )
            resources = store.seat_pool_resources(tid)
            if not resources:
                raise RuntimeError("座位池缺少可验收的直连资源")
            remote.put_content(conf, "/tmp/noi-seats.conf")
            self._activate_pool_gateway(
                remote,
                port=int(server["gateway_listen"]),
                readiness_path=novnc_path(
                    str(resources[0]["token"]),
                    int(server.get("no_vnc_quality", 9)),
                    int(server.get("no_vnc_compression", 2)),
                ),
                bind_address=str(
                    server.get("gateway_bind_address", "0.0.0.0")
                ),
            )
            self._install_freeze_watchdog(remote, contest)
            # Direct is the low-latency primary path. Keep the existing HTTPS
            # proxy as an explicit compatibility fallback for networks that
            # reject raw HTTP/IP. Both routes share the same seat token and
            # are closed by the deadline/collection lifecycle.
            frontend_attempted = True
            self._enable_frontend(ip, int(server["gateway_listen"]))
            pool = self._reserve_roster(
                contest,
                pool,
                now_ms=int(time.time() * 1000),
            )
            assigned = sum(1 for seat in pool.seats if seat.uid is not None)
            if self._direct_access_enabled():
                store.set_state(
                    tid,
                    "preparing",
                    "运行教师测试：验证学生入口页面与远程桌面连接",
                )
                self._probe_direct_gateway(ip, str(resources[0]["token"]))
            ready_message = (
                f"教师测试通过；座位池 {len(pool.seats)} 个均已验收，"
                f"当前绑定 {assigned} 人，提前 "
                f"{int(contest['release_lead_minutes'])} 分钟自动发放"
            )
            # The SG publication and ready transition share the same lock as
            # reconciliation. A crash before ready leaves a rule that startup
            # reconciliation closes; a crash after ready re-opens it idempotently.
            with self._desktop_access_guard:
                current = store.get_contest(tid)
                if not current or str(current.get("state") or "") != "preparing":
                    raise RuntimeError(
                        "备赛状态已被教师关闭或其他流程改变，拒绝重新开放桌面"
                    )
                self._ensure_desktop_access(contest)
                store.set_state(tid, "ready", ready_message)
            return ip
        except Exception as exc:
            self.log.exception("prepare failed for %s", tid)
            store.set_state(tid, "error", str(exc))
            revocation_error: Exception | None = None
            if direct_access_claimed:
                try:
                    self._revoke_desktop_access()
                except Exception as close_exc:
                    revocation_error = close_exc
                    self.log.exception(
                        "prepare failure desktop ingress revocation failed for %s", tid
                    )
            if remote:
                try:
                    self._cleanup_failed_prepare(remote, tid)
                except Exception:
                    self.log.exception("prepare cleanup failed for %s", tid)
            if fresh_prepare_started:
                try:
                    # No submit endpoint is open while state=preparing. These
                    # are partial seats from this failed prepare attempt, not
                    # evidence from a failed collection.
                    store.reset_seats(tid)
                    store.delete_seat_pool(tid)
                except Exception:
                    self.log.exception("cannot clear partial prepare seats for %s", tid)
            if frontend_attempted or owns_server:
                try:
                    self._disable_frontend(force=True)
                except Exception:
                    self.log.exception("frontend cleanup failed for %s", tid)
            if (
                owns_server
                or revocation_error is not None
            ):
                if (
                    revocation_error is not None
                    or self.cfg["orchestrator"].get(
                        "shutdown_on_prepare_error", True
                    )
                ):
                    try:
                        self._stop_server_best_effort()
                    except Exception:
                        self.log.exception(
                            "prepare failure shutdown failed for %s", tid
                        )
            if revocation_error is not None:
                raise RuntimeError(
                    f"备赛失败，且桌面公网入口未确认撤销: {revocation_error}"
                ) from exc
            raise
        finally:
            self._release_deployment_lock(deployment_lock)
            self._leave(tid)

    def collect(self, tid: str, claimed: bool = False) -> dict:
        self._enter(tid)
        store = self.store
        failed = False
        try:
            if not claimed and not store.transition(
                tid, {"ready", "error"}, "collecting", "停止桌面并收卷"
            ):
                raise RuntimeError("当前比赛状态不允许收卷")
            contest = store.get_contest(tid)
            if not contest:
                raise RuntimeError("比赛未登记")
            # Stop accepting new desktop connections before freezing or
            # downloading anything. The OJ-specific SSH/HTTP rules are not
            # owned by this operation and therefore remain intact.
            try:
                self._revoke_desktop_access()
            except Exception:
                # A stopped VM is the final safety barrier when the cloud API
                # cannot prove that the managed rule disappeared.
                self._stop_server_best_effort()
                raise
            seats = store.seats(tid)
            if not seats:
                raise RuntimeError("座位表为空，无法收卷")

            files = json.loads(contest["files"])
            prefinalized_web: dict[tuple[int, str], dict] = {}
            desktops_frozen = False

            def finalize_realtime_web() -> None:
                if (
                    not self.cfg["hydro"].get("submit_enabled")
                    or self.realtime_judge is None
                ):
                    return
                # Web submissions live on the OJ host, not on the desktop ECS.
                store.set_state(tid, "collecting", "确认网页实时评测记录")
                for seat in seats:
                    latest_web = store.latest_web_submissions(tid, int(seat["uid"]))
                    for name in files:
                        row = latest_web.get(name)
                        key = (int(seat["uid"]), name)
                        if row and row.get("submission_id") and key not in prefinalized_web:
                            try:
                                prefinalized_web[key] = self.realtime_judge.ensure(
                                    int(row["id"])
                                )
                            except Exception:
                                # One retired account, stale pid, or transient
                                # timeout must not starve every later student.
                                # The reporting pass retries this row and records
                                # its own failure after all other rows were given
                                # a chance to reach Hydro.
                                self.log.exception(
                                    "cannot prefinalize realtime submission %s",
                                    row.get("id"),
                                )

            ip = self._ensure_server()
            remote = self._remote(ip)
            if not remote.wait_ssh(60):
                raise RuntimeError("收卷时 SSH 不可达")

            pool_resources = store.seat_pool_resources(tid)
            names = " ".join(
                _q(item["container"]) for item in (pool_resources or seats)
            )
            store.set_state(tid, "collecting", "冻结桌面")
            self._freeze_for_collection(remote, tid, names)
            desktops_frozen = True
            stored_pool = store.seat_pool(tid)
            if stored_pool:
                pool = SeatPoolState.from_dict(stored_pool["state"])
                previous = pool.revision
                frozen = pool.freeze(
                    now_ms=int(time.time() * 1000),
                    command_id=f"freeze:{tid}:{int(contest.get('end_at_ms') or 0)}",
                    expected_revision=previous,
                )
                if not frozen.replayed:
                    self._save_pool_state(tid, previous, frozen.state)
            # Drain every explicit web click first. Web submission is
            # authoritative per student and problem; the frozen answer
            # directory is used only for problems that were never submitted
            # through the web page.
            finalize_realtime_web()

            server = self.cfg["contest_server"]
            seats_root = server["seats_root"]
            remote_archive = f"/tmp/noi-collect-{tid}.tar.gz"
            store.set_state(tid, "collecting", "打包并下载提交")
            answer_glob = "slots/*/answers" if pool_resources else "*/answers"
            remote.run(
                f"cd {_q(f'{seats_root}/{tid}')} && "
                f"sudo tar czf {_q(remote_archive)} -- {answer_glob}"
            )
            run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            out_dir = (
                Path(self.cfg["orchestrator"]["collected_dir"]) / tid / run_id
            )
            out_dir.mkdir(parents=True, exist_ok=True)
            try:
                with tempfile.TemporaryDirectory() as temp_dir:
                    local_archive = Path(temp_dir) / "collect.tar.gz"
                    remote.get_file(remote_archive, str(local_archive))
                    with tarfile.open(local_archive, "r:gz") as archive:
                        safe_extract(archive, out_dir)
            finally:
                remote.run(f"sudo rm -f {_q(remote_archive)}")

            folder_report: dict = {}
            web_report: dict = {}
            report: dict = {}
            selection: dict = {}
            selected_sources: dict = {}
            selected_web_rows: dict = {}
            for seat in seats:
                uname = seat["uname"]
                uid = int(seat["uid"])
                assignment = store.seat_pool_assignment(tid, uid)
                if assignment:
                    answer_root = (
                        out_dir
                        / "slots"
                        / f"{int(assignment['slot_no']):03d}"
                        / "answers"
                    )
                elif pool_resources:
                    raise RuntimeError(f"选手 {uname} 的座位池映射丢失")
                else:
                    answer_root = out_dir / str(uid) / "answers"
                user_folder = check_answer_tree(
                    str(answer_root), seat["candidate"], files
                )
                latest_web = store.latest_web_submissions(tid, uid)
                user_web = {}
                web_dir = out_dir / str(uid) / "web"
                web_dir.mkdir(parents=True, exist_ok=True)
                for name in files:
                    submitted = latest_web.get(name)
                    if submitted:
                        code = submitted["source"]
                        issues = check_code(code, name)
                        (web_dir / f"{name}.cpp").write_text(
                            code, encoding="utf-8"
                        )
                        user_web[name] = {
                            "status": "ok" if not issues else "rule_violation",
                            "file": f"{name}.cpp",
                            "issues": issues,
                            "sha256": submitted["sha256"],
                            "size": submitted["size"],
                            "submitted_at": submitted["created_at"],
                        }
                    else:
                        user_web[name] = {
                            "status": "missing",
                            "file": "",
                            "issues": [f"网页未提交 {name}.cpp（0 分）"],
                        }

                user_report: dict = {}
                user_selection: dict = {}
                user_sources: dict = {}
                user_web_rows: dict = {}
                for name in files:
                    item = dict(user_folder[name])
                    source = ""
                    if item["file"]:
                        source = (answer_root / item["file"]).read_text(
                            encoding="utf-8-sig", errors="replace"
                        )
                        encoded = source.encode("utf-8")
                        item["sha256"] = hashlib.sha256(encoded).hexdigest()
                        item["size"] = len(encoded)
                    latest = latest_web.get(name)
                    web_selected = bool(latest)
                    if web_selected:
                        # Beijing-style web collection is authoritative per
                        # student and per problem. Once a problem has a web
                        # submission, a later folder snapshot must never
                        # overwrite it at the deadline.
                        source = str(latest.get("source") or "")
                        web_item = user_web[name]
                        item["status"] = web_item["status"]
                        item["file"] = web_item["file"]
                        item["issues"] = list(web_item["issues"])
                        item["sha256"] = str(web_item.get("sha256") or "")
                        item["size"] = int(web_item.get("size") or 0)
                    source_name = "web_submit" if web_selected else "deadline_snapshot"
                    item["submission_source"] = source_name
                    item["reuses_confirmed_submission"] = web_selected
                    user_report[name] = item
                    user_selection[name] = source_name
                    user_sources[name] = source
                    user_web_rows[name] = latest if web_selected else None
                folder_report[uname] = user_folder
                web_report[uname] = user_web
                report[uname] = user_report
                selection[uname] = user_selection
                selected_sources[uname] = user_sources
                selected_web_rows[uname] = user_web_rows

            report_digests: dict[str, str] = {}
            for filename, payload in (
                ("folder_report.json", folder_report),
                ("web_report.json", web_report),
                ("selection.json", selection),
                ("report.json", report),
            ):
                report_digests[filename] = _write_durable_json(
                    out_dir / filename, payload
                )

            pid_map = json.loads(contest.get("pids") or "{}")
            submit_log: dict = {}
            submit_failures = 0
            if self.cfg["hydro"].get("submit_enabled"):
                submitter = HydroSubmitter(
                    self.cfg["hydro"]["internal_base_url"],
                    self.cfg["hydro"]["orchestrator_token"],
                    self.cfg["hydro"].get("submit_lang", ""),
                )
                for seat in seats:
                    user_log = {}
                    user_report = report[seat["uname"]]
                    for name, item in user_report.items():
                        pid = pid_map.get(name)
                        if not pid:
                            user_log[name] = {"ok": False, "error": "未配置 Hydro pid"}
                            submit_failures += 1
                            continue
                        web_row = selected_web_rows[seat["uname"]][name]
                        if (
                            web_row
                            and web_row.get("submission_id")
                            and self.realtime_judge is not None
                        ):
                            try:
                                delivered = prefinalized_web.get(
                                    (int(seat["uid"]), name)
                                ) or self.realtime_judge.ensure(
                                    int(web_row["id"])
                                )
                                delivered_rid = str(delivered.get("rid") or "")
                                if not _TID.fullmatch(delivered_rid):
                                    raise RuntimeError("OJ 未返回有效递交编号")
                                user_log[name] = {
                                    "ok": True,
                                    "rid": delivered_rid,
                                    "reused_realtime": True,
                                    "enforced_zero": bool(
                                        json.loads(
                                            delivered.get("judge_issues") or "[]"
                                        )
                                    ),
                                    "issues": json.loads(
                                        delivered.get("judge_issues") or "[]"
                                    ),
                                }
                            except Exception as exc:
                                self.log.exception(
                                    "cannot finalize realtime submission %s",
                                    web_row.get("id"),
                                )
                                user_log[name] = {
                                    "ok": False,
                                    "error": str(exc),
                                    "reused_realtime": True,
                                }
                                submit_failures += 1
                            continue
                        code = selected_sources[seat["uname"]][name]
                        if not code:
                            code = "// required source file was not collected\n"
                        enforced_zero = item["status"] != "ok"
                        if enforced_zero:
                            code = force_zero_code(code, item["issues"])
                        submission_id = submitter.submission_id(
                            contest["submission_session"], tid, seat["uid"], pid
                        )
                        result = submitter.submit_one(
                            tid, seat["uid"], pid, code, submission_id
                        )
                        result["enforced_zero"] = enforced_zero
                        result["issues"] = item["issues"]
                        if result.get("ok") and not _TID.fullmatch(
                            str(result.get("rid") or "")
                        ):
                            result = {
                                **result,
                                "ok": False,
                                "error": "OJ 未返回有效递交编号",
                            }
                        user_log[name] = result
                        if not result.get("ok"):
                            submit_failures += 1
                    submit_log[seat["uname"]] = user_log
            else:
                submit_log["_status"] = "Hydro 回传已禁用"
            report_digests["submit_log.json"] = _write_durable_json(
                out_dir / "submit_log.json", submit_log
            )

            message = f"已收卷 {len(seats)} 人；报告位于 {out_dir}"
            if submit_failures:
                message += f"；Hydro 回传失败 {submit_failures} 项，请查 submit_log.json 后重试收卷"
                raise RuntimeError(message)
            archive_manifest = _archive_tree_manifest(
                out_dir,
                excluded={"archive_manifest.json", "collection_receipt.json"},
            )
            report_digests["archive_manifest.json"] = _write_durable_json(
                out_dir / "archive_manifest.json", archive_manifest
            )
            # Keep the paused containers and their bind-mounted answer trees
            # intact during the post-deadline delivery window.  The receipt is
            # durable before the state changes, so a process restart can prove
            # what was collected without inventing a successful shutdown.
            completed_at_ms = int(time.time() * 1000)
            grace_minutes = int(
                self.cfg["orchestrator"].get("shutdown_grace_minutes", 30)
            )
            shutdown_after_ms = max(
                completed_at_ms,
                int(contest.get("end_at_ms") or completed_at_ms)
                + grace_minutes * 60 * 1000,
            )
            receipt = {
                "schema_version": 1,
                "tid": tid,
                "run_id": run_id,
                "completed_at_ms": completed_at_ms,
                "cutoff_at_ms": int(contest.get("end_at_ms") or 0),
                "shutdown_after_ms": shutdown_after_ms,
                "seat_count": len(seats),
                "problem_count": len(files),
                "submit_failures": 0,
                "files": report_digests,
            }
            receipt_digest = _write_durable_json(
                out_dir / "collection_receipt.json", receipt
            )
            wait_message = (
                f"已回收并送达 {len(seats)} 人；答案已冻结，"
                f"等待截止后 {grace_minutes} 分钟安全关机"
            )
            if not store.enter_safe_wait(
                tid,
                run_id=run_id,
                collection_dir=str(out_dir.resolve()),
                receipt_sha256=receipt_digest,
                completed_at_ms=completed_at_ms,
                shutdown_after_ms=shutdown_after_ms,
                message=wait_message,
            ):
                raise RuntimeError("回收凭据已生成，但安全等待状态提交失败")
            return report
        except Exception as exc:
            failed = True
            if not locals().get("desktops_frozen", False):
                try:
                    self._stop_server_best_effort()
                except Exception:
                    self.log.exception(
                        "cannot freeze server after collect error for %s",
                        tid,
                    )
            if "finalize_realtime_web" in locals():
                try:
                    finalize_realtime_web()
                except Exception:
                    self.log.exception(
                        "web finalization also failed after collect error for %s", tid
                    )
            self.log.exception("collect failed for %s", tid)
            store.set_state(tid, "error", str(exc))
            raise
        finally:
            direct_close_error: Exception | None = None
            try:
                self._revoke_desktop_access()
            except Exception as exc:
                direct_close_error = exc
                self.log.exception("desktop ingress shutdown failed for %s", tid)
            try:
                self._disable_frontend(force=True)
            except Exception:
                self.log.exception("frontend shutdown failed for %s", tid)
            if direct_close_error is not None:
                try:
                    self._stop_server_best_effort()
                except Exception:
                    self.log.exception("automatic shutdown failed for %s", tid)
            self._leave(tid)
            if direct_close_error is not None and not failed:
                store.set_state(
                    tid,
                    "error",
                    "收卷完成，但桌面公网入口未确认撤销",
                )
                raise RuntimeError(
                    "桌面公网入口未确认撤销"
                ) from direct_close_error

    def _verify_collection_receipt(self, contest: dict) -> dict:
        tid = str(contest.get("tid") or "")
        root = Path(self.cfg["orchestrator"]["collected_dir"]).resolve()
        contest_root = (root / tid).resolve()
        configured_directory = Path(str(contest.get("collection_dir") or ""))
        if configured_directory.is_symlink():
            raise RuntimeError("回收目录不能是符号链接")
        directory = configured_directory.resolve()
        try:
            directory.relative_to(contest_root)
        except ValueError as exc:
            raise RuntimeError("回收目录超出本场归档根目录") from exc
        if not directory.is_dir():
            raise RuntimeError("回收目录不存在或不是可信目录")
        receipt_path = directory / "collection_receipt.json"
        if not receipt_path.is_file() or receipt_path.is_symlink():
            raise RuntimeError("回收凭据不存在或类型异常")
        expected_digest = str(contest.get("collection_receipt_sha256") or "")
        if sha256_file(receipt_path) != expected_digest:
            raise RuntimeError("回收凭据摘要不匹配")
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("回收凭据无法读取") from exc
        expected_files = {
            "archive_manifest.json",
            "folder_report.json",
            "web_report.json",
            "selection.json",
            "report.json",
            "submit_log.json",
        }
        files = receipt.get("files") if isinstance(receipt, dict) else None
        if (
            receipt.get("schema_version") != 1
            or str(receipt.get("tid") or "") != tid
            or str(receipt.get("run_id") or "")
            != str(contest.get("collection_run_id") or "")
            or int(receipt.get("submit_failures", -1)) != 0
            or not isinstance(files, dict)
            or set(files) != expected_files
        ):
            raise RuntimeError("回收凭据语义不完整")
        for name in sorted(expected_files):
            path = directory / name
            if (
                not path.is_file()
                or path.is_symlink()
                or sha256_file(path) != str(files[name])
            ):
                raise RuntimeError(f"回收证据文件校验失败: {name}")
        manifest_path = directory / "archive_manifest.json"
        try:
            persisted_manifest = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("回收文件清单无法读取") from exc
        current_manifest = _archive_tree_manifest(
            directory,
            excluded={"archive_manifest.json", "collection_receipt.json"},
        )
        if persisted_manifest != current_manifest:
            raise RuntimeError("回收文件树与冻结清单不一致")
        return receipt

    def finish_safe_wait(self, tid: str, claimed: bool = False) -> dict:
        """Verify delivery evidence and stop the dedicated VM after the grace."""
        del claimed
        self._enter(tid)
        store = self.store
        try:
            contest = store.get_contest(tid)
            if not contest or str(contest.get("state") or "") != "safe_wait":
                raise RuntimeError("比赛未处于安全等待状态")
            now_ms = int(time.time() * 1000)
            shutdown_after_ms = int(contest.get("shutdown_after_ms") or 0)
            if now_ms < shutdown_after_ms:
                return {
                    "ended": False,
                    "reason": "grace_period",
                    "shutdown_after_ms": shutdown_after_ms,
                }
            self._verify_collection_receipt(contest)
            delivery = store.contest_delivery_health(tid)
            if not delivery["safe"]:
                store.set_state(
                    tid,
                    "safe_wait",
                    "已过安全关机时间，但仍有代码未确认送达；入口保持关闭并继续等待",
                )
                return {"ended": False, "reason": "delivery_pending", **delivery}

            self._revoke_desktop_access()
            self._disable_frontend(force=True)
            state, ip = self.cvm.status()
            if str(state).upper() == "RUNNING":
                remote = self._remote(str(ip))
                if not remote.wait_ssh(30):
                    raise RuntimeError("安全关机前比赛服务器不可达")
                resources = store.seat_pool_resources(tid)
                seats = store.seats(tid)
                names = " ".join(
                    _q(item["container"]) for item in (resources or seats)
                )
                if names:
                    remote.run(
                        f"docker update --restart=no {names} >/dev/null; "
                        f"docker rm -f {names} >/dev/null"
                    )
                self._remove_freeze_watchdog(remote, tid)
                self._remove_gateway(remote, tid)

            stored_pool = store.seat_pool(tid)
            if stored_pool:
                pool = SeatPoolState.from_dict(stored_pool["state"])
                previous = pool.revision
                collected = pool.collect(
                    now_ms=now_ms,
                    command_id=f"collect:{tid}:{int(contest.get('end_at_ms') or 0)}",
                    expected_revision=previous,
                )
                if not collected.replayed:
                    self._save_pool_state(tid, previous, collected.state)

            self._stop_server_best_effort()
            final_state, _ = self.cvm.status()
            if str(final_state).upper() != "STOPPED":
                raise RuntimeError("比赛服务器尚未确认关机")
            if not store.mark_safe_ended(
                tid,
                observed_at_ms=int(time.time() * 1000),
                message="代码送达、回收凭据和服务器关机均已确认",
            ):
                raise RuntimeError("安全结束状态提交失败")
            return {"ended": True, "delivery": delivery}
        except Exception as exc:
            self.log.exception("safe-wait shutdown failed for %s", tid)
            contest = store.get_contest(tid)
            if contest and str(contest.get("state") or "") == "safe_wait":
                store.set_state(
                    tid,
                    "safe_wait",
                    f"安全关机尚未完成，入口保持关闭并将重试：{type(exc).__name__}",
                )
            raise
        finally:
            self._leave(tid)
