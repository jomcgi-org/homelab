"""Semgrep scan MCP surface.

A single ``semgrep_scan`` MCP tool that forwards changed files to the
in-cluster ``semgrep-scand`` HTTP daemon and returns its findings. The
daemon URL is injected from Helm values (``SEMGREP_SCAND_URL``); this
package never hardcodes the in-cluster service address.
"""
