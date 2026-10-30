'use strict';

const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const { RELEASE_BASE, acquireLock, download, ensureUv, resolveUv } = require('../lib/install');
const { fakeRelease, makeZip, sha256, tmpdir } = require('./helpers');

const VERSION = '0.0.1';
const quiet = () => {};

function counting(archive, delayMs = 0) {
  const calls = [];
  const fetchArchive = async (url) => {
    calls.push(url);
    if (delayMs) await new Promise((resolve) => setTimeout(resolve, delayMs));
    return archive;
  };
  return { calls, fetchArchive };
}

function leftovers(root) {
  const dir = path.join(root, VERSION);
  return fs.existsSync(dir) ? fs.readdirSync(dir).sort() : [];
}

test('a verified archive is installed once and reused from the cache', async (t) => {
  const root = tmpdir(t);
  const { archive, assets, binary, target } = fakeRelease();
  const { calls, fetchArchive } = counting(archive);

  const first = await ensureUv({ target, root, version: VERSION, assets, fetchArchive, log: quiet });
  assert.equal(first, path.join(root, VERSION, target.triple, 'uv'));
  assert.deepEqual(fs.readFileSync(first), binary);
  if (process.platform !== 'win32') assert.equal(fs.statSync(first).mode & 0o777, 0o755);
  assert.deepEqual(calls, [`${RELEASE_BASE}/${VERSION}/uv-${target.triple}.tar.gz`]);

  const again = await ensureUv({ target, root, version: VERSION, assets, fetchArchive, log: quiet });
  assert.equal(again, first);
  assert.equal(calls.length, 1, 'the second run must not download');
  assert.deepEqual(leftovers(root), [target.triple]);
});

test('a checksum mismatch rejects the archive and installs nothing', async (t) => {
  const root = tmpdir(t);
  const { archive, assets, target } = fakeRelease();
  const tampered = Buffer.concat([archive, Buffer.from('x')]);

  await assert.rejects(
    ensureUv({ target, root, version: VERSION, assets, fetchArchive: async () => tampered, log: quiet }),
    (err) =>
      err.code === 'E_UV_CHECKSUM_MISMATCH' &&
      err.message.includes(sha256(tampered)) &&
      err.message.includes(assets[target.triple].sha256),
  );
  assert.deepEqual(leftovers(root), [], 'no binary, no staging directory and no lock remain');
});

test('an archive with no embedded checksum is refused before any download', async (t) => {
  const root = tmpdir(t);
  const { target } = fakeRelease();
  const { calls, fetchArchive } = counting(Buffer.alloc(0));

  await assert.rejects(
    ensureUv({ target, root, version: VERSION, assets: {}, fetchArchive, log: quiet }),
    { code: 'E_UV_CHECKSUM_MISSING' },
  );
  assert.equal(calls.length, 0);
});

test('a network failure names the URL and the override, and leaves no trace', async (t) => {
  const root = tmpdir(t);
  const { assets, target } = fakeRelease();
  const fetchArchive = async () => {
    throw new Error('getaddrinfo ENOTFOUND github.com');
  };

  await assert.rejects(
    ensureUv({ target, root, version: VERSION, assets, fetchArchive, log: quiet }),
    (err) =>
      err.code === 'E_UV_DOWNLOAD_FAILED' &&
      err.message.includes(`${RELEASE_BASE}/${VERSION}/uv-${target.triple}.tar.gz`) &&
      err.message.includes('ENOTFOUND') &&
      err.message.includes('CLOUDFROG_KNOWLEDGE_UV'),
  );
  assert.deepEqual(leftovers(root), []);
});

test('a zip target installs uv.exe', async (t) => {
  const root = tmpdir(t);
  const archive = makeZip([
    { name: 'uv.exe', data: 'windows uv' },
    { name: 'uvx.exe', data: 'uvx' },
  ]);
  const triple = 'x86_64-pc-windows-msvc';
  const target = { key: 'win32-x64', triple, archive: `uv-${triple}.zip`, kind: 'zip', exe: 'uv.exe' };
  const assets = { [triple]: { file: target.archive, sha256: sha256(archive) } };

  const binary = await ensureUv({ target, root, version: VERSION, assets, fetchArchive: async () => archive, log: quiet });
  assert.equal(path.basename(binary), 'uv.exe');
  assert.equal(fs.readFileSync(binary, 'utf8'), 'windows uv');
});

test('concurrent first runs download once and agree on the binary', async (t) => {
  const root = tmpdir(t);
  const { archive, assets, target } = fakeRelease();
  const { calls, fetchArchive } = counting(archive, 150);
  const options = { target, root, version: VERSION, assets, fetchArchive, log: quiet, lock: { pollMs: 10 } };

  const results = await Promise.all([ensureUv(options), ensureUv(options), ensureUv(options)]);
  assert.equal(new Set(results).size, 1);
  assert.equal(calls.length, 1);
  assert.deepEqual(leftovers(root), [target.triple]);
});

test('separate processes installing at once leave one complete binary', async (t) => {
  if (process.platform === 'win32') return t.skip('POSIX processes');
  const root = tmpdir(t);
  const { archive, assets, binary, target } = fakeRelease(Buffer.alloc(200000, 1));
  const archiveFile = path.join(root, 'archive.tgz');
  fs.writeFileSync(archiveFile, archive);
  const script = `
    const fs = require('node:fs');
    const { ensureUv } = require(${JSON.stringify(path.join(__dirname, '..', 'lib', 'install'))});
    const counter = process.argv[1];
    ensureUv({
      target: ${JSON.stringify(target)}, root: ${JSON.stringify(root)}, version: ${JSON.stringify(VERSION)},
      assets: ${JSON.stringify(assets)}, log: () => {}, lock: { pollMs: 10 },
      fetchArchive: async () => { fs.appendFileSync(counter, 'x'); await new Promise((r) => setTimeout(r, 200));
        return fs.readFileSync(${JSON.stringify(archiveFile)}); },
    }).then((p) => process.stdout.write(p), (e) => { console.error(e); process.exit(1); });
  `;
  const counter = path.join(root, 'downloads');
  const runs = await Promise.all(
    [0, 1, 2].map(
      () =>
        new Promise((resolve) => {
          const { spawn } = require('node:child_process');
          const child = spawn(process.execPath, ['-e', script, counter], { stdio: ['ignore', 'pipe', 'inherit'] });
          let out = '';
          child.stdout.on('data', (chunk) => (out += chunk));
          child.on('exit', (code) => resolve({ code, out }));
        }),
    ),
  );
  assert.deepEqual(runs.map((r) => r.code), [0, 0, 0]);
  assert.equal(new Set(runs.map((r) => r.out)).size, 1);
  assert.deepEqual(fs.readFileSync(runs[0].out), binary);
  assert.equal(fs.readFileSync(counter, 'utf8'), 'x', 'one process downloads, the others wait for it');
});

test('a lock left by a dead process is taken over at once', async (t) => {
  const root = tmpdir(t);
  const lockDir = path.join(root, 'x.lock');
  const dead = spawnSync(process.execPath, ['-e', '0']).pid;
  fs.mkdirSync(lockDir);
  fs.writeFileSync(path.join(lockDir, 'owner.json'), JSON.stringify({ pid: dead, host: os.hostname() }));

  const started = Date.now();
  const release = await acquireLock(lockDir, { staleMs: 60000, pollMs: 10 });
  assert.ok(Date.now() - started < 5000);
  release();
  assert.equal(fs.existsSync(lockDir), false);
});

test('a live lock is waited for, and an old one from another host is taken', async (t) => {
  const root = tmpdir(t);
  const lockDir = path.join(root, 'x.lock');
  const release = await acquireLock(lockDir, { pollMs: 10 });
  let acquired = false;
  const waiting = acquireLock(lockDir, { pollMs: 10 }).then((next) => {
    acquired = true;
    return next;
  });
  await new Promise((resolve) => setTimeout(resolve, 100));
  assert.equal(acquired, false, 'a lock held by a live process blocks');
  release();
  (await waiting)();

  fs.mkdirSync(lockDir);
  fs.writeFileSync(path.join(lockDir, 'owner.json'), JSON.stringify({ pid: process.pid, host: 'elsewhere' }));
  const old = new Date(Date.now() - 60000);
  fs.utimesSync(lockDir, old, old);
  (await acquireLock(lockDir, { staleMs: 1000, pollMs: 10 }))();
});

test('CLOUDFROG_KNOWLEDGE_UV wins over the cache and must name a file', async (t) => {
  const root = tmpdir(t);
  const uv = path.join(root, 'my-uv');
  fs.writeFileSync(uv, '');
  const fetchArchive = async () => assert.fail('the override must not download');

  assert.equal(await resolveUv({ env: { CLOUDFROG_KNOWLEDGE_UV: uv }, root, fetchArchive }), uv);
  await assert.rejects(resolveUv({ env: { CLOUDFROG_KNOWLEDGE_UV: path.join(root, 'missing') }, root, fetchArchive }), {
    code: 'E_UV_OVERRIDE_INVALID',
  });
  await assert.rejects(resolveUv({ env: { CLOUDFROG_KNOWLEDGE_UV: root }, root, fetchArchive }), {
    code: 'E_UV_OVERRIDE_INVALID',
  });
  await assert.rejects(resolveUv({ env: { CLOUDFROG_KNOWLEDGE_UV: ' ' }, platform: 'aix', arch: 'ppc64', root }), {
    code: 'E_UV_UNSUPPORTED_PLATFORM',
  });
});

test('download follows redirects and reports an HTTP failure', async (t) => {
  const server = http.createServer((req, res) => {
    if (req.url === '/moved') {
      res.writeHead(302, { Location: '/archive' });
      res.end();
    } else if (req.url === '/archive') {
      res.end('archive bytes');
    } else if (req.url === '/loop') {
      res.writeHead(302, { Location: '/loop' });
      res.end();
    } else {
      res.writeHead(404);
      res.end();
    }
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(() => server.close());
  const base = `http://127.0.0.1:${server.address().port}`;

  assert.equal((await download(`${base}/moved`)).toString(), 'archive bytes');
  await assert.rejects(download(`${base}/missing`), /HTTP 404 from/);
  await assert.rejects(download(`${base}/loop`), /too many redirects/);
  await assert.rejects(download('ftp://example.invalid/uv'), /unsupported protocol/);
});
