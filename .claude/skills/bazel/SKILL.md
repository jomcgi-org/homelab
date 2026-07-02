---
name: bazel
description: Use when working with BUILD files, understanding build targets, debugging CI failures, or writing Starlark. Bazel runs remotely via BuildBuddy CI — not locally.
---

# Bazel Build System

## Overview

Bazel is the build system for this repo but runs **remotely via BuildBuddy CI only** — not locally. Developers write BUILD files and push; CI handles building, testing, and pushing images.

Locally, use:

- **`format`** — standalone formatter (no Bazel required), runs as a pre-commit hook
- **`gh pr checks`** — monitor CI results
- **`mcp__buildbuddy__*` tools** — debug CI failures by walking invocations, targets, actions, and logs (see CLAUDE.md → Cluster Investigation)

## Local Commands

```bash
format                        # Format code + update BUILD files (standalone)
helm template <release> projects/<service>/chart/ -f projects/<service>/deploy/values.yaml  # Render templates
```

`format` runs standalone binaries in parallel:

- Formats Go (gofumpt), Python (ruff), JS/JSON/YAML (prettier), Shell (shfmt), Starlark (buildifier)
- Regenerates push/render BUILD files via grep-based scripts
- Runs gazelle to update BUILD files for Go/Python/Helm

Tools are provided by the OCI tools image via `./bootstrap.sh`.

## What CI Runs

CI is defined in `buildbuddy.yaml` with two parallel actions:

| Action            | What it does                                                 |
| ----------------- | ------------------------------------------------------------ |
| **Format check**  | Runs formatters + gazelle, auto-commits fixes on PR branches |
| **Test and push** | `bazel test //...`, pushes images + deploys pages on main    |

On PR branches, CI auto-commits formatting fixes as `ci-format-bot`. On main, formatting errors fail the build.

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

Gazelle auto-generates BUILD files for Go, Python, and Helm. Run via:

```bash
format    # Runs gazelle as part of the format pipeline
```

After adding new Go imports or Python dependencies, run `format` to regenerate BUILD files.

## Container Images with apko

For apko.yaml structure, BUILD.bazel patterns, and package reference, see the `apko` skill.

### Updating Lock Files

```bash
format    # Regenerates all apko lock files via bazel/tools/format/update-apko-locks.sh
```

`format` walks every `apko.yaml` in the repo and regenerates its `apko.lock.json` — `git diff` then shows only the locks that actually changed, so commit just those.

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
2. Run `format` to format and render
3. Review changes with `git diff`
4. Commit with conventional commit format and push
5. Create PR via `gh pr create`
6. CI builds, tests, and pushes automatically
