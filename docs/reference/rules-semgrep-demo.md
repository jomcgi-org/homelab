# rules_semgrep demo

## Pitch

Semgrep runs as a Bazel test. Gazelle auto-generates the targets. Everything is cached.

```
Code change → bazel test //... → semgrep-core runs (only if inputs changed) → PR blocked on findings
```

No pysemgrep CLI. No manual config. No special CI jobs. Just `bazel test`.

## 1. It's deterministic

Engines, rules, and SCA advisories are vendored as OCI artifacts, pinned by digest.

A weekly workflow downloads artifacts, content-hashes them, and skips the push if nothing changed. Same content = same digest = cache stays warm.

> **Open:** [Commit updating only python rules](https://github.com/jomcgi/homelab/commit/360cf6d28e53d24895b345b1445726e86acb1f2b) — show `digests.bzl` change, only the rules that changed get new digests.

## 2. It's cached

Bazel hashes: source files + rule YAMLs + engine binary + lockfiles. Cache hit = 0s. Cache miss = 2-30s.

> **Open:** [BuildBuddy full execution](https://jomcgi.buildbuddy.io/invocation/90fac43b-4e7f-4271-b42a-28492b53e4fe) — show which semgrep tests ran vs cached.

## 3. Developers don't maintain it

Gazelle auto-generates `semgrep_target_test` for every scannable target. It detects `@pip//` deps and wires in SCA lockfile scanning automatically.

```
py_venv_binary(deps = ["@pip//requests"])
  → Gazelle generates semgrep_target_test with:
    - SAST: cross-file analysis on transitive source closure
    - SCA:  requirements.txt scanned against vulnerability advisories
```

> **Open:** [main target on BuildBuddy](https://jomcgi.buildbuddy.io/invocation/e8f10161-1793-41ac-b6ab-b8f138900106?target=%2F%2Fprojects%2Fmonolith%3Amain_semgrep_test&targetStatus=5#@7): show a real semgrep_target_test execution with SAST + SCA passes.

## 4. New rules invalidate precisely

Adding a Python rule YAML invalidates all Python semgrep tests (shared `rules` input). Go tests, Kubernetes manifest tests, unit tests — all cache hit.

## 5. Violations block PRs

> **Open:** [Demo PR with security violation](https://github.com/jomcgi/homelab/pull/773) — show CI failure from `no-eval-exec` rule catching dynamic code execution.

## 6. Merge queue compatible

Semgrep tests are just `bazel test` targets. They share the remote cache with everything else. A merge queue rebase that doesn't touch your files = instant cache hit. No added latency. (vs 2-5m+ on SMS)

## 7. Result

<img width="649" height="426" alt="image" src="https://github.com/user-attachments/assets/c25cd219-f909-4a5a-86c2-3091153e023d" />
