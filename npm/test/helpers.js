'use strict';

// Builders shared by the tests: in-memory archives shaped like uv's releases
// and a fake uv that records how it was called. Loading this file runs nothing.

const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const zlib = require('node:zlib');

function tmpdir(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cfk-npm-test-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  return dir;
}

function sha256(buffer) {
  return crypto.createHash('sha256').update(buffer).digest('hex');
}

function tarHeader(name, size, type) {
  const header = Buffer.alloc(512);
  header.write(name.slice(0, 100), 0, 'utf8');
  header.write('0000755\0', 100, 'ascii');
  header.write('0000000\0', 108, 'ascii');
  header.write('0000000\0', 116, 'ascii');
  header.write(`${size.toString(8).padStart(11, '0')}\0`, 124, 'ascii');
  header.write('00000000000\0', 136, 'ascii');
  header.write('        ', 148, 'ascii');
  header.write(type, 156, 'ascii');
  header.write('ustar\0', 257, 'ascii');
  header.write('00', 263, 'ascii');
  let sum = 0;
  for (const byte of header) sum += byte;
  header.write(`${sum.toString(8).padStart(6, '0')}\0 `, 148, 'ascii');
  return header;
}

function pad(data) {
  const rest = data.length % 512;
  return rest ? Buffer.concat([data, Buffer.alloc(512 - rest)]) : data;
}

/** A .tar.gz of `entries` ({name, data, type = '0', longName = false}). */
function makeTarGz(entries) {
  const blocks = [];
  for (const { name, data = Buffer.alloc(0), type = '0', longName = false } of entries) {
    const body = Buffer.from(data);
    if (longName) {
      const encoded = Buffer.from(`${name}\0`);
      blocks.push(tarHeader('././@LongLink', encoded.length, 'L'), pad(encoded));
    }
    // A long name's own header carries a truncated name: only the 'L' entry finds the file.
    blocks.push(tarHeader(longName ? name.slice(0, 99) : name, body.length, type), pad(body));
  }
  blocks.push(Buffer.alloc(1024));
  return zlib.gzipSync(Buffer.concat(blocks));
}

/** A .zip of `entries` ({name, data, method = 8}); CRCs are left at zero. */
function makeZip(entries) {
  const locals = [];
  const central = [];
  let offset = 0;
  for (const { name, data, method = 8 } of entries) {
    const raw = Buffer.from(data);
    const stored = method === 8 ? zlib.deflateRawSync(raw) : raw;
    const nameBytes = Buffer.from(name);
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4);
    local.writeUInt16LE(method, 8);
    local.writeUInt32LE(stored.length, 18);
    local.writeUInt32LE(raw.length, 22);
    local.writeUInt16LE(nameBytes.length, 26);
    locals.push(local, nameBytes, stored);

    const entry = Buffer.alloc(46);
    entry.writeUInt32LE(0x02014b50, 0);
    entry.writeUInt16LE(20, 4);
    entry.writeUInt16LE(20, 6);
    entry.writeUInt16LE(method, 10);
    entry.writeUInt32LE(stored.length, 20);
    entry.writeUInt32LE(raw.length, 24);
    entry.writeUInt16LE(nameBytes.length, 28);
    entry.writeUInt32LE(offset, 42);
    central.push(entry, nameBytes);
    offset += 30 + nameBytes.length + stored.length;
  }
  const directory = Buffer.concat(central);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(entries.length, 8);
  end.writeUInt16LE(entries.length, 10);
  end.writeUInt32LE(directory.length, 12);
  end.writeUInt32LE(offset, 16);
  return Buffer.concat([...locals, directory, end]);
}

/** A tar.gz target and its embedded asset, shaped like the real release. */
function fakeRelease(binary = Buffer.from('#!/bin/sh\necho fake uv\n')) {
  const triple = 'x86_64-unknown-linux-gnu';
  const archive = makeTarGz([
    { name: `uv-${triple}/`, type: '5' },
    { name: `uv-${triple}/uvx`, data: 'uvx' },
    { name: `uv-${triple}/uv`, data: binary },
  ]);
  const target = { key: 'linux-x64-gnu', triple, archive: `uv-${triple}.tar.gz`, kind: 'tar.gz', exe: 'uv' };
  const assets = { [triple]: { file: target.archive, sha256: sha256(archive) } };
  return { archive, assets, binary, target };
}

/**
 * Write a POSIX fake uv into `dir`. It writes its arguments, NUL-separated, to
 * $FAKE_UV_ARGS, prints $FAKE_UV_STDOUT, and then by $FAKE_UV_MODE: `kill`
 * sends itself SIGTERM, `wait` traps SIGTERM (recording it and exiting 3) and
 * waits; otherwise it exits with $FAKE_UV_EXIT.
 */
function writeFakeUv(dir) {
  const file = path.join(dir, 'fake-uv');
  fs.writeFileSync(
    file,
    [
      '#!/bin/sh',
      'printf \'%s\\0\' "$@" > "$FAKE_UV_ARGS"',
      '[ -n "${FAKE_UV_STDOUT:-}" ] && printf \'%s\\n\' "$FAKE_UV_STDOUT"',
      'case "${FAKE_UV_MODE:-}" in',
      '  kill) kill -TERM $$ ;;',
      '  wait)',
      '    trap \'echo TERM > "$FAKE_UV_ARGS.signal"; exit 3\' TERM',
      '    : > "$FAKE_UV_ARGS.ready"',
      '    while :; do sleep 0.05; done ;;',
      'esac',
      'exit "${FAKE_UV_EXIT:-0}"',
      '',
    ].join('\n'),
    { mode: 0o755 },
  );
  return file;
}

function readArgs(file) {
  const parts = fs.readFileSync(file, 'utf8').split('\0');
  parts.pop();
  return parts;
}

module.exports = { fakeRelease, makeTarGz, makeZip, readArgs, sha256, tmpdir, writeFakeUv };
