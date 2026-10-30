'use strict';

// Puts the pinned uv release in the user's cache, once, and says where it is.
//
// The archive comes from the uv release on GitHub and is checked against the
// SHA-256 this package embeds (lib/uv-release.json, generated from the
// release's .sha256 files when the package is prepared); nothing about the
// checksum is fetched at run time. The binary is written to a temporary
// directory next to its final place and renamed into it, so a reader never
// sees a half-written uv. A lock directory keeps concurrent first runs from
// downloading the same archive twice; correctness never depends on it,
// because the rename is atomic and the loser of a race keeps the winner's copy.

const crypto = require('node:crypto');
const fs = require('node:fs');
const http = require('node:http');
const https = require('node:https');
const os = require('node:os');
const path = require('node:path');

const { extract } = require('./archive');
const { WrapperError } = require('./errors');
const { UV_OVERRIDE_ENV, cacheRoot, resolveTarget } = require('./platform');
const release = require('./uv-release.json');

const RELEASE_BASE = 'https://github.com/astral-sh/uv/releases/download';
const MAX_REDIRECTS = 5;
const IDLE_TIMEOUT_MS = 30000;
const LOCK_STALE_MS = 10 * 60 * 1000;
const LOCK_POLL_MS = 100;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** GET `url` into a Buffer, following redirects; an https URL never redirects to http. */
function download(url, { userAgent = 'cloudfrog-knowledge', redirects = MAX_REDIRECTS } = {}) {
  return new Promise((resolve, reject) => {
    let parsed;
    try {
      parsed = new URL(url);
    } catch {
      reject(new Error(`invalid URL ${url}`));
      return;
    }
    const client = parsed.protocol === 'https:' ? https : parsed.protocol === 'http:' ? http : null;
    if (!client) {
      reject(new Error(`unsupported protocol in ${url}`));
      return;
    }
    const request = client.get(parsed, { headers: { 'User-Agent': userAgent } }, (response) => {
      const status = response.statusCode || 0;
      if (status >= 300 && status < 400 && response.headers.location) {
        response.resume();
        if (redirects <= 0) {
          reject(new Error(`too many redirects from ${url}`));
          return;
        }
        const next = new URL(response.headers.location, parsed);
        if (parsed.protocol === 'https:' && next.protocol !== 'https:') {
          reject(new Error(`refusing a redirect from https to ${next.protocol} (${next.href})`));
          return;
        }
        download(next.href, { userAgent, redirects: redirects - 1 }).then(resolve, reject);
        return;
      }
      if (status !== 200) {
        response.resume();
        reject(new Error(`HTTP ${status} from ${url}`));
        return;
      }
      const chunks = [];
      response.on('data', (chunk) => chunks.push(chunk));
      response.on('end', () => resolve(Buffer.concat(chunks)));
      response.on('error', reject);
    });
    request.setTimeout(IDLE_TIMEOUT_MS, () => request.destroy(new Error(`no data from ${url} for ${IDLE_TIMEOUT_MS / 1000}s`)));
    request.on('error', reject);
  });
}

function sha256(buffer) {
  return crypto.createHash('sha256').update(buffer).digest('hex');
}

function isFile(file) {
  try {
    return fs.statSync(file).isFile();
  } catch {
    return false;
  }
}

function pidAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    return err.code === 'EPERM';
  }
}

/** True when nobody can be holding `lockDir` any more: its owner died here, or it is too old. */
function lockIsStale(lockDir, staleMs) {
  try {
    const owner = JSON.parse(fs.readFileSync(path.join(lockDir, 'owner.json'), 'utf8'));
    if (owner.host === os.hostname() && Number.isInteger(owner.pid) && !pidAlive(owner.pid)) return true;
  } catch {
    // No owner file yet (or an unreadable one): judge by age alone.
  }
  try {
    return Date.now() - fs.statSync(lockDir).mtimeMs > staleMs;
  } catch {
    return false;
  }
}

/** Take `lockDir` (mkdir is atomic) and return the function that releases it. */
async function acquireLock(lockDir, { staleMs = LOCK_STALE_MS, pollMs = LOCK_POLL_MS } = {}) {
  for (;;) {
    try {
      fs.mkdirSync(lockDir);
      try {
        fs.writeFileSync(
          path.join(lockDir, 'owner.json'),
          JSON.stringify({ pid: process.pid, host: os.hostname(), at: new Date().toISOString() }),
        );
      } catch {
        // The lock holds without its owner file; only the stale check reads it.
      }
      return () => fs.rmSync(lockDir, { recursive: true, force: true });
    } catch (err) {
      if (err.code !== 'EEXIST') throw err;
    }
    if (lockIsStale(lockDir, staleMs)) {
      fs.rmSync(lockDir, { recursive: true, force: true });
      continue;
    }
    await sleep(pollMs);
  }
}

function renameInto(from, to) {
  // Windows can refuse a rename for a moment while a scanner holds the new file.
  for (let attempt = 0; ; attempt += 1) {
    try {
      fs.renameSync(from, to);
      return;
    } catch (err) {
      if (process.platform !== 'win32' || attempt >= 5 || !['EPERM', 'EACCES', 'EBUSY'].includes(err.code)) throw err;
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 100 * (attempt + 1));
    }
  }
}

/**
 * Path of the pinned uv for `target`, downloading and installing it on first use.
 *
 * `fetchArchive(url)` resolves to the archive bytes (injected by the tests). Throws
 * `E_UV_DOWNLOAD_FAILED` or `E_UV_CHECKSUM_MISMATCH`; on either nothing is
 * left in the cache.
 */
async function ensureUv({
  target,
  root,
  version = release.version,
  assets = release.assets,
  fetchArchive = download,
  log = (line) => process.stderr.write(`${line}\n`),
  lock = {},
}) {
  const finalDir = path.join(root, version, target.triple);
  const binary = path.join(finalDir, target.exe);
  if (isFile(binary)) return binary;

  const asset = assets[target.triple];
  if (!asset || asset.file !== target.archive || !/^[0-9a-f]{64}$/.test(asset.sha256 || '')) {
    throw new WrapperError('E_UV_CHECKSUM_MISSING', `this package embeds no checksum for ${target.archive}`);
  }

  const versionDir = path.join(root, version);
  fs.mkdirSync(versionDir, { recursive: true });
  const unlock = await acquireLock(path.join(versionDir, `${target.triple}.lock`), lock);
  let staging = null;
  try {
    if (isFile(binary)) return binary;
    if (fs.existsSync(finalDir)) fs.rmSync(finalDir, { recursive: true, force: true });

    const url = `${RELEASE_BASE}/${version}/${asset.file}`;
    log(`cloudfrog-knowledge: downloading uv ${version} (${target.triple})`);
    let archive;
    try {
      archive = await fetchArchive(url);
    } catch (err) {
      throw new WrapperError(
        'E_UV_DOWNLOAD_FAILED',
        `could not download uv ${version} from ${url}: ${err.message}. ` +
          `Check the network or proxy, or set ${UV_OVERRIDE_ENV} to an existing uv binary.`,
      );
    }
    const actual = sha256(archive);
    if (actual !== asset.sha256) {
      throw new WrapperError(
        'E_UV_CHECKSUM_MISMATCH',
        `${asset.file}: SHA-256 ${actual} does not match the pinned ${asset.sha256}; nothing was installed`,
      );
    }
    const bytes = extract(target.kind, archive, target.exe);

    staging = fs.mkdtempSync(path.join(versionDir, `.${target.triple}.tmp-`));
    const staged = path.join(staging, target.exe);
    fs.writeFileSync(staged, bytes, { mode: 0o755 });
    fs.chmodSync(staged, 0o755);
    try {
      renameInto(staging, finalDir);
      staging = null;
    } catch (err) {
      if (!isFile(binary)) throw err;
    }
    return binary;
  } finally {
    if (staging) fs.rmSync(staging, { recursive: true, force: true });
    unlock();
  }
}

/**
 * The uv binary to run: `CLOUDFROG_KNOWLEDGE_UV` when set, else the pinned
 * release from the cache (installed on first use).
 */
async function resolveUv({ env = process.env, platform, arch, libc, root, fetchArchive, log } = {}) {
  const override = (env[UV_OVERRIDE_ENV] || '').trim();
  if (override) {
    if (!isFile(override)) {
      throw new WrapperError('E_UV_OVERRIDE_INVALID', `${UV_OVERRIDE_ENV}=${override} is not a file`);
    }
    return override;
  }
  const target = resolveTarget({ platform, arch, libc });
  return ensureUv({ target, root: root || cacheRoot({ env, platform }), fetchArchive, log });
}

module.exports = { RELEASE_BASE, acquireLock, download, ensureUv, resolveUv, sha256 };
