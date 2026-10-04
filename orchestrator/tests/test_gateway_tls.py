"""Opt-in direct TLS must not weaken legacy ingress or silently use HTTP."""
import base64
import copy
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shlex
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from services.config import load_config
from services.pipeline import Pipeline, probe_novnc_gateway
from tests.test_config import CONFIG
from tests import test_aliyun as aliyun_fixtures


def secure_config():
    config = yaml.safe_load(CONFIG)
    config['cloud']['aliyun']['desktop_access'] = {
        'enabled':True, 'security_group_id':'sg-test', 'source_cidr':'0.0.0.0/0',
        'management_source_cidrs':['203.0.113.7/32'], 'port':443,
    }
    config['contest_server'].update({
        'gateway_listen':80, 'gateway_scheme':'https',
        'gateway_public_base_url':'https://desktop.example.test',
        'gateway_tls':{'enabled':True, 'certificate':'/etc/noi-tls/fullchain.pem',
                       'certificate_key':'/etc/noi-tls/privkey.pem'},
    })
    config['frontend_proxy'] = {'provider':'caddy', 'domain':'exam.example.test',
        'snippet_path':'/data/exam.caddy', 'caddyfile_path':'/data/Caddyfile',
        'admin_url':'http://127.0.0.1:2019'}
    config['orchestrator']['public_base_url'] = 'https://exam.example.test'
    return config


class GatewayTLSConfigTests(unittest.TestCase):
    def load(self, cfg):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'config.yaml'
            path.write_text(yaml.safe_dump(cfg), encoding='utf-8')
            with patch.dict(os.environ, {'TEST_ACCESS_KEY':'test'}):
                return load_config(path)

    def test_accepts_explicit_tls_with_managed_443(self):
        config = self.load(secure_config())
        self.assertEqual(config['contest_server']['gateway_listen'], 80)
        self.assertEqual(config['cloud']['aliyun']['desktop_access']['port'], 443)

    def test_tls_cannot_be_enabled_with_missing_or_unsafe_material_paths(self):
        for value in ('', '/etc/key.pem;return 200;', '/etc/../tmp/key.pem', 'relative.pem'):
            with self.subTest(value=value):
                config = secure_config()
                config['contest_server']['gateway_tls']['certificate_key'] = value
                with self.assertRaisesRegex(ValueError, 'gateway_tls'):
                    self.load(config)

    def test_tls_rejects_http_ip_and_nonstandard_public_port(self):
        for value in ('http://desktop.example.test', 'https://198.51.100.10',
                      'https://desktop.example.test:8443'):
            with self.subTest(value=value):
                config = secure_config()
                config['contest_server']['gateway_public_base_url'] = value
                with self.assertRaises(ValueError):
                    self.load(config)

    def test_tls_flag_must_be_boolean(self):
        config = secure_config()
        config['contest_server']['gateway_tls']['enabled'] = 'false'
        with self.assertRaisesRegex(ValueError, 'gateway_tls.enabled'):
            self.load(config)

    def test_tls_section_must_be_a_mapping_even_when_empty(self):
        for value in (None, False, [], ''):
            with self.subTest(value=value):
                config = secure_config()
                config['contest_server']['gateway_tls'] = value
                with self.assertRaisesRegex(ValueError, 'gateway_tls'):
                    self.load(config)

    def test_tls_notification_domain_requires_explicit_allowlist(self):
        config = secure_config()
        config['hydro'].update(notify_enabled=True,
                               notify_allowed_https_hosts=['exam.example.test'])
        with self.assertRaisesRegex(ValueError, 'notify_allowed_https_hosts'):
            self.load(config)
        config['hydro']['notify_allowed_https_hosts'].append('desktop.example.test')
        self.load(config)

    def test_tls_does_not_allow_public_plaintext_ingress(self):
        config = secure_config()
        config['cloud']['aliyun']['desktop_access']['port'] = 80
        with self.assertRaisesRegex(ValueError, '443'):
            self.load(config)


class GatewayTLSRuntimeTests(unittest.TestCase):
    def pipeline(self):
        pipe = Pipeline.__new__(Pipeline)
        pipe.cfg = secure_config()
        pipe.frontend = MagicMock()
        pipe._frontend_guard = threading.Lock()
        pipe._frontend_stopped_reconciled = False
        pipe.store = MagicMock()
        pipe.store.contests.return_value = []
        return pipe

    def test_gateway_binds_plaintext_only_to_loopback_and_tls_to_public_interface(self):
        pipe = self.pipeline()
        conf = pipe._pool_gateway_conf({'tid':'a'*24, 'submission_mode':'folder'},
            [{'slot_no':1,'token':'a'*16,'cip':'172.18.0.2'}], '172.18.0.1')
        self.assertIn('listen 127.0.0.1:80 default_server;', conf)
        self.assertIn('listen 0.0.0.0:443 ssl default_server;', conf)
        self.assertIn('ssl_certificate /etc/noi-tls/fullchain.pem;', conf)
        self.assertIn('ssl_protocols TLSv1.2 TLSv1.3;', conf)
        self.assertNotIn('listen 0.0.0.0:80', conf)

    def test_secure_mode_never_enables_oj_http_fallback(self):
        pipe = self.pipeline()
        pipe._enable_frontend('198.51.100.10',80)
        pipe.frontend.enable.assert_not_called()
        pipe.frontend.disable.assert_called_once()

    def test_secure_mode_periodic_reconcile_closes_fallback_once(self):
        pipe = self.pipeline()
        pipe.reconcile_frontend()
        pipe.reconcile_frontend()
        pipe.frontend.enable.assert_not_called()
        pipe.frontend.disable.assert_called_once()

    @patch('services.pipeline.time.time', return_value=1000)
    def test_certificate_validity_covers_latest_deadline_and_collection_grace(self, _clock):
        pipe = self.pipeline()
        pipe.store.contests.side_effect = [[{'end_at_ms':2000000}], [{'end_at_ms':8000000}]]
        self.assertEqual(pipe._tls_validity_seconds(), 8800)

    def test_remote_certificate_preflight_is_read_only(self):
        pipe = self.pipeline()
        remote = MagicMock()
        pipe._check_remote_tls_materials(remote)
        command = remote.run.call_args.args[0]
        self.assertIn('sudo test -r /etc/noi-tls/privkey.pem', command)
        self.assertIn('-verify_hostname desktop.example.test', command)
        self.assertIn('openssl verify -purpose sslserver', command)
        self.assertIn('-checkend 3600', command)
        self.assertNotIn('docker', command)
        self.assertNotIn('cat ', command)

    def test_submission_proxy_verifies_external_https_upstream(self):
        pipe = self.pipeline()
        pipe.cfg['orchestrator']['public_base_url'] = 'https://exam.example.test'
        conf = pipe._pool_gateway_conf({'tid':'a'*24, 'submission_mode':'web'},
            [{'slot_no':1,'token':'a'*16,'cip':'172.18.0.2'}], '172.18.0.1')
        self.assertIn('proxy_ssl_verify on;', conf)
        self.assertIn('proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;', conf)

    @patch('services.pipeline.probe_novnc_gateway')
    @patch('socket.getaddrinfo', return_value=[(2,1,6,'',('198.51.100.10',443))])
    def test_direct_probe_uses_hostname_sni_and_443(self, _dns, probe):
        pipe = self.pipeline()
        pipe._probe_direct_gateway('198.51.100.10', 'a'*16)
        self.assertEqual(probe.call_args.args[:2], ('desktop.example.test',443))
        self.assertEqual(probe.call_args.kwargs['scheme'],'https')

    @patch('services.pipeline.probe_novnc_gateway')
    @patch('socket.getaddrinfo', return_value=[(2,1,6,'',('198.51.100.11',443))])
    def test_wrong_dns_never_reaches_the_gateway(self, _dns, probe):
        with self.assertRaisesRegex(RuntimeError, 'DNS'):
            self.pipeline()._probe_direct_gateway('198.51.100.10', 'a'*16)
        probe.assert_not_called()

    @patch('services.pipeline.http.client.HTTPSConnection')
    @patch('services.pipeline.http.client.HTTPConnection')
    @patch('services.pipeline.os.urandom', return_value=b'0'*16)
    def test_https_probe_checks_both_page_and_wss_without_http(self, _random, http, https):
        page = MagicMock()
        page.sock.getpeercert.return_value = {'notAfter':'Jan  1 00:00:00 2040 GMT'}
        page.getresponse.return_value.status = 200
        websocket = MagicMock()
        response = websocket.getresponse.return_value
        response.status = 101
        key = base64.b64encode(b'0'*16).decode()
        response.getheader.return_value = base64.b64encode(hashlib.sha1(
            (key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
        https.side_effect = [page,websocket]
        result = probe_novnc_gateway('desktop.example.test',443,'a'*16,
                                     quality=9,compression=2,scheme='https')
        self.assertEqual(result['websocket_status'],101)
        http.assert_not_called()
        self.assertTrue(https.call_args.kwargs['context'].check_hostname)
        self.assertEqual(websocket.request.call_args.kwargs['headers']['Origin'],
                         'https://desktop.example.test')

    @patch('services.aliyun.Client')
    def test_tls_ingress_lifecycle_keeps_only_exact_management_rules(self, client):
        ecs, api, rules = aliyun_fixtures.AliyunTests._desktop_fixture(client)
        ecs.desktop_access['port'] = 443
        https_rule = copy.copy(rules[-1])
        https_rule.port_range = '443/443'
        https_rule.security_group_rule_id = 'sgr-management-443'
        rules.append(https_rule)
        opened = ecs.ensure_desktop_access(tid='a'*24,end_at_ms=int(time.time()*1000)+60000)
        self.assertTrue(opened['open'])
        self.assertEqual(api.authorize_security_group.call_args.args[0].permissions[0].port_range,'443/443')
        self.assertTrue(ecs.revoke_desktop_access()['closed'])
        self.assertEqual({r.port_range for r in rules}, {'22/22','80/80','443/443'})

    @patch('services.aliyun.Client')
    def test_tls_never_opens_without_https_management_probe_rule(self, client):
        ecs, api, _ = aliyun_fixtures.AliyunTests._desktop_fixture(client)
        ecs.desktop_access['port'] = 443
        with self.assertRaisesRegex(RuntimeError, '443'):
            ecs.ensure_desktop_access(tid='a'*24, end_at_ms=int(time.time()*1000)+60000)
        api.authorize_security_group.assert_not_called()

    @patch('services.aliyun.Client')
    def test_tls_rejects_unmanaged_public_http_instead_of_deleting_it(self, client):
        ecs, api, rules = aliyun_fixtures.AliyunTests._desktop_fixture(client)
        ecs.desktop_access['port'] = 443
        extra = copy.copy(rules[-1])
        extra.source_cidr_ip = '0.0.0.0/0'
        extra.security_group_rule_id = 'sgr-unmanaged-public-http'
        rules.append(extra)
        with self.assertRaisesRegex(RuntimeError, '非编排服务'):
            ecs.ensure_desktop_access(tid='a'*24, end_at_ms=int(time.time()*1000)+60000)
        api.authorize_security_group.assert_not_called()
        self.assertIn(extra, rules)


class GatewayTLSWireTests(unittest.TestCase):
    """Real loopback TLS handshakes and HTTP upgrades, with disposable test PKI.

    The server emulates only the noVNC page and upgrade contract; this does not
    claim to test a GNOME desktop, browser keyboard or a production certificate.
    """

    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        cls.root = Path(temporary.name)
        now = datetime.now(timezone.utc)
        ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'NOI TEST ONLY CA')])
        ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
              .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
              .not_valid_before(now-timedelta(days=1)).not_valid_after(now+timedelta(days=2))
              .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
              .sign(ca_key, hashes.SHA256()))
        (cls.root/'ca.pem').write_bytes(ca.public_bytes(serialization.Encoding.PEM))
        for host in ('localhost', 'wrong.example.test'):
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
            certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(ca_name)
                           .public_key(key.public_key()).serial_number(x509.random_serial_number())
                           .not_valid_before(now-timedelta(minutes=1))
                           .not_valid_after(now+timedelta(minutes=20))
                           .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
                           .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                           .sign(ca_key, hashes.SHA256()))
            (cls.root/f'{host}.pem').write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
            (cls.root/f'{host}.key').write_bytes(key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption()))

    def setUp(self):
        self.requests = []
        self.server_names = []

    def start_gateway(self, certificate_host='localhost', websocket_status=101):
        requests = self.requests

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                requests.append((self.path, dict(self.headers)))
                if self.path.endswith('/websockify'):
                    self.send_response(websocket_status)
                    self.send_header('Connection', 'Upgrade')
                    self.send_header('Upgrade', 'websocket')
                    key = self.headers.get('Sec-WebSocket-Key', '')
                    accept = base64.b64encode(hashlib.sha1(
                        (key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
                    self.send_header('Sec-WebSocket-Accept', accept)
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.send_header('Content-Length', '4')
                    self.end_headers()
                    self.wfile.write(b'test')

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(self.root/f'{certificate_host}.pem', self.root/f'{certificate_host}.key')
        context.set_servername_callback(lambda _sock, host, _context: self.server_names.append(host))
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval':0.02}, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
        self.addCleanup(stop)
        return server.server_address[1]

    def trusted_context(self):
        return ssl.create_default_context(cafile=str(self.root/'ca.pem'))

    def probe(self, port, **kwargs):
        return probe_novnc_gateway('localhost', port, 'a'*16, quality=9,
                                   compression=2, timeout=3, scheme='https', **kwargs)

    def test_trusted_https_page_and_wss_upgrade_use_sni_and_https_origin(self):
        port = self.start_gateway()
        context = self.trusted_context()
        with patch('services.pipeline.ssl.create_default_context', return_value=context):
            result = self.probe(port)
        self.assertEqual(result['page_status'], 200)
        self.assertEqual(result['websocket_status'], 101)
        self.assertEqual(self.server_names, ['localhost', 'localhost'])
        self.assertEqual(self.requests[1][1]['Origin'], f'https://localhost:{port}')
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_untrusted_certificate_fails_before_any_http_request(self):
        port = self.start_gateway()
        with self.assertRaises(ssl.SSLCertVerificationError):
            self.probe(port)
        self.assertEqual(self.requests, [])

    def test_hostname_mismatch_is_not_accepted_even_with_trusted_ca(self):
        port = self.start_gateway('wrong.example.test')
        context = self.trusted_context()
        with patch('services.pipeline.ssl.create_default_context', return_value=context):
            with self.assertRaises(ssl.SSLCertVerificationError):
                self.probe(port)
        self.assertEqual(self.requests, [])

    def test_expiry_before_contest_end_is_not_accepted(self):
        port = self.start_gateway()
        context = self.trusted_context()
        with patch('services.pipeline.ssl.create_default_context', return_value=context):
            with self.assertRaisesRegex(RuntimeError, '有效期不足'):
                self.probe(port, required_valid_seconds=3600)
        self.assertEqual(self.requests, [])

    def test_https_page_alone_does_not_pass_when_websocket_is_rejected(self):
        port = self.start_gateway(websocket_status=403)
        context = self.trusted_context()
        with patch('services.pipeline.ssl.create_default_context', return_value=context):
            with self.assertRaisesRegex(RuntimeError, '403'):
                self.probe(port)

    def run_preflight(self, certificate_host, expected_host):
        """Execute generated OpenSSL arguments on fixture files, never remote."""
        openssl = shutil.which('openssl')
        if not openssl:
            self.skipTest('OpenSSL CLI is unavailable; TLS wire tests still run')
        pipe = Pipeline.__new__(Pipeline)
        pipe.cfg = secure_config()
        pipe.cfg['contest_server']['gateway_public_base_url'] = f'https://{expected_host}'
        pipe._tls_validity_seconds = lambda: 60
        remote = MagicMock()
        paths = {
            '/etc/noi-tls/fullchain.pem':str(self.root/f'{certificate_host}.pem'),
            '/etc/noi-tls/privkey.pem':str(self.root/f'{certificate_host}.key'),
            '/etc/ssl/certs/ca-certificates.crt':str(self.root/'ca.pem'),
        }

        def execute_fixture(command):
            for step in command.split(' && '):
                arguments = shlex.split(step.removesuffix(' >/dev/null'))[1:]
                arguments = [paths.get(value, value) for value in arguments]
                if arguments[0] == 'test':
                    self.assertTrue(Path(arguments[-1]).is_file())
                    continue
                arguments[0] = openssl
                result = subprocess.run(arguments, capture_output=True, timeout=5)
                if result.returncode:
                    raise RuntimeError('certificate preflight rejected')
        remote.run.side_effect = execute_fixture
        pipe._check_remote_tls_materials(remote)

    def test_real_openssl_preflight_accepts_valid_trusted_material(self):
        self.run_preflight('wrong.example.test', 'wrong.example.test')

    def test_real_openssl_preflight_rejects_wrong_hostname_before_seats(self):
        with self.assertRaisesRegex(RuntimeError, 'preflight rejected'):
            self.run_preflight('wrong.example.test', 'desktop.example.test')
