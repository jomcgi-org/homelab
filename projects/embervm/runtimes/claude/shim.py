#!/usr/bin/python3
"""HTTP over vsock shim for a long-lived Claude Code CLI session."""

import base64
import collections
import http.server
import json
import math
import os
import queue
import re
import signal
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import zlib


GUEST_HTTP_PORT = 1027
EGRESS_LOCALHOST = "127.0.0.1"
EGRESS_PORT_ENV = "EMBER_EGRESS_PORT"
DEFAULT_EGRESS_PORT = 1024
VSOCK_EGRESS_CID = 2
VSOCK_EGRESS_PORT = 1025
# The one destination port whose tunnels must NOT propagate the client's
# half-close onto the vsock leg (#4389): git's stateless protocol half-closes
# after the request and FC hybrid vsock kills the in-flight response on any
# shutdown. See VsockEgressForwarder._forward.
GIT_DAEMON_PORT = "9418"
EGRESS_VSOCK_CONNECT_TIMEOUT_SECONDS = 5.0
EGRESS_VSOCK_CONNECT_ATTEMPTS = 3
EGRESS_VSOCK_CONNECT_BACKOFF_SECONDS = 0.2
VSOCK_ADDRESS_FAMILY = getattr(socket, "AF_VSOCK", -1)
HEALTHZ_PATH = "/shim/healthz"
READY_PATH = "/shim/ready"
TURN_PATH = "/shim/turn"
INTERRUPT_PATH = "/shim/interrupt"
CLOCK_PATH = "/shim/clock"
DEFAULT_WORKSPACE = "/workspace"
VOLUME_DEVICE_ENV = "EMBER_VOLUME_DEV"
DEFAULT_VOLUME_DEVICE = "/dev/vdb"
MAX_TURN_DIFF_BYTES = 5 * 1024 * 1024
MAX_TURN_DIFF_COMPRESSED_BYTES = 1024 * 1024


def _emit_turn_diff_outcome(checkout_dir, phase, outcome):
    """Emit one best-effort diagnostic for a turn diff capture outcome."""
    try:
        resolved_checkout_dir = os.path.abspath(checkout_dir)
        fields = [
            "ember-claude-shim: turn-diff",
            "phase=%s" % phase,
            "outcome=%s" % outcome,
            "checkout_dir=%s" % resolved_checkout_dir,
        ]
        sys.stderr.write("%s\n" % " ".join(fields))
        sys.stderr.flush()
    except Exception:
        pass


def _git_read_argv(checkout_dir, *args):
    """git argv for reading a checkout the shim does not own.

    The shim runs as root while hydration clones under _cli_privilege_kwargs, so
    the checkout belongs to the CLI uid. git refuses to operate on a repository
    owned by another user ("detected dubious ownership") and exits non-zero,
    which is why capture reported rev_parse_failed on every turn while the agent
    used the same repo without trouble.

    safe.directory is scoped to this one path and passed per invocation, so
    nothing is written to any git config. Dropping to the CLI uid instead would
    only move the mismatch: the checkout is not always owned by that user
    either, and a read as root is the one thing that works in both directions.
    """
    return [
        "git",
        "-c",
        "safe.directory=%s" % checkout_dir,
        "-C",
        checkout_dir,
        *args,
    ]


def _capture_turn_base(checkout_dir):
    """Best-effort checkout HEAD capture. A failure must not affect the turn."""
    try:
        if not os.path.exists(os.path.join(checkout_dir, ".git")):
            _emit_turn_diff_outcome(checkout_dir, "base", "no_git_dir")
            return None
        # Run git as the CLI user, exactly as hydration does. The shim is root
        # and hydration clones under these kwargs, so the checkout is owned by
        # the CLI uid. Calling git as root against it trips git's dubious
        # ownership check, which exits non-zero with the reason on stderr. That
        # is why capture reported rev_parse_failed on every turn while the agent
        # cloned, committed and pushed the same repo without trouble.
        result = subprocess.run(
            _git_read_argv(checkout_dir, "rev-parse", "HEAD"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )
        if result.returncode != 0:
            _emit_turn_diff_outcome(
                checkout_dir,
                "base",
                "rev_parse_failed:%s"
                % (result.stderr or b"").decode("utf-8", "replace").strip()[:200],
            )
            return None
        base_sha = result.stdout.decode("ascii", "strict").strip()
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", base_sha):
            _emit_turn_diff_outcome(checkout_dir, "base", "sha_malformed")
            return None
        _emit_turn_diff_outcome(checkout_dir, "base", "success")
        return base_sha
    except Exception:
        _emit_turn_diff_outcome(checkout_dir, "base", "base_exception")
        return None


def _capture_turn_diff(checkout_dir, base_sha):
    """Return a compressed git diff record without ever failing the turn."""
    if not base_sha:
        _emit_turn_diff_outcome(checkout_dir, "diff", "no_base_sha")
        return None
    try:
        # Same privileges as the rev-parse above and as hydration's clone: the
        # checkout belongs to the CLI user, not to root.
        result = subprocess.run(
            _git_read_argv(checkout_dir, "diff", base_sha),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
        if result.returncode != 0:
            _emit_turn_diff_outcome(
                checkout_dir,
                "diff",
                "diff_failed:%s"
                % (result.stderr or b"").decode("utf-8", "replace").strip()[:200],
            )
            return None
        raw = result.stdout + _untracked_file_diffs(checkout_dir)
        if len(raw) > MAX_TURN_DIFF_BYTES:
            _emit_turn_diff_outcome(checkout_dir, "diff", "truncated_raw")
            return {"base_sha": base_sha, "zlib_b64": None, "truncated": True}
        compressed = zlib.compress(raw)
        if len(compressed) > MAX_TURN_DIFF_COMPRESSED_BYTES:
            _emit_turn_diff_outcome(checkout_dir, "diff", "truncated_compressed")
            return {"base_sha": base_sha, "zlib_b64": None, "truncated": True}
        _emit_turn_diff_outcome(checkout_dir, "diff", "success")
        return {
            "base_sha": base_sha,
            "zlib_b64": base64.b64encode(compressed).decode("ascii"),
            "truncated": False,
        }
    except Exception:
        _emit_turn_diff_outcome(checkout_dir, "diff", "diff_exception")
        return None


# Untracked files are capped so a stray build tree or vendored download cannot
# turn the diff into megabytes of noise; the raw cap above still applies.
MAX_TURN_DIFF_UNTRACKED_FILES = 200


def _untracked_file_diffs(checkout_dir):
    """Render new, untracked files as new-file hunks, read-only.

    `git diff <base>` only sees tracked paths, so a test file, a go.mod or an
    answer.json the agent created never reached the stored diff (#5051).
    `git add -N` would fix that but writes the index as root, which then locks
    the CLI user out of its own checkout, so this stays read-only:
    ls-files for the names, then one `git diff --no-index /dev/null <path>`
    per file, whose output already carries the new-file headers git apply
    expects. Anything odd (a name that is not a regular file, a git error)
    is skipped, never raised.
    """
    try:
        listing = subprocess.run(
            _git_read_argv(checkout_dir, "ls-files", "--others", "--exclude-standard"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
        if listing.returncode != 0:
            return b""
        names = [
            line
            for line in listing.stdout.decode("utf-8", "replace").split("\n")
            if line and os.path.isfile(os.path.join(checkout_dir, line))
        ]
        chunks = []
        for name in names[:MAX_TURN_DIFF_UNTRACKED_FILES]:
            # --no-index exits 1 when the files differ, which is the normal
            # case here, so the return code is not an error signal.
            single = subprocess.run(
                _git_read_argv(checkout_dir, "diff", "--no-index", "/dev/null", name),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=30,
            )
            if single.stdout:
                chunks.append(single.stdout)
        return b"".join(chunks)
    except Exception:
        return b""


def _turn_timing_now():
    """Return a monotonic timestamp without allowing diagnostics to fail work."""
    try:
        return time.monotonic()
    except Exception:
        return None


def _emit_turn_timing(phase, elapsed=None, path=None, status=None, extra=None):
    """Best-effort timing telemetry for a single turn phase."""
    try:
        if elapsed is None:
            return
        fields = ["ember-claude-shim: turn-timing", "phase=%s" % phase]
        if path is not None:
            fields.append("path=%s" % path)
        if status is not None:
            fields.append("status=%s" % status)
        extra = extra if isinstance(extra, dict) else {}
        if "calls" in extra:
            fields.append("calls=%s" % extra["calls"])
        fields.append("ms=%s" % max(0, int(elapsed * 1000)))
        for key, value in extra.items():
            if key != "calls":
                fields.append("%s=%s" % (key, value))
        sys.stderr.write("%s\n" % " ".join(fields))
        sys.stderr.flush()
    except Exception:
        pass


def _emit_elapsed(phase, started, path=None, status=None):
    """Emit elapsed monotonic time, swallowing all instrumentation failures."""
    try:
        finished = _turn_timing_now()
        if started is not None and finished is not None:
            _emit_turn_timing(phase, finished - started, path=path, status=status)
    except Exception:
        pass


def _write_hydration_diagnostics(exc, checkout_dir):
    """Write hydration diagnostics to stderr when subprocess.TimeoutExpired occurs.

    Args:
        exc: subprocess.TimeoutExpired or any Exception.
        checkout_dir: path to the directory being checked out.

    The diagnostic path is deliberately best-effort because it runs while
    handling another failure and stderr is the guest's only telemetry channel.
    """
    prefix = "ember-claude-shim: hydration-diag: "

    def write_line(line):
        sys.stderr.write(prefix + line + "\n")
        sys.stderr.flush()

    def write_value(label, value):
        for line in value.splitlines() or [""]:
            write_line("%s%s" % (label, line))

    try:
        write_line("exception=%s" % type(exc).__name__)
        stderr = getattr(exc, "stderr", None)
        if stderr is None:
            stderr = ""
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        stderr = stderr.replace("\r", "\n")
        write_value("stderr=", stderr[-2000:])

        stdout = getattr(exc, "stdout", None)
        if stdout is None:
            stdout = ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", "replace")
        write_value("stdout=", stdout[-200:])

        checkout_bytes = 0
        for root, dirs, files in os.walk(checkout_dir):
            for name in dirs + files:
                try:
                    checkout_bytes += (
                        os.stat(
                            os.path.join(root, name), follow_symlinks=False
                        ).st_blocks
                        * 512
                    )
                except OSError:
                    pass
        try:
            checkout_bytes += (
                os.stat(checkout_dir, follow_symlinks=False).st_blocks * 512
            )
        except OSError:
            pass
        write_line("checkout_kb=%s" % (checkout_bytes // 1024))

        def read_diskstats(label):
            try:
                with open("/proc/diskstats") as stream:
                    lines = [
                        line.rstrip("\n")
                        for line in stream
                        if len(line.split()) >= 3 and line.split()[2] in ("vda", "vdb")
                    ]
            except (OSError, IOError):
                lines = []
            if lines:
                for line in lines:
                    write_line("%s%s" % (label, line))
            else:
                write_line("%sunavailable" % label)

        read_diskstats("diskstats=")
        time.sleep(2)
        read_diskstats("diskstats2=")
    except Exception as diag_exc:
        try:
            write_line("hydration-diag failed: %s" % diag_exc)
        except Exception:
            pass


PREWARM_CLIS_ENV = "EMBER_PREWARM_CLIS"
SUPPORTED_PREWARM_CLIS = ("claude",)
CODEX_MODELS = {
    "luna": ("gpt-5.6-luna", "medium"),
    "terra": ("gpt-5.6-terra", "high"),
    "sol": ("gpt-5.6-sol", "high"),
}
CLAUDE_MODELS = {
    "opus": "opus",
    "sonnet": "sonnet",
    "fable": "claude-fable-5",
}
DEFAULT_CODEX_MODEL = "luna"
CODEX_SUBSCRIPTION_BASE_URL_ENV = "CODEX_SUBSCRIPTION_BASE_URL"
DEFAULT_CODEX_SUBSCRIPTION_BASE_URL = "http://chatgpt.com/backend-api/"
CODEX_DUMMY_ACCOUNT_ID = "guest-subscription-account"
PI_MODELS = {
    # Must match the vLLM --served-model-name (projects/inference/deploy/
    # values.yaml); a wrong name 404s at the provider ("The model does not
    # exist"), proven live in #4252.
    "qwen": "qwen3.6-27b",
}
DEFAULT_PI_MODEL = "qwen"

# PI context configuration. This constant is deliberately set BELOW vLLM's
# --max-model-len (projects/inference/deploy/values.yaml) because pi's
# clampMaxTokensToContext computes available output tokens by subtracting a
# fixed CONTEXT_SAFETY_TOKENS margin, and pi's estimateContextTokens uses a
# chars/4 heuristic rather than a tokenizer. The gap between PI_CONTEXT_WINDOW
# and the real vLLM limit absorbs estimate error. Measured in prod on
# 2026-08-07, a turn carrying two 50 KiB repetitive-ASCII tool results
# undercounted by 4097 tokens against pi's fixed 4096 margin, failing with a
# 400 error by one token. Setting PI_CONTEXT_WINDOW to 30000 converts the fixed
# 4096 margin into an effective margin of (32768 - 30000) + 4096 = 6864 tokens,
# approximately 1.7x the observed worst-case undercount. This gap also lowers
# the compaction trigger from 22528 to 19760 tokens, providing more mid-run
# runway before the window fills. This matters because pi does not check
# compaction between tool iterations. Do NOT "correct" this value upward to
# match vLLM's --max-model-len; the gap is the point.
PI_CONTEXT_WINDOW = 30000
# MINIMUM required gap between PI_CONTEXT_WINDOW and vLLM's real
# --max-model-len, enforced by pi_context_window_sync_test. It is a floor, not
# the actual gap: today the real gap is 32768 - 30000 = 2768. Lowering this
# constant weakens the guard, so treat a test failure against it as a signal to
# lower PI_CONTEXT_WINDOW rather than to lower this floor.
PI_CONTEXT_WINDOW_HEADROOM = 2048
# pi's hardcoded safety margin in clampMaxTokensToContext.
PI_CONTEXT_SAFETY_TOKENS = 4096
# Maximum output tokens pi will attempt to generate. This caps reply length to
# prevent bloated answers that waste the context window. It also caps pi's
# compaction summary at PI_MAX_OUTPUT_TOKENS via
# min(floor(0.8 * reserveTokens), model.maxTokens).
# pi's estimateContextTokens anchors on the provider's real usage.totalTokens
# from the last assistant message and only chars/4-estimates messages after it,
# so the exposure is one turn's trailing tool results, not the whole
# conversation. This is why PI_MAX_OUTPUT_TOKENS matters for compaction but not
# for 400-safety.
#
# 12288, not 4096: qwen3.8 thinks before it answers and the reasoning counts
# against this cap. At 4096 a hard task hit the cap mid-reasoning, pi ended
# the turn with agent_end and no assistant text, and the shim raised
# "pi turn produced no output" (3 of 5 graded long reps in the #5051
# baseline). model-bench runs the same model at 16384 and passes 6/7 hard
# tasks; 12288 keeps the compaction reserve just over half the window.
PI_MAX_OUTPUT_TOKENS = 12288

# Thinking level for the pi lane. "off" makes pi send
# chat_template_kwargs.enable_thinking=false to the qwen server, which for a
# short task cuts generation from a full reasoning trace to a direct answer
# (measured 32 -> 4 tokens on a trivial prompt, #5051). This is the SMALL-TASK
# lane (chart comment), so thinking off is the intended default; flip to
# "high" to restore full reasoning. The value must be one of pi's
# ThinkingLevel strings: off, minimal, low, medium, high.
PI_DEFAULT_THINKING_LEVEL = "off"
PI_THINKING_LEVELS = ("off", "minimal", "low", "medium", "high")


def _resolve_thinking_level(value):
    """Map an inbound thinking request to a valid pi ThinkingLevel.

    None or an unrecognised value falls back to PI_DEFAULT_THINKING_LEVEL, so a
    bad or missing field can never crash a turn or silently pick a wrong level.
    A bare True means "the caller wants thinking" -> "high"; False -> "off".
    """
    if value is True:
        return "high"
    if value is False:
        return "off"
    if isinstance(value, str) and value in PI_THINKING_LEVELS:
        return value
    return PI_DEFAULT_THINKING_LEVEL


# Compaction reserve in tokens. This must exceed PI_MAX_OUTPUT_TOKENS plus
# PI_CONTEXT_SAFETY_TOKENS so pi starts compacting while there is still room for
# a full response at turn boundaries. pi checks compaction at agent_end and
# before a prompt, not between tool iterations, so a run that approaches the
# reserve mid-execution can still produce short replies.
PI_COMPACTION_RESERVE_TOKENS = 16896

# Tokens to keep after compaction. pi's default keepRecentTokens (20000)
# exceeds the usable budget after pi's default 16384 reserve (32768 - 16384 =
# 16384), so post-compaction context would land straight back above the
# threshold. This value must be less than
# (PI_CONTEXT_WINDOW - PI_COMPACTION_RESERVE_TOKENS) so compaction actually
# helps when it fires.
PI_COMPACTION_KEEP_RECENT_TOKENS = 8000
PI_WEB_RESEARCH_EXTENSION = "/usr/share/ember-pi/extensions/web-research.ts"
MAX_REQUEST_BODY_BYTES = 1 << 20
MAX_TOOL_INPUT_BYTES = 4096
# Cap on the proxy request head the egress forwarder reads before it knows the
# destination. Generous for real headers, bounded so a client that never sends
# the terminating blank line cannot grow this buffer without limit.
MAX_PROXY_HEAD_BYTES = 64 << 10
INIT_READ_TIMEOUT_ENV = "EMBER_INIT_READ_TIMEOUT"
DEFAULT_INIT_READ_TIMEOUT = 90.0
# Initialization waits inside a turn, so this must stay below the CP per-invoke
# budget (spec.invocation.timeoutSeconds, currently 900 for claude-runtime),
# below TURN_READ_TIMEOUT's 600 second role, and generous enough for the cold
# first init of the 262MB Bun binary in a microVM. BuildBase also uses this
# through /shim/ready and has its own generous budget.


def _read_init_timeout():
    """Return the positive finite init timeout from the environment."""
    try:
        value = float(os.environ.get(INIT_READ_TIMEOUT_ENV, DEFAULT_INIT_READ_TIMEOUT))
    except (TypeError, ValueError):
        return DEFAULT_INIT_READ_TIMEOUT
    if not math.isfinite(value) or value <= 0:
        return DEFAULT_INIT_READ_TIMEOUT
    return value


INIT_READ_TIMEOUT = _read_init_timeout()
# Per-event inactivity timeout for the read loop in turn(). Resets on every
# stream event, so a turn emitting steady output can run far longer than this.
# Sized to span a single silent tool call: the CLI emits nothing while a Bash
# tool executes, so this must exceed the slowest realistic in-guest command
# (a build or test run), not the slowest turn. Its job is detecting a genuinely
# wedged CLI. The total-duration bound is enforced separately by the caller
# (read_timeout in projects/monolith/agent_sessions/transport.py), and this
# value must stay comfortably BELOW that one so the inner watchdog fires first
# and reports a specific error, rather than the caller timing out generically.
TURN_READ_TIMEOUT = 600.0
INTERRUPT_TIMEOUT = 30.0
CLI_PROBE_TIMEOUT = 10.0
HYDRATION_ATTEMPT_CAP = 3
# The guest path is proxied over vsock, so it is materially slower than a direct
# clone, and a FULL clone additionally pays 86k delta resolutions plus a 151 MB
# checkout on 2 vCPUs: the instrumented #4389 run finished deltas and most of
# the checkout just past the old 300 second cap. Only the first turn per
# session volume ever pays this (the rev-parse gate skips hydration after one
# success), and the outer budgets (monolith 1800s wall clock, invocation 900s)
# leave headroom.
GIT_CLONE_TIMEOUT_SECONDS = 600
PERMISSION_MODE_ENV = "EMBER_PERMISSION_MODE"
DEFAULT_PERMISSION_MODE = "bypassPermissions"
CLI_UID_ENV = "EMBER_CLI_UID"
CLI_GID_ENV = "EMBER_CLI_GID"
DEFAULT_CLI_UID = 65532
DEFAULT_CLI_GID = 65532
PERSISTENCE_MOUNT_PATH_ENV = "EMBER_PERSISTENCE_MOUNT_PATH"
DEFAULT_PERSISTENCE_MOUNT_PATH = "/session"
GUEST_INIT_PATH = "/usr/local/bin/ember-runtime-guest-init"
SANDBOX_PROMPT = (
    "Facts about the sandbox you are running in. They override any assumption "
    "you would otherwise make, and none of them are problems to debug.\n"
    "- You are alone in a disposable Firecracker microVM. There is no "
    "interactive terminal: you cannot prompt mid-turn, so a question ends your "
    "turn and is read asynchronously.\n"
    "- When the session has a repo, the checkout is at /workspace/src. It is a "
    "single-branch blob:none partial clone, so the FULL history of that branch "
    "is present and git log, git blame and git bisect all work. File contents "
    "are fetched on demand, so an operation touching many old revisions pauses "
    "while blobs download rather than failing.\n"
    "- origin points at GitHub over https. Pushes go to the real repository, so "
    "treat any push as publishing. The proxy attaches the credential on the way "
    "out; you hold none.\n"
    "- Your git identity is already configured. Do not set user.name or "
    "user.email.\n"
    "- All network egress is proxied. The public internet is reachable and "
    "in-cluster services are not. You hold no credentials: the proxy attaches "
    "them on the way out, so no token is readable from in here.\n"
    "- gh reaches GitHub even though `gh auth status` reports you are not "
    "logged in. That report is expected, because it inspects local credentials "
    "and the real one is never local. Judge by whether the request succeeds.\n"
)


def compose_system_prompt(caller_prompt=None):
    """Join the shim-owned sandbox prompt with an optional caller prompt."""
    if caller_prompt and caller_prompt.strip():
        return SANDBOX_PROMPT + "\n" + caller_prompt.strip()
    return SANDBOX_PROMPT


def _workspace_identity(path):
    """Get (st_dev, st_ino) for workspace path, or None on error."""
    try:
        stat = os.stat(path)
        return (stat.st_dev, stat.st_ino)
    except OSError:
        return None


def _cli_privilege_kwargs():
    """Return uid/gid kwargs only when the shim itself is running as root."""
    if os.geteuid() != 0:
        return {}
    return {
        "user": int(os.environ.get(CLI_UID_ENV, str(DEFAULT_CLI_UID))),
        "group": int(os.environ.get(CLI_GID_ENV, str(DEFAULT_CLI_GID))),
    }


GIT_PROXY_PATH = "/tmp/ember-git-proxy"

# The guest env var holding gh's login-gate dummy. Named here rather than
# inlined because it is the SAME switch two consumers flip: gh sends it as a
# bearer, and hydration sends it as Basic, and both exist only so the egress
# sidecar's presence-keyed injection fires.
GH_TOKEN_ENV = "GH_TOKEN"


def _github_basic_optin():
    """Return base64 for a Basic Authorization that opts into injection.

    The VALUE is inert and is discarded by the sidecar, which overwrites the
    header with the real credential. Only its PRESENCE matters, so an absent
    GH_TOKEN still yields a well-formed header rather than dropping the opt-in
    and taking a 401: the failure that would cause is silent hydration loss, and
    a header with an empty token costs nothing.
    """
    token = os.environ.get(GH_TOKEN_ENV, "")
    return base64.b64encode(("x-access-token:%s" % token).encode()).decode()


# The egress CA the sidecar mints MITM leaves from, and the reserved preamble
# name it serves that certificate on. Fetched at spawn rather than baked into
# the guest image so a CA rotation does not require a fleet base rebuild.
CA_FETCH_HOST = "ca.egress.internal:80"
CA_BUNDLE_PATH = "/tmp/ember-ca-bundle.crt"
# Where the image's own trust store lives (apko ca-certificates-bundle). The
# fetched CA is APPENDED to a copy of it rather than replacing it: the guest
# still has to verify the real public internet on any host the sidecar merely
# tunnels.
SYSTEM_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"


def fetch_egress_ca(timeout=EGRESS_VSOCK_CONNECT_TIMEOUT_SECONDS):
    """Return the egress CA certificate in PEM, or None when there is no CA.

    Speaks the same one-line preamble the forwarder uses, directly on the vsock,
    because this runs before any HTTP client exists to proxy through. A sidecar
    with no CA loaded closes without writing, which reads here as None and
    leaves the guest on its unmodified system trust store.
    """
    if VSOCK_ADDRESS_FAMILY == -1:
        return None
    sock = socket.socket(VSOCK_ADDRESS_FAMILY, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect((VSOCK_EGRESS_CID, VSOCK_EGRESS_PORT))
        sock.sendall(("%s\n" % CA_FETCH_HOST).encode("latin-1"))
        chunks = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
    except OSError as exc:
        sys.stderr.write("ember-claude-shim: egress CA fetch failed: %s\n" % exc)
        sys.stderr.flush()
        return None
    finally:
        try:
            sock.close()
        except OSError:
            pass
    pem = b"".join(chunks)
    return pem if b"BEGIN CERTIFICATE" in pem else None


def apply_egress_ca_trust():
    """Fetch + install the egress CA and export the trust variables.

    Called PER TURN, not at startup. A session guest is restored from a shared
    snapshot, so its process memory is cloned with main() long since finished:
    anything done there runs once, at base-build time, on a different machine,
    and can never run again for a real session. At base-build time the egress
    lane is not open either, so the fetch there fails with ECONNRESET and the
    trust is simply absent for every session that base ever serves.

    A turn is the first moment that is guaranteed to be post-restore with the
    lane live, which is why ensure_workspace_volume() is called from the same
    place. Re-running is cheap (one vsock round trip) and is what keeps a CA
    rotation from needing a fleet base rebuild.
    """
    bundle = install_egress_ca()
    if not bundle:
        return None
    os.environ.update(
        {
            # OpenSSL/python, curl, git and node/bun each read a different
            # variable for the same thing. gh is Go, which reads SSL_CERT_FILE.
            "SSL_CERT_FILE": bundle,
            "REQUESTS_CA_BUNDLE": bundle,
            "CURL_CA_BUNDLE": bundle,
            "GIT_SSL_CAINFO": bundle,
            "NODE_EXTRA_CA_CERTS": bundle,
        }
    )
    return bundle


def install_egress_ca():
    """Write system-trust + egress CA to CA_BUNDLE_PATH; return the path or None.

    Returning None means "leave every trust variable unset", which keeps the
    guest on its stock trust store. That is the correct degrade: a guest that
    trusted a CA it failed to fetch would fail every TLS handshake instead.
    """
    pem = fetch_egress_ca()
    if not pem:
        return None
    try:
        system = b""
        if os.path.exists(SYSTEM_CA_BUNDLE):
            with open(SYSTEM_CA_BUNDLE, "rb") as stream:
                system = stream.read()
        with open(CA_BUNDLE_PATH, "wb") as stream:
            stream.write(system)
            if system and not system.endswith(b"\n"):
                stream.write(b"\n")
            stream.write(pem)
    except OSError as exc:
        sys.stderr.write("ember-claude-shim: egress CA install failed: %s\n" % exc)
        sys.stderr.flush()
        return None
    return CA_BUNDLE_PATH


def _write_git_proxy_helper():
    """Install the stdlib-only git proxy used by session guests."""
    proxy = r"""#!/usr/bin/python3
import os
import socket
import sys
import threading
import time

EGRESS_LOCALHOST = "127.0.0.1"
EGRESS_PORT_ENV = "EMBER_EGRESS_PORT"
DEFAULT_EGRESS_PORT = 1024


def _pump_stdin_to_socket(source, sock):
    try:
        while True:
            # read1, NOT read: BufferedReader.read(n) blocks until it has all n
            # bytes or EOF, and git's protocol is request/response in messages of
            # a few hundred bytes. read(65536) therefore holds each request until
            # 64 KiB accumulates or the stream closes, stalling every negotiation
            # round trip and turning a 4s clone into a timeout. read1 returns what
            # is available after one syscall.
            data = source.read1(65536)
            if not data:
                break
            sock.sendall(data)
    except OSError:
        pass
    finally:
        # Half-close: tell the server the request stream is done while the
        # response direction keeps flowing. This stops at the forwarder and
        # is deliberately not propagated onto its vsock leg.
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass


LAST_RX = [time.monotonic()]


def _pump_socket_to_stdout(sock, destination):
    try:
        while True:
            data = sock.recv(65536)
            if not data:
                break
            LAST_RX[0] = time.monotonic()
            destination.write(data)
            destination.flush()
    except OSError:
        pass


def main():
    if len(sys.argv) != 3:
        sys.stderr.write("ERROR: expected host and port arguments\n")
        return 2
    host, port = sys.argv[1:]
    try:
        egress_port = int(os.environ.get(EGRESS_PORT_ENV, DEFAULT_EGRESS_PORT))
        try:
            handshake_timeout = float(
                os.environ.get("EMBER_GIT_PROXY_HANDSHAKE_TIMEOUT_SECONDS", "30")
            )
            if handshake_timeout <= 0:
                handshake_timeout = 30
        except ValueError:
            handshake_timeout = 30
        sock = socket.create_connection(
            (EGRESS_LOCALHOST, egress_port), timeout=handshake_timeout
        )
        # Git's protocol is request/response in small messages. Nagle holds
        # each write until the prior segment is ACKed, while delayed ACK waits
        # up to 40ms. This measured as ~55ms per 64 KiB chunk and about 10
        # seconds added to an 11.24 MiB clone, so disable Nagle on this socket.
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.sendall(("CONNECT %s:%s HTTP/1.1\r\n\r\n" % (host, port)).encode())
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                sys.stderr.write("ERROR: proxy closed during handshake\n")
                return 1
            response += chunk
            if len(response) > 65536:
                sys.stderr.write("ERROR: proxy handshake response is too large\n")
                return 1
        if not response.startswith(b"HTTP/1.1 200"):
            sys.stderr.write("ERROR: proxy handshake returned a non-200 response\n")
            return 1
        sock.settimeout(None)
        # This default is the whole hydration budget, not a safety margin, and it
        # has to live HERE rather than in chart values (#4429).
        #
        # The helper cannot see the end of a response: #4412 stopped propagating
        # half-close onto the vsock leg (Firecracker hybrid vsock has none, and
        # propagating it killed in-flight responses), so the server-held
        # connection never closes and git blocks until this process exits. The
        # deadline below is therefore charged to EVERY clone after its last byte.
        # Measured live at the old 10s default: an 11.24 MiB shallow clone took
        # 10.9s wall for ~1.2s of transfer.
        #
        # 2s, not lower: the mirror serves that entire clone in 1.3s over
        # loopback, so this leaves roughly 2x margin over the whole server-side
        # operation rather than over one gap. If it ever does fire early the pack
        # fails git's own checksum, so hydration errors loudly and retries rather
        # than leaving a silently truncated checkout.
        #
        # Not a chart knob: the workload CR's initEnv is a base-SIGNATURE input
        # only. The control plane sends it as BuildBaseRequest.init_env, noded
        # never reads that field (the base-build claim is ClaimSpec{Arch,
        # ThreadID}, and ClaimSpec has no env member), and the guest's entire
        # environment comes from guest-init setDefaultEnv plus ember.env.* boot
        # args that only ClaimStateful populates. Setting this in values.yaml
        # re-keys the base and rebuilds it while changing nothing in the guest,
        # which is exactly how the 10s default survived a deploy that looked
        # successful. The env override below stays for tests, which set it in
        # their own process environment.
        try:
            idle_exit = float(os.environ.get("EMBER_GIT_PROXY_IDLE_EXIT_SECONDS", "2"))
            if idle_exit <= 0:
                idle_exit = 2
        except ValueError:
            idle_exit = 2
        # Daemon threads: interpreter exit must never wait on a pump still
        # blocked in recv (the idle-exit path below leaves exactly that).
        stdin_pump = threading.Thread(
            target=_pump_stdin_to_socket, args=(sys.stdin.buffer, sock), daemon=True
        )
        stdout_pump = threading.Thread(
            target=_pump_socket_to_stdout, args=(sock, sys.stdout.buffer), daemon=True
        )
        stdin_pump.start()
        stdout_pump.start()
        stdin_pump.join()
        # git half-closes stdin right after its request and then reads the
        # response, so stdin EOF says nothing about completion. The response
        # side has no EOF either: the lane deliberately never propagates the
        # half-close onto vsock (#4389), so the server-held connection stays
        # open forever and git waits for THIS process to exit. Once the
        # response has been idle past the deadline, close up and leave; a
        # mid-response server pause shorter than the deadline is unaffected.
        LAST_RX[0] = time.monotonic()
        while stdout_pump.is_alive():
            stdout_pump.join(timeout=0.5)
            if not stdout_pump.is_alive():
                break
            if time.monotonic() - LAST_RX[0] > idle_exit:
                # shutdown, not just close: close() does not wake a thread
                # blocked in recv, SHUT_RDWR does.
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass
                break
        return 0
    except (OSError, ValueError) as exc:
        sys.stderr.write("ERROR: %s\n" % exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
"""
    with open(GIT_PROXY_PATH, "w") as stream:
        stream.write(proxy)
    os.chmod(GIT_PROXY_PATH, 0o755)


def _ensure_cli_dir(path):
    """Create a CLI state dir the CLI PROCESS can write into.

    The shim may run as root while the CLI is dropped to the runtime uid, so a
    bare makedirs here leaves a root-owned dir the CLI cannot create subdirs
    in (observed live: pi dying with EACCES on mkdir /workspace/.pi/sessions).
    Chown to the same uid/gid the CLI is spawned with whenever the shim is
    root; a non-root shim already creates dirs the CLI owns.
    """
    os.makedirs(path, exist_ok=True)
    kwargs = _cli_privilege_kwargs()
    if kwargs:
        os.chown(path, kwargs["user"], kwargs["group"])


def _persistence_mount_path():
    """Return the configured persistence mount path."""
    path = os.environ.get(PERSISTENCE_MOUNT_PATH_ENV)
    if path:
        return path

    try:
        with open("/proc/cmdline") as stream:
            cmdline = stream.read()
    except OSError:
        cmdline = ""
    for token in cmdline.split():
        if token.startswith("ember.volume_mount="):
            configured_path = token.split("=", 1)[1]
            if configured_path:
                return configured_path

    # The CR defines this durable volume path, but keep a default for non-init
    # startup paths that do not provide the boot argument.
    return DEFAULT_PERSISTENCE_MOUNT_PATH


def _ensure_persistence_mountpoint_writable(path):
    """Make the persistence mountpoint writable by the CLI process."""
    kwargs = _cli_privilege_kwargs()
    if not kwargs:
        return

    try:
        ownership = os.stat(path)
    except OSError:
        return

    if ownership.st_uid == kwargs["user"] and kwargs["group"] == ownership.st_gid:
        return

    # Only the mountpoint is chowned. Its contents can be a 10 GiB volume, so
    # recursing would put unbounded work on every boot.
    #
    # A failure here is deliberately NOT swallowed. The bug this fixes was
    # invisible precisely because the CLI fell back to ephemeral storage and
    # the session looked healthy while persisting nothing, so a mountpoint the
    # CLI still cannot write is worth failing loudly on (#4291).
    os.chown(path, kwargs["user"], kwargs["group"])


def ensure_workspace_volume():
    """Ensure the session volume is mounted before the first real turn.

    The guest-init binary owns the privileged mount implementation. It is
    invoked after the vsock request reaches the shim, which is late enough for
    a resumed VM's per-session drive to have replaced the warm-base device.
    The Go side checks mountinfo, so this remains a no-op for cold guests and
    repeated turns.

    Pass --device /dev/vdb explicitly: restored guests resume with the base's
    cmdline, which has no volume argument and never re-reads boot args, so the
    device cannot come from the kernel command line.
    """
    # The image always contains guest-init. Keeping this guard makes the shim
    # library usable in host-side unit tests and in non-microVM tooling, where
    # the privileged guest helper is intentionally absent.
    if not os.path.exists(GUEST_INIT_PATH):
        return
    try:
        subprocess.run(
            [GUEST_INIT_PATH, "--ensure-workspace-volume", "--device", "/dev/vdb"],
            check=True,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise StartupError("could not ensure workspace volume: %s" % exc) from exc


def _transcript_slug(cwd):
    """Return the Claude project directory slug for a working directory."""
    return cwd.replace("/", "-")


def _transcript_exists(cwd, session_id):
    home = os.environ.get("HOME", os.path.expanduser("~"))
    path = os.path.join(
        home,
        ".claude",
        "projects",
        _transcript_slug(cwd),
        "%s.jsonl" % session_id,
    )
    return os.path.isfile(path)


def _workspace_is_tmpfs():
    """Return whether /workspace is mounted as tmpfs, or False on probe errors."""
    try:
        with open("/proc/mounts") as mounts:
            last_match = None
            for line in mounts:
                fields = line.split()
                if (
                    len(fields) >= 3
                    and fields[1].replace(r"\040", " ") == DEFAULT_WORKSPACE
                ):
                    last_match = fields[2]
            return last_match == "tmpfs" if last_match else False
    except Exception:
        return False
    return False


def _volume_has_ext4():
    """Return whether the configured volume device has an ext4 superblock."""
    device = os.environ.get(VOLUME_DEVICE_ENV, DEFAULT_VOLUME_DEVICE)
    try:
        with open(device, "rb") as volume:
            volume.seek(0x438)
            return volume.read(2) == b"\x53\xef"
    except Exception:
        return False


def _truncate_ring_for_error(ring, max_len=1500):
    """Truncate the ring buffer for inclusion in an error message."""
    if not ring:
        return ""
    content = "\n".join(ring)
    if len(content) > max_len:
        content = content[:max_len] + "... (truncated)"
    return content


def _probe_cli_startup(executable):
    """Run CLI --version as a startup probe, log result, never fail."""
    try:
        # A version probe reads nothing; close stdin rather than inherit the
        # shim's, so a never-EOF stdin cannot block it.
        proc = subprocess.run(
            [executable, "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=CLI_PROBE_TIMEOUT,
            text=True,
            **_cli_privilege_kwargs(),
        )
        stdout = proc.stdout[:200] if proc.stdout else ""
        stderr = proc.stderr[:200] if proc.stderr else ""
        msg = "ember-claude-shim: cli-probe: exit=%d stdout=%r stderr=%r\n" % (
            proc.returncode,
            stdout,
            stderr,
        )
        sys.stderr.write(msg)
        sys.stderr.flush()
    except Exception as exc:
        sys.stderr.write("ember-claude-shim: cli-probe failed: %s\n" % exc)
        sys.stderr.flush()


class StartupError(Exception):
    pass


class SessionConflictError(StartupError):
    pass


_managed_child_pids = set()


def _reap_orphans(_signum=None, _frame=None):
    """Reap adopted grandchildren while leaving Popen-managed CLIs alone."""
    flags = os.WEXITED | os.WNOHANG | os.WNOWAIT
    while True:
        try:
            info = os.waitid(os.P_ALL, 0, flags)
        except (ChildProcessError, OSError):
            return
        if info is None or info.si_pid == 0:
            return
        if info.si_pid in _managed_child_pids:
            # Popen must reap its direct child itself to preserve its exit status.
            return
        try:
            os.waitpid(info.si_pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            pass


def install_child_reaper():
    # PID 1 adopts grandchildren left by background Bash tools. Reap only those
    # children, because reaping the CLI here would break subprocess wait/poll.
    signal.signal(signal.SIGCHLD, _reap_orphans)


def _json_line(value):
    return (json.dumps(value, separators=(",", ":")) + "\n").encode("utf-8")


def _user_message_line(message, session_id=None):
    value = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": message}],
        },
    }
    if session_id:
        value["session_id"] = session_id
    return _json_line(value)


def voice_summary(result):
    text = result if isinstance(result, str) else str(result or "")
    match = re.search(r"<voice>\s*(.*?)\s*</voice>", text, re.DOTALL)
    if match and match.group(1).strip():
        return re.sub(r"\s+", " ", match.group(1).strip())[:200]
    sentence = re.split(r"(?<=[.!?])\s+", text.strip(), maxsplit=1)[0]
    return sentence[:200]


def _input_value(value, key):
    return value.get(key, "") if isinstance(value, dict) else value


def _bounded_tool_input(value):
    try:
        encoded = json.dumps(value, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        return "[omitted: non-JSON tool input]"
    if len(encoded) > MAX_TOOL_INPUT_BYTES:
        return "[omitted: tool input exceeds %d bytes]" % MAX_TOOL_INPUT_BYTES
    return value


def activity_from_events(events):
    activity = []
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("type") == "tool_execution_start":
            name = event.get("toolName")
            value = event.get("args")
            if name == "edit":
                activity.append(
                    {"type": "edit", "file_path": _input_value(value, "path")}
                )
            elif name == "write":
                activity.append(
                    {"type": "write", "file_path": _input_value(value, "path")}
                )
            elif name == "bash":
                activity.append(
                    {"type": "bash", "command": _input_value(value, "command")}
                )
            else:
                activity.append(
                    {
                        "type": "tool_use",
                        "name": name,
                        "input": _bounded_tool_input(value),
                    }
                )
            continue
        if event.get("type") != "assistant":
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            value = block.get("input")
            if block_type == "tool_use":
                name = block.get("name")
                if name == "Edit":
                    activity.append(
                        {"type": "edit", "file_path": _input_value(value, "file_path")}
                    )
                elif name == "Write":
                    activity.append(
                        {"type": "write", "file_path": _input_value(value, "file_path")}
                    )
                elif name == "Bash":
                    activity.append(
                        {"type": "bash", "command": _input_value(value, "command")}
                    )
                else:
                    activity.append(
                        {
                            "type": "tool_use",
                            "name": name,
                            "input": _bounded_tool_input(value),
                        }
                    )
            elif block_type in ("Edit", "Write", "Bash"):
                key = {"Edit": "file_path", "Write": "file_path", "Bash": "command"}[
                    block_type
                ]
                activity.append(
                    {"type": block_type.lower(), key: _input_value(value, key)}
                )
    return activity


class VsockEgressForwarder:
    """Forward each accepted local TCP connection to one host vsock tunnel."""

    def __init__(self, port=DEFAULT_EGRESS_PORT):
        self.port = port
        self._listener = None
        self._accept_thread = None
        self._closed = threading.Event()

    def listen(self):
        if self._listener is not None:
            raise RuntimeError("egress forwarder is already listening")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((EGRESS_LOCALHOST, self.port))
        listener.listen()
        self._listener = listener
        self.port = listener.getsockname()[1]
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()

    def close(self):
        self._closed.set()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
            self._listener = None
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=1)
            self._accept_thread = None

    def _accept_loop(self):
        while not self._closed.is_set():
            try:
                client, _ = self._listener.accept()
            except OSError as exc:
                if not self._closed.is_set():
                    sys.stderr.write(
                        "ember-claude-shim: egress accept failed: %s\n" % exc
                    )
                    sys.stderr.flush()
                return
            threading.Thread(target=self._forward, args=(client,), daemon=True).start()

    @staticmethod
    def _copy(source, destination, direction=None, *, propagate_half_close=True):
        total_bytes = 0
        next_boundary = 1024 * 1024
        error = None

        def write_progress(message):
            if direction is None:
                return
            # Copy threads are daemon threads that can outlive their test's (or
            # the process's) stderr; diagnostic chatter must never raise.
            try:
                sys.stderr.write(
                    "ember-claude-shim: egress-copy: %s %s\n" % (direction, message)
                )
                sys.stderr.flush()
            except Exception:
                pass

        try:
            while True:
                data = source.recv(64 * 1024)
                if not data:
                    return
                destination.sendall(data)
                total_bytes += len(data)
                while direction is not None and total_bytes >= next_boundary:
                    write_progress(str(next_boundary))
                    next_boundary += 1024 * 1024
        except OSError as exc:
            error = exc
            return
        finally:
            if direction is not None:
                write_progress(
                    "closed total=%s err=%s"
                    % (total_bytes, repr(error) if error is not None else "none")
                )
            if propagate_half_close:
                try:
                    destination.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

    @staticmethod
    def _read_proxy_request(client):
        """Read the proxy request head and return (host_port, leftover, is_connect).

        The CLI treats this listener as an ordinary HTTP proxy because that is
        what HTTPS_PROXY means, so it opens with either a CONNECT for a TLS
        origin or an absolute-URI request for a plain one. The host-side lane
        speaks neither: it wants a single "host:port\\n" preamble and raw bytes
        after it. This reads just far enough to learn the destination.

        Returns (None, b"", False) when the head is malformed or oversized, which
        the caller answers with an error rather than guessing a destination.
        """
        head = b""
        while b"\r\n\r\n" not in head:
            if len(head) > MAX_PROXY_HEAD_BYTES:
                return None, b"", False
            chunk = client.recv(4096)
            if not chunk:
                return None, b"", False
            head += chunk
        raw_head, _, leftover = head.partition(b"\r\n\r\n")
        lines = raw_head.split(b"\r\n")
        parts = lines[0].split()
        if len(parts) < 2:
            return None, b"", False
        method, target = parts[0].upper(), parts[1].decode("latin-1")
        if method == b"CONNECT":
            # "CONNECT host:443 HTTP/1.1": the target IS the destination, and the
            # head is consumed here because the client expects a proxy response
            # before it starts its TLS handshake.
            return target, leftover, True
        # An absolute-URI request ("GET http://host/path HTTP/1.1"). Take the
        # destination from the Host header, then replay the WHOLE head upstream:
        # an origin server must accept an absolute-URI request line, so no
        # rewriting is needed and none is attempted.
        host_port = None
        for line in lines[1:]:
            name, sep, value = line.partition(b":")
            if sep and name.strip().lower() == b"host":
                host_port = value.strip().decode("latin-1")
                break
        if not host_port:
            return None, b"", False
        if ":" not in host_port:
            host_port += ":80"
        return host_port, raw_head + b"\r\n\r\n" + leftover, False

    def _forward(self, client):
        upstream = None
        try:
            host_port, pending, is_connect = self._read_proxy_request(client)
            if host_port is None:
                client.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
                return
            # Bound the connect leg so a lane that cannot establish a connection
            # fails in seconds instead of silently consuming the full hydration
            # timeout; a transfer that stalls after connect is still governed by
            # the hydration timeout by design.
            last_error = None
            for attempt_index in range(EGRESS_VSOCK_CONNECT_ATTEMPTS):
                attempt = socket.socket(VSOCK_ADDRESS_FAMILY, socket.SOCK_STREAM)
                try:
                    attempt.settimeout(EGRESS_VSOCK_CONNECT_TIMEOUT_SECONDS)
                    attempt.connect((VSOCK_EGRESS_CID, VSOCK_EGRESS_PORT))
                    attempt.settimeout(None)
                    upstream = attempt
                    break
                except OSError as exc:
                    last_error = exc
                    try:
                        attempt.close()
                    except OSError:
                        pass
                    if attempt_index + 1 < EGRESS_VSOCK_CONNECT_ATTEMPTS:
                        time.sleep(
                            EGRESS_VSOCK_CONNECT_BACKOFF_SECONDS * (2**attempt_index)
                        )
            if upstream is None:
                sys.stderr.write(
                    "ember-claude-shim: egress vsock connect failed after %s attempts: %s\n"
                    % (EGRESS_VSOCK_CONNECT_ATTEMPTS, last_error)
                )
                sys.stderr.flush()
                return
            # The one-line preamble the host lane parses before it dials, and the
            # only thing this forwarder ever writes on the guest's behalf.
            upstream.sendall(("%s\n" % host_port).encode("latin-1"))
            if is_connect:
                # The tunnel is established as far as the client is concerned; the
                # sidecar reports a dial failure by closing, which surfaces to the
                # CLI as a dropped connection rather than a proxy error, matching
                # how any CONNECT proxy behaves once it has answered.
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            if pending:
                upstream.sendall(pending)
            # FC hybrid vsock has no half-close; propagating the client's
            # SHUT_WR onto the vsock leg kills the in-flight response (#4389).
            # Suppression is scoped to the git daemon port because git:// is
            # the one protocol here that half-closes mid-exchange and then
            # expects a large response; its server never needs the EOF (the
            # response ends via flush-pkt) and the leaked tunnel is bounded by
            # the VM's lifetime. Everything else (HTTPS CONNECT) only closes
            # when the exchange is over, and keeping the propagation there is
            # what tears those tunnels down promptly.
            half_close_upstream = not host_port.endswith(":" + GIT_DAEMON_PORT)
            copies = [
                threading.Thread(
                    target=self._copy,
                    args=(client, upstream, "up"),
                    kwargs={"propagate_half_close": half_close_upstream},
                ),
                threading.Thread(target=self._copy, args=(upstream, client, "down")),
            ]
            for copy_thread in copies:
                copy_thread.start()
            for copy_thread in copies:
                copy_thread.join()
        except OSError as exc:
            sys.stderr.write(
                "ember-claude-shim: egress vsock connect failed: %s\n" % exc
            )
            sys.stderr.flush()
        finally:
            try:
                client.close()
            except OSError:
                pass
            if upstream is not None:
                try:
                    upstream.close()
                except OSError:
                    pass


class _ProgressPusher:
    """Fire-and-forget progress pusher: latest-text slot, throttled, exceptions swallowed."""

    def __init__(self, progress_token):
        self.progress_token = progress_token
        self.latest_text = ""
        self.latest_activities = []
        self.last_push_time = -1.0
        self.thread = None
        self.last_sent_text = ""
        self.last_sent_activities = []

    def push(self, text, activities=None):
        """Update the latest slot and trigger a push if throttle allows."""
        try:
            self.latest_text = text
            self.latest_activities = activities if activities is not None else []
            now = time.monotonic()
            if now - self.last_push_time >= 0.2:
                self.last_push_time = now
                if self.thread is None or not self.thread.is_alive():
                    self.thread = threading.Thread(
                        target=self._do_push,
                        daemon=True,
                    )
                    self.thread.start()
        except Exception:
            # Fire-and-forget: even thread startup failures never fail the turn
            # (ADR 051 decision 6).
            pass

    def _do_push(self):
        """Push in a background thread and drain changes made during the push."""
        try:
            egress_port = int(os.environ.get(EGRESS_PORT_ENV, str(DEFAULT_EGRESS_PORT)))
            proxy_handler = urllib.request.ProxyHandler(
                {"http": "http://%s:%s" % (EGRESS_LOCALHOST, egress_port)}
            )
            opener = urllib.request.build_opener(proxy_handler)
            url = "http://monolith.monolith.svc.cluster.local:8091/ingest/progress"
            sent_text = self.latest_text
            sent_activities = list(self.latest_activities)
            payload = {
                "partial_text": sent_text[-65536:]
                if len(sent_text) > 65536
                else sent_text,
                "activities": sent_activities,
            }
            data = json.dumps(payload).encode("utf-8")
            while len(data) >= 262144 and (sent_activities or payload["partial_text"]):
                if sent_activities:
                    sent_activities.pop(0)
                else:
                    payload["partial_text"] = payload["partial_text"][1000:]
                payload["activities"] = sent_activities
                data = json.dumps(payload).encode("utf-8")
            request = urllib.request.Request(
                url,
                data=data,
                headers={
                    "Authorization": "Bearer %s" % self.progress_token,
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            response = opener.open(request, timeout=2)
            response.close()
            self.last_sent_text = sent_text
            self.last_sent_activities = sent_activities
            if (
                self.latest_text != self.last_sent_text
                or self.latest_activities != self.last_sent_activities
            ):
                self.last_push_time = time.monotonic()
                remaining = 0.2 - (time.monotonic() - self.last_push_time)
                if remaining > 0:
                    time.sleep(remaining)
                self.thread = threading.Thread(
                    target=self._do_push,
                    daemon=True,
                )
                self.thread.start()
        except Exception:
            # Fire-and-forget: all exceptions swallowed (decision 6, ADR 051).
            pass

    def stop(self):
        """Drain and stop. Waits up to 3 seconds for in-flight push to complete."""
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3.0)


class ClaudeProcess:
    """Own the CLI and serialize turns sent through its JSONL stream."""

    def __init__(self, workspace=None, executable="claude"):
        self.workspace = workspace or os.environ.get(
            "EMBER_CLAUDE_WORKSPACE", DEFAULT_WORKSPACE
        )
        self.executable = executable
        _probe_cli_startup(executable)
        self.process = None
        self.init_event = None
        self.fatal_error = None
        self.session_id = None
        self.model = None
        self.system_prompt = None
        self._process_workspace = None
        self._process_uses_legacy_cwd = False
        self._process_workspace_identity = _workspace_identity(self.workspace)
        self._manager = None
        self.turn_lock = threading.Lock()
        self.process_lock = threading.Lock()
        self.current_result = None
        self._stdout_queue = None
        self.unparseable_lines = collections.deque(maxlen=5)
        self.stderr_lines = collections.deque(maxlen=5)
        self.parsed_events = collections.deque(maxlen=5)

    def ready(self):
        with self.process_lock:
            # The manager readiness probe does not wait for this lazily spawned
            # CLI when prewarming is unset. Configured prewarming is checked by
            # the manager, while workspace and fatal failures remain unhealthy.
            return os.path.isdir(self.workspace) and self.fatal_error is None

    def _configure_git(self):
        name = os.environ.get("EMBER_GIT_USER_NAME")
        email = os.environ.get("EMBER_GIT_USER_EMAIL")
        if not name or not email:
            raise StartupError(
                "EMBER_GIT_USER_NAME and EMBER_GIT_USER_EMAIL are required"
            )
        for key, value in (("user.name", name), ("user.email", email)):
            completed = subprocess.run(
                ["git", "config", "--global", key, value],
                cwd=self.workspace,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=False,
            )
            if completed.returncode != 0:
                detail = completed.stderr.decode("utf-8", "replace").strip()
                raise StartupError("git config failed for %s: %s" % (key, detail))

    def _spawn(
        self,
        session_id=None,
        first_message=None,
        model=None,
        init_timeout=None,
        system_prompt=None,
    ):
        if self.fatal_error is not None:
            raise StartupError(self.fatal_error)
        if not os.path.isdir(self.workspace):
            raise StartupError("workspace does not exist: %s" % self.workspace)
        self._configure_git()
        spawn_workspace = self.workspace
        uses_legacy_cwd = False
        if session_id and not _transcript_exists(self.workspace, session_id):
            legacy_workspace = os.path.dirname(self.workspace)
            # Fallback for pre-2026-08-05 sessions, safe to delete once no
            # legacy lineages remain.
            if _transcript_exists(legacy_workspace, session_id):
                spawn_workspace = legacy_workspace
                uses_legacy_cwd = True
        # The microVM is the security boundary. The shim may start as root inside the guest
        # (apko's run-as: 65532 is ignored on raw Firecracker boot, per review), so drop the
        # CLI to the runtime uid/gid only when the shim is root. In-guest
        # permission prompts add no containment that the VM boundary does not already provide.
        # There is no human on the other end of a prompt, by construction. So permission_mode
        # is bypassPermissions; future callers can override via EMBER_PERMISSION_MODE to tighten it.
        permission_mode = os.environ.get(PERMISSION_MODE_ENV, DEFAULT_PERMISSION_MODE)
        command = [
            self.executable,
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            permission_mode,
        ]
        command.extend(["--include-partial-messages"])
        if model is not None:
            command.extend(["--model", CLAUDE_MODELS.get(model, model)])
        if session_id:
            command.extend(["--resume", session_id])
        command.extend(["--append-system-prompt", compose_system_prompt(system_prompt)])
        egress_port = os.environ.get(EGRESS_PORT_ENV, str(DEFAULT_EGRESS_PORT))
        proxy_url = "http://%s:%s" % (EGRESS_LOCALHOST, egress_port)
        child_env = os.environ.copy()
        child_env.update(
            {
                "HTTPS_PROXY": proxy_url,
                "HTTP_PROXY": proxy_url,
                "NO_PROXY": "127.0.0.1,localhost",
            }
        )
        process = subprocess.Popen(
            command,
            cwd=spawn_workspace,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=child_env,
            **_cli_privilege_kwargs(),
        )
        with self.process_lock:
            self.process = process
            self._process_workspace = spawn_workspace
            self._process_uses_legacy_cwd = uses_legacy_cwd
            # Capture workspace identity to detect tmpfs-to-volume takeover.
            self._process_workspace_identity = _workspace_identity(spawn_workspace)
            self.init_event = None
            self._stdout_queue = queue.Queue()
            # REPLACE the rings rather than clearing them: a stale pump thread from
            # a previous process holds the old deque and would keep appending to a
            # shared one, reporting a dead process's output as this one's last words.
            # stderr gets its OWN ring because SIGINT on the timeout path always
            # emits a multi-line traceback, which in a shared maxlen deque evicts the
            # stdout dying words exactly when they matter most.
            self.unparseable_lines = collections.deque(maxlen=5)
            self.stderr_lines = collections.deque(maxlen=5)
            self.parsed_events = collections.deque(maxlen=5)
            _managed_child_pids.add(process.pid)
        threading.Thread(
            target=self._pump_stdout,
            args=(process, self._stdout_queue),
            daemon=True,
        ).start()
        threading.Thread(
            target=self._pump_stderr,
            args=(process, self.stderr_lines),
            daemon=True,
        ).start()
        if first_message is not None:
            self._turn_timing_model_start = _turn_timing_now()
            process.stdin.write(_user_message_line(first_message))
            process.stdin.flush()
        # Note: unparseable-line stderr writes are synchronous on the read thread
        # and race the init timeout. This is fine for tens-of-lines Bun panics
        # but could turn thousands-of-lines dumps into a generic init timeout.
        # Resolve the environment at spawn time so callers and tests that set
        # EMBER_INIT_READ_TIMEOUT after module import still affect lazy starts.
        init_read_timeout = (
            _read_init_timeout() if init_timeout is None else init_timeout
        )
        while True:
            try:
                raw = self._read_output(process, init_read_timeout)
            except TimeoutError:
                self._timeout_interrupt(process, init_read_timeout, "initialization")
                raise StartupError(
                    self._assemble_error_with_rings(
                        "timed out waiting for Claude initialization after %s seconds"
                        % init_read_timeout
                    )
                )
            if raw is None:
                break
            event = self._parse_line(raw)
            if event is None:
                continue
            if event.get("type") == "system" and event.get("subtype") == "init":
                self.init_event = event
                actual_session_id = event.get("session_id")
                if isinstance(actual_session_id, str) and actual_session_id:
                    self.session_id = actual_session_id
                elif session_id:
                    self.session_id = session_id
                self.model = model
                self.system_prompt = system_prompt
                if event.get("apiKeySource") != "none":
                    message = "apiKeySource must be none, got %r" % event.get(
                        "apiKeySource"
                    )
                    self.fatal_error = message
                    self._close_process(kill=True)
                    raise StartupError(message)
                manager = getattr(self, "_manager", None)
                if manager is not None:
                    manager.fatal_error = None
                return
            else:
                try:
                    raw_str = raw.decode("utf-8", errors="replace").rstrip("\n")
                except Exception:
                    raw_str = repr(raw[:200])
                if len(raw_str) > 200:
                    raw_str = raw_str[:200]
                self.parsed_events.append("event: " + raw_str)
        code = process.poll()
        self._close_process(kill=False)
        error_msg = "claude exited before init, exit code %s" % code
        raise RuntimeError(self._assemble_error_with_rings(error_msg))

    @staticmethod
    def _pump_stdout(process, output_queue):
        try:
            for raw in process.stdout:
                output_queue.put(raw)
        finally:
            output_queue.put(None)

    def _pump_stderr(self, process, ring):
        """Read stderr lines onto the console and into the given ring.

        The ring is passed in rather than read off self, so a pump left running
        for a previous process cannot append into the current process's ring.
        """
        try:
            # readline, not file iteration: iteration read-ahead buffers a pipe,
            # delaying lines until the buffer fills or EOF; readline yields each
            # line as the CLI writes it.
            for raw in iter(process.stderr.readline, b""):
                try:
                    line_str = raw.decode("utf-8", errors="replace").rstrip("\n")
                except Exception:
                    line_str = repr(raw[:2000])
                if len(line_str) > 2000:
                    line_str = line_str[:2000]
                sys.stderr.write("ember-claude-shim: cli-stderr: %s\n" % line_str)
                sys.stderr.flush()
                ring.append(line_str)
        except Exception:
            pass

    def _read_output(self, process, timeout):
        with self.process_lock:
            output_queue = self._stdout_queue if process is self.process else None
        if output_queue is None:
            return None
        try:
            return output_queue.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError from exc

    def _parse_line(self, raw):
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            try:
                line_str = raw[:2000].decode("utf-8", errors="replace").rstrip("\n")
            except Exception:
                line_str = repr(raw[:2000])
            sys.stderr.write("ember-claude-shim: cli-stdout: %s\n" % line_str)
            sys.stderr.flush()
            self.unparseable_lines.append(line_str)
            return None

    def _assemble_error_with_rings(self, base_msg):
        """Append truncated CLI output, stderr, and parsed events to an error."""
        sections = [
            ("CLI output:", self.unparseable_lines),
            ("CLI stderr:", self.stderr_lines),
            ("Parsed events:", self.parsed_events),
        ]
        error_msg = base_msg
        for label, ring in sections:
            content = _truncate_ring_for_error(ring)
            if content:
                error_msg += "\n%s\n%s" % (label, content)
        return error_msg

    def _close_process(self, kill=False):
        with self.process_lock:
            process = self.process
            self.process = None
            self._stdout_queue = None
        if process is None:
            return
        if kill and process.poll() is None:
            process.kill()
        if process.stdin:
            try:
                process.stdin.close()
            except OSError:
                pass
        # Bounded: most callers close an already-exited process (wait returns
        # instantly), but the model-change respawn closes a LIVE CLI, and an
        # unbounded wait there would hang the turn under turn_lock if the CLI
        # ignores stdin EOF (interrupt() treats the same wait as unsafe).
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        _managed_child_pids.discard(process.pid)
        _reap_orphans()

    def turn(
        self,
        message,
        session_id=None,
        model=None,
        progress_token=None,
        system_prompt=None,
    ):
        with self.turn_lock:
            cli_ready_start = _turn_timing_now()
            with self.process_lock:
                process = self.process
            session_was_bound = bool(self.session_id)
            if self.session_id and session_id and session_id != self.session_id:
                raise SessionConflictError(
                    "session_id %r does not match active session %r"
                    % (session_id, self.session_id)
                )
            model_changed = model is not None and model != self.model
            system_prompt_changed = system_prompt != self.system_prompt
            parked_process = (
                process is not None and process.poll() is None and not self.session_id
            )
            parked_adoption = parked_process and bool(session_id)
            cli_ready_path = None
            workspace_identity = _workspace_identity(self.workspace)
            cwd_changed = parked_process and (
                (
                    self._process_workspace != self.workspace
                    and not getattr(self, "_process_uses_legacy_cwd", False)
                )
                or (
                    self._process_workspace_identity is not None
                    and workspace_identity != self._process_workspace_identity
                )
            )
            if (
                process is not None
                and process.poll() is None
                and (model_changed or cwd_changed or system_prompt_changed)
            ):
                # Prewarm parks a CLI started without a caller prompt. Since
                # append-system-prompt is spawn-time only, adoption must respawn
                # when this turn carries a different prompt.
                self._close_process(kill=False)
                try:
                    self._spawn(
                        self.session_id or session_id,
                        first_message=message,
                        model=model,
                        system_prompt=system_prompt,
                    )
                except Exception:
                    if parked_adoption and not session_was_bound:
                        self.session_id = None
                    raise
                process = self.process
                message_sent = True
                cli_ready_path = "remediation_respawn"
            else:
                message_sent = False
            # After a model-change respawn the first_message is already in
            # flight; falling into this branch would _spawn again and deliver
            # (and bill) the same turn twice if the fresh CLI died between
            # init and this poll.
            if not message_sent and (process is None or process.poll() is not None):
                if process is not None:
                    self._close_process(kill=False)
                # A request without an id resumes the last session after an
                # interrupt or relight instead of silently creating a new one.
                try:
                    self._spawn(
                        session_id or self.session_id,
                        first_message=message,
                        model=model,
                        system_prompt=system_prompt,
                    )
                except Exception:
                    if parked_adoption and not session_was_bound:
                        self.session_id = None
                    raise
                process = self.process
                message_sent = True
                cli_ready_path = "lazy_spawn"
            pusher = None
            try:
                if not self.ready():
                    raise StartupError(self.fatal_error or "shim not ready")
                self.current_result = None
                # Adoption is latched only when the user message is about to be
                # delivered. Every later failure rolls it back below.
                if parked_adoption:
                    # Check if this legacy session must respawn due to workspace change.
                    if not _transcript_exists(self.workspace, session_id):
                        legacy_workspace = os.path.dirname(self.workspace)
                        if _transcript_exists(legacy_workspace, session_id):
                            # Close parked CLI and respawn with legacy cwd to restore state.
                            self._close_process(kill=False)
                            try:
                                self._spawn(
                                    session_id,
                                    first_message=message,
                                    model=model,
                                    system_prompt=system_prompt,
                                )
                            except Exception:
                                if parked_adoption and not session_was_bound:
                                    self.session_id = None
                                raise
                            process = self.process
                            message_sent = True
                            parked_adoption = False
                            cli_ready_path = "remediation_respawn"
                    if parked_adoption:
                        self.session_id = session_id
                        cli_ready_path = "adopt"
                if cli_ready_path is None:
                    cli_ready_path = "reuse"
                _emit_elapsed("cli_ready", cli_ready_start, path=cli_ready_path)
                if not message_sent:
                    self._turn_timing_model_start = _turn_timing_now()
                    message_line = _user_message_line(
                        message,
                        session_id=session_id if parked_adoption else None,
                    )
                    process.stdin.write(message_line)
                    process.stdin.flush()
                events = []
                accumulated_text = ""
                current_message_buffer = ""
                cached_activities = []
                activities_are_stale = True
                pusher = _ProgressPusher(progress_token) if progress_token else None
                while True:
                    try:
                        turn_read_timeout = TURN_READ_TIMEOUT
                        raw = self._read_output(process, turn_read_timeout)
                    except TimeoutError:
                        self._timeout_interrupt(
                            process, turn_read_timeout, "turn output"
                        )
                        raise RuntimeError(
                            "timed out waiting for Claude output after %s seconds"
                            % turn_read_timeout
                        )
                    if raw is None:
                        code = process.poll()
                        self._close_process(kill=False)
                        error_msg = "claude crashed during turn, exit code %s" % code
                        raise RuntimeError(self._assemble_error_with_rings(error_msg))
                    event = self._parse_line(raw)
                    if event is None:
                        continue
                    events.append(event)
                    if event.get("type") == "stream_event":
                        stream_event = event.get("event")
                        delta = (
                            stream_event.get("delta")
                            if isinstance(stream_event, dict)
                            and stream_event.get("type") == "content_block_delta"
                            else None
                        )
                        if (
                            isinstance(delta, dict)
                            and delta.get("type") == "text_delta"
                        ):
                            text = delta.get("text", "")
                            if isinstance(text, str):
                                current_message_buffer += text
                                if pusher:
                                    pusher.push(
                                        accumulated_text + current_message_buffer,
                                        cached_activities,
                                    )
                    elif event.get("type") == "assistant":
                        message_event = event.get("message")
                        if (
                            isinstance(message_event, dict)
                            and message_event.get("role") == "assistant"
                        ):
                            content = message_event.get("content")
                            if isinstance(content, list):
                                for block in content:
                                    if (
                                        isinstance(block, dict)
                                        and block.get("type") == "text"
                                    ):
                                        text = block.get("text")
                                        if isinstance(text, str):
                                            accumulated_text += text
                            current_message_buffer = ""
                            activities_are_stale = True
                    elif event.get("type") == "tool_execution_start":
                        activities_are_stale = True
                    if activities_are_stale:
                        cached_activities = activity_from_events(events)[-300:]
                        activities_are_stale = False
                    if event.get("type") == "assistant" and pusher:
                        pusher.push(
                            accumulated_text + current_message_buffer,
                            cached_activities,
                        )
                    if event.get("type") == "result":
                        if pusher:
                            pusher.push(
                                accumulated_text + current_message_buffer,
                                cached_activities,
                            )
                        self.current_result = event
                        if not self.session_id:
                            actual_id = event.get("session_id")
                            if actual_id:
                                self.session_id = actual_id
                        if event.get(
                            "is_error"
                        ) and "No conversation found with session ID:" in str(
                            event.get("result", "")
                        ):
                            self._close_process(kill=False)
                            raise RuntimeError(str(event.get("result")))
                        record = dict(event)
                        record["voice"] = voice_summary(event.get("result", ""))
                        record["activities"] = activity_from_events(events)
                        _emit_elapsed(
                            "model", getattr(self, "_turn_timing_model_start", None)
                        )
                        return record
            except Exception:
                if parked_adoption and not session_was_bound:
                    self.session_id = None
                raise
            finally:
                if pusher:
                    pusher.stop()

    def _timeout_interrupt(self, process, timeout, phase):
        sys.stderr.write(
            "ember-claude-shim: %s timed out after %s seconds, sending SIGINT\n"
            % (phase, timeout)
        )
        sys.stderr.flush()
        if process.poll() is None:
            self.interrupt(timeout=INTERRUPT_TIMEOUT)

    def interrupt(self, timeout=INTERRUPT_TIMEOUT):
        with self.process_lock:
            process = self.process
        if process is None or process.poll() is not None:
            return {
                "terminal_reason": "user_interrupt",
                "killed": False,
                "timeout": False,
            }
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
            sys.stderr.write(
                "ember-claude-shim: forcefully killing CLI after SIGINT+timeout "
                "(%s seconds exceeded)\n" % timeout
            )
            sys.stderr.flush()
            process.kill()
            process.wait()
        result = self.current_result or {}
        reason = result.get("terminal_reason", "user_interrupt")
        self._close_process(kill=False)
        return {"terminal_reason": reason, "killed": timed_out, "timeout": timed_out}


class CodexProcess:
    """Own one long-lived Codex app-server process and bind threads lazily."""

    def __init__(self, workspace=None, executable="codex"):
        self.workspace = workspace or os.environ.get(
            "EMBER_CLAUDE_WORKSPACE", DEFAULT_WORKSPACE
        )
        self.executable = executable
        self.process = None
        self.session_id = None
        self.turn_lock = threading.Lock()
        self.process_lock = threading.Lock()
        self.stderr_lines = collections.deque(maxlen=5)
        self._stderr_thread = None
        self._stdout_queue = None
        self._rpc_id = 0
        self._server_threads = set()
        self._turn_id = None
        self._turn_done = threading.Event()
        self._turn_done.set()
        self._write_lock = threading.Lock()

    def ready(self):
        with self.process_lock:
            return os.path.isdir(self.workspace)

    def _child_env(self):
        egress_port = os.environ.get(EGRESS_PORT_ENV, str(DEFAULT_EGRESS_PORT))
        proxy_url = "http://%s:%s" % (EGRESS_LOCALHOST, egress_port)
        # The CLI state dir must live under the WORKSPACE, not $HOME: the guest's
        # $HOME is on the read-only rootfs and the codex CLI refuses to start when
        # CODEX_HOME does not exist (observed live as a 422 on every codex turn).
        # The config.toml is regenerated per spawn; a stale workspace cannot pin
        # an old base URL.
        # The workspace is also where session files must sit for thread resume to
        # survive bank/relight once workspaces ride the durable volume.
        codex_home = os.path.join(self.workspace, ".codex")
        _ensure_cli_dir(codex_home)
        child_env = os.environ.copy()
        # Subscription auth is carried by the inert auth.json below. An API
        # key is not part of ChatGPT subscription mode and must not confuse
        # the CLI or the egress sidecar.
        child_env.pop("OPENAI_API_KEY", None)
        child_env.update(
            {
                "CODEX_HOME": codex_home,
                "HTTPS_PROXY": proxy_url,
                "HTTP_PROXY": proxy_url,
                "NO_PROXY": "127.0.0.1,localhost",
            }
        )
        return child_env

    @staticmethod
    def _dummy_jwt():
        """A syntactically valid, far-future JWT carrying no real credential.

        The CLI decodes these tokens locally (expiry, account id), so a bare
        placeholder string fails to parse and the CLI decides it is logged
        out. The signature is nonsense on purpose: nothing verifies it here,
        and the sidecar replaces the header with the broker's real token
        before the request leaves the guest.
        """
        header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=")
        payload = base64.urlsafe_b64encode(
            json.dumps(
                {
                    "exp": 4102444800,  # 2100-01-01, so the CLI never self-refreshes
                    "https://api.openai.com/auth": {
                        "chatgpt_account_id": CODEX_DUMMY_ACCOUNT_ID
                    },
                },
                separators=(",", ":"),
            ).encode("utf-8")
        ).rstrip(b"=")
        return b".".join([header, payload, b"ember"]).decode("ascii")

    @staticmethod
    def _write_auth_json(codex_home):
        # Shape mirrors a real subscription auth.json (auth_mode, a null API key,
        # JWT-shaped tokens): the CLI parses these, so a placeholder string reads
        # as logged out. The broker owns refresh; a guest refresh would consume
        # the rotating token and lock out the fleet (ADR 048 invariant), which is
        # why last_refresh sits in the future and the tokens never expire.
        token = CodexProcess._dummy_jwt()
        auth = {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {
                "id_token": token,
                "access_token": token,
                "refresh_token": token,
                "account_id": CODEX_DUMMY_ACCOUNT_ID,
            },
            "last_refresh": "2099-12-31T23:59:59Z",
        }
        with open(os.path.join(codex_home, "auth.json"), "w") as stream:
            json.dump(auth, stream)

    @staticmethod
    def _write_model_config(codex_home):
        base_url = os.environ.get(
            CODEX_SUBSCRIPTION_BASE_URL_ENV, DEFAULT_CODEX_SUBSCRIPTION_BASE_URL
        )
        provider_base_url = base_url.rstrip("/") + "/codex/"
        # Subscription backend endpoint over cleartext injection lane (token injection happens at sidecar).
        # chatgpt_base_url must be set too: the CLI's connector client (rmcp
        # transport) builds its URL from chatgpt_base_url, not from the model
        # provider base_url. Left at the https default it bypasses the sidecar's
        # injection lane, presents the guest's placeholder JWT, gets a 401, and
        # the CLI's resulting token refresh attempt aborts the turn (issue #4298).
        #
        # The model provider base_url is a different value on purpose. Codex
        # turn requests get posted to {provider base_url}/responses, and the
        # subscription backend only serves turns at /backend-api/codex/responses,
        # not at /backend-api/responses. The rmcp connector client above builds
        # from chatgpt_base_url at the backend-api root, so the two values must
        # diverge: chatgpt_base_url stays at the root, provider_base_url gets a
        # /codex/ suffix. The trailing slash on provider_base_url is load
        # bearing: it makes both plausible client join behaviors, trimming the
        # trailing slash and concatenating, or an RFC 3986 Url::join with a
        # relative segment, land on the same /codex/responses path.
        config = """model_provider = "ember-openai"
enable_codex_api_key_env = false
chatgpt_base_url = %s
sandbox_mode = "danger-full-access"
approval_policy = "never"

# Codex 0.146.0 binary inspection exposes [tools].web_search, while
# web_search_request is deprecated because web search is enabled by default.
[tools]
web_search = true

[model_providers.ember-openai]
name = "ember-openai"
base_url = %s
wire_api = "responses"
""" % (json.dumps(base_url), json.dumps(provider_base_url))
        with open(os.path.join(codex_home, "config.toml"), "w") as stream:
            stream.write(config)

    def _spawn(self):
        if not os.path.isdir(self.workspace):
            raise StartupError("workspace does not exist: %s" % self.workspace)
        child_env = self._child_env()
        self._write_auth_json(child_env["CODEX_HOME"])
        self._write_model_config(child_env["CODEX_HOME"])
        process = subprocess.Popen(
            [self.executable, "app-server"],
            cwd=self.workspace,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=child_env,
            **_cli_privilege_kwargs(),
        )
        output_queue = queue.Queue()
        with self.process_lock:
            self.process = process
            self._stdout_queue = output_queue
            _managed_child_pids.add(process.pid)
        threading.Thread(
            target=self._pump_codex_stdout, args=(process, output_queue), daemon=True
        ).start()
        self.stderr_lines = collections.deque(maxlen=5)
        self._stderr_thread = threading.Thread(
            target=ClaudeProcess._pump_stderr,
            args=(self, process, self.stderr_lines),
            daemon=True,
        )
        self._stderr_thread.start()
        self._server_threads = set()
        self._turn_id = None
        try:
            self._request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "homelab-shim",
                        "title": "Homelab Shim",
                        "version": "1.0",
                    }
                },
                timeout=INIT_READ_TIMEOUT,
            )
            self._send({"jsonrpc": "2.0", "method": "initialized"})
        except Exception:
            self._close_process(kill=True)
            raise
        return process

    def _empty_stream_error(self, process):
        if process is None:
            code = None
        else:
            code = process.poll()
            if code is None:
                code = process.wait()
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=1)
        error_msg = "codex exited before turn.completed, exit code %s" % code
        stderr = _truncate_ring_for_error(self.stderr_lines)
        if stderr:
            error_msg += "\nCLI stderr:\n%s" % stderr
        return RuntimeError(error_msg)

    def _send(self, value):
        with self._write_lock:
            process = self.process
            if process is None or process.poll() is not None:
                raise self._empty_stream_error(process)
            process.stdin.write(_json_line(value))
            process.stdin.flush()

    def _request(self, method, params, timeout=TURN_READ_TIMEOUT):
        with self._write_lock:
            self._rpc_id += 1
            request_id = self._rpc_id
        self._send(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        while True:
            event = self._read_event(timeout)
            if event is None:
                raise self._empty_stream_error(self.process)
            if event.get("id") == request_id:
                if "error" in event:
                    raise RuntimeError(self._rpc_error(event))
                return event.get("result", {})
            self._handle_server_request(event)

    @staticmethod
    def _rpc_error(response):
        error = response.get("error")
        if isinstance(error, dict):
            return "%s: %s" % (error.get("code", "error"), error.get("message", error))
        return str(error)

    def _handle_server_request(self, event):
        if event.get("method") is None or event.get("id") is None:
            return
        try:
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": event["id"],
                    "error": {
                        "code": -32000,
                        "message": "server request denied by homelab shim",
                    },
                }
            )
        except Exception:
            pass
        sys.stderr.write("ember-claude-shim: denied Codex server request %r\n" % event)
        sys.stderr.flush()

    def _read_event(self, timeout):
        with self.process_lock:
            output_queue = self._stdout_queue
        if output_queue is None:
            return None
        try:
            raw = output_queue.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError from exc
        if raw is None:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("codex emitted invalid JSON: %s" % exc) from exc

    def _resume(self, session_id, system_prompt=None):
        params = {
            "threadId": session_id,
            "cwd": self.workspace,
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
            "developerInstructions": compose_system_prompt(system_prompt),
        }
        try:
            result = self._request("thread/resume", params, timeout=INIT_READ_TIMEOUT)
        except RuntimeError as error:
            raise RuntimeError(
                "unable to resume session %s: %s\n%s"
                % (session_id, error, _truncate_ring_for_error(self.stderr_lines))
            ) from error
        thread = result.get("thread", {}) if isinstance(result, dict) else {}
        thread_id = (
            thread.get("id", session_id) if isinstance(thread, dict) else session_id
        )
        self.session_id = thread_id
        self._server_threads.add(thread_id)

    def _translate_activity_event(self, event):
        if event.get("method") == "item/started":
            item = event.get("params", {}).get("item", {})
            if isinstance(item, dict) and item.get("type") in (
                "commandExecution",
                "command_execution",
            ):
                return {
                    "type": "tool_execution_start",
                    "toolName": "bash",
                    "args": {"command": item.get("command", "")},
                }
        return event

    @staticmethod
    def _pump_codex_stdout(process, output_queue):
        try:
            for raw in process.stdout:
                output_queue.put(raw)
        finally:
            output_queue.put(None)

    def turn(
        self,
        message,
        session_id=None,
        model=DEFAULT_CODEX_MODEL,
        progress_token=None,
        system_prompt=None,
    ):
        with self.turn_lock:
            cli_ready_start = _turn_timing_now()
            if self.session_id and session_id and session_id != self.session_id:
                raise SessionConflictError(
                    "session_id %r does not match active session %r"
                    % (session_id, self.session_id)
                )
            requested_session = session_id or self.session_id
            with self.process_lock:
                process = self.process
            cli_ready_path = None
            process_was_unbound = not self.session_id
            if process is None or process.poll() is not None:
                self._close_process(kill=False)
                process = self._spawn()
                requested_session = session_id or self.session_id
                cli_ready_path = "lazy_spawn"
            if requested_session and (
                requested_session not in self._server_threads
                or requested_session != self.session_id
            ):
                # Resume repeats the developer instructions so a restarted
                # app-server thread receives the same prompt without duplication.
                self._resume(requested_session, system_prompt=system_prompt)
                if cli_ready_path is None and process_was_unbound:
                    cli_ready_path = "adopt"
            elif not requested_session:
                result = self._request(
                    "thread/start",
                    {
                        "cwd": self.workspace,
                        "approvalPolicy": "never",
                        "sandbox": "danger-full-access",
                        "developerInstructions": compose_system_prompt(system_prompt),
                    },
                )
                thread = result.get("thread", {}) if isinstance(result, dict) else {}
                self.session_id = thread.get("id") if isinstance(thread, dict) else None
                self._server_threads.add(self.session_id)
                if cli_ready_path is None and process_was_unbound:
                    cli_ready_path = "adopt"
            if cli_ready_path is None:
                cli_ready_path = "reuse"
            with self._write_lock:
                self._turn_done.clear()
                self._turn_id = None
                self._rpc_id += 1
                request_id = self._rpc_id
            model_name, effort = CODEX_MODELS.get(
                model, CODEX_MODELS[DEFAULT_CODEX_MODEL]
            )
            _emit_elapsed("cli_ready", cli_ready_start, path=cli_ready_path)
            self._turn_timing_model_start = _turn_timing_now()
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "turn/start",
                    "params": {
                        "threadId": self.session_id,
                        "input": [{"type": "text", "text": message}],
                        "model": model_name,
                        "effort": effort,
                        "sandboxPolicy": {"type": "dangerFullAccess"},
                        "approvalPolicy": "never",
                        "cwd": self.workspace,
                    },
                }
            )
            result_text = ""
            usage = {}
            events = []
            accumulated_text = ""
            cached_activities = []
            activities_are_stale = True
            try:
                pusher = _ProgressPusher(progress_token) if progress_token else None
            except Exception:
                pusher = None
            try:
                while True:
                    try:
                        event = self._read_event(TURN_READ_TIMEOUT)
                    except TimeoutError as exc:
                        self._close_process(kill=True)
                        raise RuntimeError(
                            "timed out waiting for Codex output after %s seconds"
                            % TURN_READ_TIMEOUT
                        ) from exc
                    if event is None:
                        raise self._empty_stream_error(process)
                    if event.get("id") == request_id:
                        if "error" in event:
                            raise RuntimeError(self._rpc_error(event))
                        continue
                    self._handle_server_request(event)
                    event_type = event.get("method")
                    params = event.get("params", {})
                    legacy_event = self._translate_activity_event(event)
                    events.append(legacy_event)
                    if event_type == "turn/started":
                        self._turn_id = params.get("turn", {}).get("id")
                    elif event_type == "item/agentMessage/delta":
                        delta = params.get("delta", {})
                        text = delta.get("text") if isinstance(delta, dict) else None
                        if isinstance(text, str):
                            accumulated_text += text
                            if pusher:
                                try:
                                    pusher.push(accumulated_text, cached_activities)
                                except Exception:
                                    pass
                    elif event_type in ("item/started", "item/completed"):
                        item = params.get("item", {})
                        if isinstance(item, dict) and item.get("type") in (
                            "commandExecution",
                            "command_execution",
                        ):
                            activities_are_stale = True
                        if event_type == "item/completed" and isinstance(item, dict):
                            if item.get("type") in ("agentMessage", "agent_message"):
                                result_text = item.get("text", "")
                    elif event_type == "thread/tokenUsage/updated":
                        last = params.get("tokenUsage", {}).get("last", {})
                        usage = {
                            "input_tokens": last.get("inputTokens", 0),
                            "output_tokens": last.get("outputTokens", 0),
                            "cache_read_tokens": last.get("cachedInputTokens", 0),
                            "cache_write_tokens": last.get("cacheWriteInputTokens", 0),
                        }
                    if activities_are_stale:
                        cached_activities = activity_from_events(events)[-300:]
                        activities_are_stale = False
                        if pusher and event_type in ("item/started", "item/completed"):
                            try:
                                pusher.push(
                                    accumulated_text or result_text,
                                    cached_activities,
                                )
                            except Exception:
                                pass
                    if event_type == "turn/completed":
                        if pusher:
                            try:
                                pusher.push(
                                    accumulated_text or result_text,
                                    cached_activities,
                                )
                            except Exception:
                                pass
                        turn = params.get("turn", {})
                        status = turn.get("status", "completed")
                        if status == "failed":
                            error = turn.get("error", "Codex turn failed")
                            if isinstance(error, dict):
                                error = error.get("message", error)
                            raise RuntimeError(
                                "Codex turn failed: %s\n%s"
                                % (error, _truncate_ring_for_error(self.stderr_lines))
                            )
                        terminal_reason = (
                            "user_interrupt" if status == "interrupted" else "completed"
                        )
                        _emit_elapsed(
                            "model", getattr(self, "_turn_timing_model_start", None)
                        )
                        return {
                            "result": result_text,
                            "terminal_reason": terminal_reason,
                            "session_id": self.session_id,
                            "usage": usage,
                            "voice": voice_summary(result_text),
                            "activities": activity_from_events(events),
                        }
            finally:
                if pusher:
                    try:
                        pusher.stop()
                    except Exception:
                        pass
                self._turn_done.set()

    @staticmethod
    def _reap_process(process):
        try:
            process.wait()
        finally:
            _managed_child_pids.discard(process.pid)

    def interrupt(self, timeout=INTERRUPT_TIMEOUT):
        with self.process_lock:
            process = self.process
            turn_id = self._turn_id
            thread_id = self.session_id
        if (
            process is None
            or process.poll() is not None
            or self._turn_done.is_set()
            or not turn_id
        ):
            return {
                "terminal_reason": "user_interrupt",
                "killed": False,
                "timeout": False,
            }
        try:
            with self._write_lock:
                self._rpc_id += 1
                request_id = self._rpc_id
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "turn/interrupt",
                    "params": {"threadId": thread_id, "turnId": turn_id},
                }
            )
        except Exception:
            self._close_process(kill=True)
            return {
                "terminal_reason": "user_interrupt",
                "killed": False,
                "timeout": False,
            }
        timed_out = not self._turn_done.wait(timeout=timeout)
        if timed_out:
            self._close_process(kill=True)
        return {
            "terminal_reason": "user_interrupt",
            "killed": timed_out,
            "timeout": timed_out,
        }

    def _close_process(self, kill=False):
        with self.process_lock:
            process = self.process
            self.process = None
            self._stdout_queue = None
            self._server_threads = set()
            self._turn_id = None
        if process is None:
            return
        if kill and process.poll() is None:
            process.kill()
        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        _managed_child_pids.discard(process.pid)
        _reap_orphans()


class PiProcess:
    """Own one long-lived Pi RPC process and bind sessions lazily."""

    INFERENCE_BASE_URL = "http://inference.inference.svc.cluster.local:8080/v1"

    def __init__(self, workspace=None, executable="pi"):
        self.workspace = workspace or os.environ.get(
            "EMBER_CLAUDE_WORKSPACE", DEFAULT_WORKSPACE
        )
        self.executable = executable
        self.process = None
        self.session_id = None
        self.turn_lock = threading.Lock()
        self.process_lock = threading.Lock()
        self.stderr_lines = collections.deque(maxlen=5)
        self._stderr_thread = None
        self._stdout_queue = None
        self._write_lock = threading.Lock()
        self._turn_done = threading.Event()
        self._turn_done.set()
        self._in_flight = False
        self._model = None
        self._system_prompt = None
        self._session_file = None

    def ready(self):
        with self.process_lock:
            return os.path.isdir(self.workspace)

    def _child_env(self):
        egress_port = os.environ.get(EGRESS_PORT_ENV, str(DEFAULT_EGRESS_PORT))
        proxy_url = "http://%s:%s" % (EGRESS_LOCALHOST, egress_port)
        # Same constraint as the codex adapter: $HOME is read-only rootfs in the
        # guest, so pi's state dir lives under the writable workspace, which is
        # also where session files must sit to survive bank/relight.
        pi_home = os.path.join(self.workspace, ".pi")
        _ensure_cli_dir(pi_home)
        _ensure_cli_dir(os.path.join(pi_home, "agent"))
        child_env = os.environ.copy()
        child_env.update(
            {
                "PI_HOME": pi_home,
                "PI_CODING_AGENT_DIR": os.path.join(pi_home, "agent"),
                "HTTPS_PROXY": proxy_url,
                "HTTP_PROXY": proxy_url,
                "NO_PROXY": "127.0.0.1,localhost",
            }
        )
        return child_env

    def _write_model_config(self, pi_home):
        agent_dir = os.path.join(pi_home, "agent")
        _ensure_cli_dir(agent_dir)
        config = {
            "providers": {
                "openai-completions": {
                    "baseUrl": self.INFERENCE_BASE_URL,
                    "api": "openai-completions",
                    "apiKey": "sk-noauth",
                    "compat": {
                        "supportsDeveloperRole": False,
                        "supportsReasoningEffort": False,
                        "thinkingFormat": "qwen-chat-template",
                    },
                    "models": [
                        {
                            "id": PI_MODELS[DEFAULT_PI_MODEL],
                            "contextWindow": PI_CONTEXT_WINDOW,
                            "maxTokens": PI_MAX_OUTPUT_TOKENS,
                            "reasoning": True,
                        }
                    ],
                }
            }
        }
        with open(os.path.join(agent_dir, "models.json"), "w") as stream:
            json.dump(config, stream)

    def _write_settings_json(self, pi_home):
        """Write pi's settings.json with managed lane configuration.

        pi may persist unrelated keys in settings.json, so this method reads
        any existing file, merges the compaction and thinking defaults, and
        writes back. If the file is missing, unreadable, or contains invalid
        JSON, fall back to writing just those defaults without crashing the
        spawn.
        """
        agent_dir = os.path.join(pi_home, "agent")
        _ensure_cli_dir(agent_dir)
        settings_path = os.path.join(agent_dir, "settings.json")

        existing_settings = {}
        try:
            with open(settings_path, "r") as stream:
                existing_settings = json.load(stream)
        except (OSError, ValueError):
            # ValueError covers both json.JSONDecodeError and UnicodeDecodeError.
            # A torn write on the durable workspace volume can produce invalid UTF-8.
            pass

        if not isinstance(existing_settings, dict):
            existing_settings = {}

        existing_settings["compaction"] = {
            "enabled": True,
            "reserveTokens": PI_COMPACTION_RESERVE_TOKENS,
            "keepRecentTokens": PI_COMPACTION_KEEP_RECENT_TOKENS,
        }
        existing_settings["defaultThinkingLevel"] = PI_DEFAULT_THINKING_LEVEL

        try:
            with open(settings_path, "w") as stream:
                json.dump(existing_settings, stream)
        except OSError as exc:
            sys.stderr.write(
                "ember-claude-shim: warning: failed to write %s: %s\n"
                % (settings_path, exc)
            )
            sys.stderr.flush()

    def _spawn(self, model, system_prompt=None):
        if not os.path.isdir(self.workspace):
            raise StartupError("workspace does not exist: %s" % self.workspace)
        model_name = PI_MODELS.get(model, PI_MODELS[DEFAULT_PI_MODEL])
        child_env = self._child_env()
        pi_home = child_env["PI_HOME"]
        self._write_model_config(pi_home)
        self._write_settings_json(pi_home)
        command = [
            self.executable,
            "--mode",
            "rpc",
            "--provider",
            "openai-completions",
            "--model",
            model_name,
            "--system-prompt",
            "You are a focused coding agent. " + compose_system_prompt(system_prompt),
            "--no-context-files",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            # Discovery remains disabled, while this image-owned extension is
            # explicitly trusted and loaded on every Pi spawn.
            "--extension",
            PI_WEB_RESEARCH_EXTENSION,
            "--tools",
            # Keep CLI validation limited to Pi's built-ins. The trusted
            # extension activates its registered tools at session_start.
            "read,bash,edit,write",
            "--session-dir",
            os.path.join(pi_home, "sessions"),
        ]
        process = subprocess.Popen(
            command,
            cwd=self.workspace,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=child_env,
            **_cli_privilege_kwargs(),
        )
        output_queue = queue.Queue()
        with self.process_lock:
            self.process = process
            self._stdout_queue = output_queue
            self._model = model
            self._system_prompt = system_prompt
            _managed_child_pids.add(process.pid)
        threading.Thread(
            target=self._pump_pi_stdout,
            args=(process, output_queue),
            daemon=True,
        ).start()
        self.stderr_lines = collections.deque(maxlen=5)
        self._stderr_thread = threading.Thread(
            target=ClaudeProcess._pump_stderr,
            args=(self, process, self.stderr_lines),
            daemon=True,
        )
        self._stderr_thread.start()
        try:
            self._state()
        except Exception:
            self._close_process(kill=True)
            raise
        return process

    @staticmethod
    def _pump_pi_stdout(process, output_queue):
        try:
            for raw in process.stdout:
                output_queue.put(raw)
        finally:
            output_queue.put(None)

    def _read_event(self, timeout):
        with self.process_lock:
            output_queue = self._stdout_queue
        if output_queue is None:
            return None
        try:
            raw = output_queue.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError from exc
        if raw is None:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("pi emitted invalid JSON: %s" % exc) from exc

    def _send(self, command):
        with self._write_lock:
            with self.process_lock:
                process = self.process
            if process is None or process.poll() is not None:
                raise self._empty_stream_error(process)
            process.stdin.write(_json_line(command))
            process.stdin.flush()

    def _command(self, command, timeout=INIT_READ_TIMEOUT):
        self._send(command)
        while True:
            event = self._read_event(timeout)
            if event is None:
                raise self._empty_stream_error(self.process)
            if event.get("type") == "response" and event.get("command") == command.get(
                "type"
            ):
                if not event.get("success", False):
                    raise RuntimeError(
                        "%s failed: %s" % (command["type"], json.dumps(event)[:1500])
                    )
                return event.get("data") or {}

    def _session_path(self, session_id):
        sessions_dir = os.path.join(self._child_env()["PI_HOME"], "sessions")
        return os.path.join(sessions_dir, "%s.jsonl" % session_id)

    def _state(self):
        data = self._command({"type": "get_state"})
        session_file = data.get("sessionFile")
        session_id = data.get("sessionId")
        if isinstance(session_id, str) and session_id:
            self.session_id = session_id
        if isinstance(session_file, str) and session_file:
            self._session_file = session_file
        model = data.get("model")
        if isinstance(model, dict):
            model_id = model.get("id")
            if isinstance(model_id, str):
                self._model = model_id
        return data

    def _empty_stream_error(self, process):
        code = None if process is None else process.poll()
        if code is None:
            if process is not None:
                code = process.wait()
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=1)
        error_msg = "pi exited before agent_end, exit code %s" % code
        stderr = _truncate_ring_for_error(self.stderr_lines)
        if stderr:
            error_msg += "\nCLI stderr:\n%s" % stderr
        return RuntimeError(error_msg)

    def _translate_activity_event(self, event):
        event_type = event.get("type")
        if event_type in ("tool_start", "tool_execution_start"):
            tool_name = event.get("toolName") or event.get("tool_name")
            args = event.get("args", event.get("input", {}))
            if tool_name:
                return {
                    "type": "tool_execution_start",
                    "toolName": str(tool_name).lower(),
                    "args": args,
                }
        return event

    def turn(
        self,
        message,
        session_id=None,
        model=DEFAULT_PI_MODEL,
        progress_token=None,
        system_prompt=None,
        thinking=None,
    ):
        with self.turn_lock:
            cli_ready_start = _turn_timing_now()
            if self.session_id and session_id and session_id != self.session_id:
                raise SessionConflictError(
                    "session_id %r does not match active session %r"
                    % (session_id, self.session_id)
                )
            with self.process_lock:
                process = self.process
            cli_ready_path = None
            process_was_unbound = not self.session_id
            model_name = PI_MODELS.get(model, PI_MODELS[DEFAULT_PI_MODEL])
            if process is None or process.poll() is not None:
                self._close_process(kill=False)
                process = self._spawn(model, system_prompt=system_prompt)
                cli_ready_path = "lazy_spawn"
            elif self._system_prompt != system_prompt:
                self._close_process(kill=False)
                process = self._spawn(model, system_prompt=system_prompt)
                cli_ready_path = "remediation_respawn"
            elif self._model != model_name:
                self._command(
                    {
                        "type": "set_model",
                        "provider": "openai-completions",
                        "modelId": model_name,
                    }
                )
                self._model = model_name
            requested_session = session_id or self.session_id
            if requested_session and requested_session != self.session_id:
                try:
                    data = self._command(
                        {
                            "type": "switch_session",
                            "sessionPath": self._session_path(requested_session),
                        }
                    )
                except RuntimeError as exc:
                    raise SessionConflictError(
                        "switch_session failed for session %s: %s"
                        % (requested_session, exc)
                    ) from exc
                if data.get("cancelled"):
                    raise SessionConflictError(
                        "switch_session cancelled for session %s" % requested_session
                    )
                self._state()
                if cli_ready_path is None and process_was_unbound:
                    cli_ready_path = "adopt"
            if cli_ready_path is None:
                cli_ready_path = "reuse"
            _emit_elapsed("cli_ready", cli_ready_start, path=cli_ready_path)
            result_text = ""
            usage = {}
            events = []
            accumulated_text = ""
            cached_activities = []
            activities_are_stale = True
            terminal_reason = "completed"
            num_turns = 0
            model_ms = 0
            tool_ms = 0
            model_calls = 0
            tool_calls = 0
            tools_by_name = {}
            model_started_at = None
            model_fallback_started_at = None
            tools_by_id = {}
            tools_without_id = collections.deque()
            self._turn_done.clear()
            self._in_flight = True
            try:
                pusher = _ProgressPusher(progress_token) if progress_token else None
            except Exception:
                pusher = None
            try:
                self._turn_timing_model_start = _turn_timing_now()
                model_fallback_started_at = _turn_timing_now()
                level = _resolve_thinking_level(thinking)
                try:
                    self._command({"type": "set_thinking_level", "level": level})
                except Exception:
                    # pi older than the RPC, or a transient RPC error, must never
                    # fail the turn: the model just keeps its current level.
                    pass
                self._send({"type": "prompt", "message": message})
                while True:
                    try:
                        event = self._read_event(TURN_READ_TIMEOUT)
                    except TimeoutError as exc:
                        self._close_process(kill=True)
                        raise RuntimeError(
                            "timed out waiting for Pi output after %s seconds"
                            % TURN_READ_TIMEOUT
                        ) from exc
                    if event is None:
                        raise self._empty_stream_error(process)
                    if event.get("type") == "response":
                        if event.get("command") == "prompt" and not event.get(
                            "success"
                        ):
                            raise RuntimeError(
                                "prompt failed: %s" % json.dumps(event)[:1500]
                            )
                        continue
                    try:
                        event_type = event.get("type")
                        if event_type == "message_start":
                            message_event = event.get("message", {})
                            if (
                                not isinstance(message_event, dict)
                                or message_event.get("role", "assistant") == "assistant"
                            ):
                                model_started_at = _turn_timing_now()
                        elif event_type == "message_end":
                            message_event = event.get("message", {})
                            if (
                                isinstance(message_event, dict)
                                and message_event.get("role") == "assistant"
                            ):
                                # Pi brackets model calls with assistant
                                # message_start/message_end. Older streams without
                                # message_start fall back to prompt send for the
                                # first call and the previous tool_execution_end
                                # for later calls.
                                model_finished_at = _turn_timing_now()
                                model_calls += 1
                                num_turns += 1
                                started_at = (
                                    model_started_at
                                    if model_started_at is not None
                                    else model_fallback_started_at
                                )
                                if (
                                    started_at is not None
                                    and model_finished_at is not None
                                ):
                                    model_ms += max(
                                        0,
                                        int((model_finished_at - started_at) * 1000),
                                    )
                                model_started_at = None
                                model_fallback_started_at = None
                        elif event_type == "tool_execution_start":
                            tool_calls += 1
                            tool_started_at = _turn_timing_now()
                            tool_name = event.get("toolName") or event.get("tool_name")
                            if tool_name:
                                tool_name = str(tool_name).lower()
                                tool_usage = tools_by_name.setdefault(
                                    tool_name, {"calls": 0, "ms": 0}
                                )
                                tool_usage["calls"] += 1
                            tool_entry = (tool_started_at, tool_name)
                            tool_id = (
                                event.get("toolCallId")
                                or event.get("tool_call_id")
                                or event.get("id")
                            )
                            if tool_id is not None:
                                tools_by_id[str(tool_id)] = tool_entry
                            else:
                                tools_without_id.append(tool_entry)
                        elif event_type == "tool_execution_end":
                            tool_finished_at = _turn_timing_now()
                            tool_id = (
                                event.get("toolCallId")
                                or event.get("tool_call_id")
                                or event.get("id")
                            )
                            if tool_id is not None:
                                tool_entry = tools_by_id.pop(str(tool_id), None)
                            elif tools_without_id:
                                tool_entry = tools_without_id.popleft()
                            else:
                                tool_entry = None
                            if tool_entry is not None:
                                tool_started_at, tool_name = tool_entry
                                elapsed_ms = 0
                                if (
                                    tool_started_at is not None
                                    and tool_finished_at is not None
                                ):
                                    elapsed_ms = max(
                                        0,
                                        int(
                                            (tool_finished_at - tool_started_at) * 1000
                                        ),
                                    )
                                tool_ms += elapsed_ms
                                if tool_name:
                                    tools_by_name[tool_name]["ms"] += elapsed_ms
                            model_fallback_started_at = tool_finished_at
                    except Exception:
                        pass
                    events.append(self._translate_activity_event(event))
                    if event.get("type") == "session":
                        candidate = event.get("id")
                        if isinstance(candidate, str) and candidate:
                            self.session_id = candidate
                    elif event.get("type") == "message_end":
                        message_event = event.get("message", {})
                        if message_event.get("role") == "assistant":
                            message_text = "".join(
                                item.get("text", "")
                                for item in message_event.get("content", [])
                                if item.get("type") == "text"
                            )
                            result_text = message_text
                            accumulated_text += message_text
                            raw_usage = message_event.get("usage", {})
                            usage = {
                                "input_tokens": raw_usage.get("input", 0),
                                "output_tokens": raw_usage.get("output", 0),
                                "cache_read_tokens": raw_usage.get("cacheRead", 0),
                                "cache_write_tokens": raw_usage.get("cacheWrite", 0),
                            }
                            terminal_reason = message_event.get(
                                "stopReason", "completed"
                            )
                            if pusher:
                                try:
                                    pusher.push(accumulated_text, cached_activities)
                                except Exception:
                                    pass
                    elif event.get("type") in (
                        "tool_start",
                        "tool_end",
                        "tool_execution_start",
                        "tool_execution_end",
                    ):
                        activities_are_stale = True
                    if activities_are_stale:
                        cached_activities = activity_from_events(events)[-300:]
                        activities_are_stale = False
                        if pusher:
                            try:
                                pusher.push(accumulated_text, cached_activities)
                            except Exception:
                                pass
                    if event.get("type") == "agent_end":
                        if not result_text:
                            for message_event in reversed(event.get("messages", [])):
                                if message_event.get("role") == "assistant":
                                    result_text = "".join(
                                        item.get("text", "")
                                        for item in message_event.get("content", [])
                                        if item.get("type") == "text"
                                    )
                                    break
                        if pusher:
                            try:
                                pusher.push(
                                    accumulated_text or result_text, cached_activities
                                )
                            except Exception:
                                pass
                        if not result_text:
                            error_detail = ""
                            for message_event in reversed(event.get("messages", [])):
                                if message_event.get("errorMessage"):
                                    error_detail = str(message_event["errorMessage"])
                                    break
                            if not error_detail:
                                error_detail = (
                                    "terminal event carried no text: %s"
                                    % (json.dumps(event)[:1500])
                                )
                            stderr = _truncate_ring_for_error(self.stderr_lines)
                            if stderr:
                                error_detail += "\nCLI stderr:\n%s" % stderr
                            raise RuntimeError(
                                "pi turn produced no output: %s" % error_detail
                            )
                        self._state()
                        usage.update(
                            {
                                "model_ms": model_ms,
                                "tool_ms": tool_ms,
                                "model_calls": model_calls,
                                "tool_calls": tool_calls,
                                "tools_by_name": tools_by_name,
                            }
                        )
                        record = {
                            "result": result_text,
                            "terminal_reason": terminal_reason,
                            "session_id": self.session_id,
                            "num_turns": num_turns,
                            "usage": usage,
                            "voice": voice_summary(result_text),
                            "activities": activity_from_events(events),
                        }
                        _emit_turn_timing(
                            "pi_model",
                            model_ms / 1000.0,
                            extra={"calls": model_calls},
                        )
                        tool_timing_fields = {"calls": tool_calls}
                        for tool_name in sorted(tools_by_name):
                            tool_usage = tools_by_name[tool_name]
                            tool_timing_fields[tool_name] = "%s:%s" % (
                                tool_usage["calls"],
                                tool_usage["ms"],
                            )
                        _emit_turn_timing(
                            "pi_tools",
                            tool_ms / 1000.0,
                            extra=tool_timing_fields,
                        )
                        _emit_elapsed(
                            "model", getattr(self, "_turn_timing_model_start", None)
                        )
                        return record
            finally:
                if pusher:
                    try:
                        pusher.stop()
                    except Exception:
                        pass
                self._in_flight = False
                self._turn_done.set()

    def interrupt(self, timeout=INTERRUPT_TIMEOUT):
        with self.process_lock:
            process = self.process
        if process is None or process.poll() is not None or not self._in_flight:
            return {
                "terminal_reason": "user_interrupt",
                "killed": False,
                "timeout": False,
            }
        self._in_flight = False
        try:
            self._send({"type": "abort"})
        except Exception:
            self._close_process(kill=True)
            return {
                "terminal_reason": "user_interrupt",
                "killed": False,
                "timeout": False,
            }
        timed_out = not self._turn_done.wait(timeout=timeout)
        if timed_out:
            self._close_process(kill=True)
        return {
            "terminal_reason": "user_interrupt",
            "killed": timed_out,
            "timeout": timed_out,
        }

    def _close_process(self, kill=False):
        with self.process_lock:
            process = self.process
            self.process = None
            self._stdout_queue = None
            self._model = None
            self._in_flight = False
        self._turn_done.set()
        if process is None:
            return
        if kill and process.poll() is None:
            process.kill()
        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        _managed_child_pids.discard(process.pid)
        _reap_orphans()


def _sync_session_volume():
    """Flush guest writes to the durable session volume.

    Park tears the VM down with a hard SIGKILL and the drive runs in
    firecracker's Unsafe cache mode, so a turn's CLI session files sit in the
    guest page cache until the periodic ext4 commit and are lost on the kill.
    sync() after each turn pushes them to the device while the session is
    still live, so a later park -> rejoin finds the conversation intact
    (issue #4309).
    """
    try:
        os.sync()
    except OSError as exc:
        # A sync failure must never fail a turn: log and continue.
        sys.stderr.write("ember-claude-shim: session volume sync failed: %s\n" % exc)
        sys.stderr.flush()


class ProcessManager:
    """Route Claude-compatible turns to the selected CLI adapter."""

    def __init__(
        self,
        workspace=None,
        claude_executable="claude",
        codex_executable="codex",
        pi_executable="pi",
    ):
        _ensure_persistence_mountpoint_writable(_persistence_mount_path())
        self.workspace = os.path.abspath(
            workspace or os.environ.get("EMBER_CLAUDE_WORKSPACE", DEFAULT_WORKSPACE)
        )
        self._hydration_attempts = 0
        self._hydration_error = None
        self._checkout_dir = None
        self._hydration_status = None
        self.fatal_error = None
        try:
            self._prewarm_clis = self._read_prewarm_clis()
        except StartupError as exc:
            self._prewarm_clis = ()
            self.fatal_error = str(exc)
        self._prewarm_complete = not self._prewarm_clis
        self._prewarm_thread = None
        self._mount_lock = threading.Lock()
        # Injectable so tests can substitute a deferred-start fake without
        # patching the stdlib threading module process-wide.
        self._thread_factory = threading.Thread
        self._remediation_lock = threading.Lock()
        self._remediation_attempts = 0
        self._remediation_thread = None
        _write_git_proxy_helper()
        cli_workspace = os.path.join(self.workspace, "src")
        _ensure_cli_dir(cli_workspace)
        self.claude = ClaudeProcess(cli_workspace, claude_executable)
        self.claude._manager = self
        self.codex = CodexProcess(cli_workspace, codex_executable)
        self.pi = PiProcess(cli_workspace, pi_executable)
        if self._prewarm_clis and self.fatal_error is None:
            self._prewarm_thread = threading.Thread(
                target=self.prewarm, name="claude-prewarm", daemon=True
            )
            self._prewarm_thread.start()

    @staticmethod
    def _workspace_identity(path):
        return _workspace_identity(path)

    @staticmethod
    def _read_prewarm_clis():
        value = os.environ.get(PREWARM_CLIS_ENV, "")
        if not value.strip():
            return ()
        clis = tuple(item.strip() for item in value.split(",") if item.strip())
        unknown = sorted(set(clis) - set(SUPPORTED_PREWARM_CLIS))
        if unknown:
            raise StartupError(
                "%s contains unsupported CLIs: %s"
                % (PREWARM_CLIS_ENV, ",".join(unknown))
            )
        return tuple(dict.fromkeys(clis))

    def prewarm(self):
        """Start configured CLIs and leave them ready for the first turn."""
        try:
            _ensure_cli_dir(self.claude.workspace)
            for cli in self._prewarm_clis:
                if cli == "claude":
                    self.claude._spawn(
                        session_id=None,
                        first_message=None,
                        model=None,
                        init_timeout=30,
                    )
                    # Claude emits a generated id in init, but a parked process
                    # has not adopted a session until its first user message.
                    self.claude.session_id = None
            self._prewarm_complete = True
        except Exception as exc:
            self.fatal_error = "CLI prewarm failed: %s" % exc
            prewarm_error = self.fatal_error
            if hasattr(self.claude, "_close_process"):
                self.claude._close_process(kill=True)
            sys.stderr.write("ember-claude-shim: %s\n" % prewarm_error)
            sys.stderr.flush()
        finally:
            self._prewarm_complete = True

    def _hydrate_workspace(self, repo, branch):
        hydration_start = _turn_timing_now()
        if self._hydration_status == "ok":
            if self._checkout_dir and os.path.isdir(self._checkout_dir):
                self._hydration_status = "skipped_existing"
                _emit_elapsed("hydration", hydration_start, status="skipped_existing")
            return
        if self._hydration_status == "skipped_existing":
            _emit_elapsed("hydration", hydration_start, status="skipped_existing")
            return
        if self._hydration_attempts >= HYDRATION_ATTEMPT_CAP:
            return
        if self._hydration_attempts:
            sys.stderr.write(
                "ember-claude-shim: retrying workspace hydration for %s@%s "
                "(attempt %s/%s)\n"
                % (
                    repo,
                    branch,
                    self._hydration_attempts + 1,
                    HYDRATION_ATTEMPT_CAP,
                )
            )
            sys.stderr.flush()
        self._hydration_attempts += 1
        checkout_dir = os.path.join(self.workspace, "src")
        # Idempotency gate: restored/rejoined volumes reuse existing checkout.
        # This deliberately ignores repo/branch changes on restored volumes; per-session
        # volumes make that unreachable in practice (repo fixed at session create).
        if os.path.isdir(checkout_dir):
            try:
                validation = subprocess.run(
                    ["git", "-C", checkout_dir, "rev-parse", "--verify", "HEAD"],
                    capture_output=True,
                    timeout=5,
                    **_cli_privilege_kwargs(),
                )
                if validation.returncode == 0:
                    self._checkout_dir = checkout_dir
                    self._hydration_status = "skipped_existing"
                    # Resume slugs are safe: session cwd never changes mid-lineage (repo fixed at create).
                    self.claude.workspace = checkout_dir
                    self.codex.workspace = checkout_dir
                    self.pi.workspace = checkout_dir
                    _emit_elapsed(
                        "hydration", hydration_start, status="skipped_existing"
                    )
                    return
            except (OSError, subprocess.TimeoutExpired):
                pass
            # A durable volume can contain a partial clone from a prior failure.
            shutil.rmtree(checkout_dir, ignore_errors=True)
        _ensure_cli_dir(self.workspace)
        # Hydrate from GitHub over https, not from the git-mirror over git://.
        #
        # The mirror was chosen (ADR agents/050) because it needs no credential
        # and is node-local. What disqualified it is the transport: #4389 stopped
        # propagating half-close onto the vsock leg, and that suppression is
        # scoped to the git-daemon port
        # (shim.py: half_close_upstream = not host_port.endswith(":9418")), so a
        # git:// tunnel never closes and a SECOND lane connection while it lingers
        # wedges (#4417). That is what forced --depth=1: a blob filter defers
        # blobs, and checkout then lazy-fetches them on exactly that second
        # connection. Shallow-but-complete was the only single-connection shape.
        #
        # Over :443 the tunnel tears down normally, which is why sequential CLI
        # HTTPS and gh both work today. So the filter becomes usable, and with it
        # FULL HISTORY: --filter=blob:none keeps every commit and tree and defers
        # only file contents, so git log, blame and bisect all work and blobs
        # arrive on demand. --depth=1 is therefore gone, not merely relaxed.
        #
        # No credential enters the guest. github.com is a credentialed egressTo
        # host (deploy/values.yaml): the sidecar terminates the guest's TLS with a
        # leaf minted from the egress CA, sets Authorization itself using Basic
        # (git-receive-pack 401s on Bearer), and originates fresh verified TLS. A
        # guest already reaches github.com this way for gh and for push, so
        # sourcing the clone here grants nothing it did not already hold. It also
        # fixes private repos, which the mirror non-fatally SKIPS when it has no
        # read token of its own.
        #
        # http.proxy rather than HTTPS_PROXY: apply_egress_ca_trust() runs before
        # this and exports GIT_SSL_CAINFO into os.environ, but the proxy URL is
        # only ever built inside the per-adapter CLI spawn envs, which this
        # subprocess does not inherit. Passing it as git config keeps the setting
        # on this one clone instead of leaking proxy env into every child.
        egress_port = os.environ.get(EGRESS_PORT_ENV, str(DEFAULT_EGRESS_PORT))
        clone_command = [
            "git",
            "clone",
            "--progress",
            "--branch",
            branch,
            "--config",
            "http.proxy=http://%s:%s" % (EGRESS_LOCALHOST, egress_port),
            # The LOGIN GATE DUMMY that opts this clone into credential
            # injection, derived from the same GH_TOKEN gh already presents
            # rather than defined here.
            #
            # The sidecar's injection is PRESENCE-KEYED: it replaces an
            # Authorization header the guest already sent and DISCARDS whatever
            # value was in it (egress-proxy injectRequest: `requested :=
            # len(req.Header.Values(sec.Header)) > 0 || sec.injectAlwaysPath(...)`
            # then `if !requested { return false }`). The header is an opt-in
            # switch, never a credential, which is what stops a prompt-injected
            # guest from authenticating as anyone else.
            #
            # git sends no Authorization on its first request, so nothing was
            # injected and github.com answered 401, which git surfaces as
            # "could not read Username for 'https://github.com'" once it falls
            # through to an interactive prompt. gh never hit this because it
            # always sends its own dummy. Reusing GH_TOKEN keeps the value where
            # guest env is defined (guest-init setDefaultEnv, alongside
            # CLAUDE_CODE_OAUTH_TOKEN) instead of adding a second, credential
            # shaped literal to this file.
            #
            # injectAlwaysPaths cannot cover this instead: it is an EXACT match
            # on req.URL.Path, and git's paths are per repository
            # (/owner/repo.git/info/refs, /owner/repo.git/git-upload-pack), so it
            # would have to enumerate every repo anyone might ever select.
            #
            # x-access-token:<token> is the Basic shape GitHub's git endpoint
            # expects. Scoped to github.com by URL so it is never presented
            # anywhere else, and written into the clone's config so the lazy blob
            # fetches --filter=blob:none defers carry it too.
            "--config",
            "http.https://github.com/.extraHeader=Authorization: Basic %s"
            % _github_basic_optin(),
            "--single-branch",
            "--filter=blob:none",
            "https://github.com/%s.git" % repo,
            checkout_dir,
        ]
        try:
            clone_start = _turn_timing_now()
            result = subprocess.run(
                clone_command,
                capture_output=True,
                timeout=GIT_CLONE_TIMEOUT_SECONDS,
                **_cli_privilege_kwargs(),
            )
            _emit_elapsed("hydration_clone", clone_start)
            if result.returncode != 0:
                stderr = result.stderr
                if isinstance(stderr, bytes):
                    stderr = stderr.decode("utf-8", "replace")
                stderr = (stderr or "").strip()
                raise RuntimeError(
                    "git command failed with exit code %s%s"
                    % (result.returncode, ": " + stderr if stderr else "")
                )
            validation = subprocess.run(
                ["git", "-C", checkout_dir, "rev-parse", "--verify", "HEAD"],
                capture_output=True,
                timeout=5,
                **_cli_privilege_kwargs(),
            )
            if validation.returncode != 0:
                raise RuntimeError("cloned directory validation failed: HEAD not found")
        except subprocess.TimeoutExpired as exc:
            _write_hydration_diagnostics(exc, checkout_dir)
            shutil.rmtree(checkout_dir, ignore_errors=True)
            self._hydration_error = str(exc)
            sys.stderr.write(
                "ember-claude-shim: workspace hydration failed for %s@%s: %s\n"
                % (repo, branch, exc)
            )
            sys.stderr.flush()
            return
        except Exception as exc:
            _write_hydration_diagnostics(exc, checkout_dir)
            shutil.rmtree(checkout_dir, ignore_errors=True)
            self._hydration_error = str(exc)
            sys.stderr.write(
                "ember-claude-shim: workspace hydration failed for %s@%s: %s\n"
                % (repo, branch, exc)
            )
            sys.stderr.flush()
            return
        self._hydration_error = None
        exclude_file = os.path.join(checkout_dir, ".git/info/exclude")
        _ensure_cli_dir(os.path.dirname(exclude_file))
        with open(exclude_file, "a") as stream:
            stream.write(".codex/\n.pi/\n")
        self._checkout_dir = checkout_dir
        self._hydration_status = "ok"
        self.claude.workspace = checkout_dir
        self.codex.workspace = checkout_dir
        self.pi.workspace = checkout_dir
        _emit_elapsed("hydration", hydration_start, status="cloned")

    def _adapter(self, model):
        if model == "qwen":
            return self.pi
        if isinstance(model, str) and model in CODEX_MODELS:
            return self.codex
        return self.claude

    def ready(self):
        # The probe must not wait for lazily spawned CLIs when prewarming is
        # unset, but must wait for every configured prewarm to complete.
        if self.fatal_error is not None or not self._prewarm_complete:
            return False
        if not self.claude.ready() or not self.codex.ready() or not self.pi.ready():
            return False
        process = getattr(self.claude, "process", None)
        # Base build safety depends on the noded placeholder volume staying blank
        # (zero-filled, no filesystem). Guest-init in guest init forbids mounting a
        # placeholder during base build (volume_linux.go:97-100). If the placeholder
        # gains a superblock this probe would misfire and skip the build liveness check.
        volume_has_ext4 = _volume_has_ext4()
        if (
            self._prewarm_clis
            and "claude" in self._prewarm_clis
            and not volume_has_ext4
        ):
            # A base build has no filesystem on the volume, so its parked CLI
            # must be alive before the image is accepted as a warm base.
            if process is None or process.poll() is not None:
                return False
        if _workspace_is_tmpfs() and volume_has_ext4:
            self._kick_remediation()
        return True

    def _kick_remediation(self):
        with self._remediation_lock:
            if self._remediation_attempts >= 3:
                return
            if (
                self._remediation_thread is not None
                and self._remediation_thread.is_alive()
            ):
                return
            # start() stays inside the lock: is_alive() is False for a
            # constructed-but-unstarted thread, so releasing before start()
            # lets two concurrent probes double-start the same Thread and
            # RuntimeError out of the readiness handler. A real remediation
            # thread reaching its own _remediation_lock uses merely waits the
            # microseconds until this block exits; only an inline-running test
            # fake would deadlock, which is why the factory is injectable.
            thread = self._thread_factory(
                target=self._remediate_workspace,
                name="claude-workspace-remediation",
                daemon=True,
            )
            self._remediation_thread = thread
            thread.start()

    def _remediate_workspace(self):
        with self._mount_lock:
            with self.claude.turn_lock:
                try:
                    ensure_workspace_volume()
                    # The volume mount binds the volume's own (initially
                    # empty) workspace dir over /workspace, which hides the
                    # src dir the constructor created on the base's tmpfs.
                    # Every adapter's ready() is isdir(workspace), so
                    # without this a restored session answers 503 to every
                    # readiness probe after the first and the prime fails
                    # with "restored guest not ready" (#5051). The turn
                    # path has always re-created it; readiness must too.
                    _ensure_cli_dir(self.claude.workspace)
                    identity = self._workspace_identity(self.claude.workspace)
                    process = getattr(self.claude, "process", None)
                    process_dead = process is not None and process.poll() is not None
                    identity_changed = identity != getattr(
                        self.claude, "_process_workspace_identity", None
                    )
                    if identity_changed or process_dead:
                        # Mount-only remediation: close the stranded process
                        # and leave the respawn to the turn path's proven lazy
                        # spawn. The eager respawn here waited its full 30s
                        # init budget without ever observing the init event
                        # (#4393), turning every warm-restore rejoin into a
                        # 30s stall; until that wait is fixed, closing early
                        # and spawning lazily restores the ~3s pre-deploy
                        # rejoin while prewarm keeps its create and cold-boot
                        # wins.
                        self.claude._close_process(kill=False)
                    with self._remediation_lock:
                        self._remediation_attempts += 1
                except Exception as exc:
                    with self._remediation_lock:
                        self._remediation_attempts += 1
                        attempts = self._remediation_attempts
                    sys.stderr.write(
                        "ember-claude-shim: warning: workspace remediation failed "
                        "(attempt %s/3): %s\n" % (attempts, exc)
                    )
                    sys.stderr.flush()

    def turn(
        self,
        message,
        session_id=None,
        model=None,
        repo=None,
        branch=None,
        progress_token=None,
        system_prompt=None,
        thinking=None,
    ):
        total_start = _turn_timing_now()
        with self._mount_lock:
            ensure_workspace_volume()
        # Per turn, for the same reason ensure_workspace_volume is: this is the
        # first point guaranteed to be post-restore with the egress lane live.
        # Doing it at startup put it at base-build time, where the lane is shut.
        apply_egress_ca_trust()
        if getattr(self.claude, "workspace", None):
            _ensure_cli_dir(self.claude.workspace)
        if repo is not None and branch is not None:
            # Say what the guest is doing while it clones. Without this the UI
            # falls through to "starting the agent..." for the whole hydration,
            # because that is its label for "VM running, no partials yet", and a
            # multi-second clone reads as dead time.
            #
            # A plain string is a valid activity (the console's activityParts
            # takes `typeof activity === "string"` as {verb, detail:""}), and
            # partial_activities is the FIRST branch of its live-line ladder, so
            # this needs no schema, no migration and no console change. The
            # adapter builds its own pusher immediately after and its real CLI
            # activities replace this line.
            #
            # Only meaningful when hydration actually clones: a restored volume
            # short-circuits on the rev-parse gate, so the pusher fires and is
            # replaced almost at once, which is the honest signal either way.
            if progress_token:
                _ProgressPusher(progress_token).push(
                    "", ["cloning %s@%s" % (repo, branch)]
                )
            self._hydrate_workspace(repo, branch)
        adapter = self._adapter(model)
        checkout_dir = os.path.join(
            getattr(self, "workspace", DEFAULT_WORKSPACE), "src"
        )
        turn_base = _capture_turn_base(checkout_dir)
        try:
            extra = {"progress_token": progress_token} if progress_token else {}
            prompt = {"system_prompt": system_prompt} if system_prompt else {}
            if adapter is self.pi:
                record = adapter.turn(
                    message,
                    session_id,
                    model or DEFAULT_PI_MODEL,
                    thinking=thinking,
                    **(extra | prompt),
                )
            elif adapter is self.codex:
                record = adapter.turn(
                    message,
                    session_id,
                    model or DEFAULT_CODEX_MODEL,
                    **(extra | prompt),
                )
            else:
                record = adapter.turn(message, session_id, model, **(extra | prompt))
            # Only Claude can recover a Claude prewarm failure. Codex and pi
            # turns must not clear the manager's Claude fatal state.
            if adapter is self.claude:
                self.fatal_error = None
            if repo is not None and isinstance(record, dict):
                if self._hydration_error:
                    record["workspace_hydration"] = {"failed": self._hydration_error}
                elif self._hydration_status:
                    record["workspace_hydration"] = self._hydration_status
            if isinstance(record, dict):
                diff = _capture_turn_diff(checkout_dir, turn_base)
                if diff is not None:
                    record["diff"] = diff
            return record
        finally:
            # End-of-turn quiescence point: park only happens after a completed
            # turn plus idle, so flushing here guarantees the device has the
            # full session file well before any later park SIGKILLs the VM
            # (#4309). The finally covers every family, and both a normal
            # return AND a raised turn (a read timeout, a mid-turn CLI death):
            # the shim process stays alive through the exception, so the sync
            # is still safe, and a raised turn may still have left durable-
            # worth CLI state that a later park would otherwise lose.
            _sync_session_volume()
            _emit_elapsed("total", total_start)

    def interrupt(self):
        # An interrupt has no model in its request, so interrupt both adapters.
        pi_result = self.pi.interrupt()
        codex_result = self.codex.interrupt()
        claude_result = self.claude.interrupt()
        if claude_result.get("killed"):
            return claude_result
        if codex_result.get("killed"):
            return codex_result
        return pi_result

    def _close_process(self, kill=False):
        self.claude._close_process(kill=kill)
        self.codex._close_process(kill=kill)
        self.pi._close_process(kill=kill)


Manager = ProcessManager


class RequestHandler(http.server.BaseHTTPRequestHandler):
    manager = None

    def log_message(self, *_args):
        pass

    def _send(self, status, value):
        body = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == HEALTHZ_PATH:
            self._send(200, {"status": "ok"})
        elif self.path == READY_PATH:
            ready = self.manager.ready()
            self._send(200 if ready else 503, {"ready": ready})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path == INTERRUPT_PATH:
            self._send(200, self.manager.interrupt())
            return
        if self.path not in (TURN_PATH, CLOCK_PATH):
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            self._send(400, {"error": "Content-Length must be an integer"})
            return
        if length < 0 or length > MAX_REQUEST_BODY_BYTES:
            self._send(
                413, {"error": "request body exceeds %d bytes" % MAX_REQUEST_BODY_BYTES}
            )
            return
        raw = self.rfile.read(length)
        if self.path == CLOCK_PATH:
            self._set_clock(raw)
            return
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            self._send(400, {"error": "invalid JSON body"})
            return
        message = payload.get("message") if isinstance(payload, dict) else None
        if not isinstance(message, str) or not message.strip():
            self._send(400, {"error": "message must not be empty"})
            return
        repo = payload.get("repo")
        branch = payload.get("branch")
        if (repo is None) != (branch is None):
            self._send(400, {"error": "repo and branch must be provided together"})
            return
        if repo is not None and (
            not isinstance(repo, str)
            or not repo
            or not isinstance(branch, str)
            or not branch
        ):
            self._send(400, {"error": "repo and branch must be non-empty strings"})
            return
        progress_token = payload.get("progress_token")
        if progress_token is not None and (
            not isinstance(progress_token, str) or not progress_token.strip()
        ):
            self._send(400, {"error": "progress_token must be a non-empty string"})
            return
        system_prompt = payload.get("system_prompt")
        if system_prompt is not None and (
            not isinstance(system_prompt, str) or not system_prompt.strip()
        ):
            self._send(400, {"error": "system_prompt must be a non-empty string"})
            return
        thinking = payload.get("thinking")
        if thinking is not None and not (
            thinking is True
            or thinking is False
            or (isinstance(thinking, str) and thinking in PI_THINKING_LEVELS)
        ):
            self._send(
                400,
                {
                    "error": "thinking must be a bool or one of %s"
                    % (PI_THINKING_LEVELS,)
                },
            )
            return
        if "session_id" in payload:
            sid = payload.get("session_id")
            if sid is not None and (not isinstance(sid, str) or not sid.strip()):
                self._send(400, {"error": "session_id must be a non-empty string"})
                return
        try:
            session_id = payload.get("session_id")
            if isinstance(session_id, str):
                session_id = session_id.strip()
            hydration = {"repo": repo, "branch": branch} if repo is not None else {}
            progress = (
                {"progress_token": progress_token.strip()} if progress_token else {}
            )
            prompt = {"system_prompt": system_prompt.strip()} if system_prompt else {}
            thinking_override = {"thinking": thinking} if thinking is not None else {}
            record = self.manager.turn(
                message,
                session_id,
                payload.get("model"),
                **(hydration | progress | prompt | thinking_override),
            )
            if repo is not None and isinstance(record, dict):
                if getattr(self.manager, "_hydration_error", None):
                    record["workspace_hydration"] = {
                        "failed": self.manager._hydration_error
                    }
                elif getattr(self.manager, "_hydration_status", None):
                    record["workspace_hydration"] = self.manager._hydration_status
        except SessionConflictError as exc:
            self._send(409, {"error": str(exc)})
        except StartupError as exc:
            sys.stderr.write(str(exc) + "\n")
            sys.stderr.flush()
            self._send(503, {"error": str(exc)})
        except Exception as exc:
            self._send(422, {"error": str(exc)})
        else:
            self._send(200, record)

    def _set_clock(self, raw):
        try:
            payload = json.loads(raw.decode("utf-8"))
            if isinstance(payload, dict):
                payload = payload.get("epoch_ms", payload.get("timestamp"))
            if isinstance(payload, bool) or not isinstance(payload, (int, float)):
                raise ValueError("epoch milliseconds required")
            epoch_ms = int(payload)
            if epoch_ms <= 0:
                raise ValueError("epoch milliseconds must be positive")
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            self._send(400, {"error": "invalid clock timestamp"})
            return
        try:
            time.clock_settime(time.CLOCK_REALTIME, epoch_ms / 1000.0)
        except (AttributeError, OSError, ValueError) as exc:
            sys.stderr.write("ember-claude-shim: clock update failed: %s\n" % exc)
            sys.stderr.flush()
            self._send(500, {"error": "could not set guest clock"})
            return
        self._send(200, {"epoch_ms": epoch_ms})


def make_handler(manager):
    return type("ClaudeRequestHandler", (RequestHandler,), {"manager": manager})


class VsockHTTPServer(http.server.ThreadingHTTPServer):
    address_family = getattr(socket, "AF_VSOCK", -1)
    allow_reuse_address = False

    def server_bind(self):
        self.socket.bind(self.server_address)
        cid, port = self.socket.getsockname()
        self.server_address = (cid, port)
        self.server_name = "vsock"
        self.server_port = port


def build_server(manager):
    port = int(os.environ.get("EMBER_HTTP_PORT", str(GUEST_HTTP_PORT)))
    return VsockHTTPServer(
        (getattr(socket, "VMADDR_CID_ANY", 0xFFFFFFFF), port),
        make_handler(manager),
    )


def main():
    install_child_reaper()
    manager = ProcessManager(
        claude_executable="claude", codex_executable="codex", pi_executable="pi"
    )
    server = build_server(manager)
    egress_port = int(os.environ.get(EGRESS_PORT_ENV, str(DEFAULT_EGRESS_PORT)))
    egress = VsockEgressForwarder(egress_port)
    try:
        egress.listen()
        sys.stderr.write(
            "ember-claude-shim: egress listening on %s:%s\n"
            % (EGRESS_LOCALHOST, egress.port)
        )
        sys.stderr.flush()
        sys.stderr.write(
            "ember-claude-shim: listening on vsock port %s\n" % server.server_port
        )
        sys.stderr.flush()
        server.serve_forever()
    finally:
        server.server_close()
        egress.close()
        manager._close_process(kill=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
