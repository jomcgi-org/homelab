import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Env keys that must never reach a verifier subprocess (untrusted model output runs here).
_DENY_PREFIXES = (
    "KUBE",
    "OPENROUTER",
    "OP_",
    "ONEPASSWORD",
    "AWS_",
    "GITHUB_TOKEN",
    "BUILDBUDDY",
    "ANTHROPIC",
    "OPENAI",
)
# Minimal env the tools actually need.
_ALLOW_KEYS = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)


@dataclass
class SandboxResult:
    rc: int
    stdout: str
    stderr: str
    timed_out: bool


def _scrubbed_env() -> dict[str, str]:
    env = {k: os.environ[k] for k in _ALLOW_KEYS if k in os.environ}
    return {k: v for k, v in env.items() if not k.startswith(_DENY_PREFIXES)}


def run_sandboxed(cmd: list[str], *, cwd: Path, timeout_s: int) -> SandboxResult:
    """Run an untrusted command with a scrubbed env in cwd. No cluster creds, no tokens.

    macOS caveat: this does not network-isolate (that needs a sandbox profile / container);
    env-scrubbing + temp cwd + no-creds is the portable floor. Tighten in CI if needed.
    """
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            env=_scrubbed_env(),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            start_new_session=True,
        )
        return SandboxResult(proc.returncode, proc.stdout, proc.stderr, False)
    except subprocess.TimeoutExpired as e:
        out = e.stdout or ""
        err = (e.stderr or "") + "\n[sandbox] timed out"
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        return SandboxResult(124, out, err, True)
