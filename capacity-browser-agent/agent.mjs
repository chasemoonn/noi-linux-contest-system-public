#!/usr/bin/env node
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import { chromium } from 'playwright';
import { installEchoProbe, focusRemoteEcho, measureRemoteEcho } from './echo-probe.mjs';
import {
  AgentError, NAMESPACE, atomicWrite, canonicalJson, envelope,
  requirePrivateRegular, requireTrustedExecutable, reserveNextSequence,
  validateConfig, validateTelemetry, verifyDirectNavigation,
  trackSeatSocket, assertSeatSockets, measureDirectTlsRtt
} from './lib.mjs';

function loadConfig(filePath) {
  if (!path.posix.isAbsolute(filePath) || path.posix.normalize(filePath) !== filePath) {
    throw new AgentError('configuration path must be absolute and normalized');
  }
  requirePrivateRegular(filePath);
  let value;
  try { value = JSON.parse(fs.readFileSync(filePath, 'utf8')); } catch {
    throw new AgentError('configuration is not strict JSON');
  }
  return validateConfig(value);
}

function signPayload(config, payload) {
  requirePrivateRegular(config.signing_key_path);
  const sshKeygen = requireTrustedExecutable(config.ssh_keygen_path);
  const directory = fs.mkdtempSync(path.join(path.dirname(config.state_path), '.noi-v1-sign-'));
  fs.chmodSync(directory, 0o700);
  const payloadPath = path.join(directory, 'payload.json');
  try {
    fs.writeFileSync(payloadPath, canonicalJson(payload), { mode: 0o600, flag: 'wx' });
    const result = spawnSync(sshKeygen, [
      '-q', '-Y', 'sign', '-f', config.signing_key_path, '-n', NAMESPACE, payloadPath
    ], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'], timeout: 10000 });
    if (result.status !== 0 || result.error) {
      throw new AgentError('telemetry signing failed');
    }
    const signaturePath = `${payloadPath}.sig`;
    requirePrivateRegular(signaturePath);
    return fs.readFileSync(signaturePath);
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
}

async function collectSample(config, pages, context, websocketState) {
  const windowStarted = new Date();
  const websocketOpensBefore = websocketState.opens;
  const attempts = await Promise.all(Array.from({ length: config.rtt_attempts }, async () => {
    const started = performance.now();
    try {
      if (config.transport_profile === 'direct_https') return await measureDirectTlsRtt(config);
      const response = await context.request.get(config.rtt_url, {
        timeout: config.navigation_timeout_ms,
        failOnStatusCode: false
      });
      if (response.status() !== 200) return null;
      return performance.now() - started;
    } catch { return null; }
  }));
  const rttSamples = attempts.filter(value => value !== null);
  const failedRtt = config.rtt_attempts - rttSamples.length;
  const keySamples = [];
  const canvases = pages.map(page => page.locator(config.canvas_selector));
  await Promise.all(canvases.map(canvas =>
    canvas.waitFor({ state: 'visible', timeout: config.navigation_timeout_ms })));
  const regions = await Promise.all(pages.map(page => focusRemoteEcho(page, config.canvas_selector)));
  for (const key of config.sample_keys) {
    const values = await Promise.all(pages.map(async (page, index) => {
      try {
        return await measureRemoteEcho(page, key, regions[index], config.frame_timeout_ms);
      } catch (error) {
        throw new AgentError(`seat ${index + 1} remote key acknowledgment failed: ${error.message}`);
      }
    }));
    keySamples.push(...values);
  }
  if (rttSamples.length < 5 || keySamples.length < 5) {
    throw new AgentError('browser sample does not contain enough successful observations');
  }
  assertSeatSockets(websocketState);
  const elapsed = Date.now() - windowStarted.getTime();
  if (elapsed < 1000) {
    await new Promise(resolve => setTimeout(resolve, 1000 - elapsed));
  }
  const sequence = reserveNextSequence(config.state_path);
  const payload = validateTelemetry({
    schema_version: 1,
    transport_profile: config.transport_profile,
    qualification_marker: config.qualification_marker,
    seat_set_sha256: config.seat_set_sha256,
    formal_seat_count: 15,
    sequence,
    window_started_at: windowStarted.toISOString(),
    observed_at: new Date().toISOString(),
    rtt_samples_ms: rttSamples.map(value => Number(value.toFixed(3))),
    packet_loss_percent: Number((failedRtt * 100 / config.rtt_attempts).toFixed(3)),
    websocket_reconnects: websocketState.opens - websocketOpensBefore,
    key_to_frame_samples_ms: keySamples.map(value => Number(value.toFixed(3)))
  });
  const signature = signPayload(config, payload);
  atomicWrite(config.output_path, canonicalJson(envelope(payload, config.signer, signature)));
  return sequence;
}

async function run(config, loop) {
  const browser = await chromium.launch({
    headless: config.headless,
    args: [
      '--disable-background-timer-throttling',
      '--disable-renderer-backgrounding',
      '--disable-backgrounding-occluded-windows'
    ]
  });
  try {
    const context = await browser.newContext({
      ignoreHTTPSErrors: false,
      serviceWorkers: 'block'
    });
    await context.addInitScript(installEchoProbe, { canvasSelector: config.canvas_selector });
    const websocketState = { opens: 0, closes: 0, connected: Array(15).fill(0), invalid: false };
    const pages = [];
    for (const seatUrl of config.seat_urls) {
      const page = await context.newPage();
      const seatIndex = pages.length;
      page.setDefaultTimeout(config.navigation_timeout_ms);
      page.on('websocket', socket => {
        trackSeatSocket(config, websocketState, seatIndex, socket);
      });
      const response = await page.goto(seatUrl, {
        waitUntil: 'domcontentloaded', timeout: config.navigation_timeout_ms
      });
      if (!response || response.status() !== 200 ||
          new URL(page.url()).origin !== new URL(seatUrl).origin) {
        throw new AgentError('qualification seat navigation failed or changed origin');
      }
      if (config.transport_profile === 'direct_https') {
        verifyDirectNavigation(config, seatUrl, page.url(), await response.serverAddr());
      }
      await page.locator(config.canvas_selector).waitFor({ state: 'visible' });
      pages.push(page);
    }
    const websocketDeadline = Date.now() + config.navigation_timeout_ms;
    while (websocketState.opens < 15 && Date.now() < websocketDeadline) {
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    if (websocketState.opens !== 15) {
      throw new AgentError('qualification seats did not establish exactly 15 initial WebSockets');
    }
    assertSeatSockets(websocketState);
    do {
      const started = performance.now();
      await collectSample(config, pages, context, websocketState);
      if (!loop) break;
      const delay = config.sample_interval_seconds * 1000 - (performance.now() - started);
      if (delay <= 0) throw new AgentError('browser sampling missed its fixed cadence');
      await new Promise(resolve => setTimeout(resolve, delay));
    } while (true);
  } finally {
    await browser.close();
  }
}

async function main() {
  if (process.platform !== 'linux' || typeof process.getuid !== 'function' || process.getuid() === 0) {
    throw new AgentError('browser agent must run as one dedicated non-root Linux user');
  }
  process.umask(0o077);
  const args = process.argv.slice(2);
  const loopIndex = args.indexOf('--loop');
  const loop = loopIndex !== -1;
  if (loop) args.splice(loopIndex, 1);
  if (args.length !== 2 || args[0] !== '--config') {
    throw new AgentError('usage: agent.mjs --config ABSOLUTE_PATH [--loop]');
  }
  const configPath = args[1];
  if (!configPath) throw new AgentError('configuration path is required');
  const config = loadConfig(configPath);
  await run(config, loop);
}

main().catch(error => {
  const message = error instanceof AgentError ? error.message : 'browser agent failed';
  process.stderr.write(`NO_GO: ${message}\n`);
  process.exitCode = 2;
});
