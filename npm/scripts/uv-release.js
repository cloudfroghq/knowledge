#!/usr/bin/env node
'use strict';

// Maintains lib/uv-release.json, the uv version this package runs and the
// SHA-256 of each release archive it may download. Development and release
// tooling only: it is not part of the published package, and the wrapper never
// fetches a checksum at run time.
//
//   node scripts/uv-release.js <version>   write lib/uv-release.json from the
//                                          release's <archive>.sha256 files
//   node scripts/uv-release.js --check     compare the committed file with the
//                                          release's .sha256 files; exit 1 on drift
//   node scripts/uv-release.js --smoke     install this platform's archive into a
//                                          temporary cache through the wrapper's
//                                          own code and run `uv --version`
//
// --check and --smoke need the network; both leave the package tree untouched.

const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { RELEASE_BASE, download, ensureUv } = require('../lib/install');
const { TARGETS, resolveTarget } = require('../lib/platform');

const RELEASE_FILE = path.join(__dirname, '..', 'lib', 'uv-release.json');

function archiveOf(triple) {
  return `uv-${triple}.${triple.endsWith('windows-msvc') ? 'zip' : 'tar.gz'}`;
}

async function releaseChecksums(version) {
  const assets = {};
  for (const triple of Object.values(TARGETS).sort()) {
    const file = archiveOf(triple);
    const text = (await download(`${RELEASE_BASE}/${version}/${file}.sha256`)).toString('utf8').trim();
    const match = /^([0-9a-f]{64}) [ *]?(\S+)$/.exec(text);
    if (!match || match[2] !== file) throw new Error(`unexpected checksum line for ${file}: ${JSON.stringify(text)}`);
    assets[triple] = { file, sha256: match[1] };
  }
  return assets;
}

async function write(version) {
  if (!/^\d+\.\d+\.\d+$/.test(version)) throw new Error(`${version} is not X.Y.Z`);
  const assets = await releaseChecksums(version);
  fs.writeFileSync(RELEASE_FILE, `${JSON.stringify({ version, assets }, null, 2)}\n`);
  console.log(`wrote ${path.relative(process.cwd(), RELEASE_FILE)} for uv ${version}`);
}

async function check() {
  const committed = JSON.parse(fs.readFileSync(RELEASE_FILE, 'utf8'));
  const expected = { version: committed.version, assets: await releaseChecksums(committed.version) };
  if (JSON.stringify(committed) !== JSON.stringify(expected)) {
    console.error(`uv-release.json does not match the .sha256 files of uv ${committed.version}`);
    return 1;
  }
  console.log(`uv-release.json matches the .sha256 files of uv ${committed.version}`);
  return 0;
}

async function smoke() {
  const { version } = JSON.parse(fs.readFileSync(RELEASE_FILE, 'utf8'));
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'cloudfrog-knowledge-uv-'));
  try {
    const uv = await ensureUv({ target: resolveTarget(), root });
    const again = await ensureUv({ target: resolveTarget(), root, fetchArchive: () => Promise.reject(new Error('cache not reused')) });
    if (again !== uv) throw new Error('the second run did not reuse the cache');
    const out = execFileSync(uv, ['--version'], { encoding: 'utf8' }).trim();
    if (!out.startsWith(`uv ${version}`)) throw new Error(`unexpected uv --version: ${out}`);
    console.log(out);
    return 0;
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
}

async function cli(args) {
  if (args.length === 1 && args[0] === '--check') return check();
  if (args.length === 1 && args[0] === '--smoke') return smoke();
  if (args.length === 1 && !args[0].startsWith('-')) {
    await write(args[0]);
    return 0;
  }
  console.error('usage: node scripts/uv-release.js <version> | --check | --smoke');
  return 2;
}

cli(process.argv.slice(2)).then(
  (code) => {
    process.exitCode = code;
  },
  (err) => {
    console.error(err.message);
    process.exitCode = 1;
  },
);
