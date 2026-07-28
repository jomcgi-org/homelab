"""MCP tool that runs Python in the EmberVM sandbox workload.

Exposes a single ``run_python`` tool. It is a thin async wrapper that POSTs
    the supplied code (and optional input files) to the EmberVM ``sandbox``
    workload and returns the structured execution result.

With an optional ``session`` handle the run is served by the EmberVM session
class (R2, ADR embervm/001): state persists best-effort across calls sharing
the handle, and a reset is surfaced as ``session_reset`` in the response.
"""

from __future__ import annotations

from app.mcp_app import mcp
from sandbox.client import run_python_in_sandbox


@mcp.tool
async def run_python(
    code: str, files: list[dict] | None = None, session: str | None = None
) -> dict:
    """Run a short Python 3.12 script in an isolated, zero-egress sandbox.

    By default the sandbox is a one-shot Firecracker microVM: nothing persists
    between calls, there is no network access at all, and the run is killed
    after about 25 seconds of wall-clock time. Use it for exact computation
    (arithmetic, date math, unit conversions, statistics, simulations),
    parsing or crunching pasted data, or generating a quick chart, rather
    than estimating an answer from memory.

    Optional stateful sessions: pass a stable session handle (any short string
    of your choosing, reused across calls) to keep one long-lived interpreter
    warm so variables, imported modules, and files written to the working
    directory carry over from one call to the next, instead of starting cold
    each snippet. State is best-effort: it persists across turns but MAY be
    reset if the session sits idle too long, is evicted under disk pressure, or
    a snippet times out. When that happens the response carries session_reset
    true and the interpreter is empty again, so re-run any setup (imports,
    variable definitions, files) the later code depends on. There is still no
    network in a session. Omit session for the classic one-shot run.

    Available besides the Python 3.12 standard library: numpy, pandas,
    scipy, matplotlib (headless "Agg" backend, save figures to files rather
    than calling plt.show()), pillow (PIL), pyyaml (yaml), and
    python-dateutil (dateutil). Nothing else is installed: there is no
    duckdb, sympy, tabulate, or openpyxl, and no requests, httpx, or any
    other HTTP client, since the sandbox cannot reach the network at all.

    Save any figure or output file with a plain relative filename such as
    chart.png. Only files written to the working directory are returned, so an
    absolute path or a /tmp path is lost.

    For tabular data there is a baked helper. Run "from sandbox_tools import
    render_table" then render_table(headers, rows, title=None) to write a styled
    table.png (dark header, zebra rows, numeric columns right-aligned).

    Args:
        code: The Python source to run. It is written to a file and executed
            with python3. Use print(...) for anything you want back as
            stdout.
        files: Optional input files to write into the working directory
            before running code. Each entry needs a path (relative
            filename) and content_b64 (its content, base64-encoded).
        session: Optional session handle. Absent runs one-shot (no state).
            Present keeps state warm across calls that reuse the same handle,
            best-effort (see above).

    Returns:
        On success, the daemon's structured result: stdout, stderr,
        exit_code, duration_ms, truncated (whether any output was cut off to
        stay under the response size cap), and files (any regular files
        present in the working directory after the run that weren't given as
        input, base64-encoded under content_b64 next to their path). In session
        mode, session_reset is true when the persisted state was lost before
        this run. On failure, a dict with a single error key describing what
        went wrong.
    """
    return await run_python_in_sandbox(code, files=files, session=session)
