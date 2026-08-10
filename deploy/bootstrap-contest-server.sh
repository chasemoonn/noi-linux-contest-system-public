#!/bin/bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "请用 sudo 运行本脚本" >&2
    exit 1
fi
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

apt-get update
apt-get install -y docker.io nginx ca-certificates
systemctl enable --now docker

install -d -m 0755 /data/seats
if ! docker network inspect seats >/dev/null 2>&1; then
    docker network create --internal \
        --opt com.docker.network.bridge.enable_icc=false seats
fi
internal="$(docker network inspect -f '{{.Internal}}' seats)"
icc="$(docker network inspect -f '{{index .Options "com.docker.network.bridge.enable_icc"}}' seats)"
if [[ "${internal}" != "true" || "${icc}" != "false" ]]; then
    echo "已有 seats 网络未同时启用 internal 和 ICC=false；请确认无容器使用后手工重建" >&2
    exit 1
fi

rm -f /etc/nginx/sites-enabled/default

# The seat gateway binds the Docker bridge address.  On a cold boot nginx may
# otherwise run its configuration test before Docker has restored that bridge.
install -d -m 0755 /etc/systemd/system/nginx.service.d
install -m 0755 "${SCRIPT_DIR}/noi-wait-nginx-bind-addresses" \
    /usr/local/sbin/noi-wait-nginx-bind-addresses
cat > /etc/systemd/system/nginx.service.d/noi-docker-bridge.conf <<'EOF'
[Unit]
Wants=docker.service network-online.target
After=docker.service network-online.target

[Service]
ExecStartPre=
ExecStartPre=/usr/local/sbin/noi-wait-nginx-bind-addresses
ExecStartPre=/usr/sbin/nginx -t -q -g 'daemon on; master_process on;'
EOF
systemctl daemon-reload
nginx -t
systemctl enable --now nginx

echo "初始化完成。上传项目与官方 ISO 后执行："
echo 'candidate="noi-linux-official:candidate-$(date -u +%Y%m%dT%H%M%SZ)"'
echo 'source_revision="<发布包标注的40位小写Git提交ID>"  # 必须显式提供，不能猜 HEAD'
echo 'bash deploy/build-noi-official-image.sh /path/to/ubuntu-noi-v2.0.iso "${candidate}" "${source_revision}"'
echo 'bash deploy/verify-contest-image-local.sh "${candidate}" "${source_revision}"'
echo 'docker tag "${candidate}" noi-linux-official:2.0'
