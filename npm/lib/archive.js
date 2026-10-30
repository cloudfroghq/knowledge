'use strict';

// Reads one file out of a uv release archive held in memory.
//
// The archive's SHA-256 is verified before either reader runs, so these only
// need to parse correctly: they return the bytes of the first regular file
// whose base name is the one asked for and never write anything themselves.

const zlib = require('node:zlib');

const { WrapperError } = require('./errors');

const BLOCK = 512;

function invalid(message) {
  return new WrapperError('E_UV_ARCHIVE_INVALID', message);
}

function baseName(name) {
  const parts = name.replace(/\\/g, '/').split('/').filter(Boolean);
  return parts.length ? parts[parts.length - 1] : '';
}

function cString(buffer, start, length) {
  const field = buffer.subarray(start, start + length);
  const end = field.indexOf(0);
  return field.subarray(0, end === -1 ? field.length : end).toString('utf8');
}

function tarSize(header) {
  const field = header.subarray(124, 136);
  if (field[0] & 0x80) {
    // Base-256 (GNU) for sizes past the octal field's range.
    let size = 0;
    for (let i = 1; i < field.length; i += 1) size = size * 256 + field[i];
    return size;
  }
  const text = cString(header, 124, 12).trim();
  return text ? parseInt(text, 8) : 0;
}

function paxPath(data) {
  let offset = 0;
  while (offset < data.length) {
    const space = data.indexOf(0x20, offset);
    if (space === -1) break;
    const length = parseInt(data.subarray(offset, space).toString('ascii'), 10);
    if (!Number.isFinite(length) || length <= 0) break;
    const record = data.subarray(space + 1, offset + length - 1).toString('utf8');
    const eq = record.indexOf('=');
    if (eq !== -1 && record.slice(0, eq) === 'path') return record.slice(eq + 1);
    offset += length;
  }
  return null;
}

/** The bytes of `wanted` inside a gzip-compressed tar. */
function extractFromTarGz(archive, wanted) {
  let tar;
  try {
    tar = zlib.gunzipSync(archive);
  } catch (err) {
    throw invalid(`not a gzip archive: ${err.message}`);
  }
  let offset = 0;
  let longName = null;
  while (offset + BLOCK <= tar.length) {
    const header = tar.subarray(offset, offset + BLOCK);
    if (header.every((byte) => byte === 0)) break;
    const size = tarSize(header);
    const type = String.fromCharCode(header[156] || 0x30);
    const dataStart = offset + BLOCK;
    const data = tar.subarray(dataStart, dataStart + size);
    if (data.length !== size) throw invalid('truncated tar entry');
    offset = dataStart + Math.ceil(size / BLOCK) * BLOCK;

    if (type === 'L') {
      longName = cString(data, 0, data.length);
      continue;
    }
    if (type === 'x') {
      longName = paxPath(data) || longName;
      continue;
    }
    if (type === 'g') continue;

    let name = longName;
    longName = null;
    if (name === null) {
      name = cString(header, 0, 100);
      if (cString(header, 257, 6) === 'ustar') {
        const prefix = cString(header, 345, 155);
        if (prefix) name = `${prefix}/${name}`;
      }
    }
    if ((type === '0' || type === '7') && baseName(name) === wanted) return Buffer.from(data);
  }
  throw invalid(`${wanted} is not in the archive`);
}

/** The bytes of `wanted` inside a zip (stored or deflated entries, no ZIP64). */
function extractFromZip(archive, wanted) {
  const EOCD = 0x06054b50;
  const CENTRAL = 0x02014b50;
  const LOCAL = 0x04034b50;
  const floor = Math.max(0, archive.length - 22 - 0xffff);
  let eocd = -1;
  for (let i = archive.length - 22; i >= floor; i -= 1) {
    if (archive.readUInt32LE(i) === EOCD) {
      eocd = i;
      break;
    }
  }
  if (eocd === -1) throw invalid('not a zip archive');
  const entries = archive.readUInt16LE(eocd + 10);
  const directory = archive.readUInt32LE(eocd + 16);
  if (entries === 0xffff || directory === 0xffffffff) throw invalid('ZIP64 archives are not supported');

  let offset = directory;
  for (let n = 0; n < entries; n += 1) {
    if (archive.readUInt32LE(offset) !== CENTRAL) throw invalid('corrupt zip central directory');
    const method = archive.readUInt16LE(offset + 10);
    const compressed = archive.readUInt32LE(offset + 20);
    const size = archive.readUInt32LE(offset + 24);
    const nameLength = archive.readUInt16LE(offset + 28);
    const extraLength = archive.readUInt16LE(offset + 30);
    const commentLength = archive.readUInt16LE(offset + 32);
    const local = archive.readUInt32LE(offset + 42);
    const name = archive.subarray(offset + 46, offset + 46 + nameLength).toString('utf8');
    offset += 46 + nameLength + extraLength + commentLength;
    if (name.endsWith('/') || baseName(name) !== wanted) continue;

    if (archive.readUInt32LE(local) !== LOCAL) throw invalid('corrupt zip local header');
    const start = local + 30 + archive.readUInt16LE(local + 26) + archive.readUInt16LE(local + 28);
    const raw = archive.subarray(start, start + compressed);
    let data;
    if (method === 0) data = Buffer.from(raw);
    else if (method === 8) data = zlib.inflateRawSync(raw);
    else throw invalid(`unsupported zip compression method ${method}`);
    if (data.length !== size) throw invalid(`${wanted}: size mismatch after extraction`);
    return data;
  }
  throw invalid(`${wanted} is not in the archive`);
}

/** Dispatch on the archive kind of a target (`tar.gz` or `zip`). */
function extract(kind, archive, wanted) {
  if (kind === 'zip') return extractFromZip(archive, wanted);
  if (kind === 'tar.gz') return extractFromTarGz(archive, wanted);
  throw invalid(`unknown archive kind ${kind}`);
}

module.exports = { extract, extractFromTarGz, extractFromZip };
