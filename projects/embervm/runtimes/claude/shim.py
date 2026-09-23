#!/usr/bin/python3
"""HTTP over vsock shim for a long-lived Claude Code CLI session."""

import base64
import collections
import hashlib
import http.server
import json
import math
import os
import queue
import re
import selectors
import signal
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
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
# Once either direction of a tunnel completes, give the remaining pump a full
# response-idle window before closing the stream. The git helper already uses
# a 2 second completion window; one extra second covers scheduling delay. This
# applies to the current HTTPS CONNECT path as well as the legacy git daemon
# path, without imposing an idle timeout on active bidirectional tunnels.
EGRESS_TUNNEL_COMPLETION_IDLE_CLOSE_SECONDS = 3.0
EGRESS_TUNNEL_JOIN_POLL_SECONDS = 0.1
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
# Per-file cap on added files kept in the reduced diff when the full diff
# blows a cap. Sized for declared artifacts such as plan.json, not for work
# products.
MAX_TURN_DIFF_REDUCED_FILE_BYTES = 64 * 1024
# Bound on a declared artifact delivered beside the diff. Independent of the
# diff caps on purpose: the artifact channel exists precisely so a huge work
# diff cannot cost the caller its small declared document.
MAX_TURN_ARTIFACT_BYTES = 256 * 1024
MAX_RESULT_RECEIPT_BYTES = 32 * 1024 * 1024
MAX_RESULT_RECEIPT_ACK_BYTES = 4096
RESULT_RECEIPT_ATTEMPTS = 3
RESULT_RECEIPT_TIMEOUT_SECONDS = 2.0
RESULT_RECEIPT_BACKOFF_SECONDS = 0.1


def egress_proxy_env():
    """Proxy variables for a CLI child, deliberately in BOTH letter cases.

    curl reads ``http_proxy`` in LOWERCASE ONLY. Uppercase ``HTTP_PROXY`` is
    ignored on purpose, because a CGI environment can forge it from a request's
    Proxy: header, and curl has refused to honour it since that class of bug.
    Every other variable here (``HTTPS_PROXY``, ``NO_PROXY``) is read in either
    case.

    That asymmetry is not cosmetic inside a guest. A session guest boots with NO
    NIC at all, so a request that misses the proxy has no route and no resolver:
    it dies as "Could not resolve host", which reads like a broken service rather
    than a bypassed lane. Pi's web_search hit exactly this against the in-cluster
    SearXNG endpoint (an http:// URL) while its web_fetch over https:// worked,
    because only the https lane was ever pointed at the forwarder.

    So set both cases once, here, and let every adapter share it. A tool that
    shells out to curl is the normal case in these guests, not the exception.
    """
    egress_port = os.environ.get(EGRESS_PORT_ENV, str(DEFAULT_EGRESS_PORT))
    proxy_url = "http://%s:%s" % (EGRESS_LOCALHOST, egress_port)
    no_proxy = "127.0.0.1,localhost"
    return {
        "HTTPS_PROXY": proxy_url,
        "HTTP_PROXY": proxy_url,
        "NO_PROXY": no_proxy,
        "https_proxy": proxy_url,
        "http_proxy": proxy_url,
        "no_proxy": no_proxy,
    }


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


def _reduced_added_file_diff(raw):
    """Keep small added files so declared artifacts survive a capped full diff.

    Server-side artifact validation retries when a declared artifact is absent.
    Dropping a small JSON artifact with the rest of a huge work diff can make
    those retries livelock.
    """
    marker = b"diff --git "
    # Boundaries are the newline-anchored header only: every hunk line carries
    # a +/-/space prefix, so a bare "diff --git " at line start is always a
    # real per-file boundary, never file content. Slicing by offset keeps each
    # section's bytes, trailing newline included, exactly as git emitted them.
    starts = [0] if raw.startswith(marker) else []
    index = 0
    while True:
        index = raw.find(b"\n" + marker, index)
        if index == -1:
            break
        starts.append(index + 1)
        index += 1
    kept = []
    for start, end in zip(starts, starts[1:] + [len(raw)]):
        section = raw[start:end]
        if (
            b"\nnew file mode " in section
            and len(section) <= MAX_TURN_DIFF_REDUCED_FILE_BYTES
        ):
            kept.append(section)
    return b"".join(kept)


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
        truncated_outcome = None
        if len(raw) > MAX_TURN_DIFF_BYTES:
            truncated_outcome = "truncated_raw"
        else:
            compressed = zlib.compress(raw)
            if len(compressed) > MAX_TURN_DIFF_COMPRESSED_BYTES:
                truncated_outcome = "truncated_compressed"
        if truncated_outcome:
            reduced = _reduced_added_file_diff(raw)
            # Check the raw bound before compressing: a raw diff can bust the
            # cap purely on added files (a committed vendor tree), leaving a
            # reduced form nearly as large, and compressing hundreds of
            # megabytes just to discard them is a second full-size buffer in
            # a small guest.
            if reduced and len(reduced) <= MAX_TURN_DIFF_BYTES:
                reduced_compressed = zlib.compress(reduced)
                if len(reduced_compressed) <= MAX_TURN_DIFF_COMPRESSED_BYTES:
                    _emit_turn_diff_outcome(
                        checkout_dir, "diff", truncated_outcome + "_reduced"
                    )
                    return {
                        "base_sha": base_sha,
                        "zlib_b64": base64.b64encode(reduced_compressed).decode(
                            "ascii"
                        ),
                        "truncated": True,
                    }
            _emit_turn_diff_outcome(checkout_dir, "diff", truncated_outcome)
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


def _capture_turn_artifact(checkout_dir, artifact_path):
    """Deliver a declared artifact whole without making it turn-critical.

    The reduced-diff fallback under truncation only preserves ADDED files under
    64 KiB. Direct delivery is whole-file, works for modified files, and removes
    the parser's NOT_FRESH constraint.
    """
    if not artifact_path:
        return None
    try:
        resolved_checkout = os.path.realpath(checkout_dir)
        resolved_artifact = os.path.realpath(os.path.join(checkout_dir, artifact_path))
        if os.path.isabs(artifact_path) or not resolved_artifact.startswith(
            resolved_checkout + os.sep
        ):
            outcome = "invalid_path"
            content_b64 = None
        elif not os.path.isfile(resolved_artifact):
            outcome = "missing"
            content_b64 = None
        else:
            # Guest code is untrusted (ARCHITECTURE.md:920). Take size and
            # content from one descriptor; O_NOFOLLOW prevents a check-to-use
            # final-component symlink swap.
            fd = os.open(resolved_artifact, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    outcome = "missing"
                    content_b64 = None
                elif info.st_size > MAX_TURN_ARTIFACT_BYTES:
                    outcome = "oversize"
                    content_b64 = None
                else:
                    raw = stream.read(MAX_TURN_ARTIFACT_BYTES + 1)
                    if len(raw) > MAX_TURN_ARTIFACT_BYTES:
                        outcome = "oversize"
                        content_b64 = None
                    else:
                        outcome = "ok"
                        content_b64 = base64.b64encode(raw).decode("ascii")
        _emit_turn_diff_outcome(checkout_dir, "artifact", outcome)
        return {
            "path": artifact_path,
            "content_b64": content_b64,
            "outcome": outcome,
        }
    except Exception:
        _emit_turn_diff_outcome(checkout_dir, "artifact", "unreadable")
        return {
            "path": artifact_path,
            "content_b64": None,
            "outcome": "unreadable",
        }


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
SUPPORTED_PREWARM_CLIS = ("claude", "codex", "pi")
# The in-image prewarm config. Task-class guests have no env delivery (#4429):
# initEnv entries never reach a guest, so the per-image list of CLIs to prewarm
# ships as a file baked into each runtime image (runtimes/claude prewarms all
# three families, runtimes/pi only pi). EMBER_PREWARM_CLIS stays as an explicit
# override for tests and one-off boots.
PREWARM_CLIS_FILE = "/usr/share/ember-shim/prewarm-clis"
CODEX_MODELS = {
    "luna": ("gpt-5.6-luna", "medium"),
    "terra": ("gpt-5.6-terra", "high"),
    "sol": ("gpt-5.6-sol", "high"),
    "astra": ("gpt-6-astra", "high"),
}
CLAUDE_MODELS = {
    "opus": "opus",
    "sonnet": "sonnet",
    "fable": "claude-fable-5",
}
DEFAULT_CODEX_MODEL = "luna"
CODEX_SUBSCRIPTION_BASE_URL_ENV = "CODEX_SUBSCRIPTION_BASE_URL"
AGENT_MCP_URL_ENV = "EMBER_AGENT_MCP_URL"
DEFAULT_CODEX_SUBSCRIPTION_BASE_URL = "http://chatgpt.com/backend-api/"
CODEX_DUMMY_ACCOUNT_ID = "guest-subscription-account"
PI_MODELS = {
    "spark": "muse-spark-1.3-contributor",
}
PI_MODEL_ALIASES = {"qwen": "spark", "pi-spark": "spark"}
DEFAULT_PI_MODEL = "spark"
# Muse requires -contributor model IDs. Contributor tier: $0.10/$0.20 per M input/output.
# Plain tier (muse-spark-1.3) is $1.25/$4.25, a 12.5x price cliff. Always pass the model
# explicitly via --model; muse resolves its default from server catalog.
MUSE_MODELS = {"spark": "muse-spark-1.3-contributor"}
MUSE_MODEL_ALIASES = {"qwen": "spark"}
DEFAULT_MUSE_MODEL = "spark"


def _canonical_pi_model(model):
    """Map persisted legacy model names to the active Pi model identity."""
    return PI_MODEL_ALIASES.get(model, model)


# Pi gets a 120K per-session budget from NInfer's shared 262144-token KV pool.
# NInfer has two generation lanes, so two full Pi contexts consume 245760
# tokens and leave 16384 tokens of shared headroom. A lone direct API request
# can still use the server's full 262144-token maxContext because the server
# does not statically partition its page pool.
#
# The shared gap also absorbs pi's context-estimation error. Pi uses a chars/4
# heuristic for trailing messages rather than the model tokenizer. Measured in
# prod on 2026-08-07, two 50 KiB repetitive-ASCII tool results undercounted by
# 4097 tokens. Pi also subtracts its fixed 4096-token safety margin inside each
# advertised window. Keep this value and PI_CONTEXT_WINDOW_HEADROOM aligned
# with ninfer.kvCapacity and ninfer.maxConcurrency; the cross-file sync test
# enforces the relationship.
PI_CONTEXT_WINDOW = 122880
PI_CONTEXT_WINDOW_HEADROOM = 16384
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
# Keep the established output cap during the provider cutover. It fits beside
# pi's fixed safety margin in the compaction reserve and remains independently
# bounded from the caller's requested output size.
PI_MAX_OUTPUT_TOKENS = 12288

# Preserve the public turn request schema while the Meta-backed Pi adapter
# ignores this legacy hint. No value from this set is sent to the provider.
PI_THINKING_LEVELS = ("off", "minimal", "low", "medium", "high")


def _is_leaked_tool_call(text):
    """Return whether text starts with tool-call syntax as the answer.

    A provider can emit a tool-call block with spurious junk closing tags.
    The parser rejects it and returns the raw XML as content. Because
    the turn ends stop it passes the clean check and records ok. Alternatively,
    an emission cut off by the token cap (terminal_reason: length) also begins
    with <tool_call>. The leading anchor prevents false positives from
    legitimate answers that merely mention tool-call syntax in prose.
    """
    if not isinstance(text, str):
        return False
    stripped = text.lstrip()
    # The leading <tool_call> anchor prevents false positives from legitimate
    # answers that merely mention tool-call syntax in prose.
    return stripped.startswith("<tool_call>")


# Compaction reserve in tokens. This must exceed PI_MAX_OUTPUT_TOKENS plus
# PI_CONTEXT_SAFETY_TOKENS so pi starts compacting while there is still room for
# a full response at turn boundaries. pi checks compaction at agent_end and
# before a prompt, not between tool iterations, so a run that approaches the
# reserve mid-execution can still produce short replies.
PI_COMPACTION_RESERVE_TOKENS = 16896

# Tokens to keep after compaction. Keeping 8000 recent tokens preserves the
# latest tool exchange while leaving ample runway below the 105984-token
# compaction trigger. This value must remain less than PI_CONTEXT_WINDOW minus
# PI_COMPACTION_RESERVE_TOKENS so compaction actually helps when it fires.
PI_COMPACTION_KEEP_RECENT_TOKENS = 8000
# Detect infinite loops: 387 consecutive bash calls in session 599 (118757 tokens,
# 8.6 minutes burned) and 400 consecutive in session 609 (118817 tokens, 420s,
# report-only job). A model re-running a command after an edit produces a
# DIFFERENT intervening call, so an unbroken run of identical calls is never
# productive work. This affects report-only jobs too, so prompt shape is not
# the trigger.
#
# 20, not 5. Only a different tool call resets the counter, and thinking
# blocks do not, so check-free POLLING is a legitimate unbroken run of
# identical calls: waiting on CI with repeated `gh pr checks`, or retrying a
# flaky egress call. Killing one of those ends the turn, and for a one-shot
# drain job that error is permanent. The observed pathological runs were 184
# to 400, so any threshold below about 50 catches all of them, and 20 buys
# that margin at no cost. A sharper signal, deferred as its own change, is to
# reset when the tool RESULT changes: an identical call whose output differs
# is progress, which is exactly what polling is.
PI_MAX_IDENTICAL_TOOL_CALLS = 20
PI_WEB_RESEARCH_EXTENSION = "/usr/share/ember-pi/extensions/web-research.ts"
# Pi has no MCP client. This image-owned extension bridges the ONE MCP server a
# guest may reach (the monolith-agents tier) into pi tools, and is loaded only
# when EMBER_AGENT_MCP_URL is set and the endpoint answered the probe, so the
# prompt's claim that the tools exist stays true (#5569).
PI_AGENT_MCP_EXTENSION = "/usr/share/ember-pi/extensions/agent-mcp.ts"
MAX_REQUEST_BODY_BYTES = 1 << 20
MAX_TOOL_INPUT_BYTES = 4096
# Cap on the proxy request head the egress forwarder reads before it knows the
# destination. Generous for real headers, bounded so a client that never sends
# the terminating blank line cannot grow this buffer without limit.
MAX_PROXY_HEAD_BYTES = 64 << 10
INIT_READ_TIMEOUT_ENV = "EMBER_INIT_READ_TIMEOUT"
DEFAULT_INIT_READ_TIMEOUT = 90.0
PARK_GRACE_SECONDS_ENV = "EMBER_PARK_GRACE_SECONDS"
# Let a message-less CLI finish its authenticated startup burst before the
# base snapshot captures it.
DEFAULT_PARK_GRACE_SECONDS = 15.0
# Initialization waits inside a turn, so this must stay below the CP per-invoke
# budget (spec.invocation.timeoutSeconds) and TURN_READ_TIMEOUT's backstop,
# while remaining generous enough for the cold
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


def _read_park_grace_seconds():
    """Return the positive finite park grace from the environment."""
    try:
        value = float(
            os.environ.get(PARK_GRACE_SECONDS_ENV, DEFAULT_PARK_GRACE_SECONDS)
        )
    except (TypeError, ValueError):
        return DEFAULT_PARK_GRACE_SECONDS
    if not math.isfinite(value) or value <= 0:
        return DEFAULT_PARK_GRACE_SECONDS
    return value


INIT_READ_TIMEOUT = _read_init_timeout()
# Last-resort silence bound, not the task's progress policy. A CLI can stay
# silent during a useful long tool call, so ten minutes without an event is
# not evidence of a stuck task. DBOS/conductor supervision owns inspection and
# earlier cancellation. Leave five minutes before Ember's twelve-hour total
# invoke ceiling for interruption, result capture and response delivery.
TURN_READ_TIMEOUT = 42900.0
INTERRUPT_TIMEOUT = 30.0
DRAIN_FLUSH_RESERVE_SECONDS = 2.0
INTERRUPT_STARTUP_GRACE_SECONDS = 0.25
CLI_PROBE_TIMEOUT = 10.0
# A ref read on a local checkout, so this only ever has to cover process spawn.
# Matches the timeout hydration's own post-clone validation has always used.
CHECKOUT_VALIDATION_TIMEOUT_SECONDS = 5
HYDRATION_ATTEMPT_CAP = 3
# The guest path is proxied over vsock, so it is materially slower than a direct
# clone, and a FULL clone additionally pays 86k delta resolutions plus a 151 MB
# checkout on 2 vCPUs: the instrumented #4389 run finished deltas and most of
# the checkout just past the old 300 second cap. Only the first turn per
# session volume ever pays this (the rev-parse gate skips hydration after one
# success), and the outer budgets (Monolith result wait and Ember invocation)
# leave headroom.
GIT_CLONE_TIMEOUT_SECONDS = 600
PERMISSION_MODE_ENV = "EMBER_PERMISSION_MODE"
DEFAULT_PERMISSION_MODE = "bypassPermissions"
CLI_UID_ENV = "EMBER_CLI_UID"
CLI_GID_ENV = "EMBER_CLI_GID"
DEFAULT_CLI_UID = 65532
DEFAULT_CLI_GID = 65532
# Staged only. No chart or base setting enables this by default.
MUSE_BINARY_PREFLIGHT_ENV = "EMBER_MUSE_BINARY_PREFLIGHT"
PERSISTENCE_MOUNT_PATH_ENV = "EMBER_PERSISTENCE_MOUNT_PATH"
DEFAULT_PERSISTENCE_MOUNT_PATH = "/session"
GUEST_INIT_PATH = "/usr/local/bin/ember-runtime-guest-init"
SANDBOX_NETWORK_EGRESS_PROMPT = (
    "- All network egress is proxied. The public internet is reachable and "
    "in-cluster services are not. You hold no credentials: the proxy attaches "
    "them on the way out, so no token is readable from in here.\n"
)
SANDBOX_AGENT_MCP_PROMPT = (
    "- All network egress is proxied. The public internet is reachable; the "
    "only in-cluster service you can reach is the `agents` MCP server, already "
    "configured in your CLI, which carries the knowledge tools "
    "search_knowledge, report_knowledge, dispute_fact and report_distress, "
    "exposed to you as mcp__agents__<name>. "
    "Call search_knowledge before investigating anything that may have been "
    "seen before, and report_knowledge for findings another agent will need. "
    "You hold no credentials: the proxy attaches them on the way out, so no "
    "token is readable from in here.\n"
)
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
    "- The checkout's origin is GitHub over HTTPS and accepts pushes. The "
    "proxy attaches the credential on the way out, so you still hold none.\n"
    "- Your git identity is already configured. Do not set user.name or "
    "user.email.\n"
    + SANDBOX_NETWORK_EGRESS_PROMPT
    + "- gh reaches GitHub even though `gh auth status` reports you are not "
    "logged in. That report is expected, because it inspects local credentials "
    "and the real one is never local. Judge by whether the request succeeds.\n"
)


def compose_system_prompt(caller_prompt=None, agent_mcp_configured=False):
    """Join the shim-owned sandbox prompt with an optional caller prompt."""
    sandbox_prompt = SANDBOX_PROMPT
    if agent_mcp_configured:
        sandbox_prompt = sandbox_prompt.replace(
            SANDBOX_NETWORK_EGRESS_PROMPT, SANDBOX_AGENT_MCP_PROMPT
        )
    if caller_prompt and caller_prompt.strip():
        return sandbox_prompt + "\n" + caller_prompt.strip()
    return sandbox_prompt


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


def _diagnostic_value(value, limit):
    """Return a bounded representation for non-secret spawn context."""
    rendered = repr(value)
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3] + "..."


def _identity_has_execute_permission(path_stat, uid, gids):
    """Check mode-bit execute permission for the identity used by Popen."""
    mode = path_stat.st_mode
    if uid == 0:
        # Linux still requires at least one execute bit when root executes a
        # regular file. The same rule is sufficient for directory traversal.
        return bool(mode & 0o111)
    if path_stat.st_uid == uid:
        return bool(mode & stat.S_IXUSR)
    if path_stat.st_gid in gids:
        return bool(mode & stat.S_IXGRP)
    return bool(mode & stat.S_IXOTH)


def _cli_executable_status(path, privilege_kwargs):
    """Return whether path is statically executable by the Popen identity."""
    uid = privilege_kwargs.get("user", os.geteuid())
    gid = privilege_kwargs.get("group", os.getegid())
    gids = set(os.getgroups())
    gids.add(gid)
    try:
        executable_stat = os.stat(path)
    except FileNotFoundError:
        return False, "missing"
    except OSError as exc:
        return False, "stat_errno_%s" % (exc.errno or "unknown")
    if not stat.S_ISREG(executable_stat.st_mode):
        return False, "not_regular"
    if not _identity_has_execute_permission(executable_stat, uid, gids):
        return False, "not_executable_by_cli_identity"

    # A file execute bit is not enough if the child cannot traverse a parent.
    # Check both lexical and resolved parents so a symlink cannot hide a
    # directory that the dropped CLI identity cannot enter.
    directories = []
    seen_directories = set()
    for candidate in (path, os.path.realpath(path)):
        directory = os.path.dirname(candidate)
        while directory:
            if directory not in seen_directories:
                directories.append(directory)
                seen_directories.add(directory)
            parent = os.path.dirname(directory)
            if parent == directory:
                break
            directory = parent
    for directory in directories:
        try:
            directory_stat = os.stat(directory)
        except OSError as exc:
            return False, "parent_stat_errno_%s" % (exc.errno or "unknown")
        if not stat.S_ISDIR(directory_stat.st_mode):
            return False, "parent_not_directory"
        if not _identity_has_execute_permission(directory_stat, uid, gids):
            return False, "parent_not_traversable_by_cli_identity"
    return True, "executable"


def _muse_executable_candidates(executable, child_env, cwd):
    """Resolve candidates with the cwd and PATH semantics Popen will use."""
    if os.path.dirname(executable):
        if os.path.isabs(executable):
            return [executable]
        return [os.path.abspath(os.path.join(cwd, executable))]

    search_path = child_env.get("PATH")
    if search_path is None:
        search_path = os.defpath
    candidates = []
    seen = set()
    for directory in search_path.split(os.pathsep):
        directory = directory or cwd
        if not os.path.isabs(directory):
            directory = os.path.join(cwd, directory)
        candidate = os.path.abspath(os.path.join(directory, executable))
        if candidate not in seen:
            candidates.append(candidate)
            seen.add(candidate)
    return candidates


def _require_muse_executable(executable, child_env, cwd, privilege_kwargs):
    """Raise a bounded StartupError when Muse cannot be executed by the child."""
    first_unusable = None
    candidates = _muse_executable_candidates(executable, child_env, cwd)
    for candidate in candidates:
        usable, reason = _cli_executable_status(candidate, privilege_kwargs)
        if usable:
            return
        if reason != "missing" and first_unusable is None:
            first_unusable = (candidate, reason)

    uid = privilege_kwargs.get("user", os.geteuid())
    gid = privilege_kwargs.get("group", os.getegid())
    if first_unusable:
        candidate, reason = first_unusable
    else:
        candidate, reason = None, "not_found"
    path_context = child_env.get("PATH")
    if path_context is None:
        path_context = os.defpath
    raise StartupError(
        "Muse executable preflight failed before Popen: "
        "executable=%s PATH=%s candidate=%s reason=%s cli_uid=%s cli_gid=%s "
        "base_generation=unknown"
        % (
            _diagnostic_value(executable, 256),
            _diagnostic_value(path_context, 512),
            _diagnostic_value(candidate, 256),
            reason,
            uid,
            gid,
        )
    )


def _checkout_is_usable(path):
    """Return whether path is a git checkout whose HEAD resolves.

    os.path.isdir was the whole test at every gate that guards a checkout, and
    it is satisfied by two things that hold no source at all. The turn path
    recreates <workspace>/src on every turn (_ensure_cli_dir), so a checkout
    lost to a park and restore, to a volume mount landing over the base's copy,
    or to a scratch reclaim comes back as an empty directory. And a .git that
    holds nothing but info/exclude, which the hydration annotation below can
    leave behind if the checkout is replaced between the clone's validation and
    that write, is the same shape one level down. A guest started on either ran
    a whole turn against no source and reported success, which is a worse
    failure than the loud StartupError callers already handle.

    Runs as the CLI uid, exactly as hydration's clone and its post-clone
    validation do, AND scopes safe.directory to this one path. Either alone
    leaves a false negative: as root against a CLI-owned checkout git refuses
    with "detected dubious ownership", and a restored volume can hold a
    checkout owned by neither. A false negative here fails a good turn, so both
    guards are worth their cost. Nothing is written to any git config.
    """
    if not path or not os.path.isdir(path):
        return False
    try:
        validation = subprocess.run(
            _git_read_argv(path, "rev-parse", "--verify", "HEAD"),
            capture_output=True,
            timeout=CHECKOUT_VALIDATION_TIMEOUT_SECONDS,
            **_cli_privilege_kwargs(),
        )
    except Exception:
        # A timeout, a missing git, or a privilege drop that cannot be applied
        # all mean the same thing to the caller: this checkout cannot be
        # trusted to hold source.
        return False
    return validation.returncode == 0


def _workspace_ready_for_spawn(workspace, requires_git_checkout):
    """Return whether a CLI may be spawned against this workspace.

    A repo-less session legitimately runs in an empty directory, so existence
    is the whole test there. A repo-backed session must clear the stricter bar
    in _checkout_is_usable.
    """
    if requires_git_checkout:
        return _checkout_is_usable(workspace)
    return os.path.isdir(workspace)


GIT_PROXY_PATH = "/tmp/ember-git-proxy"
CLAUDE_MCP_CONFIG_DIR = "/tmp/ember-mcp"
CLAUDE_MCP_CONFIG_PATH = "/tmp/ember-mcp/claude-mcp.json"

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

    Since #5358 the egress lane is open during base builds. Prewarm applies the
    trust once before spawning any CLI, so the resulting snapshot carries both
    the installed bundle and these environment variables into every session.

    A session guest is restored from that shared snapshot, so the per-turn call
    remains necessary for CA rotation without a fleet base rebuild. Re-running
    is cheap (one vsock round trip) and updates the environment before a turn
    can lazily spawn a new CLI.
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
        # 2s leaves margin over the expected server-side gaps. If it ever does
        # fire early the pack fails git's own checksum, so the caller errors
        # loudly rather than leaving a silently truncated checkout.
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


def _write_read_only_file(path, content):
    """Rewrite a shim-owned file and block in-place edits and casual drift.

    A CLI-owned parent directory still permits replace-by-rename despite mode
    0444. The microVM plus the egress sidecar allowlist, not this file mode, are
    the security boundary.
    """
    if os.path.lexists(path):
        if _cli_privilege_kwargs():
            # Recreate the inode so a legacy CLI-owned copy becomes root-owned.
            os.unlink(path)
        else:
            os.chmod(path, 0o644)
    with open(path, "w") as stream:
        stream.write(content)
    os.chmod(path, 0o444)


def _validate_claude_mcp_config_dir():
    """Reject an MCP config directory that is not the shim-owned directory."""
    if os.path.islink(CLAUDE_MCP_CONFIG_DIR):
        raise StartupError(
            "unsafe MCP config directory %s: found symbolic link"
            % CLAUDE_MCP_CONFIG_DIR
        )
    if not os.path.isdir(CLAUDE_MCP_CONFIG_DIR):
        found = "missing path"
        if os.path.lexists(CLAUDE_MCP_CONFIG_DIR):
            found = "non-directory"
        raise StartupError(
            "unsafe MCP config directory %s: found %s" % (CLAUDE_MCP_CONFIG_DIR, found)
        )
    try:
        directory_stat = os.stat(CLAUDE_MCP_CONFIG_DIR)
    except OSError as exc:
        raise StartupError(
            "unsafe MCP config directory %s: stat failed: %s"
            % (CLAUDE_MCP_CONFIG_DIR, exc)
        ) from exc
    if os.geteuid() == 0 and directory_stat.st_uid != 0:
        raise StartupError(
            "unsafe MCP config directory %s: found owner uid %s, expected 0"
            % (CLAUDE_MCP_CONFIG_DIR, directory_stat.st_uid)
        )


def _create_claude_mcp_config_dir():
    """Create the private MCP config directory before any CLI can run."""
    try:
        os.mkdir(CLAUDE_MCP_CONFIG_DIR, 0o755)
    except FileExistsError:
        pass
    if os.path.islink(CLAUDE_MCP_CONFIG_DIR):
        raise StartupError(
            "unsafe MCP config directory %s: found symbolic link"
            % CLAUDE_MCP_CONFIG_DIR
        )
    if not os.path.isdir(CLAUDE_MCP_CONFIG_DIR):
        raise StartupError(
            "unsafe MCP config directory %s: found non-directory"
            % CLAUDE_MCP_CONFIG_DIR
        )
    if os.geteuid() == 0:
        os.chown(CLAUDE_MCP_CONFIG_DIR, 0, 0)
    os.chmod(CLAUDE_MCP_CONFIG_DIR, 0o755)


def _agent_mcp_endpoint_alive(url, timeout=3.0):
    """Return whether the MCP endpoint produces any HTTP response."""
    proxy_env = egress_proxy_env()
    proxy_handler = urllib.request.ProxyHandler(
        {
            "http": proxy_env["http_proxy"],
            "https": proxy_env["https_proxy"],
        }
    )
    opener = urllib.request.build_opener(proxy_handler)
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "ember-shim-probe", "version": "1"},
            },
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    try:
        response = opener.open(request, timeout=timeout)
        response.close()
        return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, socket.timeout, OSError):
        return False


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


class TransientTurnError(RuntimeError):
    """A turn that failed underneath the CLI, and that a retry could clear.

    do_POST maps every unclassified turn failure to a bare 422, and the control
    plane proxies a guest response verbatim (Embervm.Router.send_guest_result/4,
    and session.ex: "a session invoke's guest error is the guest's answer, not a
    VM failure"). There is no retry anywhere on the session lane inside EmberVM,
    so this body is the ONLY place a transient cause can be declared: callers
    key their backoff off `retryable` in it (see _retryable_from_response in
    monolith factory/execution/transport.py).

    Deliberately narrow. A cause that reproduces on the next attempt (a missing
    config file, a missing binary, a usage limit that names its own reset hours
    away) is NOT transient, and flagging one burns the caller's whole ladder
    before failing anyway. RuntimeError is the base so the existing handlers
    that catch RuntimeError keep catching these unchanged.
    """


# An ESTABLISHED leg died, or the provider named a momentary condition. Only
# these are worth the caller's roughly two-minute ladder.
#
# Deliberately NOT here: "error sending request", "transport error" and
# "connection refused". Those are reqwest generics that a PERMANENT egress
# fault emits on every attempt (a host missing from egressTo, a crashlooping
# sidecar, a broker grant failing closed), so matching them would retry a
# condition that cannot clear. The muse model-catalog failure is the worked
# example: it reads like a transport blip and is structurally permanent,
# because muse asks for https://api.meta.ai/muse-code/models while MUSE_BASE_URL
# is plaintext (muse trusts neither the interception CA nor the system store),
# and deploy/values.yaml denies api.meta.ai outright when the catalog entry is
# dead. All 17 muse deliveries in the 2026-09-07 window failed; none recovered.
_TRANSIENT_TURN_MARKERS = (
    "stream disconnected",
    "connection reset",
    "at capacity",
)

# Checked FIRST, and a match here wins over any marker above. These read like a
# transport failure but stay pinned open for hours, so no bounded ladder
# outlasts them: a codex usage limit states its own reset time ("try again at
# Sep 15th, 2026 1:25 AM"). Retrying one only spends attempts to fail anyway.
_PERSISTENT_TURN_MARKERS = (
    "usage limit",
    "purchase more credits",
    "quota exceeded",
)


def _is_transient_turn_failure(text):
    """Whether a CLI failure message names a cause a retry could plausibly clear."""
    if not text:
        return False
    lowered = str(text).lower()
    if any(marker in lowered for marker in _PERSISTENT_TURN_MARKERS):
        return False
    return any(marker in lowered for marker in _TRANSIENT_TURN_MARKERS)


def _is_codex_config_error(text):
    """Whether a codex RPC error is the app-server failing to load its config.

    The app-server reports this as JSON-RPC -32600 and stays wedged on it for
    every later turn, so it is the signal to respawn rather than to retry.
    """
    lowered = str(text or "").lower()
    return "-32600" in lowered and "failed to load configuration" in lowered


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


_MISSING_TOOL_INPUT = object()


def _tool_event_input(event):
    for key in ("args", "input"):
        if key in event:
            return event[key]
    if "arguments" not in event:
        return _MISSING_TOOL_INPUT
    value = event["arguments"]
    if not isinstance(value, str):
        return _MISSING_TOOL_INPUT
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return _MISSING_TOOL_INPUT


def _set_activity_input(item, name, value):
    if value is _MISSING_TOOL_INPUT or value is None:
        return
    if name == "edit":
        item["file_path"] = _input_value(value, "path")
    elif name == "write":
        item["file_path"] = _input_value(value, "path")
    elif name == "bash":
        if isinstance(value, dict) and isinstance(value.get("command"), str):
            item["command"] = value["command"]
    else:
        item["input"] = _bounded_tool_input(value)


def _new_tool_activity(name, value):
    normalized_name = name.lower() if isinstance(name, str) else name
    if normalized_name in ("edit", "write", "bash"):
        item = {"type": normalized_name}
    else:
        item = {"type": "tool_use", "name": name}
    _set_activity_input(item, normalized_name, value)
    return item, normalized_name


def activity_from_events(events):
    activity = []
    tools_by_id = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        if event_type in ("tool_start", "tool_execution_start"):
            name = event.get("toolName") or event.get("tool_name")
            item, normalized_name = _new_tool_activity(name, _tool_event_input(event))
            activity.append(item)
            tool_id = (
                event.get("toolCallId") or event.get("tool_call_id") or event.get("id")
            )
            if tool_id is not None:
                tools_by_id[str(tool_id)] = (item, normalized_name)
            continue
        if event_type in (
            "tool_update",
            "tool_end",
            "tool_execution_update",
            "tool_execution_end",
        ):
            tool_id = (
                event.get("toolCallId") or event.get("tool_call_id") or event.get("id")
            )
            known_tool = tools_by_id.get(str(tool_id)) if tool_id is not None else None
            if known_tool is not None:
                item, name = known_tool
                _set_activity_input(item, name, _tool_event_input(event))
            continue
        if event_type != "assistant":
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
                    item = {"type": "bash"}
                    if isinstance(value, dict) and isinstance(
                        value.get("command"), str
                    ):
                        item["command"] = value["command"]
                    activity.append(item)
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
                item = {"type": block_type.lower()}
                input_value = _input_value(value, key)
                if block_type != "Bash" or isinstance(input_value, str):
                    item[key] = input_value
                activity.append(item)
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
    def _copy(
        source,
        destination,
        direction=None,
        *,
        propagate_half_close=True,
        last_activity=None,
    ):
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
                if last_activity is not None:
                    last_activity[0] = time.monotonic()
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
            # The one-line preamble is the host lane's first byte evidence for
            # the guest-selected destination. It is the only thing this
            # forwarder ever writes on the guest's behalf.
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
            # response ends via flush-pkt). HTTPS CONNECT keeps propagating the
            # half-close. Both transports use the response-idle cleanup below
            # once either copy direction has completed.
            half_close_upstream = not host_port.endswith(":" + GIT_DAEMON_PORT)
            last_activity = [time.monotonic()]
            copies = [
                threading.Thread(
                    target=self._copy,
                    args=(client, upstream, "up"),
                    kwargs={
                        "propagate_half_close": half_close_upstream,
                        "last_activity": last_activity,
                    },
                    daemon=True,
                ),
                threading.Thread(
                    target=self._copy,
                    args=(upstream, client, "down"),
                    kwargs={"last_activity": last_activity},
                    daemon=True,
                ),
            ]
            for copy_thread in copies:
                copy_thread.start()
            completion_observed = False
            while any(copy_thread.is_alive() for copy_thread in copies):
                for copy_thread in copies:
                    copy_thread.join(timeout=EGRESS_TUNNEL_JOIN_POLL_SECONDS)
                alive = [copy_thread.is_alive() for copy_thread in copies]
                if all(alive):
                    continue
                if not any(alive):
                    break
                if not completion_observed:
                    # EOF in either direction marks the tunnel as completing,
                    # not immediately complete. Let the remaining pump finish
                    # while it continues to deliver bytes.
                    completion_observed = True
                    last_activity[0] = time.monotonic()
                    continue
                if (
                    time.monotonic() - last_activity[0]
                    < EGRESS_TUNNEL_COMPLETION_IDLE_CLOSE_SECONDS
                ):
                    continue
                try:
                    upstream.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                # A real socket wakes immediately on SHUT_RDWR. Keep these
                # joins bounded too so a broken socket implementation cannot
                # recreate the lifetime leak in the lifecycle owner.
                for copy_thread in copies:
                    copy_thread.join(timeout=EGRESS_TUNNEL_JOIN_POLL_SECONDS)
                break
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


def _valid_result_receipt(receipt):
    """Validate per-invocation metadata before any model work starts."""
    return (
        isinstance(receipt, dict)
        and set(receipt) == {"id", "token"}
        and isinstance(receipt["id"], str)
        and re.fullmatch(r"[0-9a-f]{32}", receipt["id"]) is not None
        and isinstance(receipt["token"], str)
        and re.fullmatch(r"[A-Za-z0-9_-]{43,128}", receipt["token"]) is not None
    )


class _ResultReceiptNoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A receipt bearer is valid only at the configured callback origin.
        return None


def _emit_result_receipt_failure(reason):
    """Only callers' fixed reason codes belong in this diagnostic."""
    try:
        sys.stderr.write(f"ember-claude-shim: result-receipt failed reason={reason}\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001, S110 - diagnostics cannot fail the turn.
        pass


def _result_receipt_attempt(opener, url, data, receipt, digest):
    response = None
    try:
        request = urllib.request.Request(
            url,
            data=data,
            headers={
                "Authorization": f"Bearer {receipt['token']}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        response = opener.open(request, timeout=RESULT_RECEIPT_TIMEOUT_SECONDS)
        if response.status != 200:
            return "http_status"
        ack_data = response.read(MAX_RESULT_RECEIPT_ACK_BYTES + 1)
        if len(ack_data) > MAX_RESULT_RECEIPT_ACK_BYTES:
            return "ack_too_large"
        ack = json.loads(ack_data)
        if (
            isinstance(ack, dict)
            and ack.get("receipt_id") == receipt["id"]
            and ack.get("result_sha256") == digest
        ):
            return "acknowledged"
        return "ack_mismatch"
    except urllib.error.HTTPError as exc:
        # HTTPError owns the response stream too. Do not read or log it.
        response = exc
        return "http_status"
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "ack_invalid"
    except Exception:  # noqa: BLE001 - preserve the original synchronous response.
        return "transport_error"
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:  # noqa: BLE001, S110 - cleanup is best effort.
                pass


def _publish_result_receipt(receipt, record):
    """Capture the complete native record before attempting the vsock response.

    This optional callback neither stops the guest nor makes a turn sendable.
    Failure leaves the original response available. Without an acknowledged
    callback, losing that response can still lose the completed native record.
    """
    try:
        url = os.environ.get("EMBER_PROGRESS_URL", "").strip()
        parsed = urllib.parse.urlsplit(url)
        if not (
            parsed.scheme in ("http", "https")
            and parsed.hostname
            and (parsed.port is None or parsed.port > 0)
            and parsed.username is None
            and parsed.password is None
            and not any(character.isspace() for character in url)
        ):
            _emit_result_receipt_failure("endpoint_unavailable")
            return False
        url = urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, "/ingest/results/" + receipt["id"], "", "")
        )
        data = json.dumps(record, separators=(",", ":")).encode("utf-8")
        if len(data) > MAX_RESULT_RECEIPT_BYTES:
            _emit_result_receipt_failure("body_too_large")
            return False
        digest = hashlib.sha256(data).hexdigest()
        egress_port = int(os.environ.get(EGRESS_PORT_ENV, str(DEFAULT_EGRESS_PORT)))
        if not 0 < egress_port <= 65535:
            raise ValueError("invalid egress port")
        proxy_url = f"http://{EGRESS_LOCALHOST}:{egress_port}"
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}),
            _ResultReceiptNoRedirect(),
        )
    except Exception:  # noqa: BLE001 - preserve the original synchronous response.
        _emit_result_receipt_failure("preparation_failed")
        return False

    # Drain flush is bounded by EMBERVM_SESSION_DRAIN_FLUSH_MS (default 60000)
    # less the shim reserve. Optional receipts must not hold it for retries.
    draining = record.get("terminal_reason") == "interrupted_for_drain"
    attempts = 1 if draining else RESULT_RECEIPT_ATTEMPTS
    callback_timeout = (
        min(1.0, RESULT_RECEIPT_TIMEOUT_SECONDS)
        if draining
        else RESULT_RECEIPT_TIMEOUT_SECONDS
    )
    for attempt in range(attempts):
        results = []

        def send(results=results):
            results.append(_result_receipt_attempt(opener, url, data, receipt, digest))

        try:
            worker = threading.Thread(target=send, daemon=True)
            worker.start()
            worker.join(timeout=callback_timeout)
        except Exception:  # noqa: BLE001 - thread startup cannot fail the turn.
            _emit_result_receipt_failure("worker_failed")
            return False
        if worker.is_alive():
            # urllib's socket timeout does not bound DNS or trickled headers.
            # Stop retrying after this wall deadline, leaving at most this one
            # callback running. It may complete late; no cancellation is implied.
            _emit_result_receipt_failure("deadline_exceeded")
            return False
        reason = results[0] if results else "worker_failed"
        if reason == "acknowledged":
            return True
        if attempt + 1 < attempts:
            time.sleep(RESULT_RECEIPT_BACKOFF_SECONDS)
    _emit_result_receipt_failure(reason)
    return False


class _ProgressPusher:
    """Fire-and-forget progress pusher: latest-text slot, throttled, exceptions swallowed."""

    def __init__(self, progress_token):
        self.progress_token = progress_token
        self.url = None
        url = os.environ.get("EMBER_PROGRESS_URL", "").strip()
        try:
            parsed = urllib.parse.urlsplit(url)
            if (
                parsed.scheme in ("http", "https")
                and parsed.hostname
                and (parsed.port is None or parsed.port > 0)
                and not any(character.isspace() for character in url)
            ):
                self.url = url
        except ValueError:
            # Invalid boot configuration disables this optional callback.
            pass
        self.latest_text = ""
        self.latest_activities = []
        self.last_push_time = -1.0
        self.thread = None
        self.last_sent_text = ""
        self.last_sent_activities = []

    def push(self, text, activities=None):
        """Update the latest slot and trigger a push if throttle allows."""
        if self.url is None:
            return
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
        if self.url is None:
            return
        try:
            egress_port = int(os.environ.get(EGRESS_PORT_ENV, str(DEFAULT_EGRESS_PORT)))
            proxy_url = "http://%s:%s" % (EGRESS_LOCALHOST, egress_port)
            proxy_handler = urllib.request.ProxyHandler(
                {"http": proxy_url, "https": proxy_url}
            )
            opener = urllib.request.build_opener(proxy_handler)
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
                self.url,
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


def _partial_turn(adapter, state):
    """Retain the stream prefix even when SIGINT ends the reader with EOF."""
    return {
        "result": state.get("result_text")
        or (
            state.get("accumulated_text", "") + state.get("current_message_buffer", "")
        ),
        "session_id": getattr(adapter, "session_id", None)
        or next(
            (
                event["session_id"]
                for event in reversed(state.get("events", []))
                if isinstance(event.get("session_id"), str) and event["session_id"]
            ),
            None,
        )
        or state.get("session_id"),
        "usage": state.get("usage", {}),
        "activities": state.get("cached_activities", []),
        "events": state.get("events", []),
    }


class ClaudeProcess:
    """Own the CLI and serialize turns sent through its JSONL stream."""

    # Set by ProcessManager.turn the first time a turn names a repo, and sticky
    # for the lineage because the repo is fixed at session create. A class
    # attribute rather than an __init__ assignment so prewarm, which spawns
    # before any turn and legitimately has no checkout, reads the default.
    requires_git_checkout = False

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
        if not _workspace_ready_for_spawn(self.workspace, self.requires_git_checkout):
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
        agent_mcp_url = os.environ.get(AGENT_MCP_URL_ENV)
        agent_mcp_configured = False
        if agent_mcp_url and _agent_mcp_endpoint_alive(agent_mcp_url):
            _validate_claude_mcp_config_dir()
            mcp_config = {
                "mcpServers": {"agents": {"type": "http", "url": agent_mcp_url}}
            }
            _write_read_only_file(
                CLAUDE_MCP_CONFIG_PATH,
                json.dumps(mcp_config),
            )
            command.extend(
                ["--mcp-config", CLAUDE_MCP_CONFIG_PATH, "--strict-mcp-config"]
            )
            agent_mcp_configured = True
        elif agent_mcp_url:
            sys.stderr.write(
                "ember-claude-shim: warning: agent MCP endpoint %s is not "
                "reachable: initialize probe failed or timed out after 3.0 seconds\n"
                % agent_mcp_url
            )
            sys.stderr.flush()
        command.extend(
            [
                "--append-system-prompt",
                compose_system_prompt(
                    system_prompt, agent_mcp_configured=agent_mcp_configured
                ),
            ]
        )
        child_env = os.environ.copy()
        child_env.update(egress_proxy_env())
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
        else:
            park_grace_seconds = _read_park_grace_seconds()
            time.sleep(park_grace_seconds)
            code = process.poll()
            if code is not None:
                self._close_process(kill=False)
                raise StartupError(
                    self._assemble_error_with_rings(
                        "claude exited during the %s second park grace, exit code %s"
                        % (park_grace_seconds, code)
                    )
                )
            return
        # Note: unparseable-line stderr writes are synchronous on the read thread
        # and consume the init budget. This is fine for tens-of-lines Bun panics
        # but could turn thousands-of-lines dumps into a generic init timeout.
        # Resolve the environment at spawn time so callers and tests that set
        # EMBER_INIT_READ_TIMEOUT after module import still affect lazy starts.
        init_read_timeout = (
            _read_init_timeout() if init_timeout is None else init_timeout
        )
        init_deadline = time.monotonic() + init_read_timeout
        while True:
            try:
                remaining = init_deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                raw = self._read_output(process, remaining)
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
                if event.get("mcp_servers") is not None:
                    # Same visibility as the codex startup-status line in
                    # CodexProcess._handle_server_request.
                    sys.stderr.write(
                        "ember-claude-shim: claude mcp_servers %s\n"
                        % json.dumps(event.get("mcp_servers"))
                    )
                    sys.stderr.flush()
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
                    if (
                        parked_adoption
                        and not session_was_bound
                        and not getattr(self, "_interrupt_requested", False)
                    ):
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
                    if (
                        parked_adoption
                        and not session_was_bound
                        and not getattr(self, "_interrupt_requested", False)
                    ):
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
                                if (
                                    parked_adoption
                                    and not session_was_bound
                                    and not getattr(self, "_interrupt_requested", False)
                                ):
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
                        record["model"] = self.model or model
                        record["voice"] = voice_summary(event.get("result", ""))
                        record["activities"] = activity_from_events(events)
                        _emit_elapsed(
                            "model", getattr(self, "_turn_timing_model_start", None)
                        )
                        return record
            except Exception:
                if (
                    parked_adoption
                    and not session_was_bound
                    and not getattr(self, "_interrupt_requested", False)
                ):
                    self.session_id = None
                raise
            finally:
                if getattr(self, "_interrupt_requested", False):
                    self._partial_turn = _partial_turn(self, locals())
                if pusher and not getattr(self, "_drain_requested", False):
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

    # See ClaudeProcess.requires_git_checkout.
    requires_git_checkout = False

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
        self._agent_mcp_configured = False
        # Spawn-time workspace identity, read by the manager's remediation to
        # detect a volume mount hiding the tmpfs workspace this process was
        # spawned against. Same contract as ClaudeProcess.
        self._process_workspace_identity = _workspace_identity(self.workspace)

    def ready(self):
        with self.process_lock:
            return os.path.isdir(self.workspace)

    def _child_env(self):
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
        child_env.update(egress_proxy_env())
        child_env["CODEX_HOME"] = codex_home
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

    def _write_model_config(self, codex_home):
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

[projects.%s]
trust_level = "trusted"

# Codex 0.146.0 binary inspection exposes [tools].web_search, while
# web_search_request is deprecated because web search is enabled by default.
[tools]
web_search = true

[model_providers.ember-openai]
name = "ember-openai"
base_url = %s
wire_api = "responses"
""" % (
            json.dumps(base_url),
            json.dumps(self.workspace),
            json.dumps(provider_base_url),
        )
        agent_mcp_url = os.environ.get(AGENT_MCP_URL_ENV)
        if agent_mcp_url:
            config += """
# Written by the shim (#5569). The egress sidecar attaches the credential, so
# the guest holds none; the file is regenerated on every spawn.
[mcp_servers.agents]
url = %s
""" % json.dumps(agent_mcp_url)
        _write_read_only_file(os.path.join(codex_home, "config.toml"), config)
        return bool(agent_mcp_url)

    def _spawn(self):
        if not _workspace_ready_for_spawn(self.workspace, self.requires_git_checkout):
            raise StartupError("workspace does not exist: %s" % self.workspace)
        child_env = self._child_env()
        self._write_auth_json(child_env["CODEX_HOME"])
        self._agent_mcp_configured = self._write_model_config(child_env["CODEX_HOME"])
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
            self._process_workspace_identity = _workspace_identity(self.workspace)
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

    def _request(self, method, params, timeout=INIT_READ_TIMEOUT):
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
        if event.get("method") == "mcpServer/startupStatus/updated":
            # A dead MCP lane is otherwise indistinguishable from a live one
            # while the prompt asserts the tools exist; stderr lands in the
            # brick's noded logs.
            sys.stderr.write(
                "ember-claude-shim: codex mcp %s\n" % json.dumps(event.get("params"))
            )
            sys.stderr.flush()
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
            "developerInstructions": compose_system_prompt(
                system_prompt,
                agent_mcp_configured=self._agent_mcp_configured,
            ),
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
            # A relit VM can mount a workspace volume that ALREADY carries a
            # .codex directory, which satisfies the isdir test below while the
            # live app-server still points at the pre-mount inode. Compare the
            # workspace identity the way ClaudeProcess.turn does (cwd_changed)
            # so a REPLACED workspace respawns as well as a vanished one;
            # _spawn records the identity but nothing here used to read it.
            workspace_identity = _workspace_identity(self.workspace)
            workspace_replaced = (
                self._process_workspace_identity is not None
                and workspace_identity is not None
                and workspace_identity != self._process_workspace_identity
            )
            if (
                process is not None
                and process.poll() is None
                and (
                    not os.path.isdir(os.path.join(self.workspace, ".codex"))
                    or workspace_replaced
                )
            ):
                # The live app-server's CODEX_HOME is gone or has been swapped
                # under it: this VM was relit with a fresh workspace volume
                # mounted under the same path, so the parked (usually
                # prewarmed) server's spawn-time state predates the mount. Its
                # next thread/start or resume fails "-32600: failed to load
                # configuration" (every first codex turn on the GKE hub,
                # 2026-09-01). Respawn against the current workspace instead; a
                # cold spawn is ~250ms against the 1-5s API leg, and _spawn
                # rewrites auth.json and config.toml.
                self._close_process(kill=False)
                process = None
            if process is None or process.poll() is not None:
                self._close_process(kill=False)
                process = self._spawn()
                requested_session = session_id or self.session_id
                cli_ready_path = "lazy_spawn"

            def _bind_thread(path):
                """Resume or start the app-server thread; returns cli_ready_path."""
                if requested_session and (
                    requested_session not in self._server_threads
                    or requested_session != self.session_id
                ):
                    # Resume repeats the developer instructions so a restarted
                    # app-server thread receives the same prompt without duplication.
                    self._resume(requested_session, system_prompt=system_prompt)
                    if path is None and process_was_unbound:
                        return "adopt"
                elif not requested_session:
                    result = self._request(
                        "thread/start",
                        {
                            "cwd": self.workspace,
                            "approvalPolicy": "never",
                            "sandbox": "danger-full-access",
                            "developerInstructions": compose_system_prompt(
                                system_prompt,
                                agent_mcp_configured=self._agent_mcp_configured,
                            ),
                        },
                    )
                    thread = (
                        result.get("thread", {}) if isinstance(result, dict) else {}
                    )
                    self.session_id = (
                        thread.get("id") if isinstance(thread, dict) else None
                    )
                    self._server_threads.add(self.session_id)
                    if path is None and process_was_unbound:
                        return "adopt"
                return path

            try:
                cli_ready_path = _bind_thread(cli_ready_path)
            except RuntimeError as error:
                if not _is_codex_config_error(error):
                    raise
                # The guards above did not catch this swap: an identity that
                # did not move, or a .codex that exists but is not the one this
                # server loaded. The app-server stays wedged on stale config
                # for every later turn, so bind once against a fresh one rather
                # than failing every turn the way the hub did for 16 turns over
                # two weeks. _spawn resets _server_threads, so the retry
                # resumes the thread rather than skipping the bind.
                #
                # This covers a BIND-time -32600, which is the observed shape
                # ("every first codex turn on the GKE hub"). An already-bound
                # server that only fails at turn/start is not caught here and
                # still surfaces as a bare 422; it has not been seen in prod.
                sys.stderr.write(
                    "ember-claude-shim: codex config error, respawning: %s\n" % error
                )
                sys.stderr.flush()
                self._close_process(kill=False)
                process = self._spawn()
                requested_session = session_id or self.session_id
                cli_ready_path = _bind_thread("lazy_spawn")
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
                        response = event.get("result", {})
                        response_turn = response.get("turn", {})
                        response_turn_id = response_turn.get("id")
                        if isinstance(response_turn_id, str) and response_turn_id:
                            # TurnStartResponse names the turn created by this
                            # request. Capture it even if its turn/started
                            # notification is ordered before the response.
                            self._turn_id = response_turn_id
                        continue
                    self._handle_server_request(event)
                    event_type = event.get("method")
                    params = event.get("params", {})
                    if event_type == "turn/started":
                        started_id = params.get("turn", {}).get("id")
                        if (
                            params.get("threadId") != self.session_id
                            or not isinstance(started_id, str)
                            or not started_id
                            or self._turn_id not in (None, started_id)
                        ):
                            continue
                        # The server can emit turn/started before its response.
                        self._turn_id = started_id
                    elif event_type in (
                        "turn/completed",
                        "item/started",
                        "item/completed",
                        "item/agentMessage/delta",
                        "thread/tokenUsage/updated",
                    ):
                        event_turn_id = (
                            params.get("turn", {}).get("id")
                            if event_type == "turn/completed"
                            else params.get("turnId")
                        )
                        # One app-server carries parent and child threads.
                        # A child's result must not finish or overwrite the
                        # parent before it writes its factory artifact.
                        if (
                            not self._turn_id
                            or params.get("threadId") != self.session_id
                            or event_turn_id != self._turn_id
                        ):
                            continue
                    legacy_event = self._translate_activity_event(event)
                    events.append(legacy_event)
                    if event_type == "item/agentMessage/delta":
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
                            detail = "Codex turn failed: %s\n%s" % (
                                error,
                                _truncate_ring_for_error(self.stderr_lines),
                            )
                            # Classify on the provider's own message, never on
                            # the stderr ring: the ring carries up to five
                            # lines from earlier in the process's life, and a
                            # stale "connection reset" in it would mark a
                            # deterministic failure retryable.
                            if _is_transient_turn_failure(error):
                                raise TransientTurnError(detail)
                            raise RuntimeError(detail)
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
                            "model": model,
                            "usage": usage,
                            "voice": voice_summary(result_text),
                            "activities": activity_from_events(events),
                        }
            finally:
                if getattr(self, "_interrupt_requested", False):
                    self._partial_turn = _partial_turn(self, locals())
                if pusher and not getattr(self, "_drain_requested", False):
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


# Pi only. Pi trusts NODE_EXTRA_CA_CERTS, so it uses the sidecar's CA-backed
# HTTPS interception lane. Muse cannot (see MUSE_BASE_URL below).
PI_BASE_URL = "https://api.meta.ai/v1"

# Muse ignores every CA environment variable and the system trust store, as
# verified against 1.0.3-R2198.1 on 2026-09-10. Its bundled roots cannot trust
# the HTTPS interception lane, so plaintext through the sidecar is the only
# working path. The sidecar injects the real key, leaving no guest credential.
MUSE_BASE_URL = "http://api.meta.ai/v1"


MUSE_USAGE_TIMEOUT_SECONDS = 5.0
MUSE_USAGE_MAX_PAGES = 20
MUSE_USAGE_PAGE_SIZE = 100
MUSE_USAGE_MAX_BYTES = 8 * 1024 * 1024


def _muse_tool_events_from_view_events(events, session_id, command_id):
    """Fold authoritative MSP tool-call revisions into one event per item."""
    states = []
    states_by_identity = {}
    for position, event in enumerate(events):
        if not isinstance(event, dict):
            continue
        if event.get("method") not in (
            "item/started",
            "item/updated",
            "item/completed",
        ):
            continue
        params = event.get("params")
        if not isinstance(params, dict):
            continue
        item = params.get("item")
        if (
            params.get("sessionId") != session_id
            or not isinstance(item, dict)
            or item.get("kind") != "toolCall"
            or item.get("turnId") != command_id
        ):
            continue
        identities = [
            identity
            for identity in (item.get("itemId"), item.get("callId"))
            if isinstance(identity, str) and identity
        ]
        if not identities:
            continue
        state = next(
            (
                states_by_identity[identity]
                for identity in identities
                if identity in states_by_identity
            ),
            None,
        )
        if state is None:
            state = {"identities": [], "revisions": []}
            states.append(state)
        for identity in identities:
            if identity not in state["identities"]:
                state["identities"].append(identity)
            states_by_identity[identity] = state
        state["revisions"].append((position, item))

    tool_events = []
    for state in states:
        revisions = sorted(
            state["revisions"],
            key=lambda entry: (
                entry[1].get("revision")
                if type(entry[1].get("revision")) is int
                else entry[0]
            ),
        )
        tool_name = None
        arguments = _MISSING_TOOL_INPUT
        item_id = None
        call_id = None
        for _position, item in revisions:
            candidate_name = item.get("toolName")
            if isinstance(candidate_name, str) and candidate_name:
                tool_name = candidate_name
            candidate_item_id = item.get("itemId")
            if isinstance(candidate_item_id, str) and candidate_item_id:
                item_id = candidate_item_id
            candidate_call_id = item.get("callId")
            if isinstance(candidate_call_id, str) and candidate_call_id:
                call_id = candidate_call_id
            candidate_arguments = item.get("arguments", _MISSING_TOOL_INPUT)
            if isinstance(candidate_arguments, str):
                try:
                    json.loads(candidate_arguments)
                except (TypeError, ValueError):
                    continue
                arguments = candidate_arguments
        normalized = {
            "type": "tool_execution_start",
            "toolCallId": item_id or call_id,
        }
        if item_id is not None:
            normalized["itemId"] = item_id
        if call_id is not None:
            normalized["callId"] = call_id
        if tool_name is not None:
            normalized["toolName"] = tool_name
        if arguments is not _MISSING_TOOL_INPUT:
            normalized["arguments"] = arguments
        tool_events.append(normalized)
    return tool_events


def _muse_tool_event_identities(event):
    return {
        value
        for value in (
            event.get("toolCallId"),
            event.get("tool_call_id"),
            event.get("itemId"),
            event.get("callId"),
            event.get("id"),
        )
        if isinstance(value, str) and value
    }


def _muse_reconciled_activities(live_events, retained_events):
    """Enrich matching live Bash tools and preserve every unmatched tool."""
    retained_by_identity = {}
    for index, event in enumerate(retained_events):
        for identity in _muse_tool_event_identities(event):
            retained_by_identity.setdefault(identity, index)

    reconciled = []
    used_retained = set()
    for live_event in live_events:
        match = next(
            (
                retained_by_identity[identity]
                for identity in _muse_tool_event_identities(live_event)
                if identity in retained_by_identity
                and retained_by_identity[identity] not in used_retained
            ),
            None,
        )
        if match is None:
            reconciled.append(live_event)
            continue
        retained_event = retained_events[match]
        used_retained.add(match)
        merged = dict(live_event)
        retained_name = retained_event.get("toolName")
        if isinstance(retained_name, str) and retained_name:
            merged["toolName"] = retained_name
        if "arguments" in retained_event:
            merged["arguments"] = retained_event["arguments"]
        reconciled.append(merged)

    for index, retained_event in enumerate(retained_events):
        if index in used_retained:
            continue
        reconciled.append(dict(retained_event))
    return activity_from_events(reconciled)


def _muse_activities_from_view_events(events, session_id, command_id):
    return _muse_reconciled_activities(
        [], _muse_tool_events_from_view_events(events, session_id, command_id)
    )


def _muse_live_tool_events(tasks_by_id):
    events = []
    for task_id, task in tasks_by_id.items():
        task_kind = task.get("task_kind")
        if not isinstance(task_kind, str) or not task_kind.startswith("tool."):
            continue
        event = {
            "type": "tool_execution_start",
            "toolCallId": task_id,
            "toolName": task_kind[len("tool.") :],
        }
        idempotency_key = task.get("idempotency_key")
        if isinstance(idempotency_key, str) and idempotency_key.startswith("tool:"):
            call_id = idempotency_key[len("tool:") :]
            if call_id:
                event["callId"] = call_id
        events.append(event)
    return events


def _muse_usage_projection(events, session_id, command_id, expected_completions):
    """Project one complete retained turn, with native counter provenance.

    MSP emits per-model usage and session cumulative counters separately. Sum
    only unique per-model observations; a missing observation is not zero usage.
    """
    meta = {
        "source": "msp_retained_session_view",
        "scope": "turn",
        "coverage": "reported_model_completions",
        "status": "unavailable",
        "reason": "missing_turn_boundary",
        "session_id": session_id,
        "command_id": command_id,
        "cost_status": "not_reported",
        "observed_model_completions": (
            expected_completions
            if type(expected_completions) is int and expected_completions >= 0
            else None
        ),
    }
    result = {"muse": meta}

    def integer(value):
        if type(value) is not int or value < 0:
            raise ValueError("invalid usage integer")
        return value

    def identity(value):
        if not isinstance(value, str) or not value or len(value) > 512:
            raise ValueError("invalid usage identity")
        return value

    def source_range(value):
        projected = {
            "stream": {key: identity(value["stream"][key]) for key in ("kind", "id")}
        }
        for key in ("first", "last"):
            projected[key] = {
                "id": identity(value[key]["id"]),
                "sequence": integer(value[key]["sequence"]),
            }
        if projected["first"]["sequence"] > projected["last"]["sequence"]:
            raise ValueError("invalid usage source range")
        return projected

    raw_fields = ("inputTokens", "outputTokens", "cachedTokens", "reasoningTokens")
    cache_fields = ("cacheReadTokens", "cacheWriteTokens")

    def raw_usage(value):
        raw = {key: integer(value[key]) for key in raw_fields}
        raw.update({key: integer(value[key]) for key in cache_fields if key in value})
        return raw

    try:
        identity(session_id)
        identity(command_id)
        starts = [
            event["params"]
            for event in events
            if event.get("method") == "turn/started"
            and event.get("params", {}).get("sessionId") == session_id
            and event.get("params", {}).get("commandId") == command_id
        ]
        if not starts:
            return result
        turn_ids = {identity(start["turnId"]) for start in starts}
        if len(turn_ids) != 1:
            raise ValueError("ambiguous turn identity")
        turn_id = next(iter(turn_ids))
        meta["turn_id"] = turn_id
        selected = []
        by_cursor = {}
        by_source = {}
        for event in events:
            method, params = event.get("method"), event.get("params", {})
            if (
                method not in ("turn/started", "turn/completed", "session/tokenUsage")
                or params.get("sessionId") != session_id
                or params.get("turnId") != turn_id
            ):
                continue
            projected = {
                "sessionId": session_id,
                "turnId": turn_id,
                "viewCursor": identity(params["viewCursor"]),
                "sourceRange": source_range(params["sourceRange"]),
            }
            if method == "turn/started":
                if params.get("commandId") != command_id:
                    raise ValueError("conflicting command identity")
                projected["commandId"] = command_id
            if "usage" in params:
                projected["usage"] = raw_usage(params["usage"])
            if method == "session/tokenUsage":
                if "usage" not in projected:
                    raise ValueError("missing raw counters")
                for key in ("promptTokens", "totalTokens"):
                    projected[key] = integer(params[key])
                projected["cumulative"] = {
                    key: integer(params["cumulative"][key])
                    for key in ("promptTokens", "outputTokens", "totalTokens")
                }
                if "modelId" in params and params["modelId"] is not None:
                    projected["modelId"] = identity(params["modelId"])
            for key in ("durationMs", "timeToFirstTokenMs"):
                if key in params:
                    projected[key] = integer(params[key])
            if method == "turn/completed":
                projected["terminal"] = identity(params["terminal"])
            # A native view cursor and durable source identify a single event.
            # Compare the allowlisted payload too, so conflicting replay fails.
            signature = json.dumps(
                {
                    "method": method,
                    "params": {k: v for k, v in projected.items() if k != "viewCursor"},
                },
                sort_keys=True,
            )
            cursor_key = projected["viewCursor"]
            source_key = (method, json.dumps(projected["sourceRange"], sort_keys=True))
            for mapping, key in ((by_cursor, cursor_key), (by_source, source_key)):
                if key in mapping and mapping[key] != signature:
                    raise ValueError("conflicting replay")
            duplicate = cursor_key in by_cursor or source_key in by_source
            by_cursor[cursor_key] = by_source[source_key] = signature
            if not duplicate:
                selected.append((method, projected))
        if (
            not selected
            or selected[0][0] != "turn/started"
            or selected[-1][0] != "turn/completed"
            or any(method != "session/tokenUsage" for method, _ in selected[1:-1])
        ):
            return result
        meta["terminal"] = selected[-1][1]
        observations = [params for _, params in selected[1:-1]]
        meta["observations"] = observations
        meta["reported_usage_completions"] = len(observations)
        if not observations:
            meta["reason"] = "usage_not_reported"
            return result
        previous = None
        for observation in observations:
            counts = {
                "promptTokens": observation["promptTokens"],
                "outputTokens": observation["usage"]["outputTokens"],
                "totalTokens": observation["totalTokens"],
            }
            cumulative = observation["cumulative"]
            for values in (counts, cumulative):
                if (
                    values["totalTokens"]
                    != values["promptTokens"] + values["outputTokens"]
                ):
                    raise ValueError("inconsistent counted-once totals")
            for key, count in counts.items():
                if previous is None:
                    if cumulative[key] < count:
                        raise ValueError("cumulative below observation")
                elif cumulative[key] - previous[key] != count:
                    raise ValueError("missing or conflicting cumulative usage")
            previous = cumulative
        if type(expected_completions) is not int or expected_completions != len(
            observations
        ):
            meta.update(status="incomplete", reason="completion_count_mismatch")
            return result
        totals = {
            key: sum(observation["usage"][key] for observation in observations)
            for key in raw_fields + cache_fields
            if all(key in observation["usage"] for observation in observations)
        }
        terminal_usage = meta["terminal"].get("usage", {})
        if any(totals.get(key) != value for key, value in terminal_usage.items()):
            raise ValueError("terminal aggregate mismatch")
        names = {
            "inputTokens": "input_tokens",
            "outputTokens": "output_tokens",
            "cachedTokens": "cached_tokens",
            "reasoningTokens": "reasoning_tokens",
            "cacheReadTokens": "cache_read_input_tokens",
            "cacheWriteTokens": "cache_creation_input_tokens",
        }
        result.update({names[key]: value for key, value in totals.items()})
        result["prompt_tokens"] = sum(row["promptTokens"] for row in observations)
        result["total_tokens"] = sum(row["totalTokens"] for row in observations)
        if all("durationMs" in row for row in observations):
            result["model_ms"] = sum(row["durationMs"] for row in observations)
        meta.update(status="complete", reason="reported_usage")
    except (ValueError, TypeError, KeyError, AttributeError):
        meta.update(status="unavailable", reason="invalid_evidence")
    return result


class _MuseUsageReader:
    """Bounded, read-only MSP client. No session attach or model submission."""

    def __init__(self, process):
        self.process = process
        self.deadline = time.monotonic() + MUSE_USAGE_TIMEOUT_SECONDS
        self.buffer = bytearray()
        self.bytes_read = 0
        self.request_id = 0
        self.selector = selectors.DefaultSelector()
        self.selector.register(process.stdout, selectors.EVENT_READ)

    def request(self, method, params, notification=False):
        if method not in ("initialize", "initialized", "session/read", "view/page"):
            raise ValueError("usage reader method is not read-only")
        self.request_id += 1
        request = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            request["id"] = self.request_id
        # Requests are small metadata-only frames, below the pipe capacity.
        self.process.stdin.write(_json_line(request))
        self.process.stdin.flush()
        if notification:
            return None
        while True:
            if time.monotonic() >= self.deadline:
                raise TimeoutError("usage metadata deadline")
            while b"\n" in self.buffer:
                line, _, remainder = self.buffer.partition(b"\n")
                self.buffer[:] = remainder
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("invalid MSP frame")
                if message.get("id") == self.request_id:
                    if "error" in message:
                        # Native errors can contain paths or content. Export no text.
                        raise ValueError("MSP metadata read failed")
                    return message["result"]
            remaining = self.deadline - time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                raise TimeoutError("usage metadata deadline")
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise ValueError("MSP metadata EOF")
            self.bytes_read += len(chunk)
            if self.bytes_read > MUSE_USAGE_MAX_BYTES:
                raise ValueError("usage metadata byte limit")
            self.buffer.extend(chunk)

    def close(self):
        self.selector.close()


class MuseProcess:
    """Run one Muse exec process per turn, retaining server-side session identity."""

    # See ClaudeProcess.requires_git_checkout.
    requires_git_checkout = False

    def __init__(self, workspace=None, executable="muse", state_workspace=None):
        self.workspace = workspace or os.environ.get(
            "EMBER_CLAUDE_WORKSPACE", DEFAULT_WORKSPACE
        )
        self.state_workspace = state_workspace or self.workspace
        self.executable = executable
        self.session_id = None
        self.process = None
        self.turn_lock = threading.Lock()
        self.process_lock = threading.Lock()
        self.stderr_lines = collections.deque(maxlen=5)
        self._stderr_thread = None
        self._stdout_queue = None
        self._prompt_file_path = None
        self._mcp_probe_cached = None
        self._retained_tool_events = None
        # Set on every spawn; None until the first turn resolves a model.
        self._model = None
        self._process_workspace_identity = _workspace_identity(self.workspace)

    def ready(self):
        with self.process_lock:
            return os.path.isdir(self.workspace)

    def _child_env(self):
        # Muse follows XDG paths, while the guest's $HOME is a read-only rootfs.
        # Keep both trees on the durable workspace so the late-bound session's
        # --session-id continuity survives bank and relight even though each
        # turn gets a fresh process.
        muse_home = os.path.join(self.workspace, ".muse")
        config_home = os.path.join(muse_home, "config")
        data_home = os.path.join(muse_home, "data")
        _ensure_cli_dir(config_home)
        _ensure_cli_dir(data_home)
        child_env = os.environ.copy()
        child_env.update(egress_proxy_env())
        child_env["XDG_CONFIG_HOME"] = config_home
        child_env["XDG_DATA_HOME"] = data_home
        return child_env

    def _write_settings_json(self, muse_settings_dir):
        """Write muse's settings.json, arming the agents MCP server when live.

        muse_settings_dir is the `muse` subdirectory INSIDE $XDG_CONFIG_HOME,
        because muse reads $XDG_CONFIG_HOME/muse/settings.json. Writing one
        level up produces no error and no warning: muse simply never finds the
        file and runs with no MCP server, so the segment is load bearing and
        the caller passes the full path rather than appending it here.

        mcp_servers is a MAP keyed by server name, never an array, and a
        wrongly shaped entry is dropped just as silently. `framing` is
        stdio-only and a non-default value on streamable_http fails
        validation, so it is never emitted.

        Only the in-cluster agents tier is ever declared, over
        streamable_http. MCP tools are NOT sandboxed: a stdio server would run
        as an ordinary child process outside muse's filesystem and network
        sandbox, while an HTTP connection through the egress sidecar stays
        inside the guest's egress policy. The Authorization value is a
        placeholder because the tier lists /mcp in injectAlwaysPaths and the
        sidecar substitutes the real token, so the guest holds no credential.

        mode "required" aborts the whole run when the server cannot start,
        which is the point: a muse guest must not silently proceed without the
        knowledge graph. That couples every turn to the tier being reachable,
        so the caller gates this on the same liveness probe the pi bridge
        uses and writes no mcp_servers key at all when it fails.

        Muse 1.0.3-R2198.1 was verified on 2026-09-10 to ignore every CA
        environment variable and the system trust store, trusting only its
        bundled roots. The endpoint pin must therefore match the plaintext
        --base-url routed through the sidecar, which injects the real key so
        the guest holds no credential. `auth` must stay "bearer": the
        api.meta.ai catalog entry has no injectAlwaysPaths, so the sidecar
        injects only when the guest's request already carries an
        Authorization header. Any auth mode that stops Muse sending one
        forwards the request uncredentialed and Meta answers 401, which reads
        like a bad META_SPARK_API_KEY rather than a settings change.

        Muse telemetry does not honor endpoint_transport and otherwise calls
        its bundled https://api.meta.ai telemetry endpoints directly. Disable
        it so automatic traffic stays on the sidecar's plaintext guest lane.
        """
        agent_mcp_url = os.environ.get(AGENT_MCP_URL_ENV)
        if not self._mcp_probe_cached:
            self._mcp_probe_cached = bool(agent_mcp_url) and _agent_mcp_endpoint_alive(
                agent_mcp_url
            )

        settings = {
            "schema_version": 1,
            "telemetry": {"enabled": False},
            "endpoint_transport": {
                "base_url": MUSE_BASE_URL,
                "auth": "bearer",
            },
        }
        # A successful probe is cached for the adapter lifetime. A failed probe
        # is retried next turn so a brief tier outage cannot disable knowledge
        # tools for the session's full lifetime. This work is inside
        # _spawn, so its latency is included in cli_ready. If a cached-positive
        # tier later goes away, required mode makes Muse abort loudly.
        if self._mcp_probe_cached:
            settings["mcp_servers"] = {
                "agents": {
                    "transport": "streamable_http",
                    "url": agent_mcp_url,
                    "headers": {"Authorization": "Bearer placeholder"},
                    "enabled": True,
                    "mode": "required",
                }
            }

        settings_path = os.path.join(muse_settings_dir, "settings.json")
        _write_read_only_file(settings_path, json.dumps(settings))

    def _spawn(self, prompt, model):
        if not _workspace_ready_for_spawn(self.workspace, self.requires_git_checkout):
            raise StartupError("workspace does not exist: %s" % self.workspace)
        model = MUSE_MODEL_ALIASES.get(model, model)
        model_name = MUSE_MODELS.get(model, MUSE_MODELS[DEFAULT_MUSE_MODEL])
        # The console renders whatever turn() reports, so it must be the
        # provider id actually sent (muse-spark-1.3-contributor), not the short
        # family name the caller asked for. Pi keeps the same value in
        # self._model for the same reason.
        self._model = model_name
        # The model-map test is the enforcement point. Keep this runtime check
        # as defense in depth against a future untested configuration path.
        if not model_name.endswith("-contributor"):
            raise ValueError(
                "muse model must use the contributor tier: %s" % model_name
            )
        # $XDG_CONFIG_HOME is <workspace>/.muse/config (see _child_env), and
        # muse reads its settings from the `muse` subdirectory beneath it.
        muse_settings_dir = os.path.join(self.workspace, ".muse", "config", "muse")
        _ensure_cli_dir(muse_settings_dir)
        self._write_settings_json(muse_settings_dir)
        # ProcessManager keeps state_workspace at /workspace while cwd moves to
        # /workspace/src after hydration. A prompt therefore cannot survive a
        # mid-turn death as an untracked file in the checkout's next diff.
        prompt_dir = os.path.join(self.state_workspace, ".muse", "prompts")
        _ensure_cli_dir(prompt_dir)
        prompt_file = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".muse-prompt-",
            dir=prompt_dir,
            delete=False,
        )
        prompt_file_path = prompt_file.name
        try:
            prompt_file.close()
            # NamedTemporaryFile starts at 0600. Reuse the configuration-file
            # writer so a root shim leaves a 0444 file the dropped CLI uid can
            # read, matching the Codex configuration path.
            _write_read_only_file(prompt_file_path, prompt)
        except Exception:
            prompt_file.close()
            try:
                os.unlink(prompt_file_path)
            except OSError:
                pass
            raise
        self._prompt_file_path = prompt_file_path
        command = [
            self.executable,
            "exec",
            "--json",
            "--session-id",
            self.session_id,
            "--model",
            model_name,
            "--base-url",
            MUSE_BASE_URL,
            "--api-key-stdin",
            "--approval-mode",
            "never",
            "--disable-sandbox",
            "--trust-workspace",
            "--no-foreign-personal-context",
            "--prompt-file",
            self._prompt_file_path,
        ]
        try:
            child_env = self._child_env()
            privilege_kwargs = _cli_privilege_kwargs()
            if child_env.get(MUSE_BINARY_PREFLIGHT_ENV, "").strip().lower() in (
                "1",
                "true",
                "yes",
                "on",
            ):
                _require_muse_executable(
                    self.executable,
                    child_env,
                    self.workspace,
                    privilege_kwargs,
                )
            process = subprocess.Popen(
                command,
                cwd=self.workspace,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=child_env,
                **privilege_kwargs,
            )
        except Exception:
            try:
                os.unlink(self._prompt_file_path)
            except OSError:
                pass
            self._prompt_file_path = None
            raise
        output_queue = queue.Queue()
        with self.process_lock:
            self.process = process
            self._stdout_queue = output_queue
            self._process_workspace_identity = _workspace_identity(self.workspace)
            _managed_child_pids.add(process.pid)
        threading.Thread(
            target=self._pump_stdout,
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
        # The egress sidecar injects the real credential. Muse insists on
        # reading a value before execution, so send an inert placeholder and
        # close stdin to let the one-shot command proceed.
        process.stdin.write(b"sk-noauth\n")
        process.stdin.close()
        return process

    @staticmethod
    def _pump_stdout(process, output_queue):
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
        while True:
            try:
                raw = output_queue.get(timeout=timeout)
            except queue.Empty as exc:
                raise TimeoutError from exc
            if raw is None:
                return None
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                # Muse writes its workspace banner to stdout before the JSONL
                # stream. It is diagnostic noise, not a failed turn.
                continue

    def _empty_stream_error(self, process):
        code = process.poll() if process else None
        if code is None and process:
            code = process.wait()
        if self._stderr_thread:
            self._stderr_thread.join(timeout=1)
        error_msg = "muse exited before run.terminal.completed, exit code %s" % code
        stderr = _truncate_ring_for_error(self.stderr_lines)
        if stderr:
            error_msg += "\nCLI stderr:\n%s" % stderr
        # Here stderr IS the only evidence of the cause: muse exits non-zero
        # with the reason on stderr and nothing on stdout, which is how
        # "failed to fetch model catalog" reached callers as a bare 422.
        if _is_transient_turn_failure(stderr):
            return TransientTurnError(error_msg)
        return RuntimeError(error_msg)

    def _collect_usage(self, command_id, expected_completions):
        self._retained_tool_events = None
        unavailable = _muse_usage_projection(
            [], self.session_id, command_id, expected_completions
        )
        if not isinstance(command_id, str) or not command_id:
            return unavailable
        process = None
        reader = None
        try:
            # exec retains logs already. Read after its exit so the durable
            # terminal has flushed, using the same XDG paths and CLI identity.
            process = subprocess.Popen(
                [self.executable, "serve", "--disable-write", "--disable-shell"],
                cwd=self.workspace,
                env=self._child_env(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                **_cli_privilege_kwargs(),
            )
            _managed_child_pids.add(process.pid)
            reader = _MuseUsageReader(process)
            reader.request(
                "initialize",
                {"clientInfo": {"name": "ember_muse_usage", "version": "1"}},
            )
            reader.request("initialized", {}, notification=True)
            reader.request(
                "session/read", {"sessionId": self.session_id, "excludeItems": True}
            )
            events = []
            cursor = None
            cursors = set()
            for _ in range(MUSE_USAGE_MAX_PAGES):
                params = {
                    "sessionId": self.session_id,
                    "limit": MUSE_USAGE_PAGE_SIZE,
                    "direction": "backward",
                }
                if cursor is not None:
                    params["cursor"] = cursor
                page = reader.request("view/page", params)
                # Tool-call items are authoritative for arguments that the live
                # task lifecycle deliberately omits. They stay in-process and
                # are projected separately from the public usage result.
                page_events = [
                    event
                    for event in page["events"]
                    if event.get("method")
                    in (
                        "turn/started",
                        "turn/completed",
                        "session/tokenUsage",
                        "item/started",
                        "item/updated",
                        "item/completed",
                    )
                ]
                events = page_events + events
                if any(
                    event.get("method") == "turn/started"
                    and event.get("params", {}).get("sessionId") == self.session_id
                    and event.get("params", {}).get("commandId") == command_id
                    for event in page_events
                ):
                    self._retained_tool_events = _muse_tool_events_from_view_events(
                        events, self.session_id, command_id
                    )
                    return _muse_usage_projection(
                        events, self.session_id, command_id, expected_completions
                    )
                cursor = page["nextCursor"]
                if cursor is None:
                    break
                if not isinstance(cursor, str) or cursor in cursors:
                    raise ValueError("non-progressing usage page")
                cursors.add(cursor)
            self._retained_tool_events = _muse_tool_events_from_view_events(
                events, self.session_id, command_id
            )
            return _muse_usage_projection(
                events, self.session_id, command_id, expected_completions
            )
        except Exception:
            unavailable["muse"]["reason"] = "collection_failed"
            return unavailable
        finally:
            try:
                if reader is not None:
                    try:
                        reader.close()
                    except Exception:
                        pass
                if process is not None:
                    stdin = getattr(process, "stdin", None)
                    if stdin is not None:
                        try:
                            stdin.close()
                        except OSError:
                            pass
                    try:
                        process.wait(timeout=1)
                    except Exception:
                        try:
                            if process.poll() is None:
                                process.kill()
                        except Exception:
                            pass
                        try:
                            process.wait(timeout=1)
                        except Exception:
                            pass
                    stdout = getattr(process, "stdout", None)
                    if stdout is not None:
                        try:
                            stdout.close()
                        except Exception:
                            pass
            finally:
                if process is not None:
                    try:
                        _managed_child_pids.discard(process.pid)
                    except Exception:
                        pass

    def turn(
        self,
        message,
        session_id=None,
        model=DEFAULT_MUSE_MODEL,
        progress_token=None,
        system_prompt=None,
    ):
        with self.turn_lock:
            if self.session_id and session_id and session_id != self.session_id:
                raise SessionConflictError(
                    "session_id %r does not match active session %r"
                    % (session_id, self.session_id)
                )
            # An unbound process adopts a caller identity before its first spawn.
            if session_id and not self.session_id:
                self.session_id = session_id
            elif not self.session_id:
                self.session_id = str(uuid.uuid4())
            prompt = message
            if system_prompt:
                prompt = "%s\n\n%s" % (system_prompt, message)
            cli_ready_start = _turn_timing_now()
            process = self._spawn(prompt, model)
            _emit_elapsed("cli_ready", cli_ready_start, path="spawn")
            accumulated_text = ""
            result_text = ""
            completed_model_attempts = set()
            terminal_reason = "completed"
            tasks_by_id = {}
            live_activity_events = []
            cached_activities = []
            try:
                pusher = _ProgressPusher(progress_token) if progress_token else None
            except Exception:
                pusher = None
            try:
                self._turn_timing_model_start = _turn_timing_now()
                # Muse retries internally; a turn is judged solely by whether
                # run.terminal.completed arrives.
                while True:
                    try:
                        event = self._read_event(TURN_READ_TIMEOUT)
                    except TimeoutError as exc:
                        self._close_process(kill=True)
                        raise RuntimeError(
                            "timed out waiting for Muse output after %s seconds"
                            % TURN_READ_TIMEOUT
                        ) from exc
                    if event is None:
                        raise self._empty_stream_error(process)
                    event_type = event.get("payload_type")
                    payload = event.get("payload", {})
                    if event_type == "run.output.delta":
                        text = payload.get("text", "")
                        if isinstance(text, str):
                            accumulated_text += text
                            if pusher:
                                try:
                                    pusher.push(accumulated_text, cached_activities)
                                except Exception:
                                    pass
                    elif event_type == "run.terminal.completed":
                        text = payload.get("text", "")
                        if isinstance(text, str):
                            result_text = text
                        terminal = payload.get("terminal")
                        reason = payload.get("reason")
                        terminal_reason = reason or terminal or "completed"
                        if pusher:
                            try:
                                pusher.push(
                                    result_text or accumulated_text,
                                    cached_activities,
                                )
                            except Exception:
                                pass
                        _emit_elapsed(
                            "model", getattr(self, "_turn_timing_model_start", None)
                        )
                        self._close_process(kill=False)
                        usage_collect_start = _turn_timing_now()
                        self._retained_tool_events = None
                        try:
                            usage = self._collect_usage(
                                payload.get("command_id"),
                                len(completed_model_attempts),
                            )
                        except Exception:
                            usage = _muse_usage_projection(
                                [],
                                self.session_id,
                                payload.get("command_id"),
                                len(completed_model_attempts),
                            )
                            usage["muse"]["reason"] = "collection_failed"
                        if self._retained_tool_events:
                            cached_activities = _muse_reconciled_activities(
                                live_activity_events,
                                self._retained_tool_events,
                            )[-300:]
                            if pusher:
                                try:
                                    pusher.push(
                                        result_text or accumulated_text,
                                        cached_activities,
                                    )
                                except Exception:
                                    pass
                        try:
                            muse_meta = (
                                usage.get("muse", {}) if isinstance(usage, dict) else {}
                            )
                            status = str(muse_meta.get("status"))[:32]
                            reason = str(muse_meta.get("reason"))[:64]
                            reported = muse_meta.get("reported_usage_completions")
                            expected = muse_meta.get("observed_model_completions")
                            sys.stderr.write(
                                "ember-claude-shim: muse-usage"
                                " status=%s reason=%s observations=%s"
                                " expected=%s\n"
                                % (
                                    status,
                                    reason,
                                    reported if type(reported) is int else "unknown",
                                    expected if type(expected) is int else "unknown",
                                )
                            )
                            sys.stderr.flush()
                        except Exception:
                            pass
                        _emit_elapsed("usage_collect", usage_collect_start)
                        return {
                            "result": result_text,
                            "terminal_reason": terminal_reason,
                            "session_id": self.session_id,
                            "model": self._model,
                            "usage": usage,
                            "voice": voice_summary(result_text),
                            "activities": cached_activities,
                        }
                    elif isinstance(event_type, str) and event_type.startswith(
                        "task.lifecycle."
                    ):
                        lifecycle = payload.get("event", {})
                        if isinstance(lifecycle, dict):
                            task_id = payload.get("task_id") or lifecycle.get("task_id")
                            details = lifecycle.get("details", {})
                            if (
                                task_id
                                and isinstance(details, dict)
                                and details.get("phase") == "stream_succeeded"
                            ):
                                facets = details.get("facets", [])
                                for facet in facets if isinstance(facets, list) else []:
                                    if (
                                        isinstance(facet, dict)
                                        and facet.get("kind") == "external_attempt"
                                        and facet.get("operation") == "model.response"
                                        and type(facet.get("attempt")) is int
                                        and facet["attempt"] > 0
                                    ):
                                        completed_model_attempts.add(
                                            (task_id, facet["attempt"])
                                        )
                            if task_id:
                                tasks_by_id.setdefault(task_id, {}).update(lifecycle)
                                # Captures carry only a label such as
                                # operation="tool:add_memory" here. Arguments
                                # are not part of task.lifecycle, so do not
                                # present that label as a command or tool input.
                                live_activity_events = _muse_live_tool_events(
                                    tasks_by_id
                                )
                                cached_activities = activity_from_events(
                                    live_activity_events
                                )[-300:]
            finally:
                if getattr(self, "_interrupt_requested", False):
                    self._partial_turn = _partial_turn(self, locals())
                if pusher and not getattr(self, "_drain_requested", False):
                    try:
                        pusher.stop()
                    except Exception:
                        pass
                self._close_process(kill=False)

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
            process.kill()
            process.wait()
        self._close_process(kill=False)
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
            prompt_file_path = self._prompt_file_path
            self._prompt_file_path = None
        if process is None:
            if prompt_file_path:
                try:
                    os.unlink(prompt_file_path)
                except OSError:
                    pass
            return
        if kill and process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        if prompt_file_path:
            try:
                os.unlink(prompt_file_path)
            except OSError:
                pass
        _managed_child_pids.discard(process.pid)
        _reap_orphans()


class PiProcess:
    """Own one long-lived Pi RPC process and bind sessions lazily."""

    # See ClaudeProcess.requires_git_checkout.
    requires_git_checkout = False

    def __init__(self, workspace=None, executable="pi"):
        self.workspace = workspace or os.environ.get(
            "EMBER_CLAUDE_WORKSPACE", DEFAULT_WORKSPACE
        )
        self.executable = executable
        self.process = None
        self.session_id = None
        self._poisoned_sessions = set()
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
        # Spawn-time workspace identity, read by the manager's remediation to
        # detect a volume mount hiding the tmpfs workspace this process was
        # spawned against. Same contract as ClaudeProcess.
        self._process_workspace_identity = _workspace_identity(self.workspace)

    def ready(self):
        with self.process_lock:
            return os.path.isdir(self.workspace)

    def _child_env(self):
        # Same constraint as the codex adapter: $HOME is read-only rootfs in the
        # guest, so pi's state dir lives under the writable workspace, which is
        # also where session files must sit to survive bank/relight.
        pi_home = os.path.join(self.workspace, ".pi")
        _ensure_cli_dir(pi_home)
        _ensure_cli_dir(os.path.join(pi_home, "agent"))
        child_env = os.environ.copy()
        child_env.update(egress_proxy_env())
        child_env["PI_HOME"] = pi_home
        child_env["PI_CODING_AGENT_DIR"] = os.path.join(pi_home, "agent")
        return child_env

    def _write_model_config(self, pi_home):
        agent_dir = os.path.join(pi_home, "agent")
        _ensure_cli_dir(agent_dir)
        config = {
            "providers": {
                "openai-completions": {
                    "baseUrl": PI_BASE_URL,
                    "api": "openai-completions",
                    "apiKey": "sk-noauth",
                    "compat": {
                        "supportsDeveloperRole": False,
                        "supportsReasoningEffort": False,
                    },
                    "models": [
                        {
                            "id": PI_MODELS[DEFAULT_PI_MODEL],
                            "contextWindow": PI_CONTEXT_WINDOW,
                            "maxTokens": PI_MAX_OUTPUT_TOKENS,
                            "reasoning": False,
                        }
                    ],
                }
            }
        }
        with open(os.path.join(agent_dir, "models.json"), "w") as stream:
            json.dump(config, stream)

    def _write_settings_json(self, pi_home):
        """Write pi's settings.json with managed compaction configuration.

        pi may persist unrelated keys in settings.json, so this method reads
        any existing file, merges the compaction defaults, and
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
        # Remove the retired Qwen setting from durable session workspaces so a
        # relit session cannot keep sending chat_template_kwargs to Meta.
        existing_settings.pop("defaultThinkingLevel", None)

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
        if not _workspace_ready_for_spawn(self.workspace, self.requires_git_checkout):
            raise StartupError("workspace does not exist: %s" % self.workspace)
        model = _canonical_pi_model(model)
        model_name = PI_MODELS.get(model, PI_MODELS[DEFAULT_PI_MODEL])
        child_env = self._child_env()
        pi_home = child_env["PI_HOME"]
        self._write_model_config(pi_home)
        self._write_settings_json(pi_home)
        agent_mcp_url = os.environ.get(AGENT_MCP_URL_ENV)
        agent_mcp_configured = bool(agent_mcp_url) and _agent_mcp_endpoint_alive(
            agent_mcp_url
        )
        if agent_mcp_url and not agent_mcp_configured:
            sys.stderr.write(
                "ember-claude-shim: warning: agent MCP endpoint %s is not "
                "reachable: pi starts without the agents extension\n" % agent_mcp_url
            )
            sys.stderr.flush()
        command = [
            self.executable,
            "--mode",
            "rpc",
            "--provider",
            "openai-completions",
            "--model",
            model_name,
            "--system-prompt",
            "You are a focused coding agent. "
            + compose_system_prompt(
                system_prompt, agent_mcp_configured=agent_mcp_configured
            ),
            "--no-context-files",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            # Discovery remains disabled, while this image-owned extension is
            # explicitly trusted and loaded on every Pi spawn.
            "--extension",
            PI_WEB_RESEARCH_EXTENSION,
            # Do not pass --tools here. Pi treats it as a complete allowlist,
            # not a built-in-tool selector, so naming only read/bash/edit/write
            # prevents this extension from activating web_search and web_fetch.
            # With no allowlist Pi enables those same four built-ins by default
            # and permits explicitly loaded extension tools to join them.
            "--session-dir",
            os.path.join(pi_home, "sessions"),
        ]
        if agent_mcp_configured:
            # The MCP bridge reads EMBER_AGENT_MCP_URL from its own environment
            # (child_env is a copy of ours). Listed after web-research so the
            # existing argv assertions on the first --extension keep holding.
            command.extend(["--extension", PI_AGENT_MCP_EXTENSION])
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
            self._process_workspace_identity = _workspace_identity(self.workspace)
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
            if tool_name:
                translated = {
                    "type": "tool_execution_start",
                    "toolName": str(tool_name).lower(),
                }
                tool_id = (
                    event.get("toolCallId")
                    or event.get("tool_call_id")
                    or event.get("id")
                )
                if tool_id is not None:
                    translated["toolCallId"] = tool_id
                if "args" in event:
                    translated["args"] = event["args"]
                elif "input" in event:
                    translated["input"] = event["input"]
                else:
                    translated["args"] = {}
                return translated
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
            model = _canonical_pi_model(model)
            model_name = PI_MODELS.get(model, PI_MODELS[DEFAULT_PI_MODEL])
            workspace_identity = _workspace_identity(self.workspace)
            cwd_changed = process is not None and (
                self._process_workspace_identity is not None
                and workspace_identity != self._process_workspace_identity
            )
            if process is not None and process.poll() is None and cwd_changed:
                self._close_process(kill=False)
                process = self._spawn(model, system_prompt=system_prompt)
                cli_ready_path = "workspace_swap_respawn"
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
            if requested_session in self._poisoned_sessions:
                sys.stderr.write(
                    "ember-claude-shim: discarding poisoned pi session %s, "
                    "forcing fresh session\n" % requested_session
                )
                sys.stderr.flush()
                requested_session = None
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
            last_tool_call_key = None
            identical_tool_call_count = 0
            tool_loop_tripped = None
            self._turn_done.clear()
            self._in_flight = True
            try:
                pusher = _ProgressPusher(progress_token) if progress_token else None
            except Exception:
                pusher = None
            try:
                self._turn_timing_model_start = _turn_timing_now()
                model_fallback_started_at = _turn_timing_now()
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
                            tool_name_guard = event.get("toolName") or event.get(
                                "tool_name"
                            )
                            tool_args = event.get("args")
                            if tool_name_guard:
                                if tool_args:
                                    tool_call_key = tool_name_guard + json.dumps(
                                        tool_args, sort_keys=True, default=str
                                    )
                                else:
                                    tool_call_key = tool_name_guard

                                if tool_call_key == last_tool_call_key:
                                    identical_tool_call_count += 1
                                    if (
                                        identical_tool_call_count
                                        >= PI_MAX_IDENTICAL_TOOL_CALLS
                                    ):
                                        args_preview = (
                                            json.dumps(
                                                tool_args,
                                                sort_keys=True,
                                                default=str,
                                            )[:200]
                                            if tool_args
                                            else "(no args)"
                                        )
                                        tool_loop_tripped = (
                                            tool_name_guard,
                                            args_preview,
                                        )
                                else:
                                    last_tool_call_key = tool_call_key
                                    identical_tool_call_count = 1
                            else:
                                last_tool_call_key = None
                                identical_tool_call_count = 0
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
                    if tool_loop_tripped:
                        self._close_process(kill=True)
                        tool_name_str, args_str = tool_loop_tripped
                        raise RuntimeError(
                            f"pi repeated the same {tool_name_str} tool call "
                            f"{identical_tool_call_count} times in a row and "
                            f"made no progress; last args: {args_str}"
                        )
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
                        "tool_execution_update",
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
                                    if not result_text:
                                        result_text = "".join(
                                            item.get("text", "")
                                            for item in message_event.get("content", [])
                                            if item.get("type") == "text"
                                        )
                                    break
                        if _is_leaked_tool_call(result_text) and not getattr(
                            self, "_interrupt_requested", False
                        ):
                            try:
                                sys.stderr.write(
                                    "ember-claude-shim: pi-tool-call-leak "
                                    "terminal_reason=%s\n" % terminal_reason
                                )
                                sys.stderr.flush()
                            except Exception:
                                pass
                            # Track poisoned session to prevent caller-resupplied
                            # session_id from resurrecting it via switch_session.
                            if self.session_id:
                                self._poisoned_sessions.add(self.session_id)
                            self._close_process(kill=True)
                            self.session_id = None
                            raise RuntimeError(
                                "pi returned tool-call syntax as its answer instead of "
                                "a text response. This can happen when a tool-call block "
                                "contains spurious closing tags that the parser rejects, "
                                "or when the output was truncated by the token limit. "
                                "If truncation, reduce the job size; if malformed syntax, "
                                "check that the model is correctly installed."
                            )
                        if pusher:
                            try:
                                pusher.push(
                                    accumulated_text or result_text, cached_activities
                                )
                            except Exception:
                                pass
                        if not result_text and not getattr(
                            self, "_interrupt_requested", False
                        ):
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
                            # Classify on the provider detail ALONE, before the
                            # ring is appended. Unlike muse, which spawns per
                            # turn, pi's process outlives many turns and
                            # stderr_lines is reset only in _spawn, so a
                            # "connection reset" it printed and recovered from
                            # twenty turns ago would otherwise still be in the
                            # ring and mark this failure retryable. The mirror
                            # case matters too: a stale "usage limit" line would
                            # suppress a genuine transient classification.
                            transient = _is_transient_turn_failure(error_detail)
                            stderr = _truncate_ring_for_error(self.stderr_lines)
                            if stderr:
                                error_detail += "\nCLI stderr:\n%s" % stderr
                            # Track poisoned session to prevent caller-resupplied
                            # session_id from resurrecting it via switch_session.
                            if self.session_id:
                                self._poisoned_sessions.add(self.session_id)
                            self._close_process(kill=True)
                            self.session_id = None
                            # The session is already poisoned and cleared above,
                            # so a retry starts a fresh pi session rather than
                            # resurrecting this one (see the poisoned-session
                            # branch in turn()). The retry is therefore a cold
                            # re-ask on a new session, not a continuation.
                            if transient:
                                raise TransientTurnError(
                                    "pi turn produced no output: %s" % error_detail
                                )
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
                            "model": self._model,
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
                if getattr(self, "_interrupt_requested", False):
                    self._partial_turn = _partial_turn(self, locals())
                if pusher and not getattr(self, "_drain_requested", False):
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
        muse_executable="muse",
    ):
        _ensure_persistence_mountpoint_writable(_persistence_mount_path())
        self.workspace = os.path.abspath(
            workspace or os.environ.get("EMBER_CLAUDE_WORKSPACE", DEFAULT_WORKSPACE)
        )
        self._hydration_attempts = 0
        self._hydration_error = None
        self._checkout_dir = None
        self._hydration_status = None
        self._turn_phase_telemetry = {}
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
        # Exact dispatch identity for turn-level interrupt. This lock is held
        # through the adapter signal itself, so a completed turn cannot clear
        # its identity and let a successor start in the validation-to-signal
        # window. Adapter interrupt waits on adapter-owned completion events and
        # never needs this lock.
        self._dispatch_lock = threading.Lock()
        self._active_dispatch_id = None
        self._active_dispatch_adapter = None
        self._last_interrupt_id = None
        self._last_interrupt_result = None
        self._interrupt_reason = None
        _write_git_proxy_helper()
        _create_claude_mcp_config_dir()
        cli_workspace = os.path.join(self.workspace, "src")
        _ensure_cli_dir(cli_workspace)
        self.claude = ClaudeProcess(cli_workspace, claude_executable)
        self.claude._manager = self
        self.codex = CodexProcess(cli_workspace, codex_executable)
        self.pi = PiProcess(cli_workspace, pi_executable)
        self.muse = MuseProcess(cli_workspace, muse_executable)
        self.muse.state_workspace = self.workspace
        if self._prewarm_clis and self.fatal_error is None:
            self._prewarm_thread = threading.Thread(
                target=self.prewarm, name="cli-prewarm", daemon=True
            )
            self._prewarm_thread.start()

    @staticmethod
    def _workspace_identity(path):
        return _workspace_identity(path)

    @staticmethod
    def _read_prewarm_clis():
        value = os.environ.get(PREWARM_CLIS_ENV, "")
        if not value.strip():
            # No explicit override: fall back to the list baked into the
            # image. A missing file (dev host, older image) means no prewarm.
            # ValueError covers a torn or non-UTF-8 read the same way.
            try:
                with open(PREWARM_CLIS_FILE, "r") as stream:
                    value = stream.read()
            except (OSError, ValueError):
                return ()
        clis = tuple(
            item.strip() for item in value.replace("\n", ",").split(",") if item.strip()
        )
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
            apply_egress_ca_trust()
            _ensure_cli_dir(self.claude.workspace)
            for cli in self._prewarm_clis:
                if cli == "claude":
                    self.claude._spawn(
                        session_id=None,
                        first_message=None,
                        model=None,
                        # Preserve the existing prewarm init value. Park mode
                        # skips the init wait and uses its own grace instead.
                        init_timeout=60,
                    )
                    # A message-less Claude spawn emits no init event and owns
                    # no session until its first user message.
                    self.claude.session_id = None
                elif cli == "codex":
                    # The app-server binds thread identity only when a turn
                    # asks for one (thread/start or thread/resume), so a parked
                    # process carries nothing session-shaped to roll back.
                    # Model and effort ride every turn/start and developer
                    # instructions ride the first thread request, so spawn plus
                    # the initialize handshake is the entire init cost.
                    self.codex._spawn()
                elif cli == "pi":
                    # Pi takes model and system prompt as spawn-time flags, so
                    # the parked process carries the defaults. A turn naming a
                    # different model pays one set_model RPC; a different
                    # system prompt respawns through the turn path exactly as
                    # it would for an unparked spawn.
                    self.pi._spawn(DEFAULT_PI_MODEL, system_prompt=None)
                    # Whatever startup state get_state reported, a parked
                    # process owns no caller session until its first prompt.
                    # Clearing it keeps a later switch_session from hitting the
                    # SessionConflictError guard, exactly like the claude park
                    # above (#4358 late-bound session identity).
                    self.pi.session_id = None
            self._prewarm_complete = True
        except Exception as exc:
            self.fatal_error = "CLI prewarm failed: %s" % exc
            prewarm_error = self.fatal_error
            # Close every CLI this guest spawned: a half-prewarmed guest never
            # becomes ready, and it must not sit on hundreds of MiB of resident
            # CLIs while the base build times out against /shim/ready.
            if hasattr(self, "_close_process"):
                try:
                    self._close_process(kill=True)
                except Exception:
                    pass
            sys.stderr.write("ember-claude-shim: %s\n" % prewarm_error)
            sys.stderr.flush()
        finally:
            self._prewarm_complete = True

    def _record_turn_phase(self, phase, started, status):
        """Keep one bounded phase measurement for the current HTTP response."""
        try:
            finished = _turn_timing_now()
            turn_started = getattr(self, "_turn_started_at", None)
            if started is None or finished is None or turn_started is None:
                return
            if phase not in ("hydration", "repo-clone"):
                return
            if status not in (
                "cloned",
                "skipped_existing",
                "failed",
                "lost",
                "attempt_cap",
            ):
                return
            self._turn_phase_telemetry[phase] = {
                "ms": max(0, int((finished - started) * 1000)),
                "start_offset_ms": max(0, int((started - turn_started) * 1000)),
                "status": status,
            }
        except Exception:
            pass

    def _hydrate_workspace(self, repo, branch):
        hydration_start = _turn_timing_now()
        status = "failed"
        try:
            status = self._hydrate_workspace_inner(repo, branch, hydration_start)
            return status
        finally:
            self._record_turn_phase("hydration", hydration_start, status)

    def _hydrate_workspace_inner(self, repo, branch, hydration_start):
        if self._hydration_status in ("ok", "skipped_existing"):
            if _checkout_is_usable(self._checkout_dir):
                self._hydration_status = "skipped_existing"
                _emit_elapsed("hydration", hydration_start, status="skipped_existing")
                return "skipped_existing"
            # The checkout this session already hydrated is gone, or is a stub.
            # The skip used to return unconditionally, so hydration never ran
            # again once it had succeeded even though the source it had cloned
            # was no longer there, and the turn path's own _ensure_cli_dir put
            # an empty directory back in its place that satisfied every isdir
            # gate downstream. Fall through and clone again. The attempt
            # counter is deliberately NOT reset: a checkout that keeps
            # vanishing exhausts the cap and then fails loudly at spawn, which
            # is the documented requeue-on-a-fresh-guest recovery.
            sys.stderr.write(
                "ember-claude-shim: hydrated checkout for %s@%s is no longer "
                "usable, re-hydrating\n" % (repo, branch)
            )
            sys.stderr.flush()
            self._hydration_status = None
            self._checkout_dir = None
        if self._hydration_attempts >= HYDRATION_ATTEMPT_CAP:
            return "attempt_cap"
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
            if _checkout_is_usable(checkout_dir):
                self._checkout_dir = checkout_dir
                self._hydration_status = "skipped_existing"
                # Resume slugs are safe: session cwd never changes mid-lineage (repo fixed at create).
                self.claude.workspace = checkout_dir
                self.codex.workspace = checkout_dir
                self.pi.workspace = checkout_dir
                self.muse.workspace = checkout_dir
                _emit_elapsed("hydration", hydration_start, status="skipped_existing")
                return "skipped_existing"
            # A durable volume can contain a partial clone from a prior failure,
            # and the turn path recreates this directory empty on every turn.
            shutil.rmtree(checkout_dir, ignore_errors=True)
        _ensure_cli_dir(self.workspace)
        # Hydrate directly from GitHub over HTTPS. The full branch history is
        # retained while --filter=blob:none defers file contents, so git log,
        # blame and bisect work and blobs arrive on demand. --depth=1 stays
        # absent.
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
            # The LOGIN GATE DUMMY opts this clone into credential injection.
            # The egress sidecar replaces the value, so the guest never holds
            # the real credential. Scope it to github.com and persist it in the
            # clone config so lazy blob fetches carry the same opt-in.
            "--config",
            "http.https://github.com/.extraHeader=Authorization: Basic %s"
            % _github_basic_optin(),
            "--single-branch",
            "--filter=blob:none",
            "https://github.com/%s.git" % repo,
            checkout_dir,
        ]
        failure = None
        try:
            clone_start = _turn_timing_now()
            result = subprocess.run(
                clone_command,
                capture_output=True,
                timeout=GIT_CLONE_TIMEOUT_SECONDS,
                **_cli_privilege_kwargs(),
            )
            _emit_elapsed(
                "hydration_clone",
                clone_start,
                status="ok:github" if result.returncode == 0 else "failed:github",
            )
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
        except Exception as exc:
            failure = exc
            # A partial clone from a failed attempt must not survive. A retry
            # on a later turn starts clean.
            shutil.rmtree(checkout_dir, ignore_errors=True)
        self._record_turn_phase(
            "repo-clone", clone_start, "failed" if failure is not None else "cloned"
        )
        if failure is not None:
            if isinstance(failure, subprocess.TimeoutExpired):
                _write_hydration_diagnostics(failure, checkout_dir)
            self._hydration_error = str(failure)
            sys.stderr.write(
                "ember-claude-shim: workspace hydration failed for %s@%s: %s\n"
                % (repo, branch, failure)
            )
            sys.stderr.flush()
            return "failed"
        self._hydration_error = None
        if not _checkout_is_usable(checkout_dir):
            # The clone validated moments ago, so the checkout went away under
            # the shim: a volume mount landing on /workspace between the two,
            # or the scratch it lived on being reclaimed. Do not annotate it.
            # _ensure_cli_dir creates directories and an append-mode open
            # creates the file, so the write below would MANUFACTURE
            # .git/info/exclude inside an empty replacement, and a .git holding
            # nothing but info/exclude is exactly the artefact that let a later
            # turn pass every gate and run against no source. Leave nothing
            # behind and let the next turn clone again.
            self._hydration_error = "checkout disappeared after clone"
            self._hydration_status = None
            self._checkout_dir = None
            sys.stderr.write(
                "ember-claude-shim: workspace hydration lost the checkout for "
                "%s@%s after cloning\n" % (repo, branch)
            )
            sys.stderr.flush()
            _emit_elapsed("hydration", hydration_start, status="lost")
            return "lost"
        exclude_file = os.path.join(checkout_dir, ".git/info/exclude")
        _ensure_cli_dir(os.path.dirname(exclude_file))
        with open(exclude_file, "a") as stream:
            stream.write(".codex/\n.pi/\n.muse/\n")
        self._checkout_dir = checkout_dir
        self._hydration_status = "ok"
        self.claude.workspace = checkout_dir
        self.codex.workspace = checkout_dir
        self.pi.workspace = checkout_dir
        self.muse.workspace = checkout_dir
        _emit_elapsed("hydration", hydration_start, status="cloned")
        return "cloned"

    def _adapter(self, model):
        if model in ("spark", "qwen"):
            return self.muse
        if model == "pi-spark":
            return self.pi
        if isinstance(model, str) and model in CODEX_MODELS:
            return self.codex
        configured_clis = getattr(self, "_prewarm_clis", ())
        if "claude" not in configured_clis and len(configured_clis) == 1:
            return getattr(self, configured_clis[0])
        return self.claude

    def ready(self):
        # The probe must not wait for lazily spawned CLIs when prewarming is
        # unset, but must wait for every configured prewarm to complete.
        if self.fatal_error is not None or not self._prewarm_complete:
            return False
        if (
            not self.claude.ready()
            or not self.codex.ready()
            or not self.pi.ready()
            or not self.muse.ready()
        ):
            return False
        # Base build safety depends on the noded placeholder volume staying blank
        # (zero-filled, no filesystem). Guest-init in guest init forbids mounting a
        # placeholder during base build (volume_linux.go:97-100). If the placeholder
        # gains a superblock this probe would misfire and skip the build liveness check.
        volume_has_ext4 = _volume_has_ext4()
        if self._prewarm_clis and not volume_has_ext4:
            # A base build has no filesystem on the volume, so every configured
            # prewarm must still be alive before the image is accepted as a
            # warm base: a snapshot with a dead CLI would restore warmth that
            # is not there.
            for cli in self._prewarm_clis:
                adapter = getattr(self, cli)
                process = getattr(adapter, "process", None)
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
                    # Every prewarmed family is stranded by the same mount:
                    # its process was spawned against the tmpfs workspace the
                    # volume now hides, so close each one whose identity moved
                    # (or that died) and leave the respawn to the turn path's
                    # proven lazy spawn for that family.
                    for adapter in (self.claude, self.codex, self.pi, self.muse):
                        process = getattr(adapter, "process", None)
                        process_dead = (
                            process is not None and process.poll() is not None
                        )
                        identity_changed = identity != getattr(
                            adapter, "_process_workspace_identity", None
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
                            adapter._close_process(kill=False)
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
        artifact_path=None,
        dispatch_id=None,
        turn_seq=None,
    ):
        total_start = _turn_timing_now()
        self._turn_started_at = total_start
        self._turn_phase_telemetry = {}
        with self._mount_lock:
            ensure_workspace_volume()
        # Re-apply per turn so a restored guest picks up CA rotation without a
        # fleet base rebuild. Prewarm already applied the build-time CA once.
        apply_egress_ca_trust()
        if getattr(self.claude, "workspace", None):
            _ensure_cli_dir(self.claude.workspace)
        if repo is not None and branch is not None:
            # This lineage is repo-backed, so from here on a directory is not
            # enough: every spawn must find a checkout whose HEAD resolves.
            # Sticky, because the repo is fixed at session create and a later
            # repo-less turn must not reopen the gap. Prewarm runs before any
            # turn and keeps the plain existence check it has always had.
            for adapter in (self.claude, self.codex, self.pi, self.muse):
                adapter.requires_git_checkout = True
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
        dispatch_bound = False
        if dispatch_id is not None:
            with self._dispatch_lock:
                if self._active_dispatch_id is not None:
                    raise SessionConflictError("another dispatch is already active")
                self._active_dispatch_id = dispatch_id
                self._active_dispatch_adapter = adapter
                self._active_provider_done = threading.Event()
                self._interrupt_reason = None
                adapter._partial_turn = {}
                adapter._drain_requested = False
                adapter._interrupt_requested = False
                dispatch_bound = True
        try:
            extra = {"progress_token": progress_token} if progress_token else {}
            prompt = {"system_prompt": system_prompt} if system_prompt else {}
            try:
                if adapter is self.pi:
                    record = adapter.turn(
                        message,
                        session_id,
                        model or DEFAULT_PI_MODEL,
                        thinking=thinking,
                        **(extra | prompt),
                    )
                elif adapter is self.muse:
                    record = adapter.turn(
                        message,
                        session_id,
                        model or DEFAULT_MUSE_MODEL,
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
                    record = adapter.turn(
                        message, session_id, model, **(extra | prompt)
                    )
            except Exception:
                if not getattr(self, "_interrupt_reason", None):
                    raise
                record = dict(getattr(adapter, "_partial_turn", {}))
            finally:
                if dispatch_bound:
                    self._active_provider_done.set()
            # Interrupt acknowledgments never substitute for this terminal result.
            # The adapter has stopped; preserve its native transcript plus the
            # stream prefix before allowing the node's between-turns bank guard.
            reason = None
            if dispatch_bound:
                with self._dispatch_lock:
                    reason = self._interrupt_reason
            if reason:
                record = dict(record)
                record.update(
                    terminal_reason=reason,
                    stop_reason=reason,
                    is_error=False,
                    dispatch_id=dispatch_id,
                    turn_seq=turn_seq,
                )
                saved = dict(getattr(adapter, "_partial_turn", {}))
                saved.update(record)
                saved["message"] = message
                directory = os.path.join(self.workspace, ".ember", "interrupted-turns")
                os.makedirs(directory, exist_ok=True)
                name = hashlib.sha256(dispatch_id.encode()).hexdigest() + ".json"
                target = os.path.join(directory, name)
                with open(target + ".tmp", "w") as stream:
                    json.dump(saved, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(target + ".tmp", target)
                record["transcript_path"] = target
            if isinstance(record, dict) and dispatch_bound:
                record["dispatch_id"] = dispatch_id
                record["turn_seq"] = turn_seq
            # Only Claude can recover a Claude prewarm failure. Codex, Muse and pi
            # turns must not clear the manager's Claude fatal state.
            if adapter is self.claude:
                self.fatal_error = None
            if repo is not None and isinstance(record, dict):
                if self._hydration_error:
                    record["workspace_hydration"] = {"failed": self._hydration_error}
                elif self._hydration_status:
                    record["workspace_hydration"] = self._hydration_status
            if isinstance(record, dict) and not reason:
                diff = _capture_turn_diff(checkout_dir, turn_base)
                if diff is not None:
                    record["diff"] = diff
                artifact = _capture_turn_artifact(checkout_dir, artifact_path)
                if artifact is not None:
                    record["artifact"] = artifact
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
            if dispatch_bound:
                with self._dispatch_lock:
                    if self._active_dispatch_id == dispatch_id:
                        self._active_dispatch_id = None
                        self._active_dispatch_adapter = None
            _emit_elapsed("total", total_start)

    def interrupt(self, dispatch_id, reason="user_interrupt", timeout_ms=None):
        with self._dispatch_lock:
            if (
                self._last_interrupt_id == dispatch_id
                and self._last_interrupt_result is not None
            ):
                return dict(self._last_interrupt_result)
            if (
                not dispatch_id
                or self._active_dispatch_id != dispatch_id
                or self._active_dispatch_adapter is None
            ):
                raise SessionConflictError("dispatch is no longer active")
            # Keep the identity lock until the signal is sent and the adapter's
            # bounded wait returns. A successor cannot bind during this window.
            self._interrupt_reason = reason
            self._active_dispatch_adapter._interrupt_requested = True
            self._active_dispatch_adapter._drain_requested = (
                reason == "interrupted_for_drain"
            )
            # Reserve time for transcript sync and the optional receipt callback.
            # Re-signal when the first signal raced provider startup. The completion
            # event is set without this lock, before publishing the terminal record.
            timeout = INTERRUPT_TIMEOUT
            if timeout_ms is not None:
                timeout = max(1.0, timeout_ms / 1000.0 - DRAIN_FLUSH_RESERVE_SECONDS)
                if reason != "interrupted_for_drain":
                    timeout = min(timeout, INTERRUPT_TIMEOUT)
            deadline = time.monotonic() + timeout
            done = getattr(self, "_active_provider_done", None)
            adapter = self._active_dispatch_adapter
            if timeout_ms is None and done is None:
                result = adapter.interrupt()
            else:
                result = adapter.interrupt(timeout=timeout)
            if done is not None:
                remaining = max(0.0, deadline - time.monotonic())
                if not done.wait(min(INTERRUPT_STARTUP_GRACE_SECONDS, remaining)):
                    remaining = max(0.0, deadline - time.monotonic())
                    if remaining > 0:
                        # One retry covers a signal that raced provider startup.
                        # Keep any killed/timeout flag the first signal observed.
                        second = adapter.interrupt(timeout=remaining)
                        result = dict(
                            second,
                            killed=bool(result.get("killed"))
                            or bool(second.get("killed")),
                            timeout=bool(result.get("timeout"))
                            or bool(second.get("timeout")),
                        )
                    remaining = max(0.0, deadline - time.monotonic())
                    if not done.wait(remaining):
                        adapter._close_process(kill=True)
            result = dict(result, terminal_reason=reason)
            self._last_interrupt_id = dispatch_id
            self._last_interrupt_result = dict(result)
            return result

    def _close_process(self, kill=False):
        self.claude._close_process(kill=kill)
        self.codex._close_process(kill=kill)
        self.pi._close_process(kill=kill)
        self.muse._close_process(kill=kill)


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
        for name, header_value in getattr(self, "_phase_headers", {}).items():
            self.send_header(name, header_value)
        self.end_headers()
        self.wfile.write(body)

    def _load_phase_headers(self):
        self._phase_headers = {}
        phases = getattr(self.manager, "_turn_phase_telemetry", {})
        if not isinstance(phases, dict):
            return
        for phase in ("hydration", "repo-clone"):
            measurement = phases.get(phase)
            if not isinstance(measurement, dict):
                continue
            duration_ms = measurement.get("ms")
            start_offset_ms = measurement.get("start_offset_ms")
            status = measurement.get("status")
            if type(duration_ms) is not int or duration_ms < 0:
                continue
            if type(start_offset_ms) is not int or start_offset_ms < 0:
                continue
            if status not in (
                "cloned",
                "skipped_existing",
                "failed",
                "lost",
                "attempt_cap",
            ):
                continue
            prefix = "X-Ember-Phase-%s" % phase.title()
            self._phase_headers[prefix + "-Ms"] = str(duration_ms)
            self._phase_headers[prefix + "-Start-Offset-Ms"] = str(start_offset_ms)
            self._phase_headers[prefix + "-Status"] = status

    def do_GET(self):
        if self.path == HEALTHZ_PATH:
            self._send(200, {"status": "ok"})
        elif self.path == READY_PATH:
            ready = self.manager.ready()
            self._send(200 if ready else 503, {"ready": ready})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        self._phase_headers = {}
        if self.path not in (TURN_PATH, CLOCK_PATH, INTERRUPT_PATH):
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
        if self.path == INTERRUPT_PATH:
            dispatch_id = (
                payload.get("dispatch_id") if isinstance(payload, dict) else None
            )
            if (
                not isinstance(dispatch_id, str)
                or not dispatch_id.strip()
                or set(payload) - {"dispatch_id", "reason", "timeout_ms"}
            ):
                self._send(400, {"error": "dispatch_id must be a non-empty string"})
                return
            try:
                options = {}
                if "reason" in payload:
                    if payload["reason"] not in (
                        "user_interrupt",
                        "interrupted_for_drain",
                    ):
                        self._send(400, {"error": "invalid interrupt reason"})
                        return
                    options["reason"] = payload["reason"]
                if "timeout_ms" in payload:
                    if (
                        type(payload["timeout_ms"]) is not int
                        or payload["timeout_ms"] <= 0
                    ):
                        self._send(400, {"error": "invalid interrupt timeout"})
                        return
                    options["timeout_ms"] = payload["timeout_ms"]
                outcome = self.manager.interrupt(dispatch_id.strip(), **options)
            except SessionConflictError as exc:
                self._send(409, {"error": str(exc)})
            except Exception as exc:
                self._send(503, {"error": str(exc)})
            else:
                self._send(200, outcome)
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
        result_receipt = payload.get("result_receipt")
        if "result_receipt" in payload and not _valid_result_receipt(result_receipt):
            self._send(400, {"error": "invalid result_receipt metadata"})
            return
        system_prompt = payload.get("system_prompt")
        if system_prompt is not None and (
            not isinstance(system_prompt, str) or not system_prompt.strip()
        ):
            self._send(400, {"error": "system_prompt must be a non-empty string"})
            return
        artifact_path = payload.get("artifact_path")
        if artifact_path is not None and (
            not isinstance(artifact_path, str) or not artifact_path.strip()
        ):
            self._send(400, {"error": "artifact_path must be a non-empty string"})
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
        dispatch_id = payload.get("dispatch_id")
        if dispatch_id is not None and (
            not isinstance(dispatch_id, str) or not dispatch_id.strip()
        ):
            self._send(400, {"error": "dispatch_id must be a non-empty string"})
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
            artifact = {"artifact_path": artifact_path.strip()} if artifact_path else {}
            thinking_override = {"thinking": thinking} if thinking is not None else {}
            dispatch = {"dispatch_id": dispatch_id.strip()} if dispatch_id else {}
            if payload.get("turn_seq") is not None:
                dispatch["turn_seq"] = payload["turn_seq"]
            record = self.manager.turn(
                message,
                session_id,
                payload.get("model"),
                **(
                    hydration
                    | progress
                    | prompt
                    | artifact
                    | thinking_override
                    | dispatch
                ),
            )
            if repo is not None and isinstance(record, dict):
                if getattr(self.manager, "_hydration_error", None):
                    record["workspace_hydration"] = {
                        "failed": self.manager._hydration_error
                    }
                elif getattr(self.manager, "_hydration_status", None):
                    record["workspace_hydration"] = self.manager._hydration_status
        except SessionConflictError as exc:
            self._load_phase_headers()
            self._send(409, {"error": str(exc)})
        except StartupError as exc:
            sys.stderr.write(str(exc) + "\n")
            sys.stderr.flush()
            self._load_phase_headers()
            self._send(503, {"error": str(exc)})
        except TransientTurnError as exc:
            # Same 422 as the catch-all below, plus the one bit the caller
            # cannot derive for itself. Status stays 422 so nothing that keys
            # off the code changes; only the body grows a field.
            self._load_phase_headers()
            self._send(422, {"error": str(exc), "retryable": True})
        except Exception as exc:
            self._load_phase_headers()
            self._send(422, {"error": str(exc)})
        else:
            if result_receipt is not None:
                try:
                    _publish_result_receipt(result_receipt, record)
                except Exception:  # noqa: BLE001 - preserve the native response.
                    _emit_result_receipt_failure("unexpected_callback_failure")
            self._load_phase_headers()
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
    egress_port = int(os.environ.get(EGRESS_PORT_ENV, str(DEFAULT_EGRESS_PORT)))
    egress = VsockEgressForwarder(egress_port)
    # Listen BEFORE ProcessManager starts its prewarm thread. The prewarm spawn
    # probes the agent MCP endpoint through this local proxy; a probe that
    # arrives first takes connection-refused, parks a CLI without the MCP tools
    # and with the non-MCP prompt, and a later turn reuses that process none
    # the wiser.
    egress.listen()
    sys.stderr.write(
        "ember-claude-shim: egress listening on %s:%s\n"
        % (EGRESS_LOCALHOST, egress.port)
    )
    sys.stderr.flush()
    manager = None
    server = None
    try:
        manager = ProcessManager(
            claude_executable="claude",
            codex_executable="codex",
            pi_executable="pi",
            muse_executable="muse",
        )
        server = build_server(manager)
        sys.stderr.write(
            "ember-claude-shim: listening on vsock port %s\n" % server.server_port
        )
        sys.stderr.flush()
        server.serve_forever()
    finally:
        if server is not None:
            server.server_close()
        egress.close()
        if manager is not None:
            manager._close_process(kill=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
