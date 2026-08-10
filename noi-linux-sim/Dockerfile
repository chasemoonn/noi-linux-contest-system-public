# NOI Linux 2.0 模拟环境：Ubuntu 20.04 + g++ 9.4 + XFCE/noVNC
FROM ubuntu:20.04

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Shanghai \
    LANG=zh_CN.UTF-8 \
    LC_ALL=zh_CN.UTF-8

# 腾讯云：mirrors.cloud.tencent.com；阿里云：mirrors.cloud.aliyuncs.com
ARG APT_MIRROR=mirrors.cloud.tencent.com
RUN [[ "${APT_MIRROR}" =~ ^[A-Za-z0-9.-]+$ ]] \
 && printf '%s\n' \
    "deb http://${APT_MIRROR}/ubuntu focal main restricted universe multiverse" \
    "deb http://${APT_MIRROR}/ubuntu focal-updates main restricted universe multiverse" \
    "deb http://${APT_MIRROR}/ubuntu focal-security main restricted universe multiverse" \
    "deb http://${APT_MIRROR}/ubuntu focal-backports main restricted universe multiverse" \
    > /etc/apt/sources.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
    xfce4 xfce4-terminal dbus-x11 x11-xserver-utils xdg-utils \
    tigervnc-standalone-server tigervnc-common \
    novnc websockify supervisor \
    g++-9 gcc-9 gdb make \
    geany geany-plugins-common gedit codeblocks evince epiphany-browser \
    fonts-noto-cjk ibus ibus-libpinyin im-config locales tzdata \
    sudo ca-certificates curl \
 && update-alternatives --install /usr/bin/g++ g++ /usr/bin/g++-9 100 \
 && update-alternatives --install /usr/bin/gcc gcc /usr/bin/gcc-9 100 \
 && locale-gen zh_CN.UTF-8 \
 && ln -snf "/usr/share/zoneinfo/${TZ}" /etc/localtime \
 && echo "${TZ}" > /etc/timezone \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

COPY rootfs/ /
RUN chmod 0755 /etc /opt /usr /usr/local /usr/local/bin \
 && find /etc/supervisor /opt/geany /opt/template /usr/local/bin -type d -exec chmod 0755 {} + \
 && chmod 0755 /usr/local/bin/entrypoint.sh /usr/local/bin/start-vnc.sh

EXPOSE 6080
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
