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
| busybox | 1.38.0-r3 |
| c-ares | 1.34.9-r0 |
| ca-certificates-bundle | 20261020-r0 |
| glibc-2.44 | 2.44-r21 |
| glibc-2.44-locale-posix | 2.44-r21 |
| icu79-data-full | 79.1-r2 |
| ld-linux-2.44 | 2.44-r21 |
| libbrotlicommon1 | 1.2.1-r3 |
| libbrotlidec1 | 1.2.1-r3 |
| libbrotlienc1 | 1.2.1-r3 |
| libcrypt1-2.44 | 2.44-r21 |
| libgcc | 16.2.0-r4 |
| libicu79 | 79.1-r2 |
| libnghttp2-14 | 1.71.0-r0 |
| libstdc++ | 16.2.0-r4 |
| libuv | 1.53.0-r1 |
| libxcrypt | 4.5.2-r8 |
| nodejs-24 | 24.22.3-r2 |
| openssl-4.0-libcrypto | 4.0.3-r8 |
| openssl-4.0-libssl | 4.0.3-r8 |
| openssl-provider-legacy-allowed | 4.1.0-r0 |
| wolfi-baselayout | 20230201-r31 |
| zlib | 1.4-r0 |
