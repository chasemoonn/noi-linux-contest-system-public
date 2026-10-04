"""Small opt-in TLS policy for the existing contest-host nginx gateway."""
from __future__ import annotations

import ipaddress
from pathlib import PurePosixPath
import re
import socket
from urllib.parse import urlsplit


def gateway_tls_enabled(server: dict) -> bool:
    settings = server.get('gateway_tls', {})
    if not isinstance(settings, dict):
        raise ValueError('contest_server.gateway_tls 必须是映射')
    enabled = settings.get('enabled', False)
    if not isinstance(enabled, bool):
        raise ValueError('contest_server.gateway_tls.enabled 必须是布尔值')
    return enabled


def gateway_tls_settings(server: dict) -> dict:
    if not gateway_tls_enabled(server):
        return {}
    settings = server['gateway_tls']
    base = urlsplit(str(server.get('gateway_public_base_url') or '').strip())
    host = str(base.hostname or '').lower()
    try:
        port = base.port
    except ValueError as exc:
        raise ValueError('gateway_tls 桌面域名端口无效') from exc
    if (base.scheme != 'https' or port not in (None,443)
            or base.username or base.password or base.path not in ('','/')
            or base.query or base.fragment or '.' not in host
            or len(host) > 253
            or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                   for label in host.split('.'))):
        raise ValueError('gateway_tls 要求 gateway_public_base_url 是 HTTPS 域名根地址，端口为 443')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError('gateway_tls 必须使用与证书匹配的 DNS 域名，不能使用裸 IP')
    if str(server.get('gateway_scheme','http')).lower() != 'https':
        raise ValueError('gateway_tls 要求 gateway_scheme=https')
    if int(server.get('gateway_listen',80)) != 80:
        raise ValueError('gateway_tls 模式的 gateway_listen 必须为本机验收端口 80')
    result = {'hostname':host}
    for key in ('certificate','certificate_key'):
        value = str(settings.get(key) or '')
        path = PurePosixPath(value)
        if (not value.startswith('/') or value.startswith('//') or '..' in path.parts
                or str(path) != value or not re.fullmatch(r'/[A-Za-z0-9_./-]+', value)
                or value == '/'):
            raise ValueError(f'contest_server.gateway_tls.{key} 必须是安全的绝对文件路径')
        result[key] = value
    if result['certificate'] == result['certificate_key']:
        raise ValueError('gateway_tls 证书链与私钥必须使用独立文件')
    return result


def verify_gateway_dns(server: dict, ip: str) -> str:
    """Require the public name to resolve only to this contest's fixed IPv4."""
    host = gateway_tls_settings(server)['hostname']
    try:
        expected = ipaddress.IPv4Address(ip)
        addresses = {ipaddress.ip_address(item[4][0]) for item in
                     socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)}
    except (ValueError, OSError) as exc:
        raise RuntimeError('桌面 HTTPS DNS 无法核验，请检查域名和比赛 EIP') from exc
    if addresses != {expected}:
        raise RuntimeError('桌面 HTTPS DNS 必须只指向当前比赛 EIP，不能混入其他 A/AAAA 地址')
    return host


def gateway_http_bind(server: dict) -> str:
    # HTTP is only for a local readiness probe in TLS mode, never an escape path.
    return '127.0.0.1' if gateway_tls_enabled(server) else str(server.get('gateway_bind_address','0.0.0.0'))


def nginx_tls_server(server: dict, locations: str) -> str:
    tls = gateway_tls_settings(server)
    if not tls:
        return ''
    bind = str(ipaddress.IPv4Address(server.get('gateway_bind_address','0.0.0.0')))
    return (
        'server {\n'
        f'    listen {bind}:443 ssl default_server;\n'
        f'    server_name {tls["hostname"]};\n'
        '    server_tokens off;\n'
        f'    ssl_certificate {tls["certificate"]};\n'
        f'    ssl_certificate_key {tls["certificate_key"]};\n'
        '    ssl_protocols TLSv1.2 TLSv1.3;\n'
        '    ssl_session_cache shared:NOIDesktopTLS:1m;\n'
        '    ssl_session_timeout 10m;\n'
        + locations + '\n    location / { return 404; }\n}\n'
    )
