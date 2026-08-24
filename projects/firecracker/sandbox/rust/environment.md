# rust sandbox guest environment

Zero-egress Rust execution sandbox (ADR agents/057). One-shot: each request runs
in a fresh microVM restore and nothing persists. No network access at all. Code
runs as uid 65532 with a hard wall-clock timeout that covers COMPILATION as well
as execution; stdout, stderr, and files created in the working directory are
returned to the caller. Save files with a plain relative filename (e.g.
chart.png), never an absolute path or /tmp, or they are not collected.

Your code is written to main.rs and compiled with `rustc -O main.rs -o main`,
then the binary is run. Write a normal `fn main()`. A compile error comes back
as a nonzero exit with rustc's own diagnostics on stderr, so you see exactly
what the compiler saw.

There is no cargo and no crates.io. The guest has no network, so only the Rust
standard library is available: no serde, no rand, no regex, no itertools.
Anything you would normally pull from a crate has to be written out by hand.

Because rustc runs with optimisations on, a compute-heavy program is fast once
built, but the build itself is charged against your time budget.

## Installed packages (from the image lock; exact and exhaustive)

| Package | Version |
| ------- | ------- |
| binutils | 2.47-r0 |
| build-base | 1-r9 |
| busybox | 1.38.0-r1 |
| ca-certificates-bundle | 20260413-r1 |
| cyrus-sasl-heimdal-libs | 2.1.28-r54 |
| gcc | 16.2.0-r0 |
| gdbm | 1.26-r5 |
| glibc-2.43 | 2.43-r15 |
| glibc-2.43-dev | 2.43-r15 |
| glibc-2.43-locale-posix | 2.43-r15 |
| gmp | 6.3.0-r8 |
| heimdal-libs | 7.8.0-r51 |
| isl | 0.28-r2 |
| keyutils-libs | 1.6.3-r39 |
| krb5-conf | 1.0-r9 |
| krb5-libs | 1.22.2-r2 |
| ld-linux-2.43 | 2.43-r15 |
| libLLVM-22 | 22.1.8-r2 |
| libatomic | 16.2.0-r0 |
| libbrotlicommon1 | 1.2.0-r3 |
| libbrotlidec1 | 1.2.0-r3 |
| libcom_err | 1.47.4-r1 |
| libcrypt1-2.43 | 2.43-r15 |
| libcrypto3 | 3.6.3-r5 |
| libcurl-openssl4 | 8.21.0-r2 |
| libffi | 3.8.0-r0 |
| libgcc | 16.2.0-r0 |
| libgomp | 16.2.0-r0 |
| libidn2 | 2.3.8-r8 |
| libldap-2.7 | 2.7.0-r0 |
| libnghttp2-14 | 1.70.0-r2 |
| libpsl | 0.23.3-r0 |
| libquadmath | 16.2.0-r0 |
| libssl3 | 3.6.3-r5 |
| libstdc++ | 16.2.0-r0 |
| libstdc++-dev | 16.2.0-r0 |
| libunistring | 1.4.2-r2 |
| libverto | 0.3.2-r7 |
| libxcrypt | 4.5.2-r4 |
| libxcrypt-dev | 4.5.2-r4 |
| libxml2-16 | 2.15.3-r3 |
| libzstd1 | 1.5.7-r8 |
| linux-headers | 7.2-r0 |
| make | 4.4.1-r13 |
| mpc | 1.4.1-r0 |
| mpfr | 4.2.2-r2 |
| ncurses | 6.6.20260822-r0 |
| ncurses-terminfo-base | 6.6.20260822-r0 |
| nghttp3 | 1.18.0-r0 |
| ngtcp2 | 1.25.0-r1 |
| openssf-compiler-options | 20250904-r9 |
| pkgconf | 3.0.6-r0 |
| posix-cc-wrappers | 2-r9 |
| readline | 8.3-r2 |
| rust-1.97 | 1.97.1-r0 |
| sqlite-libs | 3.53.4-r0 |
| wolfi-baselayout | 20230201-r29 |
| zlib | 1.3.2-r4 |
