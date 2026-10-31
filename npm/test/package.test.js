'use strict';

// What `npm pack` ships, and the manifest promises the README makes.

const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const ROOT = path.join(__dirname, '..');
const manifest = require('../package.json');

const SHIPPED = [
  'LICENSE',
  'README.md',
  'bin/cloudfrog-knowledge.js',
  'bin/knowledge.js',
  'lib/archive.js',
  'lib/errors.js',
  'lib/install.js',
  'lib/platform.js',
  'lib/run.js',
  'lib/uv-release.json',
  'package.json',
];

test('npm pack ships the wrapper and nothing else', () => {
  const npm = process.platform === 'win32' ? 'npm.cmd' : 'npm';
  const out = execFileSync(npm, ['pack', '--dry-run', '--json', '--ignore-scripts'], {
    cwd: ROOT,
    encoding: 'utf8',
    shell: process.platform === 'win32',
  });
  const [packed] = JSON.parse(out);
  assert.deepEqual(packed.files.map((file) => file.path).sort(), SHIPPED);
  assert.equal(packed.name, '@cloudfrog/knowledge');
});

test('the manifest: two bins, no runtime dependencies, Node 18 or later', () => {
  assert.equal(manifest.name, '@cloudfrog/knowledge');
  assert.deepEqual(manifest.bin, { knowledge: 'bin/knowledge.js', 'cloudfrog-knowledge': 'bin/cloudfrog-knowledge.js' });
  for (const field of ['dependencies', 'optionalDependencies', 'peerDependencies', 'bundleDependencies']) {
    assert.equal(manifest[field], undefined, `${field} must stay empty`);
  }
  assert.equal(manifest.engines.node, '>=18');
  assert.equal(manifest.license, 'MIT');
  assert.equal(manifest.publishConfig.access, 'public');
  // npm provenance requires the repository to be the one the workflow runs in.
  assert.equal(manifest.repository.url, 'git+https://github.com/cloudfroghq/knowledge.git');
  assert.equal(manifest.repository.directory, 'npm');
  assert.equal(manifest.scripts.prepublishOnly, undefined);
  assert.equal(manifest.scripts.prepack, undefined);
});

test('the bins are executable node scripts', () => {
  for (const bin of Object.values(manifest.bin)) {
    const file = path.join(ROOT, bin);
    assert.ok(fs.readFileSync(file, 'utf8').startsWith('#!/usr/bin/env node\n'), bin);
    if (process.platform !== 'win32') assert.ok(fs.statSync(file).mode & 0o111, `${bin} is not executable`);
  }
});
