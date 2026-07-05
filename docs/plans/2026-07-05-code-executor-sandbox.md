# Code Executor Sandbox Implementation Plan (ADR agents/044)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Repo overrides apply: no local test runs (push and watch BuildBuddy CI), one comprehensive review per PR, chart bumps via `bazel/tools/git/bump-chart.sh` in the same PR as the change they deploy.

**Goal:** Ship the ADR 044 code executor: a zero-egress warm-restore `sandbox` fc-invoke workload exposing `run_python` to the monolith MCP surface and the Discord concierge, plus Python and a generated env-readme baked into the goosecracker guest.

**Architecture:** Three PRs after the docs PR. PR A refines the goose guest environment (Python runtime + generated `/etc/environment.md`). PR B adds the `sandbox` workload to the substrate (guest image, Go handler behind the shared shim, values block). PR C wires the monolith callers (broker module, MCP tool, concierge PydanticAI tool, Discord file attachments). Each PR is independently deployable and lands with its own chart bump; PRs merge serially to avoid chart-version rebase drops.

**Tech Stack:** apko/Wolfi guest images (dual-arch), Go guest handler on the substrate `shim` library, Helm values workload registry (ADR 030), Python/FastMCP + PydanticAI on the monolith.

**Key facts discovered during planning (do not re-derive):**

- New workloads need NO fc-invoke daemon code change. The chart templates an initContainer per `workloads.*` entry that cranes the guest OCI image into a node-local `rootfs.ext4`, and the daemon reads the workload registry from Helm-rendered config.
- The guest contract: a Go binary implementing `shim.Handler` (`projects/firecracker/substrate/shim/server.go`), HTTP-over-vsock port 1027 (`vsockproto.GuestHTTPPort`), readiness via `GET /shim/ready` (`shim.WithReady`). Warm-base snapshot is taken once the ready probe returns 200, so anything you warm before flipping ready is captured in the snapshot.
- `FC_INVOKE_URL` already reaches the monolith (chart `values.yaml` `semgrep.fcInvokeUrl`, templated in `chart/templates/deployment.yaml`). The sandbox broker reuses the same env var; no new chart env plumbing.
- The Discord bot has NO tool-result-to-attachment path today. PR C adds one (ChatDeps `generated_files` + post-stream flush).
- `projects/monolith/BUILD` `monolith_backend` lists source globs per directory explicitly; a new `sandbox/` module must be added to that list by hand (gazelle does not manage it). Same for the `py_test` target.
- The `Update apko locks` pre-commit hook regenerates `apko.lock.json` when `apko.yaml` changes; a wrong Wolfi package name fails resolution at commit time. That is the feedback loop for the library list: **drop any library Wolfi cannot resolve from the v1 set** (the env-readme and tool description keep the contract honest); do not build a pip-venv layer in v1.
- Substrate pod memory limit (18Gi) was sized as semgrep 8Gi + agent 8Gi + daemon ~1Gi. The sandbox adds concurrency 2 x 2048Mi = 4Gi; the limit and its comment must move to 22Gi in the same PR.

---

## PR A: goose guest Python + generated env-readme

Branch: `feat/goose-guest-python-env` from origin/main in a fresh worktree (`git -C ~/repos/homelab worktree add -b feat/goose-guest-python-env /tmp/claude-worktrees/goose-guest-python-env origin/main`).

### Task A1: env-readme generator tool

**Files:**
- Create: `projects/firecracker/tools/env_readme/gen_env_readme.py`
- Create: `projects/firecracker/tools/env_readme/BUILD`

**Step 1: Write the generator.** A small stdlib-only script: reads an `apko.lock.json`, emits Markdown to stdout. Inputs: `--lock <path>`, `--title <string>`, `--notes <path>` (a hand-written per-guest preamble covering what the lock cannot know: network posture, writable paths, how to use the environment).

```python
"""Generate /etc/environment.md for a Firecracker guest image.

The package table is derived from the apko lock file, so the readme cannot
drift from the image it ships in (ADR agents/044). Hand-written context goes
in the per-guest notes file, never here.
"""

from __future__ import annotations

import argparse
import json
import pathlib


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lock", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--notes", required=True)
    args = ap.parse_args()

    lock = json.loads(pathlib.Path(args.lock).read_text())
    # apko lock schema: top-level "contents" with a "packages" list of
    # {"name": ..., "version": ...}; tolerate both list-of-dicts and the
    # flat "name-version" string form older locks use.
    raw = lock.get("contents", {}).get("packages", [])
    rows = []
    for p in raw:
        if isinstance(p, dict):
            rows.append((p.get("name", "?"), p.get("version", "?")))
        else:
            name, _, version = str(p).rpartition("-")
            rows.append((name or str(p), version))

    print(f"# {args.title}")
    print()
    print(pathlib.Path(args.notes).read_text().rstrip())
    print()
    print("## Installed packages (from the image lock; exact and exhaustive)")
    print()
    print("| Package | Version |")
    print("| ------- | ------- |")
    for name, version in sorted(set(rows)):
        print(f"| {name} | {version} |")


if __name__ == "__main__":
    main()
```

**Step 2: BUILD file.**

```bazel
load("@aspect_rules_py//py:defs.bzl", "py_binary")

py_binary(
    name = "env_readme",
    srcs = ["gen_env_readme.py"],
    main = "gen_env_readme.py",
    visibility = ["//projects/firecracker:__subpackages__"],
)
```

**Step 3: Run `bazel/tools/format/fast-format.sh`, commit.**

```bash
git commit -m "feat(firecracker): add env-readme generator for guest images"
```

### Task A2: bake Python + library set into the goose guest

**Files:**
- Modify: `projects/firecracker/goosecracker/guest/apko.yaml` (packages list)
- Create: `projects/firecracker/goosecracker/guest/env-notes.md`
- Modify: `projects/firecracker/goosecracker/guest/BUILD`

**Step 1: Add the runtime block to `apko.yaml` packages** (alphabetical within the block, commented as the shared block):

```yaml
    # Python compute runtime. This block is duplicated in
    # projects/firecracker/sandbox/guest/apko.yaml (ADR agents/044): the
    # curated library set is the shared asset; keep the two lists in sync.
    - python-3.12
    - py3.12-numpy
    - py3.12-pandas
    - py3.12-matplotlib
    - py3.12-scipy
    - py3.12-pillow
    - py3.12-pyyaml
    - py3.12-python-dateutil
```

Package-name reality check happens at commit: the `Update apko locks` hook resolves against Wolfi. If a name fails, try the unversioned `py3-<name>` form; if that also fails, DROP the library from both this list and the env-notes (do not hunt for a pip fallback in v1). `sympy`, `duckdb`, `tabulate`, `openpyxl` are the likely casualties; verify rather than assume, and record the final surviving set in the PR description because Task C2's tool docstring must match it.

**Step 2: Write `env-notes.md`** (the hand-written half of the readme):

```markdown
This file describes the environment you are running in. It is generated at
image build time and always matches the installed packages exactly.

- You are inside a disposable Firecracker microVM. The rootfs is read-only;
  write scratch files under /tmp or /workspace.
- Outbound network goes through an egress proxy with an allowlist; most
  destinations are blocked. Do not assume general internet access.
- Runtimes available: python3 (with the scientific libraries listed below),
  node/pnpm, go. Prefer running real code over estimating results.
- matplotlib works headless (Agg). Save figures to files.
```

**Step 3: Wire the generated readme into the image.** In `guest/BUILD`, add a genrule + tar layer and include it in the `apko_image` layer list alongside `config_tar` (copy the exact `pkg_tar` owner/mode conventions used by `config_tar`):

```bazel
genrule(
    name = "environment_md",
    srcs = [
        "apko.lock.json",
        "env-notes.md",
    ],
    outs = ["environment.md"],
    cmd = "$(location //projects/firecracker/tools/env_readme) --lock $(location apko.lock.json) --title 'goosecracker agent guest environment' --notes $(location env-notes.md) > $@",
    tools = ["//projects/firecracker/tools/env_readme"],
)

pkg_tar(
    name = "environment_md_tar",
    srcs = [":environment_md"],
    package_dir = "/etc",
)
```

**Step 4: Commit** (the apko lock hook regenerates `apko.lock.json` and may adjust `config.checksum`; let it).

```bash
git commit -m "feat(goosecracker): bake python compute runtime + generated env-readme into agent guest"
```

### Task A3: chart bump, push, CI, merge

**Step 1:** `bazel/tools/git/bump-chart.sh projects/firecracker/substrate` (guest image digest is pinned into chart values at build time, so a guest change deploys via a chart bump). Commit as `build(firecracker): bump substrate chart for goose guest env refresh`.

**Step 2:** Push, open PR titled `feat(goosecracker): python runtime + self-describing env for agent guest (ADR 044 PR A)`, `gh pr merge --auto --rebase`, watch `gh pr checks <n> --watch`. On red, diagnose via `mcp__buildbuddy__get_invocation` (commitSha selector) before hypothesizing; quote the failing assertion verbatim.

**Step 3:** After merge, verify rollout: `kubectl get applications -n argocd` for the substrate app sync, then confirm the initContainer rebuilt the agent rootfs (pod restart on node-4, marker file logic makes this idempotent).

---

## PR B: `sandbox` fc-invoke workload

Branch: `feat/sandbox-workload` from origin/main AFTER PR A merges (both PRs touch the substrate chart; serial merge avoids the same-version chart bump rebase drop).

### Task B1: sandbox guest image

**Files:**
- Create: `projects/firecracker/sandbox/guest/apko.yaml`
- Create: `projects/firecracker/sandbox/guest/env-notes.md`
- Create: `projects/firecracker/sandbox/guest/BUILD`

**Step 1: `apko.yaml`.** Same Wolfi repo/keyring stanza as the goose guest. Contents:

```yaml
contents:
  repositories:
    - https://packages.wolfi.dev/os
  keyring:
    - https://packages.wolfi.dev/os/wolfi-signing.rsa.pub
  packages:
    - busybox
    - ca-certificates-bundle
    # Python compute runtime. This block is duplicated in
    # projects/firecracker/goosecracker/guest/apko.yaml (ADR agents/044):
    # the curated library set is the shared asset; keep the lists in sync.
    - python-3.12
    - py3.12-numpy
    - py3.12-pandas
    - py3.12-matplotlib
    - py3.12-scipy
    - py3.12-pillow
    - py3.12-pyyaml
    - py3.12-python-dateutil

archs:
  - x86_64
  - aarch64

accounts:
  groups:
    - groupname: sandbox
      gid: 65532
  users:
    - username: sandbox
      uid: 65532
      gid: 65532
  run-as: 65532

environment:
  HOME: /home/sandbox
  MPLBACKEND: Agg
  PYTHONUNBUFFERED: "1"

paths:
  - path: /home/sandbox
    type: directory
    uid: 65532
    gid: 65532
    permissions: 0o755

entrypoint:
  command: /usr/local/bin/sandbox-guest-init
```

Apply the same library-name survival rule as Task A2, and keep the two lists identical.

**Step 2: `env-notes.md`** (shorter; this guest's readme mostly serves humans debugging):

```markdown
Zero-egress Python execution sandbox (ADR agents/044). One-shot: each request
runs in a fresh microVM restore and nothing persists. No network access at
all. Code runs as uid 65532 with a hard wall-clock timeout; stdout, stderr,
and files created in the working directory are returned to the caller.
```

**Step 3: `BUILD`.** Copy the shape of `projects/firecracker/semgrep/guest/BUILD`: an `apko_image` target named `image` publishing to `ghcr.io/jomcgi/homelab/projects/firecracker/sandbox/guest`, with per-arch handler tars (`sandbox_guest_init_tar_amd64/arm64` from the Task B2 binary via `platform_transition_filegroup`, installed at `/usr/local/bin/sandbox-guest-init`, mode 0755) plus the `environment_md` genrule + tar exactly as in Task A2 (title `python sandbox guest environment`).

**Step 4: Commit** `feat(firecracker): add sandbox guest image (python, zero egress)`.

### Task B2: guest handler binary

**Files:**
- Create: `projects/firecracker/sandbox/guest-init/cmd/main.go`
- Create: `projects/firecracker/sandbox/guest-init/internal/handler/handler.go`
- Create: `projects/firecracker/sandbox/guest-init/internal/handler/handler_test.go`
- BUILD files via gazelle (`bazel/tools/format/fast-format.sh` runs it)

**Step 1: define the wire types and caps in `handler.go`.**

```go
// Package handler executes one untrusted Python snippet per request inside
// the sandbox guest (ADR agents/044). The security boundary is the microVM,
// not this process; the caps below exist to keep responses inside the
// fc-invoke 8 MiB body budget and to fail fast on runaway code.
package handler

const (
	stdoutCap    = 512 << 10
	stderrCap    = 128 << 10
	perFileCap   = 2 << 20
	totalFileCap = 5 << 20
	defaultTimeout = 20 * time.Second
	maxTimeout     = 25 * time.Second // below the workload requestTimeout (30s)
)

type ExecFile struct {
	Path       string `json:"path"`
	ContentB64 string `json:"content_b64"`
}

type ExecRequest struct {
	Code           string     `json:"code"`
	Files          []ExecFile `json:"files,omitempty"`
	TimeoutSeconds int        `json:"timeout_seconds,omitempty"`
}

type ExecResult struct {
	Stdout     string     `json:"stdout"`
	Stderr     string     `json:"stderr"`
	ExitCode   int        `json:"exit_code"`
	Files      []ExecFile `json:"files,omitempty"`
	DurationMs int64      `json:"duration_ms"`
	Truncated  bool       `json:"truncated,omitempty"`
	Error      string     `json:"error,omitempty"`
}
```

**Step 2: implement `Handle`.** Behavior, in order: reject empty `Code`; create a per-invoke workdir under `/tmp`; decode and write input `Files` (reject paths that escape the workdir via `filepath.IsLocal`); write `Code` to `main.py`; run `python3 main.py` with `cwd=workdir` and a context timeout of `min(TimeoutSeconds, maxTimeout)`; capture stdout/stderr through capped writers (set `Truncated` when a cap trips); on context deadline, kill the process group and report `exit_code: -1` with a clear `error: "timed out after Ns"`; afterwards walk the workdir and return every regular file that was not an input and is not `main.py`, base64-encoded, skipping (and flagging `Truncated`) files over `perFileCap` or once `totalFileCap` is reached. Timeout, exit code, and truncation are results, not handler errors: the handler returns HTTP 200 with the structured body; only malformed requests are 4xx.

**Step 3: table-driven `handler_test.go`** covering: happy path (stdout captured, exit 0), nonzero exit propagated, stdout truncation at cap, input file round-trip, generated file pickup, path-escape rejection, and timeout kill. Tests exec real `python3` only if present (`t.Skip` otherwise) so they pass on CI runners; the pure functions (caps, path validation, output walking) get direct coverage without python.

**Step 4: `cmd/main.go`.** Copy the semgrep `main.go` skeleton: mount tmpfs over `/tmp`, then warm the page cache with one throwaway import run so the warm-base snapshot captures the libraries' pages (this is the ADR's pre-import mechanism; a resident forkserver interpreter is a later optimization if restore-to-result latency disappoints):

```go
// Warm the page cache before flipping ready: the warm-base snapshot is taken
// after /shim/ready returns 200, so these library pages are resident in every
// restored guest and per-request python starts import from RAM.
warmCmd := exec.Command("python3", "-c",
	"import numpy, pandas, matplotlib, matplotlib.pyplot, scipy, PIL, yaml, dateutil")
```

Run it with a generous timeout, log-but-continue on failure (a missing import must not brick the guest), then `ready.Store(true)` and serve `shim.NewServer(handler.Handle, shim.WithReady(ready.Load))`. Keep the import list in sync with the surviving apko library set.

**Step 5:** `bazel/tools/format/fast-format.sh`; commit `feat(firecracker): sandbox guest handler (one-shot python exec over shim)`.

### Task B3: register the workload in the substrate chart

**Files:**
- Modify: `projects/firecracker/substrate/chart/values.yaml`
- Modify: `projects/firecracker/substrate/chart/BUILD`

**Step 1: values.** Top-level image block plus workload entry, mirroring semgrep's commenting style:

```yaml
sandbox:
  guestImage:
    repository: ghcr.io/jomcgi/homelab/projects/firecracker/sandbox/guest
    tag: latest
```

```yaml
  # ADR agents/044: one-shot zero-egress python executor. Warm-base so the
  # library page cache is resident at restore; sessioned false because every
  # request is independent by design.
  sandbox:
    image: sandbox-guest
    rootfsPath: /disks/nvme-02/fc-invoke/sandbox/rootfs.ext4
    harnessInit: /usr/local/bin/sandbox-guest-init
    vcpus: 2
    memMib: 2048
    concurrency: 2
    egressEnabled: false
    warmBase: true
    readyPath: /shim/ready
    sessioned: false
    requestTimeout: 30s
```

**Step 2: memory budget.** Raise the fc-invoke pod memory limit 18Gi to 22Gi and extend its sizing comment (semgrep 8 + agent 8 + sandbox 4 + daemon ~1). Follow `feedback_resource_sizing_convention`: memory requests = limits; CPU requests only.

**Step 3: chart BUILD.** Add `"sandbox.guestImage": "//projects/firecracker/sandbox/guest:image.info"` to the `helm_chart` `images` dict so the digest is build-time pinned.

**Step 4: render check:** `helm template fc-invoke projects/firecracker/substrate/chart/ -f projects/firecracker/substrate/deploy/values.yaml` and confirm the sandbox initContainer and workload registry entry render.

**Step 5:** `bazel/tools/git/bump-chart.sh projects/firecracker/substrate`; commit `feat(firecracker): register sandbox workload (ADR agents/044)`.

### Task B4: push, CI, merge, live probe

**Step 1:** Push, PR `feat(firecracker): sandbox workload, zero-egress python executor (ADR 044 PR B)`, auto-merge on green, watch checks; BuildBuddy MCP for failures.

**Step 2: after ArgoCD syncs**, verify the rootfs built (`kubectl get pods -n <fc-invoke namespace>` initContainer logs) and the warm base primed (daemon logs mention the sandbox snapshot). Read-only kubectl only.

---

## PR C: monolith callers (`run_python` everywhere it belongs)

Branch: `feat/run-python-tool` from origin/main after PR B merges.

### Task C1: broker module

**Files:**
- Create: `projects/monolith/sandbox/__init__.py` (empty)
- Create: `projects/monolith/sandbox/client.py`
- Modify: `projects/monolith/BUILD` (`monolith_backend` srcs: add `"sandbox/**/*.py"`)

`client.py` follows `semgrep/mcp.py`'s conventions exactly (same env var, same auth, same structured-error contract):

```python
"""Broker for the fc-invoke sandbox workload (ADR agents/044).

Shared by the MCP tool (sandbox/mcp.py) and the Discord concierge tool
(chat/agent.py). POSTs code to the in-cluster fc-invoke daemon; the guest is
zero-egress and one-shot, so this client is the only stateful party.
"""

from __future__ import annotations

import logging
import os

import httpx

from shared.k8s_auth import auth_headers

logger = logging.getLogger(__name__)

FC_INVOKE_URL = os.environ.get("FC_INVOKE_URL", "")

SANDBOX_CONNECT_TIMEOUT = 5.0
# Guest wall-clock cap is 25s inside a 30s workload requestTimeout; read a
# little past that so the daemon's timeout error reaches us intact.
SANDBOX_READ_TIMEOUT = 35.0


async def run_python_in_sandbox(code: str, files: list[dict] | None = None) -> dict:
    if not FC_INVOKE_URL:
        return {"error": "FC_INVOKE_URL is not configured"}
    if not code or not code.strip():
        return {"error": "no code provided"}

    payload: dict = {"code": code}
    if files:
        payload["files"] = files

    timeout = httpx.Timeout(SANDBOX_READ_TIMEOUT, connect=SANDBOX_CONNECT_TIMEOUT)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{FC_INVOKE_URL}/invoke/sandbox",
                json=payload,
                headers=auth_headers(),
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.ConnectError as exc:
        logger.exception("fc-invoke connection failed")
        return {"error": f"could not reach fc-invoke: {exc}"}
    except httpx.HTTPStatusError as exc:
        logger.exception("fc-invoke returned an error status")
        return {
            "error": (
                f"fc-invoke returned HTTP {exc.response.status_code}: "
                f"{exc.response.text[:500]}"
            )
        }
    except Exception as exc:  # noqa: BLE001: surface any failure as structured error
        logger.exception("sandbox execution failed")
        return {"error": f"sandbox execution failed: {exc}"}
```

Commit: `feat(monolith): sandbox broker client for fc-invoke run_python`.

### Task C2: MCP tool

**Files:**
- Create: `projects/monolith/sandbox/mcp.py`
- Create: `projects/monolith/sandbox/mcp_test.py`
- Modify: `projects/monolith/app/main.py` (add `import sandbox.mcp  # noqa: F401` beside the semgrep import)
- Modify: `projects/monolith/BUILD` (add `sandbox_mcp_test` py_test cloned from `semgrep_mcp_test`)

`mcp.py`: `@mcp.tool` async `run_python(code: str, files: list[dict] | None = None) -> dict` delegating to the broker. The docstring is a load-bearing contract (ADR 044: the description shapes the code models write). It MUST enumerate the final surviving library set from PR A/B, state "no network access", note the ~25s wall-clock cap, and explain that files written to the working directory come back base64-encoded in `files`. Write it once the PR A library list is final.

`mcp_test.py`: clone the semgrep registration smoke test, asserting `run_python` appears in `mcp.list_tools()`.

CI watch-item: if `bdd_completeness_test` trips on the new module's public callables, add the entry its error message names (known gotcha, `feedback_bdd_completeness_public_surface`).

Commit: `feat(monolith): run_python MCP tool (ADR agents/044)`.

### Task C3: concierge tool + Discord attachments

**Files:**
- Modify: `projects/monolith/chat/agent.py` (new tool + one "WHAT YOU CAN DO" line)
- Modify: `projects/monolith/chat/bot.py` (checklist label + attachment flush)
- Modify: wherever `ChatDeps` is defined (scout: `chat/agent.py`) to add `generated_files: list = field(default_factory=list)`

**Step 1: the tool**, using the established decorator + signpost pattern:

```python
@agent.tool
@signposted(
    "When a message needs an exact computed answer (arithmetic beyond "
    "trivial, date math, unit conversions, statistics, simulations, "
    "parsing pasted data, or a quick chart), run real code in the sandbox "
    "instead of estimating from memory. Prefer this over starting an "
    "agent thread when the goal is an output, not a change."
)
async def run_python(ctx: RunContext[ChatDeps], code: str) -> str:
    """Run a short Python script in an isolated sandbox and return its output. No network. Save charts/files to the working directory and they will be attached to the reply."""
    result = await run_python_in_sandbox(code)
    if result.get("error"):
        return f"sandbox error: {result['error']}"
    for f in result.get("files", []):
        try:
            ctx.deps.generated_files.append(
                (f["path"], base64.b64decode(f["content_b64"]))
            )
        except (KeyError, ValueError):
            continue
    parts = []
    if result.get("stdout"):
        parts.append(result["stdout"])
    if result.get("stderr"):
        parts.append(f"[stderr]\n{result['stderr']}")
    parts.append(f"[exit code {result.get('exit_code', '?')}]")
    if result.get("files"):
        names = ", ".join(f.get("path", "?") for f in result["files"])
        parts.append(f"[files attached to reply: {names}]")
    return "\n".join(parts)
```

Add one line to the system prompt's "WHAT YOU CAN DO" list: `"- Run a short Python snippet in an isolated sandbox for exact math, data crunching, or a quick chart, and attach the output.\n"`.

**Step 2: checklist label in `bot.py`.** In the `FunctionToolCallEvent` branch, the row text currently comes from `args.get("query", ...)`; for `run_python` calls (check `event.part.tool_name`) use `"run python: " + code.splitlines()[0][:80]` so the progress checklist shows something meaningful instead of a code blob.

**Step 3: attachment flush in `bot.py`.** After the stream loop completes (where the final content edit happens), flush `deps.generated_files` as a follow-up message: wrap each `(name, data)` in `discord.File(io.BytesIO(data), filename=os.path.basename(name) or "output.bin")`, cap at 8 files and skip any single file over Discord's upload limit (log what was skipped), send via the channel/thread the reply went to, then clear the list. Guard the whole flush so an attachment failure never eats the text reply.

**Step 4:** format, commit `feat(chat): concierge run_python tool with file attachments`.

### Task C4: chart bump, push, CI, merge, live verification

**Step 1:** `bazel/tools/git/bump-chart.sh projects/monolith` (backend + chat changed; the deploy needs the bump in this PR). Commit `build(monolith): bump chart for run_python tool`.

**Step 2:** Push, PR `feat(monolith): run_python via sandbox workload (ADR 044 PR C)`, auto-merge on green.

**Step 3: post-merge verification, in order:**
1. ArgoCD sync of the monolith app (read-only kubectl).
2. Run `/refresh-context-forge-tools` (Context Forge caches tool catalogs; `run_python` stays invisible to the `homelab` connector until refreshed).
3. Call `run_python` end to end via the refreshed MCP surface with a smoke snippet (`print(2**512 % 97)` plus a matplotlib savefig) and confirm stdout and the returned file.
4. Ask the Discord concierge a calc question in a test channel and confirm the tool fires, the checklist row renders, and the chart attaches.

---

## Explicitly out of scope (per ADR 044)

- Public /notes chat access (needs its own `security/` ADR).
- Sessioned REPL semantics, Node in the sandbox image, pip-venv fallback layer for non-Wolfi libraries, resident-interpreter forkserver.
- Goose guests calling `run_python` over MCP (they get the baked interpreter instead; revisit at ADR 034 implementation).
