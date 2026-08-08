# AGENTS.md

Instructions for coding agents (Codex workers, Claude implementers) working
in this repo or a worktree of it. Keep work scoped to the requested change and
leave a clean, reviewable diff.

## Ground rules

- Leave changes uncommitted by default. When the user explicitly asks for a
  commit or pull request, agents may create a branch, commit the scoped changes,
  push it, and open or update the pull request. Never include unrelated working
  tree changes.
- For work expected to produce a commit or pull request, create a dedicated git
  worktree and branch before editing. Do not develop directly in the primary
  checkout. Read-only investigation and changes the user explicitly wants left
  uncommitted are exempt.
- Do NOT run `bazel`, `go test`, `npm test`, or full test suites on this
  machine. macOS has no matching remote executors and the results mislead.
- You MAY run targeted `pytest` on the specific test files you edited when
  the code under test is pure Python and hermetic (no cluster, no network
  services, no bazel). Iterate until they pass. Treat a local pass as
  advisory: the orchestrator's `ci` run on Linux is the gate.
- When done, print a short summary of files changed and any open questions.

## Test-writing traps (each of these has cost a review round)

- Effectively every subpackage under `projects/monolith/` is gazelle-excluded
  (verify with `grep gazelle:exclude projects/monolith/BUILD`): any new
  `*_test.py` under `projects/monolith/` needs a hand-written `py_test` target
  in `projects/monolith/BUILD`, following the hundreds already there, or Bazel
  never runs it.
- SQLite in tests: use a file-backed database under `tmp_path`. An in-memory
  StaticPool database is ONE connection and deadlocks concurrency tests.
- `build_app()` calls `logging.basicConfig(force=True)`, which removes
  pytest's caplog handler. Re-add the handler after `build_app` or caplog
  assertions see nothing.
- Mock async callables with async functions. A sync lambda for an awaited
  callable fails only at runtime.
- Assert on ORM objects INSIDE the session context. After the session
  closes, attribute access lazy-loads and throws.
- Never monkeypatch a builtin through a module attribute (for example
  `module.open`); patch `builtins.open` or restructure the seam.
- Numbers are load-bearing: TTLs, timeouts, `max_tokens`, and retry counts
  are asserted somewhere. Grep the tests and update the assertions in the
  same change.

## Conventions

- Python deps are `@pip//package` via `aspect_rules_py`. `requirement()`
  syntax does not exist in this repo.
- The `projects/monolith/frontend/` frontend is Svelte 5 runes only (`$props`,
  `$derived`, `$effect`). CSS is
  imported from JavaScript (the route layout), never via bare `@import`
  package specifiers inside CSS: PostCSS cannot resolve them.
- Containers are apko, never Dockerfiles: dual-arch, non-root uid 65532.
- Never hardcode `.svc.cluster.local` URLs (Helm prepends the release name;
  read the URL from an env var set in values.yaml) or `@sha256:` image
  digests (build-time pinning replaces tags; hand-pinned digests go stale).
- Secrets come from the 1Password Operator (`OnePasswordItem` CRD). Never
  hardcode one.
- Keep bulk data out of `chart/migrations/*.sql`: the migrations ConfigMap
  is applied client-side with a 256 KiB annotation cap. Seeds load out of
  band.
- No em-dashes in anything you write: prose, comments, docs. Use a comma,
  colon, parentheses, or split the sentence.

## EmberVM invariants

- Base snapshots clone guest PROCESS MEMORY. A restored guest is
  bit-identical to the base, so restore-time triggers can never derive from
  in-process state; they must derive from external state the restore changed
  (device superblock, mount table).
- The control plane's kv logger renders only whitelisted `@meta_keys`; a new
  structured log field must be added to the whitelist or it is silently
  dropped.
