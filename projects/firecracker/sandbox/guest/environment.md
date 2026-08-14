# python sandbox guest environment

Zero-egress Python execution sandbox (ADR agents/044). One-shot: each request
runs in a fresh microVM restore and nothing persists. No network access at
all. Code runs as uid 65532 with a hard wall-clock timeout; stdout, stderr,
and files created in the working directory are returned to the caller. Save
files with a plain relative filename (e.g. chart.png), never an absolute path
or /tmp, or they are not collected.

Baked helper (importable, on PYTHONPATH): `from sandbox_tools import
render_table`. render_table(headers, rows, title=None, path="table.png")
writes a styled table PNG (dark header, zebra rows, numeric columns
right-aligned) and returns the path. Prefer it over hand-styling a matplotlib
table when the output is tabular.

## Installed packages (from the image lock; exact and exhaustive)

| Package | Version |
| ------- | ------- |
| busybox | 1.37.0-r61 |
| ca-certificates-bundle | 20260413-r1 |
| freetype | 2.14.3-r4 |
| gdbm | 1.26-r5 |
| glibc | 2.43-r13 |
| glibc-locale-posix | 2.43-r13 |
| lcms2 | 2.19.1-r1 |
| ld-linux | 2.43-r13 |
| libbrotlicommon1 | 1.2.0-r3 |
| libbrotlidec1 | 1.2.0-r3 |
| libbz2-1 | 1.0.8-r23 |
| libcrypt1 | 2.43-r13 |
| libcrypto3 | 3.6.3-r4 |
| libexpat1 | 2.8.3-r0 |
| libffi | 3.8.0-r0 |
| libgcc | 16.1.0-r4 |
| libgfortran | 16.1.0-r4 |
| libjpeg-turbo | 3.2.0-r1 |
| libopenblas-0 | 0.3.34-r0 |
| libpng | 1.6.58-r1 |
| libssl3 | 3.6.3-r4 |
| libstdc++ | 16.1.0-r4 |
| libuuid | 2.42.2-r2 |
| libwebp | 1.6.0-r5 |
| libxau | 1.0.12-r7 |
| libxcb | 1.17.0-r15 |
| libxcrypt | 4.5.2-r4 |
| libxdmcp | 1.1.5-r9 |
| libzstd1 | 1.5.7-r8 |
| mpdecimal | 4.0.1-r3 |
| ncurses | 6.6.20260808-r0 |
| ncurses-terminfo-base | 6.6.20260808-r0 |
| openjpeg | 2.5.4-r2 |
| py3-pip-wheel | 26.2.1-r0 |
| py3.12-contourpy | 1.3.3-r5 |
| py3.12-cycler | 0.12.1-r7 |
| py3.12-fonttools | 4.63.0-r0 |
| py3.12-kiwisolver | 1.5.0-r1 |
| py3.12-matplotlib | 3.11.1-r0 |
| py3.12-numpy-2.2 | 2.2.6-r5 |
| py3.12-packaging | 26.3-r0 |
| py3.12-pandas | 3.0.5-r0 |
| py3.12-pillow | 12.3.0-r0 |
| py3.12-pygments | 2.20.0-r1 |
| py3.12-pyparsing | 3.3.2-r1 |
| py3.12-python-dateutil | 2.9.0-r12 |
| py3.12-pytz | 2026.3-r0 |
| py3.12-pyyaml | 6.0.3-r6 |
| py3.12-scipy | 1.18.0-r0 |
| py3.12-six | 1.17.0-r7 |
| py3.12-typing-extensions | 4.16.0-r0 |
| py3.12-tzdata | 2026.3-r0 |
| python-3.12 | 3.12.13-r10 |
| python-3.12-base | 3.12.13-r10 |
| readline | 8.3-r2 |
| sqlite-libs | 3.53.4-r0 |
| tiff | 4.7.2-r1 |
| wolfi-baselayout | 20230201-r29 |
| xz | 5.8.3-r1 |
| yaml | 0.2.5-r9 |
| zlib | 1.3.2-r4 |
