"""Temporary probe to validate the semgrep scan-perf comparison pipeline end to
end. It deliberately introduces a finding so the Semgrep managed scan of this
PR becomes discoverable via the findings API (first_seen_scan_id), which is the
only way the harvest can see it. This PR exists only to trigger a homelab scan
and a managed scan on the same ref; it is closed without merging.
"""

import subprocess


def probe(cmd: str) -> None:
    # Deliberate dangerous-exec finding for the end-to-end validation.
    subprocess.Popen(cmd, shell=True)
