"""MCP tool that runs code in a per-language EmberVM sandbox workload."""

from __future__ import annotations

from core.mcp_app import mcp
from sandbox.client import run_code_in_sandbox


# The language bullets below wrap ``javascript`` in backticks on purpose. The MCP
# gateway runs every tool description through an XSS filter whose pattern treats
# whitespace followed by "javascript:" as a URI scheme, so a bare `- javascript:`
# bullet makes the gateway reject this tool and serve a catalogue without it. The
# refresh still reports success and names the drop only in validationErrors.
@mcp.tool
async def run_code(
    code: str, language: str = "python", files: list[dict] | None = None
) -> dict:
    """Run short code in an isolated, one-shot, zero-egress sandbox.

    There is no network at all. The run is killed after roughly 25 seconds of
    wall-clock time, which includes compilation for compiled languages.
    Nothing persists between calls. Only files written to the working
    directory with a plain relative filename are returned. Absolute paths and
    files under /tmp are lost.

    Supported languages and entry points:

    - python: main.py under python3. Includes numpy, pandas, scipy, matplotlib
      (headless Agg, save figures to files), pillow, pyyaml, and
      python-dateutil. Nothing else is installed, including HTTP clients.
      ``from sandbox_tools import render_table`` writes a styled table.png.
    - go: ``package main`` with ``func main()``, run with ``go run .``. Standard
      library only. Module fetching is off, so third-party imports fail at
      compile time.
    - rust: ``fn main()``, compiled with ``rustc -O``. Standard library only.
      There is no cargo or crates.io, so no serde, rand, or regex.
    - elixir: a script, not a Mix project, run with ``elixir main.exs``. Elixir
      plus OTP only. There is no Hex, so no Jason or Nx. Do not read stdin.
    - ocaml: ``let () = ...`` entry point, run with ``ocaml main.ml`` in
      bytecode script mode. Standard library only. There is no opam, Core, or
      Lwt.
    - ``javascript``: a CommonJS script under ``node main.js``. Node standard
      library only. There is no npm or node_modules. ``fetch`` exists, but every
      call fails because there is no network.

    Args:
        code: Source code for the selected language.
        language: One of python, go, rust, elixir, ocaml, or javascript.
        files: Optional input files to write into the working directory
            before running code. Each entry needs a path (relative
            filename) and content_b64 (its content, base64-encoded).

    Returns:
        stdout, stderr, exit_code, duration_ms, truncated, and files. A compile
        error has a nonzero exit_code and the compiler's diagnostics on stderr.
        Broker failures return a dict with a single error key.
    """
    return await run_code_in_sandbox(code, language=language, files=files)
