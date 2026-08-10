#!/usr/bin/env bash
set -Eeuo pipefail

# Explicit one-time recovery command; intentionally not part of normal builds.
readonly EXPECTED_SOURCE_ID="sha256:fed2063bb95263b9241368420215a4acc538e0f0253b3f4b51bdc4e1769c7631"
readonly EXPECTED_ISO_SHA256="C8824240736352E5E4AAF3F6532B40961F75FA9F23D670BB78881355A49D5878"
readonly ISO_LABEL="org.noi.iso.sha256"

TARGET_TAG="${1:-noi-linux-official-rootfs:2.0}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
ROLLBACK_TAG="${2:-noi-linux-official-rootfs:rollback-pre-attest-${stamp}}"
CANDIDATE_TAG="noi-linux-official-rootfs:attestation-candidate-${stamp}-$$"
PYTHON_BIN="${NOI_ROOTFS_ATTEST_PYTHON:-python3}"
LOCK_FILE="${NOI_ROOTFS_ATTEST_LOCK_FILE:-/var/lock/noi-rootfs-attestation.lock}"
readonly DEPLOY_LOCK_FILE="/var/lock/noi-official-image-deploy.lock"

for command in docker flock mktemp date "${PYTHON_BIN}"; do
    if ! command -v "${command}" >/dev/null 2>&1; then
        echo "缺少命令: ${command}" >&2
        exit 2
    fi
done

if ! exec 8>"${DEPLOY_LOCK_FILE}"; then
    echo "无法打开正式镜像部署锁文件: ${DEPLOY_LOCK_FILE}" >&2
    exit 2
fi
if ! flock -n 8; then
    echo "已有正式镜像部署或 rootfs 认证操作正在运行，拒绝并发执行" >&2
    exit 1
fi

if ! exec 9>"${LOCK_FILE}"; then
    echo "无法打开认证锁文件: ${LOCK_FILE}" >&2
    exit 2
fi
if ! flock -n 9; then
    echo "已有 rootfs 认证操作正在运行，拒绝并发执行" >&2
    exit 1
fi

if [[ -n "$(docker ps -q --filter label=noi.contest)" ]]; then
    echo "比赛座位容器正在运行，拒绝认证 rootfs" >&2
    exit 1
fi

image_id() { docker image inspect "$1" --format '{{.Id}}'; }
layers_json() { docker image inspect "$1" --format '{{json .RootFS.Layers}}'; }
image_label() {
    docker image inspect "$1" --format "{{index .Config.Labels \"${ISO_LABEL}\"}}"
}

current_id="$(image_id "${TARGET_TAG}")"
if [[ "${current_id}" != "${EXPECTED_SOURCE_ID}" ]]; then
    echo "拒绝认证：${TARGET_TAG} 不是已知的缓存官方 rootfs" >&2
    echo "期望 ID: ${EXPECTED_SOURCE_ID}" >&2
    echo "实际 ID: ${current_id}" >&2
    exit 1
fi

source_layers="$(layers_json "${EXPECTED_SOURCE_ID}")"
if [[ -z "${source_layers}" || "${source_layers}" == "null" || "${source_layers}" == "[]" ]]; then
    echo "拒绝认证：源镜像 RootFS.Layers 为空" >&2
    exit 1
fi

if docker image inspect "${ROLLBACK_TAG}" >/dev/null 2>&1; then
    echo "拒绝覆盖已有回滚标签: ${ROLLBACK_TAG}" >&2
    exit 1
fi
if docker image inspect "${CANDIDATE_TAG}" >/dev/null 2>&1; then
    echo "临时候选标签已存在: ${CANDIDATE_TAG}" >&2
    exit 1
fi

docker image tag "${EXPECTED_SOURCE_ID}" "${ROLLBACK_TAG}"
if [[ "$(image_id "${ROLLBACK_TAG}")" != "${EXPECTED_SOURCE_ID}" ]]; then
    echo "无法建立精确回滚标签" >&2
    exit 1
fi

work_dir="$(mktemp -d)"
source_archive="${work_dir}/source.tar"
wrapped_archive="${work_dir}/wrapped.tar"
promotion_may_have_happened=0
completed=0

cleanup() {
    local status=$?
    trap - EXIT
    set +e
    if [[ "${completed}" != "1" && "${promotion_may_have_happened}" == "1" ]]; then
        echo "认证未完成，恢复 ${TARGET_TAG} -> ${ROLLBACK_TAG}" >&2
        docker image tag "${ROLLBACK_TAG}" "${TARGET_TAG}"
        if [[ "$(image_id "${TARGET_TAG}" 2>/dev/null)" != "${EXPECTED_SOURCE_ID}" ]]; then
            echo "严重错误：自动恢复失败，请保留主机并人工检查 ${ROLLBACK_TAG}" >&2
            status=1
        fi
    fi
    docker image rm "${CANDIDATE_TAG}" >/dev/null 2>&1 || true
    rm -rf -- "${work_dir}"
    exit "${status}"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

echo "[1/5] 导出已知缓存 rootfs（不运行、不修改容器文件系统）"
docker image save --output "${source_archive}" "${EXPECTED_SOURCE_ID}"

echo "[2/5] 仅改写 OCI/Docker 镜像配置标签"
"${PYTHON_BIN}" - \
    "${source_archive}" "${wrapped_archive}" "${CANDIDATE_TAG}" \
    "${EXPECTED_SOURCE_ID}" "${EXPECTED_ISO_SHA256}" "${ISO_LABEL}" <<'PY'
import hashlib
import io
import json
from pathlib import PurePosixPath
import sys
import tarfile

source_path, output_path, candidate_tag, expected_id, iso_sha, label_name = sys.argv[1:]
expected_digest = expected_id[7:] if expected_id.startswith("sha256:") else expected_id

def fail(message: str) -> None:
    raise SystemExit(f"拒绝改写镜像归档: {message}")

def read_member(archive: tarfile.TarFile, member: tarfile.TarInfo) -> bytes:
    stream = archive.extractfile(member)
    if stream is None:
        fail(f"无法读取 {member.name}")
    return stream.read()

with tarfile.open(source_path, "r:*") as source:
    members = source.getmembers()
    names = [member.name for member in members]
    if len(names) != len(set(names)):
        fail("归档包含重复成员")
    for name in names:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name:
            fail(f"不安全的归档成员名: {name}")

    try:
        manifest_member = next(member for member in members if member.name == "manifest.json")
    except StopIteration:
        fail("缺少 manifest.json")
    manifest = json.loads(read_member(source, manifest_member))
    if not isinstance(manifest, list) or len(manifest) != 1:
        fail("必须且只能包含一个镜像 manifest")
    entry = manifest[0]
    old_config_name = entry.get("Config")
    layer_names = entry.get("Layers")
    if not isinstance(old_config_name, str) or old_config_name not in names:
        fail("manifest 的 Config 无效")
    if not isinstance(layer_names, list) or not layer_names:
        fail("manifest 的 Layers 为空")
    if any(not isinstance(name, str) or name not in names for name in layer_names):
        fail("manifest 引用了缺失的层")

    old_config_member = next(member for member in members if member.name == old_config_name)
    old_config_bytes = read_member(source, old_config_member)
    if hashlib.sha256(old_config_bytes).hexdigest() != expected_digest:
        fail("导出配置摘要与已知镜像 ID 不一致")
    config = json.loads(old_config_bytes)
    rootfs = config.get("rootfs")
    if not isinstance(rootfs, dict) or rootfs.get("type") != "layers":
        fail("配置缺少有效 rootfs")
    diff_ids = rootfs.get("diff_ids")
    if not isinstance(diff_ids, list) or not diff_ids:
        fail("配置的 rootfs.diff_ids 为空")
    if len(diff_ids) != len(layer_names):
        fail("配置层数与 manifest 层数不一致")

    runtime_config = config.get("config")
    if not isinstance(runtime_config, dict):
        fail("配置缺少 config 对象")
    labels = runtime_config.get("Labels")
    if labels is None:
        labels = {}
        runtime_config["Labels"] = labels
    if not isinstance(labels, dict):
        fail("Config.Labels 不是对象")
    previous = labels.get(label_name)
    if previous not in (None, iso_sha):
        fail(f"现有 {label_name} 与目标值冲突")
    labels[label_name] = iso_sha

    new_config_bytes = json.dumps(
        config, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    new_digest = hashlib.sha256(new_config_bytes).hexdigest()
    if new_digest == expected_digest:
        fail("配置 ID 未发生变化")
    new_config_name = f"{new_digest}.json"
    entry["Config"] = new_config_name
    entry["RepoTags"] = [candidate_tag]
    manifest_bytes = json.dumps(manifest, separators=(",", ":")).encode("utf-8")

    with tarfile.open(output_path, "w") as output:
        for member in members:
            if member.name in {"manifest.json", "repositories", old_config_name}:
                continue
            output.addfile(member, source.extractfile(member) if member.isfile() else None)
        for name, payload in ((new_config_name, new_config_bytes), ("manifest.json", manifest_bytes)):
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            member.mode = 0o644
            member.uid = member.gid = 0
            member.mtime = 0
            output.addfile(member, io.BytesIO(payload))
PY

docker image load --input "${wrapped_archive}" >/dev/null

echo "[3/5] 验证配置标签和 RootFS.Layers 完全不变"
candidate_id="$(image_id "${CANDIDATE_TAG}")"
if [[ "${candidate_id}" == "${EXPECTED_SOURCE_ID}" ]]; then
    echo "候选镜像配置 ID 未变化" >&2
    exit 1
fi
if [[ "$(image_label "${CANDIDATE_TAG}")" != "${EXPECTED_ISO_SHA256}" ]]; then
    echo "候选镜像 ISO SHA256 标签不匹配" >&2
    exit 1
fi
if [[ "$(layers_json "${CANDIDATE_TAG}")" != "${source_layers}" ]]; then
    echo "候选镜像 RootFS.Layers 发生变化，拒绝提升" >&2
    exit 1
fi

echo "[4/5] 在无网络、只读容器中核对官方关键软件"
docker run --rm --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges --pids-limit 128 --memory 512m \
    --entrypoint /bin/bash "${CANDIDATE_TAG}" -euc '
        . /etc/os-release
        test "${VERSION_ID}" = "20.04"
        test "$(gcc -dumpfullversion -dumpversion)" = "9.3.0"
        test "$(g++ -dumpfullversion -dumpversion)" = "9.3.0"
        test "$(fpc -iV)" = "3.0.4"
        for package in gdb ddd codeblocks lazarus geany ibus ibus-libpinyin; do
            test "$(dpkg-query -W -f="\${Status}" "${package}")" = "install ok installed"
        done
        test -x /usr/local/arbiter/local/arbiter_local
    '

echo "[5/5] 最后复核旧标签后，原子切换 ${TARGET_TAG}"
if [[ "$(image_id "${TARGET_TAG}")" != "${EXPECTED_SOURCE_ID}" ]]; then
    echo "提升前目标标签发生变化，拒绝覆盖" >&2
    exit 1
fi
if [[ "$(image_id "${ROLLBACK_TAG}")" != "${EXPECTED_SOURCE_ID}" ]]; then
    echo "提升前回滚标签发生变化，拒绝继续" >&2
    exit 1
fi

promotion_may_have_happened=1
docker image tag "${candidate_id}" "${TARGET_TAG}"
if [[ "$(image_id "${TARGET_TAG}")" != "${candidate_id}" ]]; then
    echo "目标标签未指向候选镜像" >&2
    exit 1
fi
if [[ "$(image_label "${TARGET_TAG}")" != "${EXPECTED_ISO_SHA256}" ]]; then
    echo "提升后目标标签的 ISO SHA256 不匹配" >&2
    exit 1
fi
if [[ "$(layers_json "${TARGET_TAG}")" != "${source_layers}" ]]; then
    echo "提升后目标标签的 RootFS.Layers 不匹配" >&2
    exit 1
fi

completed=1
echo "认证完成: ${TARGET_TAG} -> ${candidate_id}"
echo "回滚保留: ${ROLLBACK_TAG} -> ${EXPECTED_SOURCE_ID}"
