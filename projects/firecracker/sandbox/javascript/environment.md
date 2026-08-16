# javascript sandbox guest environment

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

## Installed packages (from the image lock; exact and exhaustive)

| Package | Version |
| ------- | ------- |
| busybox | 1.37.0-r61 |
| c-ares | 1.34.8-r0 |
| ca-certificates-bundle | 20260413-r1 |
| glibc | 2.43-r13 |
| glibc-locale-posix | 2.43-r13 |
| icu78-data-full | 78.3-r1 |
| ld-linux | 2.43-r13 |
| libbrotlicommon1 | 1.2.0-r3 |
| libbrotlidec1 | 1.2.0-r3 |
| libbrotlienc1 | 1.2.0-r3 |
| libcrypt1 | 2.43-r13 |
| libcrypto3 | 3.6.3-r4 |
| libgcc | 16.1.0-r4 |
| libicu78 | 78.3-r1 |
| libnghttp2-14 | 1.70.0-r2 |
| libssl3 | 3.6.3-r4 |
| libstdc++ | 16.1.0-r4 |
| libuv | 1.52.1-r1 |
| libxcrypt | 4.5.2-r4 |
| nodejs-24 | 24.19.0-r0 |
| wolfi-baselayout | 20230201-r29 |
| zlib | 1.3.2-r4 |
