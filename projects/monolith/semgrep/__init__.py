"""Semgrep scan MCP surface.

A single ``semgrep_scan`` MCP tool that forwards changed files to the
in-cluster ``fc-invoke`` HTTP daemon and returns its findings. The
daemon URL is injected from Helm values (``FC_INVOKE_URL``); this
package never hardcodes the in-cluster service address.
"""
