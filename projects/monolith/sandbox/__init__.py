"""Sandbox code-executor MCP surface.

A single ``run_python`` MCP tool that forwards code (and optional input
    files) to the EmberVM ``sandbox`` workload -- a zero-egress, one-shot Python
    executor -- and returns its structured result.
"""
