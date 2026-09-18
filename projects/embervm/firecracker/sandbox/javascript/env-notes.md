Zero-egress JavaScript execution sandbox (ADR agents/057). One-shot: each
request runs in a fresh microVM restore and nothing persists. No network access
at all. Code runs as uid 65532 with a hard wall-clock timeout; stdout, stderr,
and files created in the working directory are returned to the caller. Save
files with a plain relative filename (e.g. chart.png), never an absolute path or
/tmp, or they are not collected.

Your code is written to main.js and run with `node main.js`, as a CommonJS
script. console.log is how you return anything as stdout. Top-level await is not
available in this mode, so wrap async work in an async function and call it, or
use .then.

The Node standard library is all you get. There is no npm install and no
node_modules: the guest has no network, so no lodash, no axios, no date-fns.
Use the built-ins (fs, path, crypto, util, zlib, buffer, and the JavaScript
standard library including Intl, BigInt, and Temporal-free Date).

fetch exists in this Node version but every request fails, because there is no
network at all. Do not reach for it.
