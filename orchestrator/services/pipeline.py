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
from .seat_pool import SeatPoolState, TeacherApprovalRequiredError
from .static_check import check_answer_tree, check_code, force_zero_code

_TID = re.compile(r"^[0-9a-fA-F]{24}$")
DESKTOP_IMAGE_CONTRACT_LABEL = "org.noi.desktop.contract"
DESKTOP_IMAGE_CONTRACT = "finalizer-status-v1"

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
    listen {port} default_server;
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
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 60s;
        client_max_body_size 256k;
    }}
    location / {{ return 404; }}
}}
"""


def _q(value) -> str:
    return shlex.quote(str(value))


def novnc_path(token: str, quality: int, compression: int) -> str:
    """Return the one canonical student noVNC path used by UI and nginx."""
    return (
        f"/s/{token}/vnc.html?path=s/{token}/websockify"
        f"&autoconnect=true&resize=scale&quality={quality}"
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
        teacher_approved: bool = False,
    ) -> SeatPoolState:
        """Bind every new Hydro participant to a pre-verified seat."""
        roster = self.hydro.roster(str(contest["tid"]))
        for participant in roster:
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
                teacher_approved=teacher_approved,
                command_id=f"reserve:{contest['tid']}:{uid}",
                expected_revision=previous,
            )
            pool = self._save_pool_state(str(contest["tid"]), previous, result.state)
            self.store.bind_pool_seat(
                str(contest["tid"]), uid, uname, int(result.value["slot_no"])
            )
        return pool

    def sync_roster(self, tid: str, *, teacher_approved: bool = False) -> dict:
        """Assign late Hydro participants without rebuilding verified desktops."""
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
                teacher_approved=teacher_approved,
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
                "roster": len(self.hydro.roster(tid)),
                "assigned": sum(1 for seat in pool.seats if seat.uid is not None),
                "released": released_uids,
                "counts": pool.state_counts(),
                "release_at_ms": pool.release_at_ms,
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
        actual_image_id = remote.run(
            "docker inspect -f '{{.Image}}' " + _q(name)
        ).strip()
        if actual_image_id != context["image_digest"]:
            raise RuntimeError(f"容器 {name} 使用了非预期镜像")
        cip = remote.run(
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
        bundle_dir = "/run/contest-materials"
        paper_check = (
            "test \"$(sha256sum /home/student/试题/paper.pdf | awk '{print $1}')\" = "
            + _q(contest["paper_sha256"])
        )
        checks = [
            f"docker exec {_q(name)} grep -Fqx ready /home/student/.contest-finalizer-status",
            f"docker exec {_q(name)} test -L /home/student/比赛资料（从这里开始）",
            f"docker exec {_q(name)} test -L /home/student/Desktop/比赛资料（从这里开始）",
            f"docker exec {_q(name)} grep -Fqx schema=2 {bundle_dir}/.manifest",
            f"docker exec {_q(name)} test -r {bundle_dir}/00_请先看.txt",
            f"docker exec {_q(name)} test -r {bundle_dir}/01_试题.pdf",
            f"docker exec {_q(name)} sh -lc {_q(paper_check)}",
            f"docker exec -u student {_q(name)} test ! -w /home/student/试题/paper.pdf",
            f"docker exec -u student {_q(name)} test -w {bundle_dir}/03_答案文件夹（自动回收）",
        ]
        for problem in files:
            checks.append(
                f"docker exec -u student {_q(name)} test -w "
                + _q(f"/home/student/答案/{candidate}/{problem}")
            )
        if has_testdata:
            checks.extend(
                [
                    f"docker exec {_q(name)} test -d {bundle_dir}/02_测试数据",
                    f"docker exec {_q(name)} sh -lc "
                    + _q(
                        "test \"$(find /home/student/测试数据 -type f | wc -l)\" "
                        f"-eq {int(contest['testdata_files'])}"
                    ),
                    f"docker exec -u student {_q(name)} test ! -w /home/student/测试数据",
                ]
            )
        if context["web_enabled"]:
            checks.append(
                f"docker exec {_q(name)} test -r {bundle_dir}/04_CSP程序回收系统.html"
            )
        bundle_check = " && ".join(checks)
        remote.run(
            "ok=0; for _ in $(seq 1 45); do "
            f"if ! docker exec {_q(name)} pgrep -f gnome-initial-setup >/dev/null 2>&1 && "
            f"docker exec {_q(name)} pgrep -x systemd-logind >/dev/null 2>&1 && "
            f"docker exec {_q(name)} pgrep -f '/usr/libexec/gnome-session-binary' >/dev/null 2>&1 && "
            f"docker exec {_q(name)} pgrep -x gnome-shell >/dev/null 2>&1 && "
            f"{bundle_check} && curl -fsS --max-time 3 http://{_q(cip)}:6080/vnc.html "
            ">/dev/null 2>&1; then ok=$((ok + 1)); else ok=0; fi; "
            "if [ \"$ok\" -ge 3 ]; then exit 0; fi; sleep 2; done; "
            f"docker logs --tail 120 {_q(name)} >&2; exit 1",
            timeout=120,
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
            port=int(server["gateway_listen"]),
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
        teacher_approved: bool,
    ) -> dict:
        """Safely provision only newly appended slots and atomically publish them."""
        if not teacher_approved:
            raise TeacherApprovalRequiredError("现场扩容必须由教师明确确认")
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
                teacher_approved=True,
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
                f"现场扩容完成：新增 {len(new_resources)} 个座位，"
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
                    tid, "ready", f"现场扩容失败，原座位池保持运行：{exc}"
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
            self.store.set_state(
                tid,
                "ready",
                f"故障座位 {int(slot_no):03d} 已隔离"
                + (
                    f"，学生已切换到备用座位 {int(replacement['slot_no']):03d}"
                    if replacement
                    else ""
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
        self._remove_freeze_watchdog(remote, tid)
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
            f"sudo systemctl enable --now {_q(unit + '.timer')} >/dev/null && "
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
                and contest["state"] in {"preparing", "ready", "collecting"}
            ]
            if active:
                raise RuntimeError(f"已有活动比赛 {active[0]['tid']}，暂不支持重叠办赛")
            # Once overlap has been ruled out, stale direct rules belong to an
            # interrupted earlier run and must be closed before any new work.
            direct_access_claimed = True
            store.set_state(tid, "preparing", "收回旧桌面公网入口")
            self._revoke_desktop_access()
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
            material_ready = str(contest.get("material_state") or "") == "approved"
            if (
                not material_ready
                and str(contest.get("materials_mode") or "manual") == "manual"
                and contest.get("paper_sha256")
            ):
                material_ready = True
            if not material_ready:
                raise RuntimeError("备赛材料尚未由教师批准并冻结")
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

            materials_dir = Path(
                self.cfg["orchestrator"].get("materials_dir", "/app/data/materials")
            )
            active_revision = str(contest.get("active_material_revision") or "")
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
            if contest.get("testdata_sha256"):
                if testdata_local is None or not testdata_local.is_file():
                    raise RuntimeError("测试数据归档文件丢失")
                if sha256_file(testdata_local) != contest["testdata_sha256"]:
                    raise RuntimeError("测试数据哈希与登记记录不一致")

            server = self.cfg["contest_server"]
            seats_root = server["seats_root"]
            network = server["docker_network"]
            files = json.loads(contest["files"])
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
                max_participants=int(contest.get("max_participants") or 15),
                spare_count=int(contest.get("spare_seats") or 0),
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
            remote.put_file(str(paper_local), remote_paper)
            remote_digest = remote.run(
                f"sha256sum {_q(remote_paper)} | awk '{{print $1}}'"
            ).strip()
            if remote_digest != contest["paper_sha256"]:
                raise RuntimeError("试题 PDF 上传后哈希校验失败")
            remote.run(f"chmod 0444 {_q(remote_paper)}")
            remote_testdata = f"{seats_root}/{tid}/testdata"
            if testdata_local:
                store.set_state(tid, "preparing", "下发只读自测数据")
                remote_testdata_archive = f"{remote_materials}/testdata.tar.gz"
                remote.put_file(str(testdata_local), remote_testdata_archive)
                remote_digest = remote.run(
                    f"sha256sum {_q(remote_testdata_archive)} | awk '{{print $1}}'"
                ).strip()
                if remote_digest != contest["testdata_sha256"]:
                    raise RuntimeError("测试数据上传后哈希校验失败")
                remote.run(
                    f"rm -rf {_q(remote_testdata)} && mkdir -p {_q(remote_testdata)} && "
                    f"tar -xzf {_q(remote_testdata_archive)} -C {_q(remote_testdata)} && "
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
                actual_image_id = remote.run(
                    "docker inspect -f '{{.Image}}' " f"{_q(name)}"
                ).strip()
                if actual_image_id != expected_image_id:
                    raise RuntimeError(f"容器 {name} 使用了非预期镜像")
                cip = remote.run(
                    "docker inspect -f "
                    f"'{{{{with index .NetworkSettings.Networks \"{network}\"}}}}{{{{.IPAddress}}}}{{{{end}}}}' "
                    f"{_q(name)}"
                ).strip()
                if not cip:
                    raise RuntimeError(f"容器 {name} 未取得内网 IP")
                bundle_dir = "/run/contest-materials"
                paper_check = (
                    "test \"$(sha256sum /home/student/试题/paper.pdf "
                    "| awk '{print $1}')\" = "
                    + _q(contest["paper_sha256"])
                )
                bundle_checks = [
                    f"docker exec {_q(name)} grep -Fqx ready "
                    f"{_q('/home/student/.contest-finalizer-status')}",
                    f"docker exec {_q(name)} test -L "
                    f"{_q('/home/student/比赛资料（从这里开始）')}",
                    f"docker exec {_q(name)} test -L "
                    f"{_q('/home/student/Desktop/比赛资料（从这里开始）')}",
                    f"docker exec {_q(name)} grep -Fqx schema=2 "
                    f"{_q(bundle_dir + '/.manifest')}",
                    f"docker exec {_q(name)} test -r "
                    f"{_q(bundle_dir + '/00_请先看.txt')}",
                    f"docker exec {_q(name)} test -r "
                    f"{_q(bundle_dir + '/01_试题.pdf')}",
                    f"docker exec {_q(name)} sh -lc {_q(paper_check)}",
                    f"docker exec -u student {_q(name)} test ! -w "
                    f"{_q('/home/student/试题/paper.pdf')}",
                    f"docker exec -u student {_q(name)} test -w "
                    f"{_q(bundle_dir + '/03_答案文件夹（自动回收）')}",
                ]
                for problem in files:
                    bundle_checks.append(
                        f"docker exec -u student {_q(name)} test -w "
                        f"{_q('/home/student/答案/' + candidate + '/' + problem)}"
                    )
                if testdata_local:
                    testdata_check = (
                        "test \"$(find /home/student/测试数据 -type f | wc -l)\" "
                        f"-eq {int(contest['testdata_files'])}"
                    )
                    bundle_checks.append(
                        f"docker exec {_q(name)} test -d "
                        f"{_q(bundle_dir + '/02_测试数据')}"
                    )
                    bundle_checks.append(
                        f"docker exec {_q(name)} sh -lc {_q(testdata_check)}"
                    )
                    bundle_checks.append(
                        f"docker exec -u student {_q(name)} test ! -w "
                        f"{_q('/home/student/测试数据')}"
                    )
                if web_enabled:
                    bundle_checks.append(
                        f"docker exec {_q(name)} test -r "
                        f"{_q(bundle_dir + '/04_CSP程序回收系统.html')}"
                    )
                bundle_check = " && ".join(bundle_checks)
                remote.run(
                    "ok=0; for _ in $(seq 1 45); do "
                    f"if ! docker exec {_q(name)} pgrep -f gnome-initial-setup >/dev/null 2>&1 && "
                    f"docker exec {_q(name)} pgrep -x systemd-logind >/dev/null 2>&1 && "
                    f"docker exec {_q(name)} pgrep -f '/usr/libexec/gnome-session-binary' >/dev/null 2>&1 && "
                    f"docker exec {_q(name)} pgrep -x gnome-shell >/dev/null 2>&1 && "
                    f"{bundle_check} && "
                    f"curl -fsS --max-time 3 http://{_q(cip)}:6080/vnc.html >/dev/null 2>&1; "
                    "then ok=$((ok + 1)); else ok=0; fi; "
                    "if [ \"$ok\" -ge 3 ]; then exit 0; fi; sleep 2; done; "
                    f"docker logs --tail 120 {_q(name)} >&2; exit 1",
                    timeout=120,
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
                port=int(server["gateway_listen"]),
                locations="".join(locations),
            )
            if web_enabled:
                conf += SUBMIT_PROXY_CONF.format(
                    gateway=network_gateway,
                    port=submit_proxy_port,
                    origin=public_base,
                    origin_host=parsed_public.netloc,
                )
            remote.put_content(conf, "/tmp/noi-seats.conf")
            remote.run(
                "sudo install -m 0644 /tmp/noi-seats.conf /etc/nginx/conf.d/noi-seats.conf && "
                "sudo nginx -t && sudo systemctl reload-or-restart nginx"
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
            resources = store.seat_pool_resources(tid)
            if not resources:
                raise RuntimeError("座位池缺少可验收的直连资源")
            if self._direct_access_enabled():
                store.set_state(tid, "preparing", "验证直连页面与 WebSocket")
                self._probe_direct_gateway(ip, str(resources[0]["token"]))
            ready_message = (
                f"服务器 {ip}；座位池 {len(pool.seats)} 个均已验收，"
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
        should_shutdown = self.cfg["orchestrator"].get(
            "auto_shutdown_after_collect", True
        )
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
            mode = str(contest.get("submission_mode") or "folder")
            prefinalized_web: dict[tuple[int, str], dict] = {}
            desktops_frozen = False

            def finalize_realtime_web() -> None:
                if (
                    mode not in {"web", "both"}
                    or not self.cfg["hydro"].get("submit_enabled")
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

            if mode == "web":
                # Pure web mode has no mutable folder fallback. Finalize before
                # touching the cloud server so SSH failure cannot affect OJ.
                finalize_realtime_web()

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
            if mode == "both":
                # Folder fallbacks remain mutable until the desktops stop, so
                # never wait for Hydro before freezing a dual-track contest.
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

                user_report = {}
                user_selection = {}
                user_sources = {}
                user_web_rows = {}
                for name in files:
                    use_web = mode == "web" or (
                        mode == "both" and name in latest_web
                    )
                    if use_web:
                        item = dict(user_web[name])
                        source = latest_web[name]["source"] if name in latest_web else ""
                        source_name = "web"
                    else:
                        item = dict(user_folder[name])
                        source_name = "folder"
                        source = ""
                        if item["file"]:
                            source = (answer_root / item["file"]).read_text(
                                encoding="utf-8-sig", errors="replace"
                            )
                    item["submission_source"] = source_name
                    user_report[name] = item
                    user_selection[name] = source_name
                    user_sources[name] = source
                    user_web_rows[name] = latest_web.get(name) if use_web else None
                folder_report[uname] = user_folder
                web_report[uname] = user_web
                report[uname] = user_report
                selection[uname] = user_selection
                selected_sources[uname] = user_sources
                selected_web_rows[uname] = user_web_rows

            for filename, payload in (
                ("folder_report.json", folder_report),
                ("web_report.json", web_report),
                ("selection.json", selection),
                ("report.json", report),
            ):
                (out_dir / filename).write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
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
                        if (
                            item.get("submission_source") == "web"
                            and item.get("status") == "missing"
                        ):
                            user_log[name] = {
                                "ok": True,
                                "skipped": True,
                                "reason": "网页未提交，不创建覆盖成绩的占位记录",
                            }
                            continue
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
                                user_log[name] = {
                                    "ok": True,
                                    "rid": str(delivered["rid"]),
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
                        user_log[name] = result
                        if not result.get("ok"):
                            submit_failures += 1
                    submit_log[seat["uname"]] = user_log
            else:
                submit_log["_status"] = "Hydro 回传已禁用"
            (out_dir / "submit_log.json").write_text(
                json.dumps(submit_log, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            message = f"已收卷 {len(seats)} 人；报告位于 {out_dir}"
            if submit_failures:
                message += f"；Hydro 回传失败 {submit_failures} 项，请查 submit_log.json 后重试收卷"
                raise RuntimeError(message)
            # Do not destroy the frozen source of truth until every selected
            # answer has reached Hydro.  A transient OJ/token failure must
            # leave the seat bind mounts available for an idempotent retry.
            store.set_state(tid, "collecting", "清理桌面容器")
            remote.run(
                f"docker unpause {names} >/dev/null 2>&1 || true; "
                f"docker rm -f {names} >/dev/null 2>&1 || true"
            )
            self._remove_freeze_watchdog(remote, tid)
            self._remove_gateway(remote, tid)
            stored_pool = store.seat_pool(tid)
            if stored_pool:
                pool = SeatPoolState.from_dict(stored_pool["state"])
                previous = pool.revision
                collected = pool.collect(
                    now_ms=int(time.time() * 1000),
                    command_id=f"collect:{tid}:{int(contest.get('end_at_ms') or 0)}",
                    expected_revision=previous,
                )
                if not collected.replayed:
                    self._save_pool_state(tid, previous, collected.state)
            store.set_state(tid, "done", message)
            return report
        except Exception as exc:
            failed = True
            if (
                "mode" in locals()
                and mode == "both"
                and not locals().get("desktops_frozen", False)
            ):
                try:
                    self._stop_server_best_effort()
                except Exception:
                    self.log.exception(
                        "cannot freeze dual-track server after collect error for %s",
                        tid,
                    )
            if "mode" in locals() and mode == "both" and "finalize_realtime_web" in locals():
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
            if direct_close_error is not None or (
                should_shutdown
                and (
                    not failed
                    or self.cfg["orchestrator"].get(
                        "shutdown_on_collect_error", True
                    )
                )
            ):
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
