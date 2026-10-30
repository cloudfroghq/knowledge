'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');

const { TARGETS, UV_OVERRIDE_ENV, cacheRoot, detectLibc, resolveTarget } = require('../lib/platform');
const release = require('../lib/uv-release.json');

test('every supported platform maps to its uv release archive', () => {
  const cases = [
    [{ platform: 'darwin', arch: 'arm64' }, 'aarch64-apple-darwin', 'tar.gz', 'uv'],
    [{ platform: 'darwin', arch: 'x64' }, 'x86_64-apple-darwin', 'tar.gz', 'uv'],
    [{ platform: 'linux', arch: 'x64', libc: 'gnu' }, 'x86_64-unknown-linux-gnu', 'tar.gz', 'uv'],
    [{ platform: 'linux', arch: 'x64', libc: 'musl' }, 'x86_64-unknown-linux-musl', 'tar.gz', 'uv'],
    [{ platform: 'linux', arch: 'arm64', libc: 'gnu' }, 'aarch64-unknown-linux-gnu', 'tar.gz', 'uv'],
    [{ platform: 'linux', arch: 'arm64', libc: 'musl' }, 'aarch64-unknown-linux-musl', 'tar.gz', 'uv'],
    [{ platform: 'win32', arch: 'x64' }, 'x86_64-pc-windows-msvc', 'zip', 'uv.exe'],
    [{ platform: 'win32', arch: 'arm64' }, 'aarch64-pc-windows-msvc', 'zip', 'uv.exe'],
  ];
  for (const [input, triple, kind, exe] of cases) {
    const target = resolveTarget(input);
    assert.equal(target.triple, triple);
    assert.equal(target.kind, kind);
    assert.equal(target.exe, exe);
    assert.equal(target.archive, `uv-${triple}.${kind}`);
  }
  assert.equal(cases.length, Object.keys(TARGETS).length);
});

test('an unsupported platform is a clear error that names the way out', () => {
  for (const input of [
    { platform: 'freebsd', arch: 'x64' },
    { platform: 'linux', arch: 'ia32', libc: 'gnu' },
    { platform: 'linux', arch: 'arm', libc: 'musl' },
    { platform: 'win32', arch: 'ia32' },
    { platform: 'darwin', arch: 'ppc64' },
  ]) {
    assert.throws(
      () => resolveTarget(input),
      (err) =>
        err.code === 'E_UV_UNSUPPORTED_PLATFORM' &&
        err.message.includes(UV_OVERRIDE_ENV) &&
        err.message.includes('darwin-arm64') &&
        err.message.startsWith(`no uv build for ${input.platform}-${input.arch}`),
    );
  }
});

test('linux without an explicit libc uses the detected one', () => {
  const libc = detectLibc();
  assert.ok(['gnu', 'musl'].includes(libc));
  assert.equal(resolveTarget({ platform: 'linux', arch: 'x64' }).key, `linux-x64-${libc}`);
});

test('the package embeds one checksum per supported archive and nothing else', () => {
  assert.match(release.version, /^\d+\.\d+\.\d+$/);
  assert.deepEqual(Object.keys(release.assets).sort(), Object.values(TARGETS).sort());
  for (const key of Object.keys(TARGETS)) {
    const [platform, arch, libc] = key.split('-');
    const target = resolveTarget({ platform, arch, libc });
    const asset = release.assets[target.triple];
    assert.equal(asset.file, target.archive);
    assert.match(asset.sha256, /^[0-9a-f]{64}$/);
  }
});

test('the cache lives under the platform cache home', () => {
  assert.equal(
    cacheRoot({ env: { XDG_CACHE_HOME: '/srv/cache' }, platform: 'linux', homedir: '/home/me' }),
    '/srv/cache/cloudfrog-knowledge/uv',
  );
  assert.equal(
    cacheRoot({ env: { XDG_CACHE_HOME: 'relative/cache' }, platform: 'linux', homedir: '/home/me' }),
    '/home/me/.cache/cloudfrog-knowledge/uv',
  );
  assert.equal(cacheRoot({ env: {}, platform: 'darwin', homedir: '/Users/me' }), '/Users/me/.cache/cloudfrog-knowledge/uv');
  assert.equal(
    cacheRoot({ env: { LOCALAPPDATA: 'C:\\Users\\me\\AppData\\Local' }, platform: 'win32', homedir: 'C:\\Users\\me' }),
    'C:\\Users\\me\\AppData\\Local\\cloudfrog-knowledge\\uv',
  );
  assert.equal(
    cacheRoot({ env: {}, platform: 'win32', homedir: 'C:\\Users\\me' }),
    'C:\\Users\\me\\AppData\\Local\\cloudfrog-knowledge\\uv',
  );
});
