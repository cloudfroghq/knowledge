'use strict';

/**
 * A failure of the wrapper itself, before the CLI runs.
 *
 * `code` is a stable `E_` identifier; the wrapper prints `<code>: <message>`
 * on stderr and exits with status 2, the way the CLI reports a problem that
 * kept it from starting.
 */
class WrapperError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'WrapperError';
    this.code = code;
  }
}

module.exports = { WrapperError };
