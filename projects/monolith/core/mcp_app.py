"""Shared FastMCP instance for the monolith.

Each domain module (knowledge, chat, etc.) imports ``mcp`` and registers
tools with ``@mcp.tool``.  The instance is mounted once in ``app/main.py``.
"""

from fastmcp import FastMCP

mcp = FastMCP("Monolith")
