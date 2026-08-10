#!/usr/bin/env bash
set -euo pipefail

stage="${1:-}"
app=/opt/noi-linux-contest-system
secret_input="${NOI_SECRET_INPUT:?set NOI_SECRET_INPUT to a root-owned mktemp file}"
caddy_snippet="${app}/orchestrator/runtime/caddy-exam.conf"
caddy_candidate=''
frontend_domain="${NOI_FRONTEND_DOMAIN:?set NOI_FRONTEND_DOMAIN}"
hydro_public_base_url="${NOI_HYDRO_PUBLIC_BASE_URL:?set NOI_HYDRO_PUBLIC_BASE_URL}"
gateway_public_base_url="${NOI_GATEWAY_PUBLIC_BASE_URL:-}"
deployment_label="${NOI_DEPLOYMENT_LABEL:-hydro}"
contest_source_cidr="${NOI_CONTEST_SOURCE_CIDR:?set NOI_CONTEST_SOURCE_CIDR}"
aliyun_region_id="${NOI_ALIYUN_REGION_ID:?set NOI_ALIYUN_REGION_ID}"
aliyun_instance_id="${NOI_ALIYUN_INSTANCE_ID:?set NOI_ALIYUN_INSTANCE_ID}"
contest_host_key_sha256="${NOI_CONTEST_SSH_HOST_KEY_SHA256:?set NOI_CONTEST_SSH_HOST_KEY_SHA256}"
desktop_security_group_id="${NOI_ALIYUN_DESKTOP_SECURITY_GROUP_ID:-}"
student_desktop_source_cidr="${NOI_STUDENT_DESKTOP_SOURCE_CIDR:?set NOI_STUDENT_DESKTOP_SOURCE_CIDR explicitly}"
hydro_internal_base_url="${NOI_HYDRO_INTERNAL_BASE_URL:-http://127.0.0.1:8888}"
hydro_domain_id="${NOI_HYDRO_DOMAIN_ID:-system}"

if [[ "${EUID}" -ne 0 ]]; then
    echo "must run as root" >&2
    exit 1
fi
if [[ ! "${frontend_domain}" =~ ^[A-Za-z0-9][A-Za-z0-9.-]+[A-Za-z0-9]$ ]]; then
    echo "invalid NOI_FRONTEND_DOMAIN" >&2
    exit 1
fi
if [[ ! "${hydro_public_base_url}" =~ ^https://[A-Za-z0-9.-]+/?$ ]]; then
    echo "invalid NOI_HYDRO_PUBLIC_BASE_URL" >&2
    exit 1
fi
if [[ ! "${contest_source_cidr}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/32$ ]]; then
    echo "invalid NOI_CONTEST_SOURCE_CIDR" >&2
    exit 1
fi
if [[ ! "${aliyun_region_id}" =~ ^[a-z0-9-]+$ ]]; then
    echo "invalid NOI_ALIYUN_REGION_ID" >&2
    exit 1
fi
if [[ ! "${aliyun_instance_id}" =~ ^i-[A-Za-z0-9]+$ ]]; then
    echo "invalid NOI_ALIYUN_INSTANCE_ID" >&2
    exit 1
fi
if [[ ! "${contest_host_key_sha256}" =~ ^SHA256:[A-Za-z0-9+/]{43}=?$ ]]; then
    echo "invalid NOI_CONTEST_SSH_HOST_KEY_SHA256" >&2
    exit 1
fi
if [[ -n "${desktop_security_group_id}" ]] \
    && [[ ! "${desktop_security_group_id}" =~ ^sg-[A-Za-z0-9]+$ ]]; then
    echo "invalid NOI_ALIYUN_DESKTOP_SECURITY_GROUP_ID" >&2
    exit 1
fi
if [[ ! "${student_desktop_source_cidr}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/([0-9]|[12][0-9]|3[0-2])$ ]]; then
    echo "invalid NOI_STUDENT_DESKTOP_SOURCE_CIDR" >&2
    exit 1
fi
if [[ ! "${stage}" =~ ^/tmp/noi-deploy\.[A-Za-z0-9]+$ ]] \
    || [[ ! -d "${stage}/orchestrator" ]]; then
    echo "invalid staging directory" >&2
    exit 1
fi
if [[ ! "${secret_input}" =~ ^/tmp/noi-deploy-secrets\.[A-Za-z0-9]+$ ]] \
    || [[ ! -f "${secret_input}" ]] \
    || [[ -L "${secret_input}" ]] \
    || [[ "$(stat -c '%u:%a:%h' -- "${secret_input}")" != "0:600:1" ]] \
    || [[ ! -s "${secret_input}" ]]; then
    echo "NOI_SECRET_INPUT must be a non-empty root-owned 0600 single-link regular file from mktemp /tmp/noi-deploy-secrets.XXXXXX" >&2
    exit 1
fi
cleanup() {
    rm -f -- "${secret_input}"
    if [[ -n "${caddy_candidate}" ]]; then
        rm -f -- "${caddy_candidate}"
    fi
}
trap cleanup EXIT

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="/root/noi-backups/hydro-host-${timestamp}"
install -d -m 0700 "${backup}"
for source in \
    /root/.hydro/Caddyfile \
    /root/.hydro/addon.json \
    /root/.hydro/orchestrator-plugin.env \
    "${app}/orchestrator/.env" \
    "${app}/orchestrator/config.yaml"; do
    if [[ -f "${source}" ]]; then
        cp -a "${source}" "${backup}/$(echo "${source}" | tr '/' '_')"
    fi
done
preserved_config="${backup}/orchestrator-config.yaml.preserved"
had_existing_config=0
if [[ -f "${app}/orchestrator/config.yaml" ]]; then
    cp -a "${app}/orchestrator/config.yaml" "${preserved_config}"
    had_existing_config=1
fi

install -d -m 0755 "${app}"
cp -a "${stage}/." "${app}/"
install -d -m 0700 "${app}/secrets"
install -d -m 0755 \
    "${app}/orchestrator/data" \
    "${app}/orchestrator/runtime" \
    "${app}/artifact-tools"
install -d -m 0700 /root/.hydro/orchestrator-state
caddy_candidate=$(mktemp "${app}/orchestrator/runtime/.caddy-exam.conf.install.XXXXXX")

if [[ ! -s "${app}/secrets/contest.pem" ]]; then
    ssh-keygen -q -t rsa -b 3072 -N '' \
        -C "noi-orchestrator@${deployment_label}" \
        -f "${app}/secrets/contest.pem"
fi
chmod 0600 "${app}/secrets/contest.pem"
chmod 0644 "${app}/secrets/contest.pem.pub"
: > "${app}/secrets/known_hosts"
chmod 0644 "${app}/secrets/known_hosts"

if [[ "${had_existing_config}" -eq 1 ]]; then
    # Staging may contain a config.yaml. Restore the operator-owned file before
    # merging the single installer-owned key below.
    cp -a "${preserved_config}" "${app}/orchestrator/config.yaml"
else
    cp "${app}/orchestrator/config.example.yaml" \
        "${app}/orchestrator/config.yaml"
fi
chmod 0644 "${app}/orchestrator/config.yaml"
python3 "${app}/deploy/merge_orchestrator_config.py" \
    "${app}/orchestrator/config.yaml" "${frontend_domain}"

python3 - \
    "${app}/orchestrator/.env" \
    "${secret_input}" \
    "${frontend_domain}" \
    "${hydro_public_base_url%/}" \
    "${gateway_public_base_url%/}" \
    "${contest_source_cidr}" \
    "${caddy_candidate}" \
    "${aliyun_region_id}" \
    "${aliyun_instance_id}" \
    "${contest_host_key_sha256}" \
    "${desktop_security_group_id}" \
    "${student_desktop_source_cidr}" \
    "${hydro_internal_base_url%/}" \
    "${hydro_domain_id}" <<'PY'
import json
import os
from pathlib import Path
import secrets
import sys
from urllib.parse import quote_plus

output = Path(sys.argv[1])
secret_input = Path(sys.argv[2])
frontend_domain = sys.argv[3]
hydro_public_base_url = sys.argv[4]
gateway_public_base_url = sys.argv[5]
contest_source_cidr = sys.argv[6]
caddy_candidate = Path(sys.argv[7])
aliyun_region_id = sys.argv[8]
aliyun_instance_id = sys.argv[9]
contest_host_key_sha256 = sys.argv[10]
desktop_security_group_id = sys.argv[11]
student_desktop_source_cidr = sys.argv[12]
hydro_internal_base_url = sys.argv[13]
hydro_domain_id = sys.argv[14]

def parse_env(path: Path):
    result = {}
    if not path.exists():
        return result
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        result[key.strip()] = value
    return result

def hydro_mongo_uri(config: dict) -> str:
    if config.get('uri'):
        return str(config['uri'])
    required = ('host', 'port', 'name', 'username', 'password')
    missing = [key for key in required if config.get(key) in (None, '')]
    if missing:
        raise ValueError('Hydro Mongo config missing: ' + ', '.join(missing))
    user = quote_plus(str(config['username']))
    password = quote_plus(str(config['password']))
    host = str(config['host'])
    port = int(config['port'])
    name = quote_plus(str(config['name']))
    return f'mongodb://{user}:{password}@{host}:{port}/{name}'

old = parse_env(output)
incoming = parse_env(secret_input)
hydro = json.loads(
    Path('/root/.hydro/config.json').read_text(encoding='utf-8')
)
token = old.get('HYDRO_ORCHESTRATOR_TOKEN') or secrets.token_hex(32)
admin = old.get('ADMIN_PASSWORD') or secrets.token_urlsafe(24)

values = {
    'CLOUD_PROVIDER': 'aliyun',
    'ALIYUN_ACCESS_KEY_ID': incoming['ALIYUN_ACCESS_KEY_ID'],
    'ALIYUN_ACCESS_KEY_SECRET': incoming['ALIYUN_ACCESS_KEY_SECRET'],
    'ALIYUN_REGION_ID': aliyun_region_id,
    'ALIYUN_INSTANCE_ID': aliyun_instance_id,
    'ALIYUN_DESKTOP_SECURITY_GROUP_ID': desktop_security_group_id,
    'STUDENT_DESKTOP_SOURCE_CIDR': student_desktop_source_cidr,
    'CONTEST_SOURCE_CIDR': contest_source_cidr,
    'TENCENT_SECRET_ID': '',
    'TENCENT_SECRET_KEY': '',
    'TENCENT_REGION': 'ap-beijing',
    'TENCENT_INSTANCE_ID': '',
    'HYDRO_ORCHESTRATOR_TOKEN': token,
    'HYDRO_PUBLIC_BASE_URL': hydro_public_base_url,
    'HYDRO_INTERNAL_BASE_URL': hydro_internal_base_url,
    'HYDRO_MONGO_URI': hydro_mongo_uri(hydro),
    'HYDRO_DOMAIN_ID': hydro_domain_id,
    'ADMIN_PASSWORD': admin,
    'CONTEST_SSH_USER': 'root',
    'CONTEST_SSH_KEY': '/opt/noi-linux-contest-system/secrets/contest.pem',
    'CONTEST_KNOWN_HOSTS': '/opt/noi-linux-contest-system/secrets/known_hosts',
    'CONTEST_SSH_HOST_KEY_SHA256': contest_host_key_sha256,
    'GATEWAY_PUBLIC_BASE_URL': gateway_public_base_url,
    'ORCHESTRATOR_PUBLIC_BASE_URL': f'https://{frontend_domain}',
    'FRONTEND_PROXY_PROVIDER': 'caddy',
    'FRONTEND_DOMAIN': frontend_domain,
    'FRONTEND_CADDY_DIR': '/root/.hydro',
    'ARTIFACT_TOOLS_DIR': '/opt/noi-linux-contest-system/artifact-tools',
}

ai_key = incoming.get('NOI_ARTIFACT_AI_API_KEY') or old.get(
    'NOI_ARTIFACT_AI_API_KEY'
)
if ai_key:
    # The key is intentionally present only in the chmod-0600 runtime env.
    # config.yaml names this variable through api_key_env and never stores it.
    values['NOI_ARTIFACT_AI_API_KEY'] = ai_key

def quote(value: str) -> str:
    if "'" in value or '\n' in value or '\r' in value:
        raise ValueError('unsupported character in environment value')
    return "'" + value + "'"

temporary = output.with_suffix('.env.tmp')
temporary.write_text(
    ''.join(f'{key}={quote(str(value))}\n' for key, value in values.items()),
    encoding='utf-8',
)
os.chmod(temporary, 0o600)
temporary.replace(output)

plugin_env = Path('/root/.hydro/orchestrator-plugin.env')
plugin_values = {
    'ORCHESTRATOR_TOKEN_FILE': '/root/.hydro/orchestrator-token',
    'ORCHESTRATOR_DOMAIN': hydro_domain_id,
    'ORCHESTRATOR_MAX_CODE_BYTES': '524288',
    'ORCHESTRATOR_IDEMPOTENCY_FILE': (
        '/root/.hydro/orchestrator-state/submissions.json'
    ),
    'ORCHESTRATOR_IDEMPOTENCY_MAX_ENTRIES': '20000',
    'ORCHESTRATOR_NOTIFICATION_IDEMPOTENCY_FILE': (
        '/root/.hydro/orchestrator-state/notifications.json'
    ),
    'ORCHESTRATOR_NOTIFICATION_IDEMPOTENCY_MAX_ENTRIES': '20000',
    'ORCHESTRATOR_PROBLEM_DRAFT_IDEMPOTENCY_FILE': (
        '/root/.hydro/orchestrator-state/problem-drafts.json'
    ),
    'ORCHESTRATOR_PROBLEM_DRAFT_IDEMPOTENCY_MAX_ENTRIES': '2000',
    'ORCHESTRATOR_NOTIFY_ALLOWED_HTTPS_HOSTS': frontend_domain,
}
plugin_temporary = plugin_env.with_suffix('.env.tmp')
plugin_temporary.write_text(
    ''.join(
        f'{key}={quote(str(value))}\n'
        for key, value in plugin_values.items()
    ),
    encoding='utf-8',
)
os.chmod(plugin_temporary, 0o600)
plugin_temporary.replace(plugin_env)

credential = Path('/root/noi-orchestrator-admin.txt')
credential.write_text(
    f'URL=https://{frontend_domain}/admin\n'
    f'USER=teacher\nPASSWORD={admin}\n',
    encoding='utf-8',
)
os.chmod(credential, 0o600)

token_file = Path('/root/.hydro/orchestrator-token')
token_file.write_text(token + '\n', encoding='utf-8')
os.chmod(token_file, 0o600)

caddy_candidate.write_text(
    f'''# Generated by NOI orchestrator. Do not edit while it is running.
{frontend_domain} {{
    encode zstd gzip
    log {{
        output file /root/.hydro/noi-exam.access.log {{
            roll_size 200mb
            roll_keep_for 168h
        }}
        format json
    }}
    @desktop path /s/*
    handle @desktop {{
        respond "比赛桌面尚未开放" 503
    }}
    handle {{
        reverse_proxy http://127.0.0.1:8600
    }}
}}
''',
    encoding='utf-8',
)
os.chmod(caddy_candidate, 0o600)
PY

CADDY_SNIPPET_BACKUP="${backup}/caddy-exam.conf.before-install" \
    bash "${app}/deploy/publish-caddy-exam-snippet.sh" \
    "${caddy_candidate}" "${caddy_snippet}"
rm -f -- "${caddy_candidate}"
caddy_candidate=''

cd "${app}/orchestrator"
if docker compose version >/dev/null 2>&1; then
    compose=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
    compose=(docker-compose)
else
    echo "Docker Compose is missing" >&2
    exit 1
fi

"${compose[@]}" config --quiet
"${compose[@]}" build
"${compose[@]}" run --rm --no-deps \
    --volume "${app}/deploy:/deploy:ro" \
    orchestrator \
    python -m unittest discover -s tests -v
"${compose[@]}" up -d

for _ in $(seq 1 30); do
    if curl -fsS http://127.0.0.1:8600/healthz >/dev/null; then
        echo "orchestrator_ready backup=${backup}"
        exit 0
    fi
    sleep 1
done

"${compose[@]}" logs --tail 100 orchestrator >&2
exit 1
