# goosecracker agent guest environment

This file describes the environment you are running in. It is generated
from the image's package lock and always matches the installed packages
exactly.

- You are inside a disposable Firecracker microVM. The rootfs is read-only,
  write scratch files under /tmp or /workspace.
- Outbound network goes through an egress proxy with an allowlist; most
  destinations are blocked. Do not assume general internet access.
- Runtimes available: python3 (with the scientific libraries listed below),
  node/pnpm, go. Prefer running real code over estimating results.
- matplotlib works headless (Agg). Save figures to files.

## Installed packages (from the image lock; exact and exhaustive)

| Package | Version |
| ------- | ------- |
| bash | 5.3-r12 |
| busybox | 1.37.0-r61 |
| c-ares | 1.34.6-r1 |
| ca-certificates-bundle | 20260413-r0 |
| coreutils | 9.11-r3 |
| curl | 8.21.0-r1 |
| cyrus-sasl-heimdal-libs | 2.1.28-r52 |
| freetype | 2.14.3-r3 |
| gdbm | 1.26-r5 |
| gh | 2.96.0-r0 |
| git | 2.55.0-r1 |
| glibc | 2.43-r10 |
| glibc-locale-posix | 2.43-r10 |
| go-1.26 | 1.26.4-r1 |
| heimdal-libs | 7.8.0-r51 |
| icu78-data-full | 78.3-r1 |
| jq | 1.8.2-r0 |
| keyutils-libs | 1.6.3-r38 |
| krb5-conf | 1.0-r9 |
| krb5-libs | 1.22.2-r2 |
| lcms2 | 2.19.1-r1 |
| ld-linux | 2.43-r10 |
| libacl1 | 2.4.0-r1 |
| libattr1 | 2.6.0-r1 |
| libbrotlicommon1 | 1.2.0-r3 |
| libbrotlidec1 | 1.2.0-r3 |
| libbrotlienc1 | 1.2.0-r3 |
| libbz2-1 | 1.0.8-r23 |
| libcom_err | 1.47.4-r1 |
| libcrypt1 | 2.43-r10 |
| libcrypto3 | 3.6.3-r3 |
| libcurl-openssl4 | 8.21.0-r1 |
| libedit | 3.1-r17 |
| libexpat1 | 2.8.2-r0 |
| libffi | 3.6.0-r2 |
| libgcc | 16.1.0-r4 |
| libgfortran | 16.1.0-r4 |
| libgomp | 16.1.0-r4 |
| libicu78 | 78.3-r1 |
| libidn2 | 2.3.8-r7 |
| libjpeg-turbo | 3.1.4.1-r2 |
| libldap-2.6 | 2.6.13-r4 |
| libnghttp2-14 | 1.69.0-r0 |
| libopenblas-0 | 0.3.33-r0 |
| libpcre2-8-0 | 10.47-r0 |
| libpng | 1.6.58-r1 |
| libpsl | 0.22.0-r1 |
| libselinux | 3.10-r0 |
| libsepol | 3.11-r0 |
| libssl3 | 3.6.3-r3 |
| libstdc++ | 16.1.0-r4 |
| libunistring | 1.4.2-r2 |
| libuuid | 2.42.2-r0 |
| libuv | 1.52.1-r1 |
| libverto | 0.3.2-r7 |
| libwebp | 1.6.0-r3 |
| libxau | 1.0.12-r5 |
| libxcb | 1.17.0-r15 |
| libxcrypt | 4.5.2-r3 |
| libxdmcp | 1.1.5-r9 |
| libzstd1 | 1.5.7-r7 |
| mpdecimal | 4.0.1-r3 |
| ncurses | 6.6.20260704-r0 |
| ncurses-terminfo-base | 6.6.20260704-r0 |
| nghttp3 | 1.17.0-r0 |
| ngtcp2 | 1.24.0-r0 |
| nodejs-26 | 26.4.0-r1 |
| oniguruma | 6.9.10-r3 |
| openjpeg | 2.5.4-r2 |
| openssh-client | 10.3_p1-r0 |
| pnpm-11.8 | 11.8.0-r2 |
| py3-pip-wheel | 26.1.2-r1 |
| py3.12-contourpy | 1.3.3-r5 |
| py3.12-cycler | 0.12.1-r7 |
| py3.12-fonttools | 4.63.0-r0 |
| py3.12-kiwisolver | 1.5.0-r1 |
| py3.12-matplotlib | 3.11.0-r1 |
| py3.12-numpy-2.2 | 2.2.6-r5 |
| py3.12-packaging | 26.2-r0 |
| py3.12-pandas | 3.0.4-r0 |
| py3.12-pillow | 12.3.0-r0 |
| py3.12-pygments | 2.20.0-r1 |
| py3.12-pyparsing | 3.3.2-r1 |
| py3.12-python-dateutil | 2.9.0-r12 |
| py3.12-pytz | 2026.2-r0 |
| py3.12-pyyaml | 6.0.3-r6 |
| py3.12-scipy | 1.18.0-r0 |
| py3.12-six | 1.17.0-r7 |
| py3.12-typing-extensions | 4.16.0-r0 |
| py3.12-tzdata | 2026.2-r0 |
| python-3.12 | 3.12.13-r8 |
| python-3.12-base | 3.12.13-r8 |
| readline | 8.3-r2 |
| sqlite | 3.53.3-r1 |
| sqlite-libs | 3.53.3-r1 |
| tiff | 4.7.1-r6 |
| tzdata | 2026b-r0 |
| wolfi-baselayout | 20230201-r29 |
| xz | 5.8.3-r1 |
| yaml | 0.2.5-r9 |
| zlib | 1.3.2-r3 |
