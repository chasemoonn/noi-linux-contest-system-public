import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import {
  AgentError, atomicWrite, canonicalJson, envelope, readSequence,
  reserveNextSequence, validateConfig, validateTelemetry
  , seatSetSha256
} from '../lib.mjs';

function config(root = '/root/noi-v1-browser') {
  const marker = 'NOI-V1-QUAL-1234567890ABCDEF';
  const row = {
    schema_version: 1,
    transport_profile: 'compat_https',
    qualification_marker: 'NOI-V1-QUAL-1234567890ABCDEF',
    seat_set_sha256: '',
    seat_urls: Array.from({ length: 15 }, (_, index) =>
      `https://qualification.example/s/private-${index + 1}?qualification_marker=${marker}`),
    rtt_url: 'https://qualification.example/healthz',
    websocket_url_pattern: '^wss://qualification[.]example/.+$',
    canvas_selector: '#noVNC_container canvas',
    sample_keys: ['a', 'b', 'c', 'd', 'e'],
    rtt_attempts: 10,
    sample_interval_seconds: 10,
    navigation_timeout_ms: 10000,
    frame_timeout_ms: 1000,
    headless: true,
    signer: 'hangzhou-browser-agent',
    signing_key_path: `${root}/signing-key`,
    ssh_keygen_path: '/usr/bin/ssh-keygen',
    state_path: `${root}/sequence.json`,
    output_path: `${root}/telemetry-envelope.json`
  };
  row.seat_set_sha256 = seatSetSha256(row);
  return row;
}

function payload(sequence = 1) {
  return {
    schema_version: 1,
    transport_profile: 'compat_https',
    qualification_marker: 'NOI-V1-QUAL-1234567890ABCDEF',
    seat_set_sha256: 'a'.repeat(64),
    formal_seat_count: 15,
    sequence,
    window_started_at: '2026-08-13T00:00:00.000Z',
    observed_at: '2026-08-13T00:00:05.000Z',
    rtt_samples_ms: [10, 11, 12, 13, 14],
    packet_loss_percent: 0,
    websocket_reconnects: 0,
    key_to_frame_samples_ms: [20, 21, 22, 23, 24]
  };
}

test('configuration binds the exact seat target set on one origin', () => {
  assert.equal(validateConfig(config()).qualification_marker, 'NOI-V1-QUAL-1234567890ABCDEF');
  const wrong = config();
  wrong.seat_urls[0] = 'https://qualification.example/s/private';
  assert.throws(() => validateConfig(wrong), /SHA256 differs/);
  const forged = config();
  forged.seat_set_sha256 = '0'.repeat(64);
  assert.throws(() => validateConfig(forged), /SHA256 differs/);
});

test('configuration refuses an interval too short for key samples', () => {
  const wrong = config();
  wrong.sample_interval_seconds = 8;
  wrong.frame_timeout_ms = 2000;
  assert.throws(() => validateConfig(wrong), /too short/);
});

test('direct profile is explicit and accepts only a plain HTTP IPv4 origin', () => {
  const direct = config();
  direct.transport_profile = 'direct_http';
  direct.seat_urls = Array.from({ length: 15 }, (_, index) =>
    `http://203.0.113.9/s/private-${index + 1}?qualification_marker=NOI-V1-QUAL-1234567890ABCDEF`);
  direct.rtt_url = 'http://203.0.113.9/healthz';
  direct.seat_set_sha256 = seatSetSha256(direct);
  assert.equal(validateConfig(direct).transport_profile, 'direct_http');
  direct.seat_urls[0] = 'http://qualification.example/s/private?qualification_marker=NOI-V1-QUAL-1234567890ABCDEF';
  assert.throws(() => validateConfig(direct), /HTTP IPv4/);
});

test('canonical JSON recursively sorts keys and ends with one newline', () => {
  assert.equal(canonicalJson({ z: 1, a: { y: 2, b: 3 } }), '{"a":{"b":3,"y":2},"z":1}\n');
});

test('sequence state advances durably and rejects malformed state', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'noi-v1-agent-test-'));
  try {
    const state = path.join(root, 'state.json');
    assert.equal(readSequence(state), 0);
    assert.equal(reserveNextSequence(state), 1);
    assert.equal(reserveNextSequence(state), 2);
    fs.writeFileSync(state, '{}\n', { mode: 0o600 });
    assert.throws(() => readSequence(state), /field set differs/);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('atomic write replaces the complete envelope without temporary residue', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'noi-v1-agent-test-'));
  try {
    const output = path.join(root, 'envelope.json');
    atomicWrite(output, 'first\n');
    atomicWrite(output, 'second\n');
    assert.equal(fs.readFileSync(output, 'utf8'), 'second\n');
    assert.deepEqual(fs.readdirSync(root), ['envelope.json']);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('atomic write rejects a missing parent', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'noi-v1-agent-test-'));
  try {
    assert.throws(
      () => atomicWrite(path.join(root, 'missing', 'envelope.json'), 'x\n'),
      /parent is unavailable/
    );
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('signed envelope carries no URL token or browser configuration', () => {
  const row = validateTelemetry(payload());
  const result = envelope(row, 'hangzhou-browser-agent', Buffer.alloc(64, 7));
  const serialized = canonicalJson(result);
  assert.match(serialized, /noi-v1-capacity-telemetry/);
  assert.doesNotMatch(serialized, /qualification[.]example|private|signing-key/);
  assert.equal(result.payload.seat_set_sha256, 'a'.repeat(64));
});

test('telemetry rejects missing samples and non-increasing windows', () => {
  const wrong = payload();
  wrong.rtt_samples_ms = [1, 2, 3, 4];
  assert.throws(() => validateTelemetry(wrong), /rtt_samples_ms/);
  const reversed = payload();
  reversed.observed_at = reversed.window_started_at;
  assert.throws(() => validateTelemetry(reversed), /window/);
});

test('telemetry rejects negative reconnect deltas', () => {
  const wrong = payload();
  wrong.websocket_reconnects = -1;
  assert.throws(() => validateTelemetry(wrong), /counters/);
});
