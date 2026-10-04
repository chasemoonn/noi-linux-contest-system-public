import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { once } from 'node:events';
import fs from 'node:fs';
import https from 'node:https';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { measureDirectTlsRtt } from '../lib.mjs';

const openssl = process.env.NOI_TEST_OPENSSL || 'openssl';
const available = spawnSync(openssl, ['version'], { timeout: 5000 }).status === 0;

// A real loopback TLS exchange, not a mocked TLS success. Tests alone substitute
// an ephemeral local port and a freshly generated test CA; production has no
// ignore-certificate or custom-port config switch.
test('native TLS RTT verifies CA/hostname/peer and refuses redirects', { skip: !available }, async t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'noi-capacity-tls-'));
  const key = path.join(root, 'key.pem'), cert = path.join(root, 'cert.pem');
  try {
    const generated = spawnSync(openssl, [
      'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
      '-subj', '/CN=desktop.example.test', '-addext', 'subjectAltName=DNS:desktop.example.test',
      '-keyout', key, '-out', cert,
    ], { timeout: 15000, stdio: 'pipe' });
    assert.equal(generated.status, 0, generated.stderr?.toString());
    const ca = fs.readFileSync(cert);
    let status = 200, serverName = '';
    const server = https.createServer({ key: fs.readFileSync(key), cert: ca }, (req, res) => {
      serverName = req.socket.servername;
      res.writeHead(status, status === 302 ? { Location: 'https://other.example.test/' } : {});
      res.end('fixture');
    });
    server.listen(0, '127.0.0.1');
    await once(server, 'listening');
    try {
      const config = { rtt_url: 'https://desktop.example.test/s/test-only-seat/vnc.html',
        direct_endpoint_ipv4: '127.0.0.1', navigation_timeout_ms: 3000 };
      const request = (overrides = {}) => (options, callback) => https.request({
        ...options, port: server.address().port, ca, ...overrides,
      }, callback);
      await t.test('trusted endpoint returns a positive measured RTT and correct SNI', async () => {
        assert.ok(await measureDirectTlsRtt(config, request()) > 0);
        assert.equal(serverName, 'desktop.example.test');
      });
      await t.test('untrusted certificate is rejected', async () => {
        await assert.rejects(measureDirectTlsRtt(config, request({ ca: undefined })), /certificate/);
      });
      await t.test('trusted certificate with wrong hostname is rejected', async () => {
        await assert.rejects(measureDirectTlsRtt(
          { ...config, rtt_url: config.rtt_url.replace('desktop.example.test', 'wrong.example.test') }, request()
        ), /certificate/);
      });
      await t.test('HTTP redirect is rejected without following Location', async () => {
        status = 302;
        await assert.rejects(measureDirectTlsRtt(config, request()), /HTTP checks/);
        status = 200;
      });
      await t.test('unexpected network peer is rejected', async () => {
        await assert.rejects(measureDirectTlsRtt({ ...config, direct_endpoint_ipv4: '203.0.113.9' },
          request({ lookup: (_hostname, options, callback) => options.all
            ? callback(null, [{ address: '127.0.0.1', family: 4 }])
            : callback(null, '127.0.0.1', 4) })), /endpoint/);
      });
    } finally {
      server.closeAllConnections();
      await new Promise(resolve => server.close(resolve));
    }
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
