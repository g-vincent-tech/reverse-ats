#!/usr/bin/env node
/**
 * Start the full Reverse ATS dev stack with one command:
 *   npm run dev
 *
 * Services:
 *   - backend  → http://localhost:8091  (Python API, self-hosted)
 *   - worker   → http://localhost:8787  (Cloudflare Worker, hosted API)
 *   - app      → http://localhost:5173  (self-hosted UI)
 *   - web      → http://localhost:5174  (marketing + hosted UI)
 *
 * Reuses any service already listening on its port.
 * Ctrl+C stops only the processes started by this script.
 */
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const BACKEND_DIR = path.join(ROOT, 'backend');
const APP_DIR = path.join(ROOT, 'app');
const WEB_DIR = path.join(ROOT, 'web');
const CLOUDFLARE_DIR = path.join(ROOT, 'cloudflare');
const BACKEND_PORT = Number(process.env.REVERSE_ATS_PORT || 8091);
const WORKER_PORT = 8787;
const APP_PORT = 5173;
const WEB_PORT = 5174;

const children = [];
const reused = [];

function portInUse(port) {
  return new Promise((resolve) => {
    const socket = net.createConnection({ port, host: '127.0.0.1' });
    socket.setTimeout(500);
    socket.once('connect', () => {
      socket.destroy();
      resolve(true);
    });
    socket.once('timeout', () => {
      socket.destroy();
      resolve(false);
    });
    socket.once('error', () => resolve(false));
  });
}

function hasNodeModules(dir) {
  return fs.existsSync(path.join(dir, 'node_modules'));
}

function resolvePython() {
  const venvPython =
    process.platform === 'win32'
      ? path.join(BACKEND_DIR, '.venv', 'Scripts', 'python.exe')
      : path.join(BACKEND_DIR, '.venv', 'bin', 'python3');

  if (fs.existsSync(venvPython)) {
    return venvPython;
  }

  return process.platform === 'win32' ? 'python' : 'python3';
}

function startProcess(label, command, args, cwd, extraEnv = {}) {
  const child = spawn(command, args, {
    cwd,
    stdio: 'inherit',
    shell: process.platform === 'win32',
    env: { ...process.env, ...extraEnv },
  });

  child.on('error', (err) => {
    console.error(`[${label}] failed to start: ${err.message}`);
    shutdown(1);
  });

  child.on('exit', (code, signal) => {
    if (signal) {
      console.log(`[${label}] stopped (${signal})`);
      return;
    }
    if (code !== 0 && code !== null) {
      console.error(`[${label}] exited with code ${code}`);
      shutdown(code);
      return;
    }
    if (children.length > 0) {
      shutdown(0);
    }
  });

  children.push(child);
  return child;
}

function shutdown(exitCode = 0) {
  for (const child of children) {
    if (child.killed || !child.pid) continue;

    if (process.platform === 'win32') {
      spawn('taskkill', ['/PID', String(child.pid), '/T', '/F'], { shell: true });
    } else {
      child.kill('SIGTERM');
    }
  }
  process.exit(exitCode);
}

process.on('SIGINT', () => {
  console.log('\n[dev] Shutting down...');
  shutdown(0);
});

process.on('SIGTERM', () => shutdown(0));

async function ensureDeps(label, dir) {
  if (hasNodeModules(dir)) return;
  console.log(`[dev] Installing ${label} dependencies...`);
  await new Promise((resolve, reject) => {
    const install = spawn('npm', ['install'], {
      cwd: dir,
      stdio: 'inherit',
      shell: process.platform === 'win32',
    });
    install.on('error', reject);
    install.on('exit', (code) => {
      if (code === 0) resolve();
      else reject(new Error(`${label} npm install failed with code ${code}`));
    });
  });
}

async function startOrReuse(name, port, url, start) {
  if (await portInUse(port)) {
    console.log(`[dev] ${name.padEnd(8)} → ${url} (already running)`);
    reused.push(name);
    return false;
  }

  console.log(`[dev] ${name.padEnd(8)} → ${url}`);
  start();
  return true;
}

async function main() {
  console.log('[dev] Reverse ATS — starting full dev stack\n');

  await startOrReuse('backend', BACKEND_PORT, `http://localhost:${BACKEND_PORT}`, () => {
    const python = resolvePython();
    startProcess(
      'backend',
      python,
      ['-m', 'uvicorn', 'api:app', '--host', '0.0.0.0', '--port', String(BACKEND_PORT)],
      BACKEND_DIR,
    );
  });

  await ensureDeps('cloudflare', CLOUDFLARE_DIR);
  const workerStarted = await startOrReuse('worker', WORKER_PORT, `http://localhost:${WORKER_PORT}`, () => {
    startProcess('worker', 'npm', ['run', 'dev', '--', '--local'], CLOUDFLARE_DIR);
  });

  await ensureDeps('app', APP_DIR);
  await startOrReuse('app', APP_PORT, `http://localhost:${APP_PORT}`, () => {
    startProcess('app', 'npm', ['run', 'dev'], APP_DIR);
  });

  await ensureDeps('web', WEB_DIR);
  const workerAvailable = workerStarted || reused.includes('worker') || (await portInUse(WORKER_PORT));
  const webEnv = workerAvailable ? { VITE_API_URL: `http://localhost:${WORKER_PORT}` } : {};
  await startOrReuse('web', WEB_PORT, `http://localhost:${WEB_PORT}`, () => {
    startProcess('web', 'npm', ['run', 'dev'], WEB_DIR, webEnv);
  });

  console.log('\n[dev] All services up. Press Ctrl+C to stop.\n');
}

main().catch((err) => {
  console.error('[dev]', err.message);
  process.exit(1);
});
