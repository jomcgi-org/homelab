# Build and CI Architecture

Everything that turns a commit into tested code, images and published charts: the Bazel graph, the vendored developer tools, the BuildBuddy workflow, the local `ci` loop, the formatters and generators, the Claude and git hooks, and the Semgrep and OCaml rulesets. Current as of a93980260 (2026-09-05).

What happens after publish (chart versions, the write-back commit, Kargo, ArgoCD) is in `projects/platform/ARCHITECTURE.md`. The runtime Semgrep scan lane (the MCP tool, the PR webhook, the EmberVM workload) is in `projects/monolith/ARCHITECTURE.md` and `projects/embervm/ARCHITECTURE.md`.

---

## 1. Developer tools

No Bazel runs on a Mac. `bootstrap.sh` pulls the host's slice of the tools image named in `.tools-version` (`ghcr.io/jomcgi/homelab/bazel/tools/image:main`, built by `bazel/tools/image/BUILD` as a `rules_oci` index over linux/amd64, linux/arm64 and darwin/arm64) with `crane export` into `~/.cache/homelab-tools`, keyed by manifest digest so a re-run is a no-op until the image moves. `.envrc` puts `bazel/tools/ci` and that cache's `usr/bin` on PATH and checks for a newer digest in the background. The image carries the multitool binaries (argocd, buildozer, crane, gh, gofumpt, helm, kind, op, ruff, shfmt), node, pnpm, prettier with the Svelte plugin, a Python 3.13 runtime with the CLI's pip closure, and the hand-written `homelab` CLI from `tools/cli/`. It deliberately carries no `gazelle`: the only gazelle the multitool lockfile can supply is aspect-gazelle, a different program from `//:gazelle_binary`, and the correct one links a cgo tree-sitter that the amd64 executors cannot cross-build for darwin. `bb`, the BuildBuddy CLI that `ci test` needs, is not in the image and is installed separately.

Direct `bazel` on the Mac is blocked by a Claude hook (`bazel/tools/hooks/prefer-bb-remote.sh`). darwin has no executors, so the loop is `ci` (section 5).

(see: `bootstrap.sh`, `.envrc`, `.tools-version`, `bazel/tools/image/BUILD`, `bazel/tools/image/README.md`, `bazel/tools/tools.lock.json`)

**Why.** Building the tool tree through `bazel_env` took about 45 seconds warm and needed a working local Bazel just to render a chart, and every environment (Mac, CI, agent guests) resolved tool versions separately (ADR tooling/001). A symlink subtree was rejected because on macOS the links resolved to the host's `/usr/bin`; the whole filesystem is extracted so Python finds its stdlib at a relative path. One multi-arch image with one lockfile gives identical versions everywhere, at the cost of a daily digest check and a bootstrap that depends on GHCR being reachable.

---

## 2. The Bazel graph

bzlmod only (`MODULE.bazel`, no WORKSPACE). Language rulesets: `rules_go` with Gazelle and a `nogo` set (`bazel/tools/lint`), `aspect_rules_py` over a `rules_python` 3.13.9 toolchain (the stripped python-build-standalone tarballs, about a fifth of the size in every test's runfiles), `aspect_rules_js`, `rules_ts` and `rules_esbuild` on pnpm 10, `rules_rust` 1.75, `rules_oci` and `rules_apko`, `rules_multitool`, `rules_multirun`, `aspect_rules_lint`, `rules_shell`, and `toolchains_buildbuddy` for the RBE C++ toolchain. Five in-repo rulesets: `bazel/helm`, `bazel/semgrep`, `bazel/ocaml`, `bazel/erlang`, `bazel/tla`. Binaries no ruleset provides (firecracker, k3s, kubectl, k9s, the claude, codex and pi CLIs, DuckDB extensions, `bb`) are vendored by the `multiarch_http_archive` and `multiarch_http_file` repo rules in `bazel/tools/http`, sha256-pinned per arch, with the comment on each `MODULE.bazel` entry recording why that pin and that arch set.

Python dependencies are `@pip//<package>` from the pip hub over `bazel/requirements/all.txt`; `requirement()` syntax is wrong here. The lock is layered: `pyproject.toml` compiles to `runtime.txt`, which constrains `test.in` and `tools.in`; `bazel run //bazel/requirements:runtime` and `:requirements.all` regenerate them (`bazel/tools/format/update-python-requirements.sh`).

`rules_apko` is patched (`bazel/patches/rules_apko_no_range.patch` under a `single_version_override`) so each apk is fetched whole: upstream's three ranged requests collide with Bazel's own resume `Range` header on a flaky link and abort analysis (#4849; dropping the patch is #5399). Under `--config=ci` every `rctx.download` goes through BuildBuddy's Remote Asset API with local fallback, so a cold queue candidate no longer depends on Wolfi or PyPI being healthy.

**BUILD generation is CI-only.** `//:gazelle` is a custom `gazelle_binary` carrying the `argocd` (helm), `semgrep`, `bzl`, `go`, `proto` and `python` languages, with `# gazelle:go_naming_convention_external import` so external Go deps resolve to the labels the `go_deps` extension actually creates. It runs inside `bazel run //bazel/tools/format:format` in CI's format stage, which auto-commits the result on PR branches as `ci-format-bot`: a BUILD change lands as a `style: auto-format` commit after yours, and your next push is non-fast-forward until you fetch and rebase. `ci regen` prints a reminder and runs nothing. The Python gazelle manifest lives in `bazel/tools/python`, which every CI command passes as `--deleted_packages` so analysis never fetches every wheel.

(see: `MODULE.bazel`, `.bazelrc`, `bazel/remote.bazelrc`, root `BUILD`, `bazel/requirements/README.md`, `bazel/tools/http/`, `bazel/patches/`)

**Why.** bzlmod with pinned rulesets and sha256-pinned binaries is what lets BuildBuddy cache actions across runners; `aspect_rules_py` was chosen over `rules_python`'s rules because its venv-style binaries are what the image layers package, and the `no-rules-python` Semgrep rule was written to hold the line. Running Gazelle locally was rejected after aspect-gazelle rewrote BUILD files by its own defaults in whichever directory you were editing, silently; CI-only generation costs a second commit per BUILD change and buys a single correct generator. The apko patch and the remote downloader trade a vendored patch and a BuildBuddy dependency for queue candidates that stop failing on an upstream mirror.

---

## 3. Images

Application images are apko (`bazel/tools/oci/apko_image.bzl`) over Wolfi packages, non-root uid 65532, from an `apko.yaml` plus a committed `apko.lock.json`; 25 targets today. Two older shapes remain: `go_image` (four targets, distroless base) and one `py3_image` (ubuntu base). Every macro emits `{name}.push` (collected into the generated `//bazel/images:push_all` multirun), `{name}.info` (an `OciImageInfo` that `helm_images_values` deep-merges into the chart's `values.yaml` at package time, which is why no values file ever hand-pins a `@sha256:` digest), and for apko a `{name}_lock_test` that fails when the lock's config checksum no longer matches the YAML.

**amd64 only.** `apko_image` defaults `arm64 = True` and every one of its 26 call sites passes `False`: all nodes and the RBE executor are amd64, no chart pins an arch, and the aarch64 half of a manifest had no consumer. `go_image` and `py3_image` default to single-platform. Re-adding an arch is `arm64 = True` plus per-arch `tars`; `arm64 = False` with `multiarch_tars` fails to push a layer blob at push time, so PR CI stays green and main's deploy is what breaks.

Digests are reproducible: `.bazelrc` pins `SOURCE_DATE_EPOCH` to a constant so apko stops stamping a wall-clock `created` time, and `--stamp` is applied only by the push job, never by `--config=ci`, because a stamped graph turns every image's digest rule into a cache miss. On main, `bazel/images/push/push-changed.sh` builds everything, reads each image's content digest from `//bazel/images/digests:manifest`, and pushes only what the registry does not already hold; PR branches `bazel build` the images and push nothing.

Lock maintenance: `update-apko-locks.sh` re-resolves a lock (needs Bazel, and forces the host's execution platform because the registered aarch64 OCaml platform would otherwise pick an arm64 apko binary), `fix-apko-checksum.sh` re-hashes a config-only edit without Bazel, and the Renovate apko-lock CronWorkflow refreshes every lock weekly on its own PR. The lock checksums the whole YAML file, so a comment edit invalidates it.

(see: `bazel/tools/oci/`, `bazel/images/README.md`, `bazel/images/push/push-changed.sh`, `bazel/tools/format/update-apko-locks.sh`, `bazel/tools/format/fix-apko-checksum.sh`, `projects/platform/renovate/`)

**Why.** apko over Dockerfiles gives a package-locked, non-root, reproducible image without a daemon, and the `no-dockerfile` rule exists to keep it that way; the aarch64 half was dropped because nothing could run it and every lock refresh re-baked it anyway. Content-digest pushes replaced `bazel run push_all`, which materialised every image's runfiles on the runner and was the repo's largest single source of BuildBuddy download. The accepted cost is that an identical rebuild pushes nothing, so a chart pinning a build-timestamped tag can name a tag that was never pushed, which is what `check_helm_deps.py` catches.

---

## 4. CI: BuildBuddy Workflows

Two actions in `buildbuddy.yaml`, each one runner, both cloning full history (`git_fetch_depth: 0`):

- **`pr-checks`** is the only required status check (the GitHub ruleset matches it by exact name). It runs on every pull request, on every merge-queue candidate (`gh-readonly-queue/*`, which arrive as push events), and on `main` only to seed a warm runner snapshot for new branches. Order is load-bearing: tests, then `bazel build //bazel/images:push_all`, then the rendered-manifest diff comment (`bazel/helm/ci-diff-manifests.sh`, which hard-fails on duplicate env var names), then the format stage. PR branches test only affected targets; queue candidates and main run `//...`, and the queue candidate is the authoritative full-suite gate.
- **`deploy`** runs on main: tests, then `push-changed.sh` (images, then charts through `push_charts`), then the chart-version write-back pushed as the repository owner (the ruleset's only bypass), then the format checks read-only with no auto-commit. Publishing runs before the format checks on purpose, so a flaky formatter cannot stop a good build from shipping. Timeout is two hours, sized for a fourteen-image cold rebuild.

The format stage runs `bazel run //bazel/tools/format:format` plus the guards in section 6, then `git diff --exit-code`. Drift on a PR branch is committed and pushed as `ci-format-bot`, and the run that pushed it exits red so a green status on the superseded head cannot enqueue a merge without the fix (#5089). Drift on a queue candidate fails the run, which ejects the PR.

Runner cost is dominated by cold snapshot restores, so the two actions each keep one shared workspace, remove Bazel's sandbox stash before the runner snapshots the VM, and disable the stash under `--config=ci`. `resource_requests` are part of the snapshot key, so changing them invalidates every snapshot and they change rarely. Two further actions (BDD future features, Buck2 rules) have been commented out since 2026-08-09 to cut runner spin-ups; nothing required depends on them. `gh run` sees none of this: PR and queue checks live on BuildBuddy, and a red one is re-triggered from there.

Merge mechanics (rebase-only, the merge queue, why a PR is never rebased by hand for BEHIND) are in the `pr-workflow` skill.

(see: `buildbuddy.yaml`, `bazel/remote.bazelrc`, `.bazelrc` under `common:ci`, `bazel/images/push/push-changed.sh`, `bazel/helm/write-back-versions.sh`, `bazel/helm/chart-version.sh`)

**Why.** Separate Format, Test and Push actions were collapsed into two because cold snapshot restores were most of the runner bill and three output bases were each evicted separately (the measurements are in `buildbuddy.yaml`). Affected-target PR runs behind a full-suite queue gate were chosen over full runs everywhere: PR feedback gets cheap while the queue still tests every candidate against current main. Publishing before formatting accepts an unformatted main turning the action red in exchange for never blocking a good image on a missing formatter binary. Full-history clones cost about 150 MB once per cold fetch and are what lets `chart-version.sh` walk the conventional commits since the last publish; a shallow clone computed "no bump" for every chart and took main's deploy down.

---

## 5. The local loop: `ci`

`ci` (`bazel/tools/ci/ci`) is lint, then regen, then test, each file-selective against `origin/main` plus the working tree. `ci lint` formats only changed files with the PATH tools (buildifier is skipped if absent; CI covers BUILD files). `ci regen` runs `run-generators.sh` only when a generator input changed and the migration-ordering check only when a migration changed. `ci test` ships the local diff (committed, staged, unstaged and untracked) to one hosted Linux runner with `bb remote`, whose exec properties mirror the `pr-checks` `resource_requests` (`ci_test.sh` asserts the parity), and runs `local-affected-test.sh` there with the PR test flags. `ci test -- //...` is the explicit full-suite escape hatch.

**Judge a run by its summary line, never by exit code.** `bb remote` exits 0 when the remote action fails (#4118), so `ci` requires positive evidence: an `Executed N out of M tests` line with no `FAILED`, or an explicit "no targets affected" line. A missing summary, an `Action failed`, a `Build did NOT complete` (which is how a failing verification genrule presents), and the apk duplicate-`Range` abort are each named and returned non-zero. `Executed 0` on an affected run gets a note: every affected test was a cache hit, so check that the changed targets are in the affected set (#5538). On a branch already pushed with no local delta the affected set is empty and the run tests nothing.

Affected targets (`affected-targets.sh`) map changed files to source labels, probe them with one `set()` query, then take the tests that transitively depend on them over a test-only universe (Semgrep tests are excluded from the traversal and added back wholesale, because their configured closure loads every platform engine), plus a second query for targets tagged `verification`: genrule suites that are not test rules (the Elixir control-plane suite, the release boot smoke, the TLA+ checks). Both queries fail closed to `//...` on any error, on a partial result that could have dropped the suite, or on a timeout, after which the wedged Bazel server is shut down. Anything under `bazel/`, any BUILD, lock, `.bzl` or `.apko.yaml`, and any deleted file also fall back to `//...`. The same script runs on PR branches in `pr-checks`.

The pre-push hook runs `ci test` only under `RUN_CI_TEST=1`: every push is tested twice after landing anyway (the PR run, then the queue candidate). Two Claude hooks keep the loop honest: `prefer-bb-remote.sh` blocks direct `bazel` on the Mac, and `check-ci-pipe-mask.sh` blocks piping `ci` or `bb remote` output into `tail`, `grep`, `head` and friends or discarding it, because a truncated read is how a false-green report happens.

(see: `bazel/tools/ci/`, `bazel/tools/git/pre-push-ci-test.sh`, `bazel/tools/hooks/prefer-bb-remote.sh`, `bazel/tools/hooks/check-ci-pipe-mask.sh`)

**Why.** A local Bazel loop was rejected outright: darwin has no executors, the platforms are wrong, and a cold local build costs more than a hosted runner. `bb remote` reuses the Workflows backend, so its action cache is shared with PR CI. Each fail-closed heuristic was added after a false-green run, because `bb remote` cannot tell "the tests passed" from "the runner never ran them" (#4118) and `tests()` cannot see genrule suites (#5538); the accepted cost is the occasional `//...` fallback that could have been a subset.

---

## 6. Formatting, generators and guards

`//bazel/tools/format:format` is a multirun of four commands: `run_generators`, `format_code` (gofumpt, prettier, ruff, rustfmt, shfmt, buildifier through `aspect_rules_lint`), `format_svelte` (the same hermetic prettier with `prettier-plugin-svelte`, since rules_lint has no Svelte dialect), and `//:gazelle`. `run-generators.sh` is the single list of committed-artifact generators, run by both CI and the local paths so they cannot drift: the home-cluster kustomization, the `push_all` BUILD, the monolith routes, the three docs manifests (repo docs for KG ingest, public docs, posts), the orchestrator bundle, and the per-guest `environment.md` files. `sync-helm-deps.sh` and the Atlas checksum update are deliberately not in that list: they need `helm` and `atlas`, which the CI runner lacks, so they run only from `fast-format.sh` locally and CI enforces the same invariants read-only.

Guards that run in CI's format stage, each with the pure logic pinned by its own bazel test: `validate-generate-scripts.sh` (the grep-based generators agree with `bazel query`), `check-migration-ordering.sh` (linear, non-duplicated Atlas versions), `check-commit-msg-ascii.sh --all` (Wrangler rejects non-ASCII commit titles), `validate-hooks-executable.sh` (every hook in `.claude/settings.json` exists and is executable), `check_readme_structure.py` (root README links resolve and every `projects/*` is mentioned or allowlisted), `check_doc_links.py` (every ADR path reference resolves, over `git ls-files`, which is what makes deleting a harvested ADR safe), and `check_helm_deps.py` (vendored chart tarballs match their `file://` source and no chart pins a build-timestamped tag).

The docs manifests enumerate `git ls-files`, so `git add` a new file by name before `ci lint` or the manifests will not see it.

(see: `bazel/tools/format/`, `bazel/images/validate-generate-scripts.sh`, `bazel/tools/git/check-commit-msg-ascii.sh`)

**Why.** One generator list run by both paths replaced two lists that drifted into "green locally, red in CI"; auto-committing drift on PR branches replaced a failing check because the fix is mechanical and the bot's commit is usually the head that merges. Guards live in the format stage rather than as bazel tests when they need the full checkout (an RBE sandbox cannot list `projects/*`), with the logic pinned by a test so the shell glue stays thin.

---

## 7. Hooks

Claude PreToolUse hooks (`bazel/tools/hooks/`, wired in `.claude/settings.json`, each with an `sh_test`). Blocking: `prefer-bb-remote.sh`, `check-ci-pipe-mask.sh`, `pretooluse-write-edit.sh` (writes under `docs/plans/` are refused; plans live in GitHub Issues), and `check-public-reader-grant.sh` (a `CREATE TABLE` in a public-served schema must carry a `public_reader` grant or an explicit override comment). Advisory: `check-em-dash.sh`, `check-large-migration-sql.sh` (the migrations ConfigMap's 256 KiB annotation cap), `check-constant-change-test-grep.sh` (grep the tests before changing a numeric constant), `check-chart-version-targetrevision-sync.sh`, and `check-adr-architecture-sync.sh` (editing a covered config or build tree reminds you that the domain's `ARCHITECTURE.md` is the source of truth).

Git hooks (`bazel/tools/git/`, installed by pre-commit): `protect-main.sh` refuses commits on main, `check-commit-msg-ascii.sh` is the commit-msg check, `check-stale-pr.sh` blocks pushes to a branch whose PR is closed or merged, `post-rewrite-format.sh` runs selective lint after a rebase, and `pre-push-ci-test.sh` as above.

Codex dispatch lives here too: `bazel/tools/codex/dispatch.sh` runs an implementer tier (`luna`, `terra`, `frontier`) in a workspace-write sandbox scoped to one worktree, exits 42 on quota exhaustion, and refuses to double-dispatch a worktree.

(see: `bazel/tools/hooks/`, `bazel/tools/git/`, `bazel/tools/codex/dispatch.sh`, `.claude/settings.json`)

**Why.** Hooks encode the invariants a reviewer would otherwise re-explain every session: `kubectl` is read-only, no local Bazel, no truncated gate output, no plans in the repo. Each blocking hook exists because the failure it prevents was observed and was silent; the advisory ones stay advisory where a false positive would block legitimate work. `validate-hooks-executable.sh` exists because a hook committed as mode 644 exits 126 on every call and nothing else notices.

---

## 8. Semgrep

`bazel/semgrep` runs Semgrep as Bazel tests by invoking `semgrep-core` and `semgrep-core-proprietary` directly, never the Python wrapper, so a scan is cached on the hash of its rules, sources and engine and re-runs only when one of them changes. Three macros from `//bazel/semgrep/defs:defs.bzl`: `semgrep_test` over a file list, `semgrep_target_test` over a binary's transitive main-repo sources (an aspect collects them, which is what gives `-pro_inter_file` files to cross), and `semgrep_manifest_test`, which `argocd_app` emits for every `deploy/` overlay to scan the rendered chart with the Kubernetes rules. The `semgrep` Gazelle language generates the first two. Both engines are digest-pinned OCI artifacts under `ghcr.io/jomcgi/homelab/tools/semgrep` and `.../semgrep-pro`, one repository per platform, plus the Pro rule packs and SCA advisory packs; `.github/workflows/update-semgrep-pro.yaml` rebuilds them from the PyPI wheel and the Semgrep API every Monday and opens an auto-merging PR that bumps the two `digests.bzl` files. The EmberVM Semgrep guest pins its own copies (`semgrep_guest`, `semgrep_experimental`) by hand so a CI digest bump never re-bakes the guest.

The rule library (`bazel/semgrep/rules/`, about 93 rules in ten language directories, exposed as `:<lang>_rules` filegroups merged with the matching Pro pack) is the repo's structural lint: no Dockerfiles, no `rules_python`, no hardcoded in-cluster URLs or image digests, required probes and limits in manifests, and so on. `semgrep-core` ignores `paths:` filters and `# nosemgrep`, so a suppression is `exclude_rules` on the BUILD target and a path-scoped rule gets its own target. `.semgrepignore` keeps the rule fixtures, the ADR tree, the vendored OCaml sources and the generated docs manifest out of scans. Pro interfile analysis runs offline: the App token defaults to a placeholder, the API URL to a dead socket, and result upload is disabled.

**None of it gates a PR today (2026-09-05).** The vendored OSS engine was extracted from the wheel without the `libs/` directory its RPATH points at, so on the Linux runner it cannot start, and `semgrep-test.sh` treats an engine that cannot execute as "wrong platform" and exits 0 with a SKIPPED notice. Every Semgrep test target has been green without scanning (#4777). #5746 (merged today) packages the libraries and globs them into the engine filegroup; the rebuilt images are blocked on GHCR write credentials for the user-owned packages (the workflow logs in with the org repo's token), the pinned digests are still the 2026-08-17 images, and the fail-closed scripts wait on branch `fix/semgrep-fail-closed` until the images exist. Separately, the rule-library self-tests (`SEMGREP_TEST_MODE`) exit 0 by design because the `# ruleid:` annotation protocol needs the Python CLI, and the Pro engine image is a musl build that cannot run on the glibc executor at all, which is why it is unused with the offline placeholder.

(see: `bazel/semgrep/README.md`, `bazel/semgrep/defs/`, `bazel/semgrep/rules/BUILD`, `bazel/semgrep/third_party/`, `.github/workflows/update-semgrep-pro.yaml`, `.semgrepignore`, `.bazelrc` under `test:ci`)

**Why.** Direct `semgrep-core` over the Python wrapper cut a diff scan from over two minutes to about thirty seconds and made results deterministic (ADR security/001); vendoring the engine as a digest-pinned OCI artifact was chosen over pip because pip resolution is not hermetic and the Pro binary is not on PyPI at all. Offline interfile with a placeholder token replaced the original required-credentials design, which tied every test to a live App session; the cost is that CI findings never reach the App. The fail-open skip was written for macOS, where the Linux ELF cannot run, and its blast radius on Linux is the #4777 finding above.

---

## 9. OCaml

`bazel/ocaml` is a native OCaml ruleset (`ocaml_library`, `ocaml_binary`, `ocaml_test`, `ocaml_ppx`) whose one purpose is building the Semgrep engine with Bazel on this repo's RBE. The compiler is the pinned Semgrep OCaml 5.3 fork, built from source as a Bazel action on the executor it will run on and staged into every OCaml action as a sysroot tar: this BuildBuddy deployment does not honour per-action `container-image`, and a compiler built on the workflow runner links a newer glibc than the executor has. The opam universe is a committed `lock.json` (url plus sha256 per package, maintained from a workstation by `update_lock.py`); each package is fetched and either translated from its own dune files by `dune2bazel.py`, which rejects loudly on anything it does not model, or built from a hand-written override. The pinned Semgrep CE tree (`semgrep_src`) is translated the same way, bottom-up, with a README table of the frontier reached so far. tOyCaml (`examples/toycaml`) is the engine-shaped acceptance target the ruleset grows against.

Per-arch toolchains come from the `OCAML_ARCHES` registry: linux x86_64 and aarch64 are both enabled and registered, aarch64 is a registered execution platform, and there is deliberately no unconstrained fallback toolchain (it would outrank the per-arch ones from the default amd64 executor and hand aarch64 targets x86_64 binaries). The arm64 pool is asserted on every full CI run by `executor_arch_probe_arm64_test`, but nothing else selects the aarch64 platform: the arm64 example shard was dropped when the workflow collapsed into `pr-checks` on 2026-08-09, so the aarch64 sysroot and toolchain are registered and unexercised, and the `no-arm64` tags on the C-stub examples currently exclude nothing. The examples run on amd64 as part of `//...`. There is no OCaml Gazelle extension and no CLI release lane; both are decided and unbuilt (Decision history).

(see: `bazel/ocaml/README.md`, `bazel/ocaml/toolchain/arches.bzl`, `bazel/ocaml/platforms/BUILD`, `bazel/ocaml/opam/`, `bazel/ocaml/semgrep_src/README.md`, `MODULE.bazel` `register_toolchains`)

**Why.** The custom ruleset was kept over obazl because obazl imports a host-built opam switch, which on this RBE means the runner's glibc executing on the executor, the exact mismatch the from-source compiler action escapes (ADR tooling/004). Per-arch native executors were chosen over cross-compilation, immature at OCaml 5.3 and needing per-target compiler forks (ADR tooling/006), and over QEMU, five to twenty times slower on compiler workloads (ADR tooling/008); a darwin executor means Apple hardware because the SDK licence ties it there, and the accepted cost is a release lane nobody has provisioned. Per-library compile actions trade per-module incrementality for a driver simple enough to translate dune stanzas onto, and the loud-rejection translator makes every unmodelled feature a named error rather than a silent mistranslation.

---

## 10. Erlang and TLA+

Both are build-time-only toolchains for EmberVM, vendored prebuilt rather than built. `bazel/erlang` fetches the hex.pm OTP build compiled for the executor's Ubuntu 22.04 (so crypto links the executor's libssl with no provisioning), Elixir, and the hex dependency tarballs as `http_file`s that the `mix_*.sh` drivers unpack into `deps/` as path deps, so mix never contacts hex.pm; the deployed control plane gets its OTP from Wolfi. `bazel/tla` fetches `tla2tools.jar` (v1.7.4, the last tag whose asset is immutable) and a Temurin 21 JRE for the TLC checks over `projects/embervm/specs`, with `tlc.sh` reporting INCOMPLETE rather than PASS when `stopAfter` truncates a run.

Their suites are genrules, not test rules, tagged `verification` so `affected-targets.sh` selects them by tag (#5538); they never appear in the `Executed N out of M` count, and a failure surfaces as a build failure. Nothing stops the next such genrule being added untagged (#5626).

(see: `bazel/erlang/repositories.bzl`, `bazel/tla/repositories.bzl`, `projects/embervm/control/BUILD`, `projects/embervm/specs/BUILD`)

**Why.** Prebuilt OTP and a vendored JRE were chosen over hermetic rules_erlang and rules_java toolchains because the executor is a known Ubuntu 22.04 image and nothing deployed depends on either artifact; the cost is a pin that moves by hand. The suites stayed genrules rather than test rules because the mix and TLC drivers own their own process lifecycle, and a tag is the cheapest way to make the affected-target walk see them.

---

## Direction

Decided and not yet built, each with the issue that tracks it. A row leaves
this table when the work ships or the issue closes without it.

| Direction | Decided in | Tracks | State |
| --- | --- | --- | --- |
| Semgrep tests fail closed instead of skip-passing when the engine or Pro image can't run | section 8 | #4777 | in progress (#5838) |
| The native Linux ARM64 OCaml CI shard runs again instead of sitting registered and unexercised | section 9 | #3927 | not started |
| The ArgoCD live-diff target exists again for charts with `generate_diff` enabled | section 1 | #3916 | not started |
| A Copier template scaffolds new services instead of copying `projects/monolith/deploy/` by hand | Decision history (tooling/002) | #3918 | not started |

## Decision history

The ADR files were removed on 2026-09-06 (#4667); `git log -- docs/decisions/`
has the full text.

| ADR | Decision | Status | Disposition |
|---|---|---|---|
| tooling/001 | Distribute developer tools as a multi-arch OCI image pulled by `crane export`; no local Bazel; all execution remote | Implemented. Standalone render and lint scripts, the live ArgoCD diff (its `diff` target references a script that does not exist) and Claude-in-cluster convergence are unbuilt (#3914, #3915, #3916, #3917) | deleted |
| tooling/002 | Copier template that scaffolds a new service | Draft, never executed; the current recipe is to copy `projects/monolith/deploy/` (#3918) | deleted |
| tooling/003 | Generate the `homelab` CLI and Claude skills from the FastAPI OpenAPI spec | Deprecated; `tools/cli` stays hand-written | deleted |
| tooling/004 | Scale the custom `bazel/ocaml` ruleset rather than adopt obazl: ppx first, a locked opam universe, dune translation, per-arch native toolchains | Accepted, partly executed; CE translation and arm64 C stubs open (#3921, #3922, #3923) | deleted |
| tooling/005 | tOyCaml as the engine-shaped acceptance target | Accepted, built; remaining feature coverage open (#3924) | deleted |
| tooling/006 | Data-driven arch registry; per-arch toolchain registration gated on a verified executor pool | Accepted, executed; the arm64 pool was verified 2026-06-12 | deleted |
| tooling/007 | A Gazelle extension generates first-party OCaml BUILD files | Accepted, unbuilt (#3925) | deleted |
| tooling/008 | One graph, native execution platforms (cloud arm64, self-hosted darwin) for CLI releases; no cross-compilation, QEMU or wasm | Accepted, unbuilt; the arm64 CI shard was removed 2026-08-09 (#3926, #3927, #3928, #3929) | deleted |
| tooling/009 | Per-package visibility and tags classify monolith packages; lint out central `gazelle:exclude` | Accepted, unbuilt; `projects/monolith/BUILD` still carries the central excludes (#3930, #3931, #3932) | deleted |
| tooling/010 | Hermetic Bazel-native visual regression on an apko chromium image | Deprecated; the suite was removed 2026-08-09 | deleted |
| tooling/011 | Two Semgrep scan tiers: a warm single-file `mcp --pro` server for MCP and PR, and a scheduled interfile full scan on main | Accepted; the warm tier lives on EmberVM, the full-scan tier was built on fc-invoke on 2026-07-11 and removed with it on 2026-07-28 | deleted |
| security/001 | Hermetic Semgrep via Bazel: vendored engines, direct `semgrep-core`, cached tests, Gazelle-generated targets | Accepted, executed; the required-credentials decision is reversed (offline placeholder token, upload disabled, #3893) | deleted |
| security/002 | RL-finetune a 9B model to write Semgrep rules from CVE descriptions | Deprecated, nothing built | deleted |
