# rules_semgrep

Custom Semgrep rule library and hermetic engine wiring for this repo's CI.
Rules run as native Bazel tests against source files and rendered Helm
manifests; results are cached by Bazel on input hashes so scans only
re-execute when sources or rule definitions change.

The design bypasses the Python `pysemgrep` wrapper entirely, invoking
`semgrep-core` (and `semgrep-core-proprietary`) directly from shell scripts.
Both engine binaries are digest-pinned OCI archives vendored at
`bazel/semgrep/third_party/`.

## Public API

Load from `//bazel/semgrep/defs:defs.bzl`. The other `.bzl` files are
implementation details.

```python
load("//bazel/semgrep/defs:defs.bzl",
     "semgrep_test", "semgrep_manifest_test", "semgrep_target_test")
```

| Symbol                  | Kind  | Use                                                                 |
| ----------------------- | ----- | ------------------------------------------------------------------- |
| `semgrep_test`          | macro | Scan a flat list of source files (sh_test backed)                   |
| `semgrep_manifest_test` | macro | Render a Helm chart with `helm template`, then scan the YAML output |
| `semgrep_target_test`   | rule  | Scan a target's full transitive source tree via an aspect           |

See [`defs/README.md`](defs/README.md) for full attribute documentation,
SCA lockfile scanning, and the Gazelle extension directives. The Gazelle
extension (in `defs/gazelle/`) auto-generates `semgrep_test` and
`semgrep_target_test` targets when you run `bazel run gazelle`; it names
the primary test target `main_semgrep_test` by convention.

### Common attributes

| Attribute       | Type          | Default                                             | Purpose                                      |
| --------------- | ------------- | --------------------------------------------------- | -------------------------------------------- |
| `rules`         | `label_list`  | (required; `semgrep_manifest_test` defaults to k8s) | Filegroups of Semgrep rule YAML files        |
| `exclude_rules` | `string_list` | `[]`                                                | Rule IDs to skip (filename stem or check_id) |
| `lockfiles`     | `label_list`  | `[]`                                                | Lockfiles for SCA dependency scanning        |
| `sca_rules`     | `label_list`  | `[]`                                                | Semgrep Supply Chain advisory rule configs   |
| `pro_engine`    | `label`       | `//bazel/semgrep/third_party/semgrep_pro:engine`    | Pro engine binary (set `None` to disable)    |

`exclude_rules` applies both as a filename-based filter (entire rule config
skipped) and as a post-scan `check_id` suffix filter. Use it in BUILD files
for false-positives rather than inline `# nosemgrep` comments: `semgrep-core`
invoked directly does not honour `# nosemgrep`.

## Rule library

Rules live under `bazel/semgrep/rules/`, one subdirectory per language.
Named filegroup targets are declared in `rules/BUILD` and are publicly visible.

| Dir           | Filegroup target    | Sample rules                                                                                                                      |
| ------------- | ------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `bazel/`      | `:bazel_rules`      | `no-rules-python` (enforce `@aspect_rules_py`), `py-glob-missing-tests-exclude`                                                   |
| `dockerfile/` | `:dockerfile_rules` | `no-dockerfile` (all images must use apko)                                                                                        |
| `generic/`    | `:generic_rules`    | `no-stale-repo-paths`, `no-generic-test-filename`, `no-deprecated-api-subdomain`, `css-import-after-rules`                        |
| `golang/`     | `:golang_rules`     | `no-hardcoded-k8s-service-url`, `no-bare-error-return`, `no-discarded-json-marshal`, `stale-copyright-year`                       |
| `kubernetes/` | `:kubernetes_rules` | `no-hardcoded-image-digest`, `no-privileged`, `require-readiness-probe`, `require-resource-limits`, `require-fsgroup-with-pvc`    |
| `python/`     | `:python_rules`     | `no-hardcoded-k8s-service-url`, `no-hardcoded-secret`, `no-requests` (use httpx), `sqlmodel-*`, `no-sync-session-in-async-def`    |
| `shell/`      | `:shell_rules`      | `no-kubectl-mutate` (GitOps guard), `no-direct-test`, `claude-print-missing-permission-mode`                                      |
| `sql/`        | `:sql_rules`        | `no-create-extension-sql`                                                                                                         |
| `typescript/` | `:typescript_rules` | `fetch-no-timeout`, `sveltekit-form-action-unvalidated-path`, `svelte-hardcoded-color-in-style`                                   |
| `yaml/`       | `:yaml_rules`       | `no-hardcoded-k8s-url-in-helm-env`, `argocd-retry-under-spec`, `no-httproute-rule-without-timeout`, `no-unquoted-helm-range-args` |

The `:javascript_rules` filegroup combines `:typescript_rules` with the
upstream Semgrep Pro JavaScript rule pack. The `:kubernetes_rules` filegroup
similarly merges local rules with the Pro Kubernetes pack. An `:all_rules`
filegroup aggregates every rule YAML for convenience.

### SCA rule packs

Supply Chain Analysis rule packs are vendored from the Semgrep registry as
separate OCI archives and exposed as filegroup targets:

| Target                  | Ecosystem  |
| ----------------------- | ---------- |
| `:sca_golang_rules`     | Go modules |
| `:sca_python_rules`     | pip/uv     |
| `:sca_javascript_rules` | npm/pnpm   |
| `:sca_rules`            | all three  |

When a `semgrep_test` or `semgrep_target_test` target has `lockfiles` set,
the Gazelle extension auto-selects the matching SCA rules. See
[`defs/README.md`](defs/README.md) for the lockfile kind table.

## How rules are tested

Each rule has a corresponding annotation fixture in `bazel/semgrep/tests/fixtures/`
that marks expected findings with `# ruleid:` and safe patterns with `# ok:`.
The `rules/BUILD` file declares one `semgrep_test` target per language group
(e.g. `:python_rules_test`, `:kubernetes_rules_test`) plus isolated tests for
rules that use `languages: [generic]` with a non-standard fixture extension.

These rule-library tests set `env = {"SEMGREP_TEST_MODE": "1"}` and the tag
`"semgrep"`. The test runner detects `SEMGREP_TEST_MODE=1` and exits 0 with a
skip notice because the annotation protocol (`# ruleid:` / `# ok:`) requires
the `pysemgrep` CLI, which is no longer bundled. Rule correctness validation
against fixtures is therefore deferred; the primary CI gate for rules is the
production `semgrep_test` and `semgrep_target_test` targets in each service's
`deploy/` or source tree.

The `tests/rules/` subdirectory holds additional experimental rule YAML files
(e.g. `goroutine-without-sync.yaml`) that are paired with their own fixtures
in `tests/fixtures/` but are not yet wired into the public rule filegroups.

## Third-party engines

Both the open-source engine (`semgrep-core`) and the Pro engine
(`semgrep-core-proprietary`) are vendored as digest-pinned OCI archives
published to `ghcr.io/jomcgi/homelab/tools/`. A Bazel module extension
instantiates one repository per platform; a `select()`-based alias resolves
the correct binary at analysis time.

| Target                                           | Binary                     | Source image prefix                        |
| ------------------------------------------------ | -------------------------- | ------------------------------------------ |
| `//bazel/semgrep/third_party/semgrep:engine`     | `semgrep-core`             | `jomcgi/homelab/tools/semgrep/engine-`     |
| `//bazel/semgrep/third_party/semgrep_pro:engine` | `semgrep-core-proprietary` | `jomcgi/homelab/tools/semgrep-pro/engine-` |

Supported platforms: `linux/amd64`, `linux/arm64`, `macos/x86_64`,
`macos/aarch64`.

The Pro engine also ships per-language rule packs (`@semgrep_pro_rules_golang`,
`@semgrep_pro_rules_python`, `@semgrep_pro_rules_javascript`,
`@semgrep_pro_rules_kubernetes`) and SCA advisory packs
(`@semgrep_sca_rules_{golang,python,javascript}`), each a separate OCI
archive with its own digest pin in
`third_party/semgrep_pro/digests.bzl`.

CI tests use the auto-updated `semgrep_pro` rule-pack digests. The Firecracker
and EmberVM guest image uses the same rule-pack artifacts through
`semgrep_guest`, which has manual pins in `third_party/semgrep_guest/digests.bzl`
so CI digest updates do not rebuild the deployable guest.

The test runner stages both binaries in the same temp directory (Pro requires
`semgrep-core` as a co-located runtime dependency) and passes
`-pro_inter_file` for cross-file taint analysis. `SEMGREP_APP_TOKEN` is
set to an offline stub when not present so the engine does not phone home.

## Engine build

The `semgrep-core` engine is written in OCaml. Building it from source is
handled by the separate `bazel/ocaml` ruleset. See
[`../ocaml/README.md`](../ocaml/README.md) for details on `ocaml_library`,
`ocaml_binary`, and the ppx driver model. The pre-built binaries vendored
here are the normal path for CI; the OCaml build is only needed when updating
the engine version.

## Conventions

- Load only from `//bazel/semgrep/defs:defs.bzl`.
- Suppress false positives with `exclude_rules` in the BUILD entry, not
  `# nosemgrep` in source. `semgrep-core` does not honour inline suppressions.
- Rules that use path filters (e.g. `migration-destructive-ddl`) must be wired
  to dedicated `semgrep_test` targets rather than added to the broad language
  filegroup, because `semgrep-core` ignores `paths:` filters (a `pysemgrep`-only
  feature) and would produce false positives against all project files.
- The `semgrep_manifest_test` used by `argocd_app` in `bazel/helm` defaults
  to `//bazel/semgrep/rules:kubernetes_rules`. Override via
  `semgrep_rules = [...]` on `argocd_app`, or suppress individual rules with
  `semgrep_exclude_rules`.
- Bump `third_party/semgrep/digests.bzl` and
  `third_party/semgrep_pro/digests.bzl` together when updating the engine
  version; both binaries must be co-located at the same version.
