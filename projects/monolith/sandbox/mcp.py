"""MCP tool that runs Python in the fc-invoke sandbox workload.

Exposes a single ``run_python`` tool. It is a thin async wrapper that POSTs
the supplied code (and optional input files) to the in-cluster ``fc-invoke``
``sandbox`` workload (ADR agents/044) and returns the structured execution
result. The daemon URL is injected from Helm values as ``FC_INVOKE_URL`` and
is never hardcoded here.
"""

from __future__ import annotations

from app.mcp_app import mcp
from sandbox.client import run_python_in_sandbox


@mcp.tool
async def run_python(code: str, files: list[dict] | None = None) -> dict:
    """Run a short Python 3.12 script in an isolated, zero-egress sandbox.

    The sandbox is a one-shot Firecracker microVM: nothing persists between
    calls, there is no network access at all, and the run is killed after
    about 25 seconds of wall-clock time. Use it for exact computation
    (arithmetic, date math, unit conversions, statistics, simulations),
    parsing or crunching pasted data, or generating a quick chart, rather
    than estimating an answer from memory.

    Available besides the Python 3.12 standard library: numpy, pandas,
    scipy, matplotlib (headless "Agg" backend, save figures to files rather
    than calling plt.show()), pillow (PIL), pyyaml (yaml), and
    python-dateutil (dateutil). Nothing else is installed: there is no
    duckdb, sympy, tabulate, or openpyxl, and no requests, httpx, or any
    other HTTP client, since the sandbox cannot reach the network at all.

    Args:
        code: The Python source to run. It is written to a file and executed
            with python3; use print(...) for anything you want back as
            stdout.
        files: Optional input files to write into the working directory
            before running code. Each entry needs a path (relative
            filename) and content_b64 (its content, base64-encoded).

    Returns:
        On success, the daemon's structured result: stdout, stderr,
        exit_code, duration_ms, truncated (whether any output was cut off to
        stay under the response size cap), and files (any regular files
        present in the working directory after the run that weren't given as
        input, base64-encoded under content_b64 next to their path). On
        failure, a dict with a single error key describing what went wrong.
    """
    return await run_python_in_sandbox(code, files=files)
