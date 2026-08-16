import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

export const NAMESPACE = 'noi-v1-capacity-telemetry';
const SAFE_SIGNER = /^[A-Za-z0-9_.@+-]{1,80}$/;
const HEX64 = /^[a-f0-9]{64}$/;
const SAFE_KEY = /^(?:Arrow(?:Left|Right|Up|Down)|[a-z0-9])$/;

export class AgentError extends Error {}

function exactObject(value, keys, label) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new AgentError(`${label} must be an object`);
  }
  const observed = Object.keys(value).sort();
  const expected = [...keys].sort();
  if (JSON.stringify(observed) !== JSON.stringify(expected)) {
    throw new AgentError(`${label} field set differs`);
  }
  return value;
}

function absolutePath(value, label) {
  if (typeof value !== 'string' || value.includes('\0') || !path.posix.isAbsolute(value)) {
    throw new AgentError(`${label} must be an absolute path`);
  }
  if (path.posix.normalize(value) !== value || value.split('/').includes('..')) {
    throw new AgentError(`${label} must be normalized`);
  }
  return value;
}

function strictOrigin(value, label, transportProfile) {
  let parsed;
  try { parsed = new URL(value); } catch { throw new AgentError(`${label} is invalid`); }
  if (parsed.username || parsed.password || parsed.hash) {
    throw new AgentError(`${label} must not embed credentials or a fragment`);
  }
  if (transportProfile === 'direct_http') {
    if (parsed.protocol !== 'http:' || !/^\d{1,3}(?:[.]\d{1,3}){3}$/.test(parsed.hostname) ||
        (parsed.port && parsed.port !== '80')) {
      throw new AgentError(`${label} must be one HTTP IPv4 URL on the direct profile`);
    }
  } else if (transportProfile === 'compat_https') {
    if (parsed.protocol !== 'https:') {
      throw new AgentError(`${label} must be HTTPS on the compatibility profile`);
    }
  } else {
    throw new AgentError('transport profile is invalid');
  }
  return parsed;
}

export function validateConfig(value) {
  const row = exactObject(value, new Set([
    'schema_version', 'transport_profile', 'qualification_marker', 'seat_set_sha256', 'seat_urls', 'rtt_url',
    'websocket_url_pattern', 'canvas_selector', 'sample_keys', 'rtt_attempts',
    'sample_interval_seconds', 'navigation_timeout_ms', 'frame_timeout_ms',
    'headless', 'signer', 'signing_key_path', 'ssh_keygen_path', 'state_path',
    'output_path'
  ]), 'agent configuration');
  if (row.schema_version !== 1) throw new AgentError('agent configuration schema differs');
  if (typeof row.qualification_marker !== 'string' ||
      !/^NOI-V1-QUAL-[A-Z0-9]{16,64}$/.test(row.qualification_marker)) {
    throw new AgentError('qualification marker is invalid');
  }
  if (typeof row.seat_set_sha256 !== 'string' || !HEX64.test(row.seat_set_sha256)) {
    throw new AgentError('seat set SHA256 is invalid');
  }
  if (!Array.isArray(row.seat_urls) || row.seat_urls.length !== 15 ||
      new Set(row.seat_urls).size !== 15) {
    throw new AgentError('configuration must bind 15 unique formal seat URLs');
  }
  const seats = row.seat_urls.map(value => strictOrigin(value, 'seat URL', row.transport_profile));
  const rtt = strictOrigin(row.rtt_url, 'RTT URL', row.transport_profile);
  if (seats.some(seat => seat.origin !== rtt.origin)) {
    throw new AgentError('qualification URLs must use one origin');
  }
  if (rtt.search || rtt.pathname === '/') {
    throw new AgentError('RTT URL must be one non-root path without a query');
  }
  if (typeof row.websocket_url_pattern !== 'string' || row.websocket_url_pattern.length > 512) {
    throw new AgentError('WebSocket URL pattern is invalid');
  }
  try { new RegExp(row.websocket_url_pattern); } catch {
    throw new AgentError('WebSocket URL pattern is invalid');
  }
  if (seatSetSha256(row) !== row.seat_set_sha256) {
    throw new AgentError('seat set SHA256 differs from the frozen browser targets');
  }
  if (row.canvas_selector !== '#noVNC_container canvas') {
    throw new AgentError('canvas selector must be the canonical noVNC canvas');
  }
  if (!Array.isArray(row.sample_keys) || row.sample_keys.length < 5 ||
      row.sample_keys.length > 30 || row.sample_keys.some(key => !SAFE_KEY.test(key))) {
    throw new AgentError('sample keys are invalid');
  }
  for (const [key, minimum, maximum] of [
    ['rtt_attempts', 5, 100], ['sample_interval_seconds', 8, 60],
    ['navigation_timeout_ms', 5000, 60000], ['frame_timeout_ms', 250, 5000]
  ]) {
    if (!Number.isInteger(row[key]) || row[key] < minimum || row[key] > maximum) {
      throw new AgentError(`${key} is invalid`);
    }
  }
  if (row.sample_interval_seconds * 1000 <=
      row.sample_keys.length * row.frame_timeout_ms + 1000) {
    throw new AgentError('sample interval is too short for the key-to-frame workload');
  }
  if (typeof row.headless !== 'boolean') throw new AgentError('headless must be boolean');
  if (typeof row.signer !== 'string' || !SAFE_SIGNER.test(row.signer)) {
    throw new AgentError('signer identity is invalid');
  }
  for (const key of ['signing_key_path', 'ssh_keygen_path', 'state_path', 'output_path']) {
    row[key] = absolutePath(row[key], key);
  }
  if (new Set([row.signing_key_path, row.state_path, row.output_path]).size !== 3) {
    throw new AgentError('private key, state, and output paths must differ');
  }
  return row;
}

export function canonicalJson(value) {
  const normalize = item => {
    if (Array.isArray(item)) return item.map(normalize);
    if (item && typeof item === 'object') {
      return Object.fromEntries(Object.keys(item).sort().map(key => [key, normalize(item[key])]));
    }
    return item;
  };
  return `${JSON.stringify(normalize(value))}\n`;
}

export function seatSetSha256(config) {
  const frozen = {
    transport_profile: config.transport_profile,
    seat_urls: [...config.seat_urls].sort(),
    rtt_url: config.rtt_url,
    websocket_url_pattern: config.websocket_url_pattern
  };
  return crypto.createHash('sha256').update(canonicalJson(frozen)).digest('hex');
}

export function requirePrivateRegular(filePath, { executable = false, allowMissing = false } = {}) {
  let info;
  try { info = fs.lstatSync(filePath); } catch (error) {
    if (allowMissing && error?.code === 'ENOENT') return null;
    throw new AgentError('required private file is unavailable');
  }
  if (!info.isFile() || info.isSymbolicLink() || info.nlink !== 1) {
    throw new AgentError('required private file metadata is unsafe');
  }
  if (process.platform !== 'win32') {
    requireSafeAncestors(filePath, false);
    if (info.uid !== process.getuid() || (info.mode & 0o077) !== 0 ||
        (executable && (info.mode & 0o100) === 0)) {
      throw new AgentError('required private file permissions are unsafe');
    }
  }
  return info;
}

function requireSafeAncestors(filePath, includeLeaf = false) {
  if (process.platform === 'win32') return;
  const parts = path.posix.normalize(filePath).split('/').filter(Boolean);
  const end = includeLeaf ? parts.length : parts.length - 1;
  let current = '/';
  for (let index = 0; index < end; index += 1) {
    current = path.posix.join(current, parts[index]);
    let info;
    try { info = fs.lstatSync(current); } catch {
      throw new AgentError('required path ancestor is unavailable');
    }
    if (!info.isDirectory() || info.isSymbolicLink() ||
        ![0, process.getuid()].includes(info.uid) || (info.mode & 0o022) !== 0) {
      throw new AgentError('required path ancestor is unsafe');
    }
  }
}

export function requireTrustedExecutable(filePath) {
  let resolved;
  let info;
  try {
    resolved = fs.realpathSync(filePath);
    info = fs.lstatSync(resolved);
  } catch {
    throw new AgentError('trusted executable is unavailable');
  }
  if (process.platform !== 'win32') {
    requireSafeAncestors(resolved, false);
    if (resolved !== filePath || !info.isFile() || info.isSymbolicLink() ||
        info.nlink !== 1 || info.uid !== 0 || (info.mode & 0o022) !== 0 ||
        (info.mode & 0o111) === 0) {
      throw new AgentError('trusted executable metadata is unsafe');
    }
  }
  return resolved;
}

export function requirePrivateParent(filePath) {
  const parent = path.dirname(filePath);
  let info;
  try { info = fs.lstatSync(parent); } catch {
    throw new AgentError('private output parent is unavailable');
  }
  if (!info.isDirectory() || info.isSymbolicLink()) {
    throw new AgentError('private output parent is unsafe');
  }
  if (process.platform !== 'win32' &&
      (info.uid !== process.getuid() || (info.mode & 0o077) !== 0)) {
    throw new AgentError('private output parent permissions are unsafe');
  }
  requireSafeAncestors(filePath, false);
  return parent;
}

export function readSequence(statePath) {
  const info = requirePrivateRegular(statePath, { allowMissing: true });
  if (!info) return 0;
  let value;
  try { value = JSON.parse(fs.readFileSync(statePath, 'utf8')); } catch {
    throw new AgentError('sequence state is invalid JSON');
  }
  exactObject(value, new Set(['schema_version', 'sequence']), 'sequence state');
  if (value.schema_version !== 1 || !Number.isSafeInteger(value.sequence) || value.sequence < 1) {
    throw new AgentError('sequence state is invalid');
  }
  return value.sequence;
}

export function atomicWrite(filePath, bytes, mode = 0o600) {
  const parent = requirePrivateParent(filePath);
  const temporary = path.join(parent, `.noi-v1-${process.pid}-${crypto.randomBytes(12).toString('hex')}`);
  let descriptor;
  try {
    descriptor = fs.openSync(temporary, fs.constants.O_CREAT | fs.constants.O_EXCL |
      fs.constants.O_WRONLY | fs.constants.O_NOFOLLOW, mode);
    fs.writeFileSync(descriptor, bytes);
    fs.fsyncSync(descriptor);
    fs.closeSync(descriptor); descriptor = undefined;
    fs.renameSync(temporary, filePath);
    if (process.platform !== 'win32') {
      const directory = fs.openSync(parent, fs.constants.O_RDONLY);
      try { fs.fsyncSync(directory); } finally { fs.closeSync(directory); }
    }
  } finally {
    if (descriptor !== undefined) fs.closeSync(descriptor);
    try { fs.unlinkSync(temporary); } catch {}
  }
}

export function reserveNextSequence(statePath) {
  const current = readSequence(statePath);
  if (current >= Number.MAX_SAFE_INTEGER - 1) throw new AgentError('sequence exhausted');
  const next = current + 1;
  atomicWrite(statePath, canonicalJson({ schema_version: 1, sequence: next }));
  return next;
}

export function validateTelemetry(payload) {
  const row = exactObject(payload, new Set([
    'schema_version', 'transport_profile', 'qualification_marker', 'seat_set_sha256', 'formal_seat_count', 'sequence',
    'window_started_at', 'observed_at',
    'rtt_samples_ms', 'packet_loss_percent', 'websocket_reconnects',
    'key_to_frame_samples_ms'
  ]), 'telemetry payload');
  if (row.schema_version !== 1 || !Number.isSafeInteger(row.sequence) || row.sequence < 1) {
    throw new AgentError('telemetry identity is invalid');
  }
  if (typeof row.qualification_marker !== 'string' ||
      !/^NOI-V1-QUAL-[A-Z0-9]{16,64}$/.test(row.qualification_marker)) {
    throw new AgentError('telemetry qualification marker is invalid');
  }
  if (typeof row.seat_set_sha256 !== 'string' || !HEX64.test(row.seat_set_sha256)) {
    throw new AgentError('telemetry seat set SHA256 is invalid');
  }
  if (!['direct_http', 'compat_https'].includes(row.transport_profile)) {
    throw new AgentError('telemetry transport profile is invalid');
  }
  if (row.formal_seat_count !== 15) {
    throw new AgentError('telemetry must bind 15 formal browser seats');
  }
  const started = Date.parse(row.window_started_at);
  const observed = Date.parse(row.observed_at);
  if (!Number.isFinite(started) || !Number.isFinite(observed) || observed <= started ||
      observed - started > 60000) throw new AgentError('telemetry window is invalid');
  for (const key of ['rtt_samples_ms', 'key_to_frame_samples_ms']) {
    if (!Array.isArray(row[key]) || row[key].length < 5 || row[key].length > 10000 ||
        row[key].some(value => typeof value !== 'number' || !Number.isFinite(value) || value <= 0)) {
      throw new AgentError(`${key} is invalid`);
    }
  }
  if (typeof row.packet_loss_percent !== 'number' || !Number.isFinite(row.packet_loss_percent) ||
      row.packet_loss_percent < 0 || row.packet_loss_percent > 100 ||
      !Number.isInteger(row.websocket_reconnects) || row.websocket_reconnects < 0) {
    throw new AgentError('telemetry counters are invalid');
  }
  return row;
}

export function envelope(payload, signer, signatureBytes) {
  validateTelemetry(payload);
  if (!Buffer.isBuffer(signatureBytes) || signatureBytes.length < 32) {
    throw new AgentError('signature bytes are invalid');
  }
  return {
    schema_version: 1,
    namespace: NAMESPACE,
    signer,
    payload,
    signature_base64: signatureBytes.toString('base64')
  };
}
