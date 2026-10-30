'use strict';

// Which uv release archive serves this machine, and where its cache lives.

const os = require('node:os');
const path = require('node:path');

const { WrapperError } = require('./errors');

/** Node's `platform-arch[-libc]` to the target triple of a uv release archive. */
const TARGETS = Object.freeze({
  'darwin-arm64': 'aarch64-apple-darwin',
  'darwin-x64': 'x86_64-apple-darwin',
  'linux-arm64-gnu': 'aarch64-unknown-linux-gnu',
  'linux-arm64-musl': 'aarch64-unknown-linux-musl',
  'linux-x64-gnu': 'x86_64-unknown-linux-gnu',
  'linux-x64-musl': 'x86_64-unknown-linux-musl',
  'win32-arm64': 'aarch64-pc-windows-msvc',
  'win32-x64': 'x86_64-pc-windows-msvc',
});

/** Environment variable that names an existing uv binary and skips the download. */
const UV_OVERRIDE_ENV = 'CLOUDFROG_KNOWLEDGE_UV';

/**
 * `gnu` when the running Node links glibc, `musl` otherwise.
 *
 * Node's diagnostic report carries `glibcVersionRuntime` only on glibc. The
 * musl build of uv is static, so it is also the safe answer for a libc that
 * cannot be identified.
 */
function detectLibc() {
  try {
    if (process.report) {
      if ('excludeNetwork' in process.report) process.report.excludeNetwork = true;
      const report = process.report.getReport();
      const header = typeof report === 'string' ? JSON.parse(report).header : report.header;
      if (header && header.glibcVersionRuntime) return 'gnu';
    }
  } catch {
    // No report: fall through to the static build.
  }
  return 'musl';
}

/**
 * The uv target for a platform: `{key, triple, archive, kind, exe}`.
 *
 * Throws `E_UV_UNSUPPORTED_PLATFORM` for a combination uv publishes no
 * archive for here.
 */
function resolveTarget({ platform = process.platform, arch = process.arch, libc } = {}) {
  let key = `${platform}-${arch}`;
  if (platform === 'linux') key += `-${libc || detectLibc()}`;
  const triple = TARGETS[key];
  if (!triple) {
    const supported = Object.keys(TARGETS).join(', ');
    throw new WrapperError(
      'E_UV_UNSUPPORTED_PLATFORM',
      `no uv build for ${key}; supported: ${supported}. ` +
        `Install uv yourself and set ${UV_OVERRIDE_ENV} to its path, or run ` +
        '`uvx cloudfrog-knowledge` directly.',
    );
  }
  const windows = platform === 'win32';
  const kind = windows ? 'zip' : 'tar.gz';
  return { key, triple, archive: `uv-${triple}.${kind}`, kind, exe: windows ? 'uv.exe' : 'uv' };
}

/**
 * Root of the uv cache: `<cache home>/cloudfrog-knowledge/uv`.
 *
 * The cache home is `%LOCALAPPDATA%` on Windows and `$XDG_CACHE_HOME` (only
 * when absolute, as the XDG specification requires) or `~/.cache` elsewhere.
 */
function cacheRoot({ env = process.env, platform = process.platform, homedir = os.homedir() } = {}) {
  let base;
  if (platform === 'win32') {
    base = env.LOCALAPPDATA || path.win32.join(homedir, 'AppData', 'Local');
    return path.win32.join(base, 'cloudfrog-knowledge', 'uv');
  }
  const xdg = env.XDG_CACHE_HOME;
  base = xdg && path.posix.isAbsolute(xdg) ? xdg : path.posix.join(homedir, '.cache');
  return path.posix.join(base, 'cloudfrog-knowledge', 'uv');
}

module.exports = { TARGETS, UV_OVERRIDE_ENV, cacheRoot, detectLibc, resolveTarget };
