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
