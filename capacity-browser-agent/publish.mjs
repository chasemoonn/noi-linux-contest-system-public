#!/usr/bin/env node
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import { AgentError, requirePrivateRegular, requireTrustedExecutable } from './lib.mjs';

function fail(message) {
  process.stderr.write(`NO_GO: ${message}\n`);
  process.exit(2);
}

try {
  if (process.platform !== 'linux' || typeof process.getuid !== 'function' || process.getuid() === 0) {
    throw new AgentError('publisher must run as the dedicated non-root Linux agent user');
  }
  const args = process.argv.slice(2);
  if (args.length !== 10 || args[0] !== '--envelope' || args[2] !== '--ssh' ||
      args[4] !== '--identity' || args[6] !== '--known-hosts' || args[8] !== '--target') {
    throw new AgentError('usage: publish.mjs --envelope PATH --ssh PATH --identity PATH --known-hosts PATH --target USER@HOST');
  }
  const envelope = args[1];
  const ssh = args[3];
  const identity = args[5];
  const knownHosts = args[7];
  const target = args[9];
  if (![envelope, identity, knownHosts].every(item =>
        path.posix.isAbsolute(item) && path.posix.normalize(item) === item) ||
      !/^[A-Za-z0-9_.-]{1,64}@[A-Za-z0-9.-]{1,253}$/.test(target)) {
    throw new AgentError('publisher arguments are invalid');
  }
  requirePrivateRegular(envelope);
  requirePrivateRegular(identity);
  requirePrivateRegular(knownHosts);
  const sshBinary = requireTrustedExecutable(ssh);
  const descriptor = fs.openSync(envelope, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
  try {
    const result = spawnSync(sshBinary, [
      '-T', '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
      '-i', identity, '-o', `UserKnownHostsFile=${knownHosts}`,
      '-o', 'GlobalKnownHostsFile=/dev/null', '-o', 'PasswordAuthentication=no',
      '-o', 'KbdInteractiveAuthentication=no',
      '-o', 'StrictHostKeyChecking=yes', '-o', 'UpdateHostKeys=no',
      '-o', 'ClearAllForwardings=yes', '-o', 'PermitLocalCommand=no',
      '-o', 'RequestTTY=no', target
    ], { stdio: [descriptor, 'pipe', 'pipe'], encoding: 'utf8', timeout: 15000 });
    if (result.status !== 0 || result.error || !/^TELEMETRY_INSTALLED sequence=[1-9][0-9]*\r?\n$/.test(result.stdout)) {
      throw new AgentError('remote telemetry installation failed');
    }
    process.stdout.write(result.stdout);
  } finally {
    fs.closeSync(descriptor);
  }
} catch (error) {
  fail(error instanceof AgentError ? error.message : 'publisher failed');
}
