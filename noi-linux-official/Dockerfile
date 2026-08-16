# The base image is imported from the official ubuntu-noi-v2.0 live ISO by
# deploy/build-noi-official-image.sh. This layer only adds remote-display
# plumbing; the desktop, compilers and editors remain the official image's.
ARG NOI_ROOTFS_IMAGE=noi-linux-official-rootfs:2.0
FROM ${NOI_ROOTFS_IMAGE}

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Shanghai \
    LANG=zh_CN.UTF-8 \
    LANGUAGE=zh_CN:zh \
    LC_ALL=zh_CN.UTF-8

ARG APT_MIRROR=mirrors.aliyun.com
RUN [[ "${APT_MIRROR}" =~ ^[A-Za-z0-9.-]+$ ]] \
 && printf '%s\n' \
    "deb http://${APT_MIRROR}/ubuntu focal main restricted universe multiverse" \
    "deb http://${APT_MIRROR}/ubuntu focal-updates main restricted universe multiverse" \
    "deb http://${APT_MIRROR}/ubuntu focal-security main restricted universe multiverse" \
    > /etc/apt/sources.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
    curl dbus-x11 python3-minimal supervisor tigervnc-standalone-server tigervnc-common \
    x11-xkb-utils \
    novnc websockify \
 && locale-gen zh_CN.UTF-8 \
 && update-locale LANG=zh_CN.UTF-8 LANGUAGE=zh_CN:zh LC_ALL=zh_CN.UTF-8 \
 && ln -snf "/usr/share/zoneinfo/${TZ}" /etc/localtime \
 && echo "${TZ}" > /etc/timezone \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/* /var/lib/dbus/machine-id \
 && mkdir -p /var/lib/dbus \
 && ln -s /etc/machine-id /var/lib/dbus/machine-id

COPY rootfs/ /
# COPY preserves directory modes from the build context.  Release sources are
# intentionally staged below a root-only directory, so without this explicit
# normalization Docker can replace official /usr, /etc, or /opt modes with
# 0700 and make the desktop unusable by the student account.
RUN chmod 0755 \
    /etc /etc/supervisor /etc/supervisor/conf.d /etc/xdg /etc/xdg/autostart \
    /opt /opt/contest-template /opt/contest-template/Desktop \
    /usr /usr/local /usr/local/bin \
 && chmod 0644 \
    /etc/supervisor/conf.d/contest.conf \
    /etc/xdg/autostart/noi-contest-desktop-finalize.desktop \
 && chmod 0755 /usr/local/bin/contest-entrypoint.sh /usr/local/bin/start-contest-vnc.sh \
    /usr/local/bin/configure-contest-desktop.sh /usr/local/bin/finalize-contest-session.sh \
    /usr/local/bin/capture-formal-source.py \
    /usr/local/bin/install-firefox-cookie-policy.py \
    /usr/local/bin/verify-firefox-cookie-runtime.py \
 && find /opt/contest-template/Desktop -type f -name '*.desktop' -exec chmod 0755 {} +

# Firefox 79 supports the Cookies enterprise policy. Merge only the required
# fields into the base image's policy document; malformed, conflicting, or
# ambiguous multi-source policy state must stop the build.
RUN python3 /usr/local/bin/install-firefox-cookie-policy.py

# Keep the mutable source identity after all expensive filesystem layers.
# A new audited source revision must change the immutable image identity, but
# must not force the official ISO import and desktop package installation to be
# repeated when their inputs are unchanged.
ARG NOI_SOURCE_REVISION
ARG NOI_ISO_SHA256
RUN [[ "${NOI_SOURCE_REVISION}" =~ ^[0-9a-f]{40}$ ]] \
 && [[ "${NOI_ISO_SHA256}" =~ ^[0-9a-f]{64}$ ]]

# Orchestration fails closed unless the desktop implements this versioned
# finalizer-status protocol.
LABEL org.noi.desktop.contract="finalizer-status-v1" \
      org.noi.iso.sha256="${NOI_ISO_SHA256}" \
      org.opencontainers.image.revision="${NOI_SOURCE_REVISION}"

EXPOSE 6080
ENTRYPOINT ["/usr/local/bin/contest-entrypoint.sh"]
