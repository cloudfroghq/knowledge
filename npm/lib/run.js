'use strict';

// Runs the cloudfrog-knowledge CLI of this package's version through uv.
//
// `uv tool run --from cloudfrog-knowledge==<version> <command> <args>` installs
// the PyPI release in uv's tool cache on first use (and a uv-managed Python
// when none fits) and reuses it afterwards. The child shares stdin, stdout
// and stderr, so an MCP client speaking stdio talks to the server directly:
// nothing the wrapper prints goes to stdout.

const { spawn } = require('node:child_process');
const os = require('node:os');

const { WrapperError } = require('./errors');
const { resolveUv } = require('./install');
const { version } = require('../package.json');

const DISTRIBUTION = 'cloudfrog-knowledge';
// The release's requires-python is ">=3.12,<3.13", and uv ignores the upper
// bound when it picks an interpreter: without --python it would run the CLI
// on whatever newer Python it finds first. A uv-managed CPython also loads
// SQLite extensions, which the vector search of the index needs.
const PYTHON = ['--managed-python', '--python', '3.12'];
const FORWARDED = process.platform === 'win32' ? [] : ['SIGINT', 'SIGTERM', 'SIGHUP', 'SIGQUIT'];

/** The uv arguments that run `command` (a console script of the package) with `args`. */
function uvArguments(command, args, packageVersion = version) {
  return ['tool', 'run', ...PYTHON, '--from', `${DISTRIBUTION}==${packageVersion}`, command, ...args];
}

function fail(err) {
  if (err instanceof WrapperError) {
    process.stderr.write(`${err.code}: ${err.message}\n`);
  } else {
    process.stderr.write(`E_WRAPPER_FAILED: ${err && err.stack ? err.stack : err}\n`);
  }
  process.exitCode = 2;
}

/**
 * Run `command` with the process arguments and leave with the child's status.
 *
 * Signals the wrapper receives are passed to the child (on Windows the console
 * already delivers Ctrl+C to it, so the wrapper only stays alive to wait). A
 * child killed by a signal kills the wrapper with the same signal, so the
 * caller sees what happened to the CLI, not to the wrapper.
 */
async function main(command, { argv = process.argv.slice(2), env = process.env } = {}) {
  let uv;
  try {
    uv = await resolveUv({ env });
  } catch (err) {
    fail(err);
    return;
  }

  const child = spawn(uv, uvArguments(command, argv), { stdio: 'inherit', env });
  const handlers = new Map();
  const relay = (signal) => () => {
    if (child.exitCode === null && child.signalCode === null) child.kill(signal);
  };
  for (const signal of FORWARDED) {
    handlers.set(signal, relay(signal));
    process.on(signal, handlers.get(signal));
  }
  if (process.platform === 'win32') {
    for (const signal of ['SIGINT', 'SIGBREAK']) {
      handlers.set(signal, () => {});
      process.on(signal, handlers.get(signal));
    }
  }
  const detach = () => {
    for (const [signal, handler] of handlers) process.removeListener(signal, handler);
  };

  child.on('error', (err) => {
    detach();
    fail(new WrapperError('E_UV_EXEC_FAILED', `could not run ${uv}: ${err.message}`));
  });
  child.on('exit', (code, signal) => {
    detach();
    if (signal) {
      process.kill(process.pid, signal);
      // Still here: the signal is ignored in this process; report it the shell's way.
      process.exitCode = 128 + (os.constants.signals[signal] || 0);
      return;
    }
    process.exitCode = code === null ? 1 : code;
  });
}

module.exports = { DISTRIBUTION, PYTHON, main, uvArguments };
