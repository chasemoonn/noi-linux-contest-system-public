#!/usr/bin/env bash
set -euo pipefail

EXPECTED_SHA256="C8824240736352E5E4AAF3F6532B40961F75FA9F23D670BB78881355A49D5878"
EXPECTED_DESKTOP_CONTRACT="finalizer-status-v1"
ISO_PATH="${1:-}"
IMAGE_TAG="${2:-noi-linux-official:2.0}"
SOURCE_REVISION="${3:-}"
ROOTFS_TAG="${NOI_ROOTFS_TAG:-noi-linux-official-rootfs:2.0}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

if [[ -z "${ISO_PATH}" || ! -f "${ISO_PATH}" \
    || ! "${SOURCE_REVISION}" =~ ^[0-9a-f]{40}$ ]]; then
    echo "用法: $0 /path/to/ubuntu-noi-v2.0.iso [image-tag] <40位小写源码revision>" >&2
    echo "源码 revision 必须显式提供，不能从当前目录或 HEAD 猜测" >&2
    exit 2
fi

for command in sha256sum unsquashfs tar docker; do
    if ! command -v "${command}" >/dev/null 2>&1; then
        echo "缺少命令: ${command}" >&2
        echo "Ubuntu 可先安装: apt-get install -y squashfs-tools libarchive-tools" >&2
        exit 2
    fi
done
if ! command -v bsdtar >/dev/null 2>&1 && ! command -v xorriso >/dev/null 2>&1; then
    echo "需要 bsdtar 或 xorriso 来读取 ISO" >&2
    exit 2
fi

actual="$(sha256sum "${ISO_PATH}" | awk '{print toupper($1)}')"
if [[ "${actual}" != "${EXPECTED_SHA256}" ]]; then
    echo "ISO SHA256 不匹配" >&2
    echo "期望: ${EXPECTED_SHA256}" >&2
    echo "实际: ${actual}" >&2
    exit 1
fi

work_dir="$(mktemp -d)"
cleanup() { rm -rf -- "${work_dir}"; }
trap cleanup EXIT
mkdir -p "${work_dir}/iso" "${work_dir}/rootfs"

echo "[1/4] 提取官方 squashfs"
if command -v bsdtar >/dev/null 2>&1; then
    bsdtar -xf "${ISO_PATH}" -C "${work_dir}/iso" casper/filesystem.squashfs
else
    xorriso -osirrox on -indev "${ISO_PATH}" \
        -extract /casper/filesystem.squashfs "${work_dir}/iso/filesystem.squashfs"
fi

echo "[2/4] 解包官方根文件系统"
if [[ -f "${work_dir}/iso/casper/filesystem.squashfs" ]]; then
    squashfs="${work_dir}/iso/casper/filesystem.squashfs"
else
    squashfs="${work_dir}/iso/filesystem.squashfs"
fi
unsquashfs -f -d "${work_dir}/rootfs" "${squashfs}"
rm -f "${work_dir}/rootfs/etc/machine-id"
touch "${work_dir}/rootfs/etc/machine-id"

echo "[3/4] 导入官方根文件系统 ${ROOTFS_TAG}"
tar --numeric-owner --xattrs --acls -C "${work_dir}/rootfs" -cf - . \
    | docker import \
        --change "LABEL org.noi.iso.sha256=${EXPECTED_SHA256}" \
        - "${ROOTFS_TAG}" >/dev/null

rootfs_sha256="$(docker image inspect "${ROOTFS_TAG}" \
    --format '{{index .Config.Labels "org.noi.iso.sha256"}}')"
if [[ "${rootfs_sha256}" != "${EXPECTED_SHA256}" ]]; then
    echo "rootfs 镜像缺少可信的 ISO SHA256 标签" >&2
    exit 1
fi

echo "[4/4] 加入 noVNC 远程显示层 ${IMAGE_TAG}"
docker build \
    --build-arg "NOI_ROOTFS_IMAGE=${ROOTFS_TAG}" \
    --build-arg "NOI_SOURCE_REVISION=${SOURCE_REVISION}" \
    -t "${IMAGE_TAG}" "${PROJECT_DIR}/noi-linux-official"

# Resolve the mutable tag once and verify the immutable image ID used below.
image_id="$(docker image inspect "${IMAGE_TAG}" --format '{{.Id}}')"
image_iso_sha256="$(docker image inspect "${image_id}" \
    --format '{{index .Config.Labels "org.noi.iso.sha256"}}')"
image_contract="$(docker image inspect "${image_id}" \
    --format '{{index .Config.Labels "org.noi.desktop.contract"}}')"
image_source_revision="$(docker image inspect "${image_id}" \
    --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')"
if [[ "${image_iso_sha256}" != "${EXPECTED_SHA256}" ]]; then
    echo "正式镜像未继承可信的 ISO SHA256 标签" >&2
    exit 1
fi
if [[ "${image_contract}" != "${EXPECTED_DESKTOP_CONTRACT}" ]]; then
    echo "正式镜像缺少桌面就绪契约 ${EXPECTED_DESKTOP_CONTRACT}" >&2
    exit 1
fi
if [[ "${image_source_revision}" != "${SOURCE_REVISION}" ]]; then
    echo "正式镜像源码 revision 标签与构建输入不一致" >&2
    exit 1
fi

echo "镜像构建完成: ${IMAGE_TAG} (${image_id})"
docker run --rm --entrypoint /bin/bash "${image_id}" -lc \
    'printf "gcc="; gcc --version | head -n1; printf "g++="; g++ --version | head -n1; printf "codeblocks="; dpkg-query -W -f="${Version}\n" codeblocks 2>/dev/null || true'
