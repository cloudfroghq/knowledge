'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');

const { extract } = require('../lib/archive');
const { makeTarGz, makeZip } = require('./helpers');

test('tar.gz: the binary comes out of its target directory', () => {
  const archive = makeTarGz([
    { name: 'uv-aarch64-apple-darwin/', type: '5' },
    { name: 'uv-aarch64-apple-darwin/uvx', data: 'not me' },
    { name: 'uv-aarch64-apple-darwin/uv', data: 'the uv binary' },
  ]);
  assert.equal(extract('tar.gz', archive, 'uv').toString(), 'the uv binary');
});

test('tar.gz: a GNU long name is followed', () => {
  const name = `${'deep/'.repeat(30)}uv`;
  const archive = makeTarGz([{ name, data: 'long', longName: true }]);
  assert.equal(extract('tar.gz', archive, 'uv').toString(), 'long');
});

test('zip: deflated and stored entries, Windows layout', () => {
  const archive = makeZip([
    { name: 'uvx.exe', data: 'no' },
    { name: 'uv.exe', data: Buffer.alloc(70000, 7) },
    { name: 'uvw.exe', data: 'no', method: 0 },
  ]);
  assert.deepEqual(extract('zip', archive, 'uv.exe'), Buffer.alloc(70000, 7));
  assert.equal(extract('zip', archive, 'uvw.exe').toString(), 'no');
});

test('a missing binary or a broken archive is E_UV_ARCHIVE_INVALID', () => {
  const tar = makeTarGz([{ name: 'x/uvx', data: 'uvx' }]);
  assert.throws(() => extract('tar.gz', tar, 'uv'), { code: 'E_UV_ARCHIVE_INVALID' });
  assert.throws(() => extract('zip', makeZip([{ name: 'uvx.exe', data: 'x' }]), 'uv.exe'), {
    code: 'E_UV_ARCHIVE_INVALID',
  });
  assert.throws(() => extract('tar.gz', Buffer.from('not gzip'), 'uv'), { code: 'E_UV_ARCHIVE_INVALID' });
  assert.throws(() => extract('zip', Buffer.alloc(64), 'uv.exe'), { code: 'E_UV_ARCHIVE_INVALID' });
});
