'use strict';

// The bin scripts end to end, with CLOUDFROG_KNOWLEDGE_UV pointing at a fake uv.

const assert = require('node:assert/strict');
const { spawn, spawnSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const { uvArguments } = require('../lib/run');
const { version } = require('../package.json');
const { readArgs, tmpdir, writeFakeUv } = require('./helpers');

const BIN = path.join(__dirname, '..', 'bin');
const posixOnly = process.platform === 'win32' ? 'the fake uv is a POSIX shell script' : false;

function setup(t, extra = {}) {
  const dir = tmpdir(t);
  const argsFile = path.join(dir, 'args');
  const env = { ...process.env, CLOUDFROG_KNOWLEDGE_UV: writeFakeUv(dir), FAKE_UV_ARGS: argsFile, ...extra };
  return { argsFile, env };
}

function run(bin, args, env) {
  return spawnSync(process.execPath, [path.join(BIN, bin), ...args], { env, encoding: 'utf8', timeout: 30000 });
}

test('uv runs the console script of this package version on a managed Python 3.12', () => {
  assert.deepEqual(uvArguments('knowledge', ['doctor', '--brief']), [
    'tool',
    'run',
    '--managed-python',
    '--python',
    '3.12',
    '--from',
    `cloudfrog-knowledge==${version}`,
    'knowledge',
    'doctor',
    '--brief',
  ]);
});

test('arguments, stdout and the exit status pass through untouched', { skip: posixOnly }, (t) => {
  const { argsFile, env } = setup(t, { FAKE_UV_EXIT: '7', FAKE_UV_STDOUT: 'from uv' });
  const args = ['serve', '--transport', 'stdio', 'two words', '', '--', '-x'];

  const result = run('knowledge.js', args, env);
  assert.equal(result.status, 7);
  assert.equal(result.stdout, 'from uv\n', 'the wrapper prints nothing on stdout');
  assert.equal(result.stderr, '');
  assert.deepEqual(readArgs(argsFile), uvArguments('knowledge', args));
});

test('each bin runs the console script of its own name', { skip: posixOnly }, (t) => {
  const { argsFile, env } = setup(t);

  assert.equal(run('cloudfrog-knowledge.js', ['--version'], env).status, 0);
  assert.deepEqual(readArgs(argsFile), uvArguments('cloudfrog-knowledge', ['--version']));
  assert.equal(run('knowledge.js', [], env).status, 0);
  assert.deepEqual(readArgs(argsFile), uvArguments('knowledge', []));
});

test('a child killed by a signal kills the wrapper with the same signal', { skip: posixOnly }, (t) => {
  const { env } = setup(t, { FAKE_UV_MODE: 'kill' });

  const result = run('knowledge.js', ['serve'], env);
  assert.equal(result.status, null);
  assert.equal(result.signal, 'SIGTERM');
});

test('a signal sent to the wrapper reaches the child', { skip: posixOnly }, async (t) => {
  const { argsFile, env } = setup(t, { FAKE_UV_MODE: 'wait' });
  const child = spawn(process.execPath, [path.join(BIN, 'knowledge.js'), 'serve'], { env, stdio: 'ignore' });
  const exited = new Promise((resolve) => child.on('exit', (code, signal) => resolve({ code, signal })));

  const deadline = Date.now() + 15000;
  while (!fs.existsSync(`${argsFile}.ready`)) {
    assert.ok(Date.now() < deadline, 'the fake uv never started');
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  child.kill('SIGTERM');

  assert.deepEqual(await exited, { code: 3, signal: null });
  assert.equal(fs.readFileSync(`${argsFile}.signal`, 'utf8').trim(), 'TERM');
});

test('a wrapper failure is one coded line on stderr and status 2', (t) => {
  const dir = tmpdir(t);
  const env = { ...process.env, CLOUDFROG_KNOWLEDGE_UV: path.join(dir, 'no-such-uv') };

  const result = run('knowledge.js', ['doctor'], env);
  assert.equal(result.status, 2);
  assert.equal(result.stdout, '');
  assert.match(result.stderr, /^E_UV_OVERRIDE_INVALID: CLOUDFROG_KNOWLEDGE_UV=.*no-such-uv is not a file\n$/);
});
