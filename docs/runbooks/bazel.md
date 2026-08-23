---
name: bazel
invoke: explicit
summary: BUILD files, gazelle, and BuildBuddy CI debugging (execution is ci/bb remote)
---

> **Runbook (explicit-only).** Open only when Joe asks for this procedure, or a
> claude.ai routine prompt names this file. Do not auto-load from skill matching.

# Bazel Build System

## Overview

Bazel is the build system for this repo. **Execution is remote**, not bare Mac
`bazel`. The local feedback loop is **`ci`** (selective lint/regen + BuildBuddy
Remote Bazel Test with the same flags as Workflows).

Locally:

- **`ci` / `ci lint` / `ci test`** — see `.claude/CLAUDE.md` Essential Commands
- **`helm template`** — render charts without installing
- **`mcp__buildbuddy__*`** — inspect Workflows invocations when debugging

## What CI Runs

Defined in `buildbuddy.yaml`:

| Action | What it does |
| ------ | ------------ |
| **Format check** | Formatters + generators + gazelle; auto-commits on PR branches |
| **PR Test** | affected targets with `--config=ci --deleted_packages=bazel/tools/python --test_tag_filters=-external,-future` |
| **Merge queue Test** | `bazel test //...` with the same flags |
| **Push images** | OCI publish + chart bumps on main |

`ci test` resolves the local committed and worktree diff inside one hosted Linux
runner, then tests only affected targets with the PR Test flags. The merge queue
alone runs the mandatory full `//...` suite. Use `ci test -- //...` only when an
explicit local full run is needed.

## Key Targets (CI-only)

| Target                             | Description               |
| ---------------------------------- | ------------------------- |
| `//projects/<service>/image:image` | Container image           |
| `//projects/<service>/image:push`  | Push image to registry    |
| `//bazel/images:push_all`          | Push all container images |
| `//bazel/tools/format:format`      | Format + render all       |

## Writing BUILD Files

BUILD files are still written locally — they define what CI builds.

### Querying Build Graph

Ad-hoc `bazel query` (deps, rdeps, target patterns) isn't run locally — there's no local bazel server. For one-off questions, the simplest path is reading a recent BuildBuddy invocation via `mcp__buildbuddy__get_invocation` to see what targets actually built. For programmatic graph traversal, add a temporary CI step that runs `bazel query` and prints results, then push the branch and read the output via the MCP.

### Gazelle (BUILD File Generation)

Gazelle auto-generates BUILD files for Go, Python, and Helm. It runs **only in
CI**, in the Format check, which auto-commits the result on PR branches as
`style: auto-format`. There is no local gazelle and there deliberately is no
`gazelle` on `$PATH`: the only one this repo could distribute is
aspect-gazelle, a different program that corrupts BUILD files (see
[`bazel/tools/image/README.md`](../../bazel/tools/image/README.md)).

```bash
ci regen    # generators when inputs changed; reports when BUILD files need CI
ci lint     # format changed files (includes buildifier when BUILD staged)
```

So after adding new Go imports or Python dependencies, expect the BUILD update
to arrive as a bot commit on your PR rather than in your own. Fetch and rebase
before your next push.

## Container Images with apko

See [apko.md](apko.md) for apko.yaml, locks, and `apko_image` patterns.

### Updating Lock Files

Pre-commit runs apko lock update when `apko.yaml` is staged; or:

```bash
bazel/tools/format/update-apko-locks.sh
```

## Debugging CI Failures

Use the `mcp__buildbuddy__*` tools:

1. Look up the invocation: `mcp__buildbuddy__get_invocation` with the `commitSha` selector. Always `git rev-parse <short>` to a full 40-char SHA first — short SHAs silently miss.
2. Find failing targets: `mcp__buildbuddy__get_target` (filter by tag or label).
3. Read the log: `mcp__buildbuddy__get_log` for the failing invocation.
4. For large artifacts: `mcp__buildbuddy__get_file_range` with the CAS blob URI from build events (16 MiB ranges).

Per CLAUDE.md's CI failure diagnosis rule: quote the actual assertion error before hypothesizing — don't blame infrastructure until a real test failure has been ruled out.

## Workflow Integration

Typical workflow after making changes:

1. Edit code or chart files
2. Run `ci` (lint + regen + remote test)
3. Review with `git diff`, commit, push, open PR
4. PR Workflows should cache-hit the affected test actions if `ci test` was green
