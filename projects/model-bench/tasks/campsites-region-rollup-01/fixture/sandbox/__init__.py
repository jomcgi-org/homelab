"""Sandbox code-executor MCP surface (ADR agents/044).

A single ``run_python`` MCP tool that forwards code (and optional input
files) to the in-cluster ``fc-invoke`` ``sandbox`` workload -- a zero-egress,
one-shot Python executor -- and returns its structured result. The daemon URL
is injected from Helm values (``FC_INVOKE_URL``); this package never
hardcodes the in-cluster service address.
"""
