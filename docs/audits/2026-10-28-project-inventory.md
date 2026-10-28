# Project inventory and dead-code delta, 2026-10-28

The repository retains working deployment paths beside staged and retired components. Keep the wired services and shared tooling. Finish the existing issues for documentation, calibration and staged activation. Recommend retiring the goosecracker recipe residue and World Cup backend only within their existing issues. No removal or activation is performed here.

Two new unused functions are supported by the checks below. Existing findings are linked to #7020 children. Go deadcode did not finish, so this report does not claim complete Go reachability coverage.

## Evidence boundary

- Source: `f2334287e8f65dc2e98d1d06a03ca4ba51c804dc` (`SOURCE_SHA`), observed at **2026-10-28T16:32:36Z**. All repository paths and line numbers below refer to this pin. History tables use author date and show the committer-date distinction where it matters.
- Repository wiring only. No fresh cluster health, request, GPU benchmark, credentials, package-publication or deployment check was performed. This audit itself requires no runtime rollout.
- Authorized scope: [#7366 operator decision](https://github.com/jomcgi-org/homelab/issues/7366#issuecomment-6460542145). Exactly this report and one issue summary comment. Recommendations remain Joe’s decisions. `leave_unmerged`; independent exact-head review is a separate factory node.
- Model/effort requirement: #7366 and #7359 require Astra high. Model routing is owned by the factory; this document supplies repository evidence, not an independent attestation of runner configuration.
- Coverage: **19 immediate project directories, 18 immediate bazel/tools directories, and six requested nested candidates**, 43 rows. Each top-level row owns all its descendants; nested rows deliberately overlap. OCI Model Cache is at `projects/operators/oci-model-cache`, not an immediate project directory. No requested nested candidate is absent at this pin.
- Absent/retired candidates: home configuration (`projects/home-cluster`), standalone fc-invoke and the Semgrep guest were retired; do not infer that `firecracker/sandbox` or every goosecracker-named secret retired with them. Reproduce names with `git ls-tree -r --name-only "$SOURCE_SHA" projects` and the consumer search D.
- GitHub observations were collected after the source pin on 2026-10-28. Q exhausted three open-issue pages (100, 100, 85 objects), comprising 237 issues and 48 PR objects. The separate #7020 sub-issue pagination returned 78 children: 43 open and 35 closed. Issue state is mutable; re-running Q gives a new observation, not a historical snapshot. “No directly assigned issue identified” never means no open work exists.

## Reproduce the inventory

Run source commands in a detached read-only checkout of `SOURCE_SHA`, or prefix individual reads with `git show "$SOURCE_SHA:path"`. This audit used a dedicated linked worktree and did not modify its source files. `rg` was unavailable, so searches used tracked, repository-wide `git grep`.

```sh
SOURCE_SHA=f2334287e8f65dc2e98d1d06a03ca4ba51c804dc
BASE_SHA=575f982b11dc18731a5d23080fb187c5cbd49e02
# I: exact top-level coverage; the tables use this sorted inventory.
git ls-tree -d --name-only "$SOURCE_SHA:projects"
git ls-tree -d --name-only "$SOURCE_SHA:bazel/tools"
# G and P: trace the root before considering an Application file active.
git show "$SOURCE_SHA:projects/gke-cluster/kustomization.yaml"
git show "$SOURCE_SHA:projects/gke-apps/kustomization.yaml"
git show "$SOURCE_SHA:projects/platform-gke/kustomization.yaml"
# D: substitute the row name or exact symbol; inspect matching callers.
git grep -n -i -F "$NAME" "$SOURCE_SHA" -- projects bazel tools .github MODULE.bazel buildbuddy.yaml
# H: first surviving record is the last substantive commit for a row.
git log --format="%H%x09%aI%x09%an%x09%s" "$SOURCE_SHA" -- "$ROW" |
  grep -viE "chart-version-bot|ci-format-bot|chore\(charts\): publish|style: auto-format|write.?back" | head -1
# L: approximate tracked text lines, excluding undecodable binary files.
# See the Python reproduction below; no generated-source exclusion for L.
# Q: exhaust pagination; exclude objects containing pull_request.
gh api --paginate --slurp "repos/jomcgi-org/homelab/issues?state=open&per_page=100"
gh api --paginate --slurp repos/jomcgi-org/homelab/issues/7020/sub_issues
gh api --paginate --slurp repos/jomcgi-org/homelab/issues/7366/comments
# Fetch each cited issue and its comments; inspect sibling links in #6829.
gh issue view "$N" --repo jomcgi-org/homelab --json number,title,state,body,comments
```

H excludes bot/format/version-writeback churn by author and subject. Dependency changes, source refactors, tests and documentation changes count as substantive. `git log` traversal order is retained; it is not a sort by author timestamp. Every present row has a surviving commit. Approximate retirement counts use L over the named subtree only, including tests/docs and generated text; they are not executable-line counts.

T and L below reproduce direct test declarations and text counts for each `$ROW`. T is a lexical inventory, not a Bazel graph evaluation. Macro-generated targets, cross-package tests and verification genrules are described separately.

```python
import pathlib, re, subprocess
row = "projects/embervm"  # replace with any table path at SOURCE_SHA
files = subprocess.check_output(["git", "ls-files", row], text=True).splitlines()
total = 0
for file in files:
    try:
        text = pathlib.Path(file).read_text()
    except (UnicodeError, OSError):
        continue
    total += len(text.splitlines())
    if pathlib.Path(file).name not in ("BUILD", "BUILD.bazel"):
        continue
    for m in re.finditer(r"^([a-zA-Z_][\w]*test)\(\n(.*?)^\)", text, re.M | re.S):
        name = re.search(r'name\s*=\s*"([^"]+)"', m[2])
        if name:
            print(file, text[:m.start()].count("\n") + 1, name[1], m[2])
print("tracked text lines", total)
```

## What CI can select

C: `buildbuddy.yaml:24-25,83-104,327-354` defines Linux Ubuntu 24.04 `pr-checks` for PRs, main and queue pushes. Main/queue use `//...`; PRs invoke `bazel/tools/ci/affected-targets.sh`. That selector uses `tests(rdeps(deps(all_tests), changed_labels))` and a separate verification-target walk, falling back to the full suite for BUILD/lock/tooling/deletion changes or query failure (`affected-targets.sh:17-37,298-340`). `ci test` runs the corresponding logic on hosted Linux (`bazel/tools/ci/local-affected-test.sh`).

Selectable below means a declared target can enter that graph when its inputs are affected, subject to tags and dependencies. It does not mean that this documentation PR selects it, or that a prior CI run executed it. At source inspection time no project test execution was observed. Required PR CI evidence belongs to the delivery receipt, after the report commit exists. A green status alone supplies no runtime-suite test count.

- Standard test invocations exclude `external` and `future` tags and use `--deleted_packages=bazel/tools/python` (`buildbuddy.yaml:340,350`). `bazel/tools/python:gazelle_python_manifest.test` is therefore excluded even though its macro declares it. #7087 already tracks this.
- `.bazelignore:3` excludes `projects/oom-inference`; it has no Bazel BUILD. Rust/Cargo checks are outside the required Bazel selection. No Cargo run was performed here.
- `projects/loom/deploy:manifests` carries `manual` (`BUILD:44`), but it is a genrule used as test data by `chart_render_test`, not an excluded test. Its explicit dependency remains reachable. No other literal `manual`, `external`, or `future` test tags were found in the enumerated BUILD files by `git grep -nE 'manual|"external"|"future"|future = True' -- 'projects/**/BUILD*' 'bazel/tools/**/BUILD*'`. Macros can add tags: inspect `.bzl` definitions too.
- EmberVM control, protobuf roundtrip and TLA specs include verification-tagged genrules (`projects/embervm/control/BUILD:66`, `proto/embervm/node/v1/roundtrip/BUILD:46`, `specs/BUILD`). They supplement the direct-test counts. The BDD future action at `buildbuddy.yaml:549` onward is commented out.
- Monolith excludes subpackages from Gazelle discovery but declares tests centrally (`projects/monolith/AGENTS.md`, `projects/monolith/BUILD`). Worldcup/moving having zero local test declarations is not zero test coverage. Chart macros also emit render/lock tests not counted by T.

## Per-directory recommendations

Each path in the first column expands relative documentation paths in that row. G/P/D/H/T/L/Q refer to the exact commands above. Open-issue cells give relevant examples, not a semantic census. All verdicts are recommendations, with no authority to execute retirements.

| Project | Deployed on the hub? | CI runs its tests? | Last substantive commit | Docs claim vs reality | Open issues | Verdict |
| --- | --- | --- | --- | --- | --- | --- |
| `bazel/tools/buildbuddy` | Build/developer tooling, no standalone hub service. `bb_usage.py` CLI and `bb_usage_test` support build-traffic inspection; no service chart. | T: 1 direct test declarations; e.g. `//bazel/tools/buildbuddy:bb_usage_test` (`bazel/tools/buildbuddy/BUILD:15`). Selectable under C; execution unobserved. | `613331a5ebfd4a0735e6aea287095e7c4f875b4b` (2026-09-27; H) | No directory-local README or ARCHITECTURE.md. `bb_usage.py` CLI and `bb_usage_test` support build-traffic inspection; no service chart. | No directly assigned issue identified in Q; this is not proof of no related work. | keep: retained diagnostic CLI and tests. |
| `bazel/tools/ci` | Build/developer tooling, no standalone hub service. `buildbuddy.yaml:343` calls affected-targets; `.envrc` exposes the local ci command. | T: 8 direct test declarations; e.g. `//bazel/tools/ci:central_build_ratchet_test` (`bazel/tools/ci/BUILD:19`). Selectable under C; execution unobserved. | `7c659f956cbadbcb90fffbc8aa0b2dc1aa6a28e7` (2026-10-25; H) | No directory-local README or ARCHITECTURE.md. `buildbuddy.yaml:343` calls affected-targets; `.envrc` exposes the local ci command. | [#7005](https://github.com/jomcgi-org/homelab/issues/7005) (open) | keep: required PR workflow and local test selector. |
| `bazel/tools/cluster` | Build/developer tooling, no standalone hub service. `BUILD` exports `argocd_api`; `scripts/argocd-api.sh` is an operator CLI. | T: no direct local test declarations. Consumer/macro coverage only where described in C; execution unobserved. | `70ca0d484dd63f2dcbd25220a8bb1ceb4dd40375` (2026-10-19; H) | No directory-local README or ARCHITECTURE.md. `BUILD` exports `argocd_api`; `scripts/argocd-api.sh` is an operator CLI. | No directly assigned issue identified in Q; this is not proof of no related work. | keep: explicit operator tool; lack of in-repo callers is not proof of disuse. |
| `bazel/tools/embervm` | Build/developer tooling, no standalone hub service. `measure-vm-overhead.sh` measures guest memory; #7083 explicitly retains it for jailer soak. | T: 1 direct test declarations; e.g. `//bazel/tools/embervm:measure_vm_overhead_test` (`bazel/tools/embervm/BUILD:7`). Selectable under C; execution unobserved. | `ed8240c4ebf074e98873828e59376ad90929242e` (2026-09-15; H) | No directory-local README or ARCHITECTURE.md. `measure-vm-overhead.sh` measures guest memory; #7083 explicitly retains it for jailer soak. | [#7083](https://github.com/jomcgi-org/homelab/issues/7083) (open), [#7371](https://github.com/jomcgi-org/homelab/issues/7371) (open) | keep: #7083 records the retention decision. |
| `bazel/tools/format` | Build/developer tooling, no standalone hub service. `buildbuddy.yaml` invokes formatting/generation helpers; `BUILD` defines format orchestration. | T: 5 direct test declarations; e.g. `//bazel/tools/format:check_migration_ordering_test` (`bazel/tools/format/BUILD:19`). Selectable under C; execution unobserved. | `ad6e26beaf8ec832753a8ea3cdcecb2615c7cfd0` (2026-10-27; H) | No directory-local README or ARCHITECTURE.md. `buildbuddy.yaml` invokes formatting/generation helpers; `BUILD` defines format orchestration. | No directly assigned issue identified in Q; this is not proof of no related work. | keep: CI formatting and dependency-generation consumers. |
| `bazel/tools/git` | Build/developer tooling, no standalone hub service. `.pre-commit-config.yaml:8,57` and `.claude/settings.json:52` invoke these hooks. | T: no direct local test declarations. Consumer/macro coverage only where described in C; execution unobserved. | `0671cba45991212a78f54383a798f598579ae3a4` (2026-09-05; H) | No directory-local README or ARCHITECTURE.md. `.pre-commit-config.yaml:8,57` and `.claude/settings.json:52` invoke these hooks. | No directly assigned issue identified in Q; this is not proof of no related work. | keep: active commit/worktree policy checks. |
| `bazel/tools/hf2oci` | Build/developer tooling, no standalone hub service. `cmd` builds the CLI; `projects/operators/oci-model-cache` imports its packages and the tools image packages the binary. | T: 6 direct test declarations; e.g. `//bazel/tools/hf2oci/cmd/hf2oci/cmd:cmd_test` (`bazel/tools/hf2oci/cmd/hf2oci/cmd/BUILD:20`). Selectable under C; execution unobserved. | `81da76990f0f94d20cf0834cd068e31b7b9397a2` (2026-10-18; H) | `bazel/tools/hf2oci/README.md:3`: "Copy HuggingFace model repositories to OCI container registries." `cmd` builds the CLI; `projects/operators/oci-model-cache` imports its packages and the tools image packages the binary. No contradiction established for that quoted claim. | [#7062](https://github.com/jomcgi-org/homelab/issues/7062) (open) | keep: CLI and retained operator consumer; existing unused wrappers belong to #7062. |
| `bazel/tools/hooks` | Build/developer tooling, no standalone hub service. `.claude/settings.json` invokes the shell policy checks; BUILD declares shell tests. | T: 8 direct test declarations; e.g. `//bazel/tools/hooks:prefer_bb_remote_test` (`bazel/tools/hooks/BUILD:7`). Selectable under C; execution unobserved. | `70ca0d484dd63f2dcbd25220a8bb1ceb4dd40375` (2026-10-19; H) | No directory-local README or ARCHITECTURE.md. `.claude/settings.json` invokes the shell policy checks; BUILD declares shell tests. | No directly assigned issue identified in Q; this is not proof of no related work. | keep: hook configuration has concrete consumers. |
| `bazel/tools/http` | Build/developer tooling, no standalone hub service. `MODULE.bazel:68,477,507` loads these repository rules. | T: 1 direct test declarations; e.g. `//bazel/tools/http:stage_layer_tar_test` (`bazel/tools/http/BUILD:18`). Selectable under C; execution unobserved. | `77be23f2b1ba1fda78fc3170503bf6e68f6e643f` (2026-10-23; H) | No directory-local README or ARCHITECTURE.md. `MODULE.bazel:68,477,507` loads these repository rules. | No directly assigned issue identified in Q; this is not proof of no related work. | keep: active tool/archive repository declarations. |
| `bazel/tools/image` | Build/developer tooling, no standalone hub service. `bootstrap.sh` consumes the tools image; `BUILD` assembles per-platform tools. | T: 4 direct test declarations; e.g. `//bazel/tools/image:python_deps_test` (`bazel/tools/image/BUILD:314`). Selectable under C; execution unobserved. | `70ca0d484dd63f2dcbd25220a8bb1ceb4dd40375` (2026-10-19; H) | `bazel/tools/image/README.md:3`: "Builds the multi-arch developer tools OCI image pushed to" `bootstrap.sh` consumes the tools image; `BUILD` assembles per-platform tools. No contradiction established for that quoted claim. | No directly assigned issue identified in Q; this is not proof of no related work. | keep: developer-tool distribution consumer. |
| `bazel/tools/js` | Build/developer tooling, no standalone hub service. `projects/monolith/BUILD:61` loads exec_filegroup; frontend/image rules use the tar helper. | T: no direct local test declarations. Consumer/macro coverage only where described in C; execution unobserved. | `68c06338c9645795560dedcd3174b8b0f01c6071` (2026-10-19; H) | `bazel/tools/js/README.md:3`: "Bazel helpers for building JavaScript and TypeScript frontends in this" `projects/monolith/BUILD:61` loads exec_filegroup; frontend/image rules use the tar helper. No contradiction established for that quoted claim. | [#7090](https://github.com/jomcgi-org/homelab/issues/7090) (open) | keep: frontend build consumers; #7090 owns the separate Node pin issue. |
| `bazel/tools/lint` | Build/developer tooling, no standalone hub service. `MODULE.bazel:207` registers `//bazel/tools/lint:nogo`. | T: no direct local test declarations. Consumer/macro coverage only where described in C; execution unobserved. | `70ca0d484dd63f2dcbd25220a8bb1ceb4dd40375` (2026-10-19; H) | No directory-local README or ARCHITECTURE.md. `MODULE.bazel:207` registers `//bazel/tools/lint:nogo`. | No directly assigned issue identified in Q; this is not proof of no related work. | keep: build-time Go analysis registration. |
| `bazel/tools/oci` | Build/developer tooling, no standalone hub service. `bazel/helm` consumes OciImageInfo; application BUILD files load the image macros. | T: no direct local test declarations. Consumer/macro coverage only where described in C; execution unobserved. | `70ca0d484dd63f2dcbd25220a8bb1ceb4dd40375` (2026-10-19; H) | `bazel/tools/oci/README.md:3`: "Bazel macros for building OCI images and the `OciImageInfo` provider" `bazel/helm` consumes OciImageInfo; application BUILD files load the image macros. No contradiction established for that quoted claim. | No directly assigned issue identified in Q; this is not proof of no related work. | keep: active image construction and chart pinning graph. |
| `bazel/tools/platforms` | Build/developer tooling, no standalone hub service. `projects/monolith/domain_images.bzl:164,180` names platform targets; this fan-out itself is under #7026. | T: no direct local test declarations. Consumer/macro coverage only where described in C; execution unobserved. | `d7bad223319fe9bc184276406843d50a9ecff8e9` (2026-03-08; H) | No directory-local README or ARCHITECTURE.md. `projects/monolith/domain_images.bzl:164,180` names platform targets; this fan-out itself is under #7026. | [#7026](https://github.com/jomcgi-org/homelab/issues/7026) (open) | keep: platform definitions are shared; decide the fan-out in #7026 without assuming all platform targets are dead. |
| `bazel/tools/postgres` | Build/developer tooling, no standalone hub service. `MODULE.bazel:369` loads the extension; PostgreSQL fixture targets consume its binaries. | T: no direct local test declarations. Consumer/macro coverage only where described in C; execution unobserved. | `a0f1027f01aeff9a1f922996755ae7345bbc82b4` (2026-08-22; H) | No directory-local README or ARCHITECTURE.md. `MODULE.bazel:369` loads the extension; PostgreSQL fixture targets consume its binaries. | No directly assigned issue identified in Q; this is not proof of no related work. | keep: migration/integration fixture dependency. |
| `bazel/tools/previews` | Build/developer tooling, no standalone hub service. `.github/workflows/fixture-preview-build.yml` and `fixture-preview-publish.yml` call preview tooling. | T: 3 direct test declarations; e.g. `//bazel/tools/previews:publish_test` (`bazel/tools/previews/BUILD:4`). Selectable under C; execution unobserved. | `468a7a0d56979614930415bdd22f5bf75acfa3a0` (2026-10-23; H) | No directory-local README or ARCHITECTURE.md. `.github/workflows/fixture-preview-build.yml` and `fixture-preview-publish.yml` call preview tooling. | No directly assigned issue identified in Q; this is not proof of no related work. | keep: fixture build/publication workflow consumers. |
| `bazel/tools/pytest` | Build/developer tooling, no standalone hub service. `BUILD:111` maps py_test to this wrapper; project BUILD files explicitly load it. | T: no direct local test declarations. Consumer/macro coverage only where described in C; execution unobserved. | `2adbd9ed00ba5ef4400ddec14ede0fe41fe4e693` (2026-10-03; H) | No directory-local README or ARCHITECTURE.md. `BUILD:111` maps py_test to this wrapper; project BUILD files explicitly load it. | No directly assigned issue identified in Q; this is not proof of no related work. | keep: test entrypoint wrapper, not a service. |
| `bazel/tools/python` | Build/developer tooling, no standalone hub service. Root `BUILD:117` points at gazelle_python.yaml. Its macro emits a manifest drift test; standard CI deletes this package from analysis. | Macro declares `gazelle_python_manifest.test` (`BUILD:25`), excluded by C’s deleted_packages flag. Execution unobserved. | `a483a913be2aca0333195265764fd44110f075c1` (2026-03-29; H) | No directory-local README or ARCHITECTURE.md. Root `BUILD:117` points at gazelle_python.yaml. Its macro emits a manifest drift test; standard CI deletes this package from analysis. | [#7087](https://github.com/jomcgi-org/homelab/issues/7087) (open) | finish: #7087 owns the excluded manifest check; retain generation configuration until that fix is accepted. |
| `projects/design-system` | CSS ships in monolith frontend: `frontend/src/routes/+layout.svelte:2` imports the token contract. Shared primitives are opt-in. | T: no direct local test declarations. Consumer/macro coverage only where described in C; execution unobserved. | `83618e5a29f72f9615548ab59f23555dc7bbbfef` (2026-10-18; H) | `README.md:60`: "The contract is wired; production pages do not consume the shared primitives." The layout import supports the contract claim; this does not imply every primitive has a production consumer. | [#7016](https://github.com/jomcgi-org/homelab/issues/7016) (open), [#7160](https://github.com/jomcgi-org/homelab/issues/7160) (open) | keep: retain the consumed token contract; finish the tracked migration before removing legacy styles. |
| `projects/embervm` | Yes: G lists `embervm` and `embervm-dev`; both Applications select the embervm chart and their respective base/GKE overlays. | T: 69 direct test declarations; e.g. `//projects/embervm/chart:chart_inpod_bake_test` (`projects/embervm/chart/BUILD:104`). Selectable under C; execution unobserved. | `b5ec277f64152a62de26354ed62b81e841cfeb0f` (2026-10-24; H) | `deploy/README.md:97`: "ArgoCD and SigNoz at `private.jomcgi.dev/app/*`, `kubectl get workloads`" retains obsolete access guidance. `projects/platform/argocd/values.yaml` disables ingress and the active telemetry overlay is `projects/platform-gke/otel-collector`. The home-overlay section already calls itself residual; do not call all of it an undisclosed live claim. | [#7061](https://github.com/jomcgi-org/homelab/issues/7061) (open), [#7371](https://github.com/jomcgi-org/homelab/issues/7371) (open), [#6562](https://github.com/jomcgi-org/homelab/issues/6562) (open), [#6563](https://github.com/jomcgi-org/homelab/issues/6563) (open) | finish: #7061 owns the documentation mismatch; retain the deployed runtime and the separate jailer acceptance work. |
| `projects/firecracker` | Ships inside EmberVM: `projects/embervm/chart/BUILD:26` onward pins sandbox guest images; substrate code is imported by noded and runtime BUILD targets. | T: 4 direct test declarations; e.g. `//projects/firecracker/sandbox/guest-init/internal/handler:handler_test` (`projects/firecracker/sandbox/guest-init/internal/handler/BUILD:16`). Selectable under C; execution unobserved. | `f1a8c9082c03cb33255f92ce9427c328dcc76f88` (2026-10-20; H) | `README.md:4`: "[EmberVM](../embervm/). The original `fc-invoke` daemon and its deployment were" continues with its retirement on line 5. The retained guest image wiring agrees. No separate Firecracker Application is expected. | [#7062](https://github.com/jomcgi-org/homelab/issues/7062) (open), [#7151](https://github.com/jomcgi-org/homelab/issues/7151) (open) | keep: guest images and substrate have consumers; do not revive retired fc-invoke or Semgrep guests. |
| `projects/gke-apps` | Yes, Application definitions reached by `projects/gke-cluster/kustomization.yaml:17`. G includes eight application directories; Loom is deliberately not listed. | T: no direct local test declarations. Consumer/macro coverage only where described in C; execution unobserved. | `22236387e689b0834b62f7abf7c129728685f451` (2026-10-20; H) | No root README or ARCHITECTURE.md. `kustomization.yaml` and each selected Application are the source contract. | [#6605](https://github.com/jomcgi-org/homelab/issues/6605) (open), [#3832](https://github.com/jomcgi-org/homelab/issues/3832) (open) | keep: this is the active application registry; finish Loom only under #6605. |
| `projects/gke-cluster` | Yes, management wiring: `root/root-application.yaml` selects this root, which includes platform-gke and gke-apps. | T: no direct local test declarations. Consumer/macro coverage only where described in C; execution unobserved. | `3e35cf4d065eb589806945b499fe6903ec2cb4ad` (2026-10-20; H) | No root README or ARCHITECTURE.md. `kustomization.yaml:1` says "# Hand-maintained during the GKE migration; not generated. Lists what the GKE"; its resource entries are concrete. | No directly assigned issue identified in Q; this is not proof of no related work. | keep: retain the parent/root reconciliation wiring. |
| `projects/inference` | Partly: G selects `inference/deploy`, applying values.yaml then values-gke.yaml. The latter enables embeddings and disables NInfer and cfIngress. Monolith GKE values line 233 and public-tier GKE values line 42 consume the embeddings Service. | T: no direct local test declarations. Consumer/macro coverage only where described in C; execution unobserved. | `1743873b9eeae12da107bfb7d63427d07a265812` (2026-10-19; H) | `README.md:3`: "This project serves generative inference from the home GPU and embeddings from" overstates the deployed half. `values-gke.yaml:1-9` disables NInfer; `templates/service.yaml` remains unconditional. Existing #7076 describes this mismatch. | [#7076](https://github.com/jomcgi-org/homelab/issues/7076) (open), [#7013](https://github.com/jomcgi-org/homelab/issues/7013) (open), [#7042](https://github.com/jomcgi-org/homelab/issues/7042) (open) | finish: #7076 removes the NInfer half and corrects docs; keep embeddings and preserve any pi values consumer until checked. |
| `projects/loom` | Default-off: its Application exists but G omits `./loom`; neither its namespace nor CNPG manifests are reached through that Application. `deploy/values.yaml` also holds worker replicas at zero. | T: 3 direct test declarations; e.g. `//projects/loom/deploy:deploy_manifests_test` (`projects/loom/deploy/BUILD:18`). Selectable under C; execution unobserved. Manual render genrule is an explicit test dependency (C). | `58422775ed50693b7a8a7f0604e7f0b0859cd582` (2026-10-20; H) | `deploy/README.md:9`: "This configuration is default-off. The Application at `projects/gke-apps/loom`" agrees with G. Source-render tests do not prove package publication, credentials, backups or runtime enforcement. | [#6601](https://github.com/jomcgi-org/homelab/issues/6601) (open), [#6605](https://github.com/jomcgi-org/homelab/issues/6605) (open), [#6604](https://github.com/jomcgi-org/homelab/issues/6604) (open), [#6634](https://github.com/jomcgi-org/homelab/issues/6634) (open), [#6635](https://github.com/jomcgi-org/homelab/issues/6635) (open) | finish: #6605 owns activation and human operational checks. Keep the staged configuration; no activation in this audit. |
| `projects/mcp` | Yes: G selects context-forge-gateway; its Application points to `projects/mcp/context-forge-gateway/chart`. This wraps the upstream chart. | T: 3 direct test declarations; e.g. `//projects/mcp/context-forge-gateway/chart:reconcile_team_mapping_test` (`projects/mcp/context-forge-gateway/chart/BUILD:23`). Selectable under C; execution unobserved. | `b74ce1805338b65ca6ac885d158c4c38836ed066` (2026-10-19; H) | `ARCHITECTURE.md:21`: "The direct `mcp.jomcgi.dev` route is staged, not live until the hub value flips" agrees with the monolith `cfIngress.mcp.enabled` gate and the retained gateway Application. Runtime user registrations were not queried. | [#3832](https://github.com/jomcgi-org/homelab/issues/3832) (open), [#3833](https://github.com/jomcgi-org/homelab/issues/3833) (open), [#5636](https://github.com/jomcgi-org/homelab/issues/5636) (open) | finish: #3832 and #3833 own cutover/decommission; keep the current gateway until that work is accepted. |
| `projects/model-bench` | No hub Application. Local benchmark CLI; `bench/report.py` and committed leaderboard data are repo consumers. A CLI need not be a deployed service. | T: 25 direct test declarations; e.g. `//projects/model-bench/bench:index_test` (`projects/model-bench/bench/BUILD:44`). Selectable under C; execution unobserved. | `177a9302bf11da36359b0a62c73580e0e7e9649a` (2026-10-28; H) | `ARCHITECTURE.md:3`: "The CLI runs task fixtures through file-editing agents and deterministic" matches `bench/cli.py` and verifier packages. `README.md` section "In-cluster llama.cpp" still describes the undeployed NInfer route (#7076). The retired SigNoz probe remains under `probe/`. | [#6919](https://github.com/jomcgi-org/homelab/issues/6919) (open), [#6922](https://github.com/jomcgi-org/homelab/issues/6922) (open), [#7076](https://github.com/jomcgi-org/homelab/issues/7076) (open) | finish: #6919 removes probe only; #6922 owns human calibration. Keep bench and its reports. |
| `projects/monolith` | Yes: G selects the monolith chart and base/GKE overlays. `app/modules_private.py` composes live modules; default-off paths require individual checks. | T: 606 direct test declarations; e.g. `//projects/monolith:platform_auth_service_test` (`projects/monolith/BUILD:2223`). Selectable under C; execution unobserved. | `ff238b253c116bc87244b5ae99c823f53e416faa` (2026-10-27; H) | `ARCHITECTURE.md:3`: "Factory ownership is consolidated under `factory.module` within this application." `app/modules_private.py` and `factory/module.py` agree. Separate stale-doc claims remain tracked by #7098; this matched line does not certify the whole architecture. | [#7098](https://github.com/jomcgi-org/homelab/issues/7098) (open), [#7024](https://github.com/jomcgi-org/homelab/issues/7024) (open), [#7043](https://github.com/jomcgi-org/homelab/issues/7043) (open), [#7038](https://github.com/jomcgi-org/homelab/issues/7038) (open), [#5277](https://github.com/jomcgi-org/homelab/issues/5277) (open) | keep: application composition is wired; finish individual outstanding items in their existing issues. |
| `projects/monolith-agents` | Yes: G selects the dedicated chart; its deployment runs the agents image defined from the monolith agents entrypoint. | T: 2 direct test declarations; e.g. `//projects/monolith-agents/chart:chart_smoke_test` (`projects/monolith-agents/chart/BUILD:17`). Selectable under C; execution unobserved. | `b59f89151a333332511135471a823737ab54e6c0` (2026-10-20; H) | No root README or ARCHITECTURE.md. `projects/mcp/ARCHITECTURE.md` documents the separate agents entrypoint; `chart/templates/deployment.yaml` and `projects/monolith/app/agents_main.py` implement it. | [#6442](https://github.com/jomcgi-org/homelab/issues/6442) (open), [#5704](https://github.com/jomcgi-org/homelab/issues/5704) (open) | keep: separately scoped agent service and its chart remain in the active registry. |
| `projects/monolith-public` | Yes: G selects its chart and base/GKE overlays. The public image has its own entrypoint and reduced import closure in `projects/monolith/BUILD`. | T: no direct local test declarations. Consumer/macro coverage only where described in C; execution unobserved. | `96a73b6ae06e9c68e79e2268d9e2e8f2edf9a0b2` (2026-10-24; H) | No root README or ARCHITECTURE.md. `projects/monolith/README.md` describes tiers; `projects/monolith/app/modules_public.py` is the concrete composition. | [#5276](https://github.com/jomcgi-org/homelab/issues/5276) (open), [#7041](https://github.com/jomcgi-org/homelab/issues/7041) (open) | keep: public tier is wired; #5276 owns the separate egress acceptance. |
| `projects/oom-inference` | No hub Application or shipped-chart consumer found by D. Cargo engine and CLI, with hardware work tracked separately. | Excluded: `.bazelignore:3`; Cargo-only tests, no Bazel BUILD and no Cargo execution observed. | `23fa19a122976078967e0415d74fce9a38897889` (2026-10-28; H) | `README.md:6`: "in pinned RAM and the rest on disk. The CLI is `oominf`; it speaks the OpenAI" is a CLI claim, not a hub claim. `Cargo.toml` and Rust sources implement the CLI. Hardware throughput claims were not remeasured. | [#6829](https://github.com/jomcgi-org/homelab/issues/6829) (open), [#6825](https://github.com/jomcgi-org/homelab/issues/6825) (open), [#6831](https://github.com/jomcgi-org/homelab/issues/6831) (open), [#6835](https://github.com/jomcgi-org/homelab/issues/6835) (open), [#6883](https://github.com/jomcgi-org/homelab/issues/6883) (open), [#6884](https://github.com/jomcgi-org/homelab/issues/6884) (open), [#6885](https://github.com/jomcgi-org/homelab/issues/6885) (open), [#6909](https://github.com/jomcgi-org/homelab/issues/6909) (open) | finish: #6829 and its performance/concurrency follow-ups own measurements; keep the engine. No evidence supports retirement merely because it lacks a hub Application. |
| `projects/operators` | No operator from this directory is registered by G or P. OCI Model Cache is the sole retained implementation; see its separate nested row. | T: 9 direct test declarations; e.g. `//projects/operators/oci-model-cache/api/v1alpha1:v1alpha1_test` (`projects/operators/oci-model-cache/api/v1alpha1/BUILD:20`). Selectable under C; execution unobserved. | `81da76990f0f94d20cf0834cd068e31b7b9397a2` (2026-10-18; H) | `README.md:3`: "This directory retains one Kubernetes operator, `oci-model-cache`, with no" continues "deployment after the home cluster was retired." G/P and the actual directory contents agree. | [#6918](https://github.com/jomcgi-org/homelab/issues/6918) (open), [#7062](https://github.com/jomcgi-org/homelab/issues/7062) (open) | keep: follow #6918 retention direction; unused deployment and retained library code are different questions. |
| `projects/platform` | Partly, through P: 16 platform overlays select this directory’s charts/configuration. This does not make every subdirectory active. Renovate is registered but suspended; inspect its CronWorkflow values. | T: 10 direct test declarations; e.g. `//projects/platform/authentik:blueprint_tags_test` (`projects/platform/authentik/BUILD:31`). Selectable under C; execution unobserved. | `ec3a3a2d4da90a231ab813b51c3ea66d7ec722f1` (2026-10-27; H) | `README.md:4`: "but none of them own. Everything here is deployed the same way as any other service" is generic packaging guidance. Actual hub selection is P, not presence under platform. `ARCHITECTURE.md` explicitly records the retired home configuration. | [#7001](https://github.com/jomcgi-org/homelab/issues/7001) (open), [#7068](https://github.com/jomcgi-org/homelab/issues/7068) (open), [#7075](https://github.com/jomcgi-org/homelab/issues/7075) (open), [#4745](https://github.com/jomcgi-org/homelab/issues/4745) (open), [#3879](https://github.com/jomcgi-org/homelab/issues/3879) (open) | keep: active platform dependencies. Finish the existing staged-feature and documentation issues individually. |
| `projects/platform-gke` | Yes: reached by the cluster root. P lists argocd, onepassword, atlas-operator, cert-manager, cloudnative-pg, authentik, argo-workflows, renovate, otel-collector, cloudflare-gateway, tailscale, kyverno, priority-classes, kargo, spire and polylane. | T: no direct local test declarations. Consumer/macro coverage only where described in C; execution unobserved. | `050d3d1a63aa9819dc6fa13b93aa15abbc4aad05` (2026-10-18; H) | No root README or ARCHITECTURE.md. `kustomization.yaml` is explicit; its Renovate entry says both writers are suspended pending #7068/#7001. Registration does not prove job execution. | [#7001](https://github.com/jomcgi-org/homelab/issues/7001) (open), [#7068](https://github.com/jomcgi-org/homelab/issues/7068) (open), [#4745](https://github.com/jomcgi-org/homelab/issues/4745) (open) | keep: active hub overlays; leave suspended writers to their own acceptance. |
| `projects/scotland-connector` | Yes: G selects scotland-connector; Application names its chart and deploy values. `chart/templates/deployment.yaml` and BUILD define the server/image. | T: 16 direct test declarations; e.g. `//projects/scotland-connector/chart:chart_posture_test` (`projects/scotland-connector/chart/BUILD:33`). Selectable under C; execution unobserved. | `fe41c98ac48c433d8a96956c544267dcd6d249dd` (2026-10-27; H) | `ARCHITECTURE.md:6`: "Tracking issue: #7240. Deployed on the hub through Kargo" matches Application registration and the platform Kargo pipeline source. Runtime traffic was not checked. | [#7240](https://github.com/jomcgi-org/homelab/issues/7240) (open), [#7248](https://github.com/jomcgi-org/homelab/issues/7248) (open), [#7245](https://github.com/jomcgi-org/homelab/issues/7245) (open), [#7265](https://github.com/jomcgi-org/homelab/issues/7265) (open), [#7274](https://github.com/jomcgi-org/homelab/issues/7274) (open) | keep: shipped source and ongoing scoped work; the unused helper below does not justify retiring the service. |
| `projects/sextant` | No hub Application. Generator consumed by OCI Model Cache’s state-machine generation; see `projects/operators/oci-model-cache/internal/statemachine/BUILD` and spec. | T: 5 direct test declarations; e.g. `//projects/sextant/cmd/sextant:main_test` (`projects/sextant/cmd/sextant/BUILD:20`). Selectable under C; execution unobserved. | `eca6ad6e4a54b0e1d32933c215cc5fe7cde64257` (2026-10-18; H) | `README.md:3`: "Generates type-safe Go state machines for Kubernetes operators from a YAML" matches generator code. It does not yet lead with #6918’s required no-deployment statement. | [#6918](https://github.com/jomcgi-org/homelab/issues/6918) (open) | finish: #6918 asks to keep code/posts and state deployment status in the first paragraph. Keep the generator. |
| `projects/shared` | Ships inside another chart: `projects/mcp/context-forge-gateway/chart/Chart.yaml:23` depends on homelab-library; `_helpers.tpl:1` actually calls `homelab.name`. | T: no direct local test declarations. Consumer/macro coverage only where described in C; execution unobserved. | `7c659f956cbadbcb90fffbc8aa0b2dc1aa6a28e7` (2026-10-25; H) | `README.md:4`: "one thing: the `homelab-library` Helm library chart." The tracked subtree contains that library; its helpers are consumed. No mismatch found for this claim. | No directly assigned issue identified in Q; this is not proof of no related work. | keep: shared named templates have an active chart consumer. |
| `projects/operators/oci-model-cache` | No hub registration in G/P. Chart and image publication remain in the build graph; publishing an image does not deploy the controller. | T: 9 direct test declarations; e.g. `//projects/operators/oci-model-cache/api/v1alpha1:v1alpha1_test` (`projects/operators/oci-model-cache/api/v1alpha1/BUILD:20`). Selectable under C; execution unobserved. | `81da76990f0f94d20cf0834cd068e31b7b9397a2` (2026-10-18; H) | `README.md:3`: "Mount Hugging Face models in Kubernetes pods using your OCI registry as the cache." This describes capability without the no-deployment opening required by #6918. `projects/operators/README.md:3-4` already states the retirement. | [#6918](https://github.com/jomcgi-org/homelab/issues/6918) (open), [#7062](https://github.com/jomcgi-org/homelab/issues/7062) (open) | finish: #6918’s documentation-only retention decision; retain the code and posts without activation. |
| `projects/firecracker/sandbox` | Ships inside EmberVM. `chart/BUILD:26-31` pins six guest images; `chart/values.yaml:756` enables sandbox workloads and `templates/workload-sandbox-languages.yaml:1` iterates those gates. `projects/monolith/sandbox/client.py` submits work to the named workloads. | T: 1 direct test declarations; e.g. `//projects/firecracker/sandbox/guest-init/internal/handler:handler_test` (`projects/firecracker/sandbox/guest-init/internal/handler/BUILD:16`). Selectable under C; execution unobserved. | `613331a5ebfd4a0735e6aea287095e7c4f875b4b` (2026-09-27; H) | No README/ARCHITECTURE at sandbox root. Parent `projects/firecracker/README.md:10` calls it "Python sandbox guest retained for the EmberVM task and session runtimes." This understates the six-language chart wiring; the directory is not retired. | No directly assigned issue identified in Q; this is not proof of no related work. | keep: six-language workload/image consumers remain. Correct the parent’s Python-only summary when editing that documentation. |
| `projects/monolith/goosecracker` | Retired harness, retained residue. `BUILD` still packages its Python/config data; recipes are referenced by chat prompts. The historical secret name remains an active provider-key input, expressly protected in `deploy/values.yaml:270-280`. | T: no direct local test declarations. Consumer/macro coverage only where described in C; execution unobserved. | `02c300324671f244476f0f435ad9935212feb996` (2026-09-26; H) | No local README or ARCHITECTURE.md. `projects/monolith/ARCHITECTURE.md` calls the goosecracker agent retired (D search). Packaged residue and prompt references do not establish a working artifact publisher. | [#7029](https://github.com/jomcgi-org/homelab/issues/7029) (open), [#7043](https://github.com/jomcgi-org/homelab/issues/7043) (open) | retire recommendation: the recipe/schema subtree is approximately 1,007 tracked text lines (L); #7043 owns recipe references and #7029 the separate artifact writer. Preserve live provider secrets and artifact reads. |
| `projects/monolith/worldcup` | Still shipped: private and public module lists import `worldcup.module`; `chart/values.yaml:401-407` retains worldcup-sim with `suspend: true`. Stored-data read routes remain. | Central `projects/monolith/BUILD` declares worldcup tests; `app/jobs_main_test.py:147` also covers CLI dispatch. Selectable under C; no observed execution. | `e631cb03cde3a2ad61d88e033da703e96f03cbc3` (2026-10-18; H) | No local README or ARCHITECTURE.md. The chart comment at `values.yaml:398` says "# still serves the stored rows and deleting the entry would tear down the". Module registration supports the stored-data surface; suspended polling does not mean absent routes. | [#6916](https://github.com/jomcgi-org/homelab/issues/6916) (open) | retire recommendation under #6916: approximately 3,282 backend tracked text lines (L), plus the separately scoped wc2026/frank frontend and references in that issue. Existing PR #7226 is not evidence of landed removal. |
| `projects/monolith/moving` | Yes, inside monolith: `app/modules_private.py:29,60`; friends ingress in deploy values; hourly `moving-gcal-sync` entry at `chart/values.yaml:1082` is unsuspended, with no-op behavior when credentials are absent. | Central `projects/monolith/BUILD` declares moving tests and `app/jobs_main_test.py:160` dispatch coverage. Selectable under C; no observed execution. | `f836c46c9ad59b7d7d9171d4c5161c58062d7467` (2026-10-18; H) | No local README or ARCHITECTURE.md. `deploy/values.yaml:120` says "# The moving planner on friends.jomcgi.dev/moving, gated to the authentik". Private module and friends route wiring support it; calendar execution was not observed. | [#7158](https://github.com/jomcgi-org/homelab/issues/7158) (open) | keep: live composition and route consumer; finish the sheet migration under #7158. A personal app is not dead solely because of its name. |
| `projects/platform/authentik` | Yes via P/authentik. Enrollment is off (`values.yaml:6-9`), management is on (`:11-14`). The blueprint template reads both gates. | T: 4 direct test declarations; e.g. `//projects/platform/authentik:blueprint_tags_test` (`projects/platform/authentik/BUILD:31`). Selectable under C; execution unobserved. | `e0db0c80216d8c813cbb2416483c08848c36b892` (2026-10-19; H) | `README.md:3`: "This deploys the official authentik Helm chart with a separately managed" matches the chart/CNPG overlay. #7075’s proposed deletion needs care: `templates/blueprints-configmap.yaml:41-47` includes `optional/platform-profile.yaml` when either management OR enrollment is enabled. | [#7075](https://github.com/jomcgi-org/homelab/issues/7075) (open) | finish: #7075 owns enrollment retirement. Preserve or replace the management consumer before removing the shared profile fragment; do not delete the whole authentik deployment. |

## Existing decisions and mismatches

#6913 is closed. #7020, #7359 and #7366 are open at the GitHub observation. #6913 is the earlier module-level audit; #7020 is the function/config follow-up. Their text describes historical observations, not the present issue state. The current #7020 child census is below. #7359 records the Astra lane and human ownership boundaries. No existing issue or decision was changed by this audit.

#6918 now directs retention without deployment, so retirement is not recommended for Sextant or OCI Model Cache. #6919 directs removal of the inoperative probe, while #6922 remains a human calibration campaign. #7076 directs removal of NInfer’s chart half; the pi runtime exported-values consumer must be checked in that task. #7013/#7042 retain the distinct private Qwen placement question. #6829 and #6883/#6884/#6885 keep hardware and concurrent-serving acceptance separate from repository wiring.

#6601/#6605 own Loom’s activation, backup, credential and restore checks. Their outstanding operations do not create a live-validation requirement for this read-only inventory. #7029 owns the artifact writer, #7043 owns retired goosecracker recipes, and #6916 owns the World Cup/frank retirement. #7158 is the current moving-page work.

The #7061 pattern still exists in the deployment README’s ArgoCD/SigNoz guidance and missing `projects/monolith/deploy/embervm-oplog-secret.md` link. Its home-fleet section already discloses the residual overlay; that narrower correction matters. Authentik supplies another source-contract warning: the management gate currently consumes the shared profile fragment named in #7075’s deletion list. A removal must preserve that consumer.

## Function-level delta

Baseline `575f982b11dc18731a5d23080fb187c5cbd49e02` has author and committer time `2026-10-17T23:05:39Z`. `git merge-base --is-ancestor "$BASE_SHA" "$SOURCE_SHA"` returned 0. Both date-based history and the ancestry range contain **1,318 unique paths**, with an identical intersection. This is a union of paths touched by commits, not merely the net tree diff; subsequently reverted changes remain represented.

```sh
git log --since=2026-10-18T00:00:00Z --format= --name-only "$SOURCE_SHA" | sort -u > date-files.txt
git log --format= --name-only "$BASE_SHA..$SOURCE_SHA" | sort -u > range-files.txt
comm -12 date-files.txt range-files.txt | sed '/^$/d' > intersection.txt
comm -3 date-files.txt range-files.txt  # observed empty
```

The manifest at the end lists the complete intersection with status. Of 1,318 paths, 1,227 still exist and 91 are absent. Two present generated protobuf Go files are excluded from final findings. Inputs comprise 454 Python files and 147 non-generated Go files in 37 directories. The other 624 present files are outside these two language scanners. This does not claim Rust, Elixir, JS/TS/Svelte, SQL or shell function coverage. Deleted files cannot be reported as currently dead functions.

Filter: retain existing `.py`/`.go` paths; exclude `.pb.go`, `_pb2.py`, `_pb2_grpc.py`, and Python/Go files whose first 1,000 characters contain `Code generated`. Tests and fixtures remain in scope. Go tools may analyze additional files in selected packages/programs, but only diagnostics whose file is in this retained intersection can be new delta findings.

### Tool execution

The #7020 recipe names ruff, vulture, Go deadcode (with and without test contexts) and staticcheck, followed by whole-repository search. It supplies no exact tool versions beyond repository pins. This audit ran all four, using test context for Go and recording the incomplete deadcode attempts. No tool was substituted for another.

Tools were installed outside the checkout. Ruff 0.15.5 matches `bazel/tools/tools.lock.json:395`; it reads `pyproject.toml` and uses `--no-cache`. Go 1.26.9 is the guest toolchain; deadcode uses `golang.org/x/tools v0.48.0` from `go.mod:123`. Vulture 2.14 and staticcheck 2026.1 (v0.7.0) are explicit audit-only pins; no repository Vulture/staticcheck pin was found (`git grep -niE 'vulture|staticcheck' -- pyproject.toml go.mod MODULE.bazel bazel/tools/tools.lock.json`).

```sh
python3 -m venv /workspace/audit-tools/venv
/workspace/audit-tools/venv/bin/pip install ruff==0.15.5 vulture==2.14
GOBIN=/workspace/audit-tools/bin go install golang.org/x/tools/cmd/deadcode@v0.48.0
GOBIN=/workspace/audit-tools/bin go install honnef.co/go/tools/cmd/staticcheck@2026.1
# Extract the exact tool inputs from this report's complete manifest.
# Paths in this manifest contain no shell whitespace.
AUDIT_REPORT=docs/audits/2026-10-28-project-inventory.md
PY_FILES=$(awk '/^P / {print $2}' "$AUDIT_REPORT")
GO_PACKAGES=$(awk '/^G / {sub("/[^/]+$", "", $2); print "./" $2}' "$AUDIT_REPORT" | sort -u)
/workspace/audit-tools/venv/bin/ruff check --no-cache --output-format concise $PY_FILES
/workspace/audit-tools/venv/bin/vulture --min-confidence 80 $PY_FILES
/workspace/audit-tools/venv/bin/vulture --min-confidence 60 $PY_FILES
/workspace/audit-tools/bin/deadcode -test ./...
/workspace/audit-tools/bin/staticcheck -tests $GO_PACKAGES
timeout 180 /workspace/audit-tools/bin/deadcode -test ./...
```

| Tool | Observed result | Coverage and interpretation |
| --- | --- | --- |
| Ruff 0.15.5 | Exit 1, 25 diagnostics, 0.25 s. Example: `chart_env_identity_test.py:1487:5: F841 Local variable prod_workloads is assigned to but never used`. | 454 Python paths; configured lint rules, not function reachability. Unused imports/locals do not prove dead functions. No `--fix`. |
| Vulture 2.14 | Exit 3 at 80% confidence (58 warnings, 7.38 s); exit 3 at 60% (809 warnings, including 519 function/method/class leads). An initial xargs wrapper returned 123 for the tool’s diagnostic exit; a direct invocation confirmed 3. | Same 454 files; references in unchanged files are absent from its input and require whole-repo grep. Decorated callbacks, registered tools and fixtures cause false positives. |
| Go deadcode v0.48.0 | First `-test ./...` attempt exceeded a 240-second subprocess deadline (268.3 s including termination); no output. Warm-cache retry with `timeout 180` returned 143 and `Terminated`. | Unknown reachability for the entire Go program, including all 147 in-scope Go files. No findings can be inferred from silence. A no-test run was not completed. |
| Staticcheck 2026.1 | Exit 1 with diagnostics after 202.33 s; 81 diagnostic lines. Includes `registry.go:156:22 ... liveCount ... (U1000)` and `walks.go:590:6 ... sameHours ... (U1000)`. | 37 whole package directories including tests. Most output is SA1019 deprecation, with SA4000/ST1013 too. Filter filenames to the changed universe; do not count deprecation/constant-style findings as dead functions. |

### Verified and excluded findings

Searches below used `git grep -n -w -F SYMBOL "$SOURCE_SHA" -- .`, including tests, BUILD files, templates, generated tracked files and docs. The 519 Vulture function/class leads received this reference search. A grep alone cannot disprove reflection or registration. Only the two new rows have sufficient caller, registration and history evidence to recommend removal; other low-confidence candidates remain unproven.

| Finding | New versus existing | Evidence and reference verification |
| --- | --- | --- |
| `projects/embervm/chart/chart_env_identity_test.py:3167: _overlay_documents` | **New supported unused helper** in the ancestry/date delta. | Vulture 60%; repo-wide word search returns only the definition. Its body only renders supplied overlays. It is neither `test_*` nor a pytest fixture, has no decorator, and the file has no globals/getattr lookup that dispatches it. `_HUB_OVERLAYS` also has only its definition. `git log -S "def _overlay_documents" "$BASE_SHA..$SOURCE_SHA" -- projects/embervm/chart/chart_env_identity_test.py` identifies `a46f33b9c22e45573a488d0ae35323c635e4f9f5`: authored October 11, committed October 18 at 13:07:19Z. Baseline file has no symbol. |
| `projects/scotland-connector/internal/walks/walks.go:590: sameHours` | **New supported unused function**. | Staticcheck U1000; whole-repo word search returns only the definition. It is unexported, has no callers in package tests and no go:linkname/reflection/plugin registration. `git log -S "func sameHours" "$BASE_SHA..$SOURCE_SHA" -- projects/scotland-connector/internal/walks/walks.go` identifies `e9703550f6550f8f31a97b277f3b4d2e1c1db5ea`, committed October 21 at 06:44:10Z. The entire path is absent at baseline. |
| `projects/monolith/factory/orchestration/factory_rollout.py:321: chart_versions` | Existing [#7024](https://github.com/jomcgi-org/homelab/issues/7024) (open). **Not new work.** | Vulture; word grep returns its definition only; `chart_catalog` is called directly at line 339. Baseline already defines chart_versions at line 241, exactly as #7024 records. |
| `projects/embervm/noded/server/registry.go:156: (*vmRegistry).liveCount` | Existing [#7062](https://github.com/jomcgi-org/homelab/issues/7062) (open). **Not new work.** | Staticcheck U1000. `git grep -n -w -F liveCount "$SOURCE_SHA" -- .` and the #7062 body locate the existing helper. No new issue requested. |
| `projects/embervm/runtimes/claude/shim.py:4422: _reap_process` | Baseline residue, excluded from new delta. | Vulture reports the decorator line 4421; grep locates definition at 4422. Baseline already contains the method at line 4106. No proof that its reachability changed in this window; do not call it new. |
| `bazel/tools/previews/publish.py:138: handle_endtag` and companion HTMLParser callbacks | False-positive class of Vulture leads. | `git grep -nE "HTMLParser\|handle_endtag\|handle_startendtag\|handle_data\|handle_comment\|handle_decl\|unknown_decl\|handle_pi" "$SOURCE_SHA" -- bazel/tools/previews` shows the subclass/overrides. The parser dispatches callbacks by contract; direct calls are not required. |
| `projects/monolith/knowledge/extraction.py:230: _cap_subjects` and sibling validators | Registered callbacks, not proven dead. | Whole-repo symbol grep returns definitions; `@field_validator`/`@classmethod` at lines 228-230 and 257-277 register them. `git grep -nE "field_validator\|_cap_subjects\|_cap_body\|_clamp_confidence\|_iso_timestamp" "$SOURCE_SHA" -- projects/monolith/knowledge/extraction.py` verifies the contract. |
| `projects/monolith/grimoire/audience.py:139: _has_pc_postgresql` / `:149: _has_pc_sqlite` | Registered SQL compiler hooks. | `git grep -nE "compiles\|_HasPC\|_has_pc_" "$SOURCE_SHA" -- projects/monolith/grimoire/audience.py` shows dialect-specific `@compiles` registration and the FunctionElement consumer. |

MCP handlers in `agent/mcp.py`, Typer commands in `app/jobs_main.py`, FastAPI routes such as `grimoire/join_links.py:325:list_links`, pytest fixtures and SQLAlchemy event handlers are similarly retained as registered entrypoints, not promoted from static warnings to dead-function findings. Inspect `register_mcp`/`mcp.tool`, `@app.command`, `@router.get`, fixture decorators and event listeners before treating them as unused. No claim is made that every remaining 60% candidate is live or dead.

New-finding deduplication searched every retrieved #7020 child body and the complete open-issue body snapshot for `sameHours`, `_overlay_documents`, `chart_versions` and `_reap_process`. Only chart_versions matched an existing issue (#7024). Staticcheck’s liveCount matches #7062. This covers that audit family and the observed open issues; it is not a search of every historical closed issue or external repository.

## Coverage checks

The 43 inventory rows match I plus the six named nested paths. All have the seven requested columns. Every row has a source/history command; documentation quotations are literal line excerpts, and missing local documentation is identified. Both hub roots, active overlay selection, default-off gates and actual nested consumers were checked. All four analysis tools were attempted; incomplete Go deadcode coverage is explicitly unknown.

Only this report may enter the tracked diff. `git diff --stat` and `git diff --name-only` must name only `docs/audits/2026-10-28-project-inventory.md`. `command -v ci` returned no path in this Linux guest; required Linux `pr-checks` is the validation path. PR readiness, exact-head status and any format writeback are recorded in the delivery artifact after commit. No independent approval, merge, rollout or runtime test execution is asserted here.

## #7020 issue census

Observed via all pages of the sub-issue endpoint on 2026-10-28. Titles are omitted to keep this a state ledger; each link supplies the original finding and scope.

| State | Issues |
| --- | --- |
| open | [#7022](https://github.com/jomcgi-org/homelab/issues/7022), [#7023](https://github.com/jomcgi-org/homelab/issues/7023), [#7024](https://github.com/jomcgi-org/homelab/issues/7024), [#7025](https://github.com/jomcgi-org/homelab/issues/7025), [#7026](https://github.com/jomcgi-org/homelab/issues/7026), [#7027](https://github.com/jomcgi-org/homelab/issues/7027), [#7028](https://github.com/jomcgi-org/homelab/issues/7028), [#7029](https://github.com/jomcgi-org/homelab/issues/7029), [#7030](https://github.com/jomcgi-org/homelab/issues/7030), [#7034](https://github.com/jomcgi-org/homelab/issues/7034), [#7036](https://github.com/jomcgi-org/homelab/issues/7036), [#7037](https://github.com/jomcgi-org/homelab/issues/7037), [#7038](https://github.com/jomcgi-org/homelab/issues/7038), [#7039](https://github.com/jomcgi-org/homelab/issues/7039), [#7040](https://github.com/jomcgi-org/homelab/issues/7040), [#7041](https://github.com/jomcgi-org/homelab/issues/7041), [#7042](https://github.com/jomcgi-org/homelab/issues/7042), [#7043](https://github.com/jomcgi-org/homelab/issues/7043), [#7048](https://github.com/jomcgi-org/homelab/issues/7048), [#7050](https://github.com/jomcgi-org/homelab/issues/7050), [#7051](https://github.com/jomcgi-org/homelab/issues/7051), [#7053](https://github.com/jomcgi-org/homelab/issues/7053), [#7056](https://github.com/jomcgi-org/homelab/issues/7056), [#7057](https://github.com/jomcgi-org/homelab/issues/7057), [#7058](https://github.com/jomcgi-org/homelab/issues/7058), [#7059](https://github.com/jomcgi-org/homelab/issues/7059), [#7060](https://github.com/jomcgi-org/homelab/issues/7060), [#7061](https://github.com/jomcgi-org/homelab/issues/7061), [#7062](https://github.com/jomcgi-org/homelab/issues/7062), [#7065](https://github.com/jomcgi-org/homelab/issues/7065), [#7068](https://github.com/jomcgi-org/homelab/issues/7068), [#7071](https://github.com/jomcgi-org/homelab/issues/7071), [#7072](https://github.com/jomcgi-org/homelab/issues/7072), [#7075](https://github.com/jomcgi-org/homelab/issues/7075), [#7076](https://github.com/jomcgi-org/homelab/issues/7076), [#7083](https://github.com/jomcgi-org/homelab/issues/7083), [#7085](https://github.com/jomcgi-org/homelab/issues/7085), [#7087](https://github.com/jomcgi-org/homelab/issues/7087), [#7088](https://github.com/jomcgi-org/homelab/issues/7088), [#7090](https://github.com/jomcgi-org/homelab/issues/7090), [#7091](https://github.com/jomcgi-org/homelab/issues/7091), [#7097](https://github.com/jomcgi-org/homelab/issues/7097), [#7098](https://github.com/jomcgi-org/homelab/issues/7098) |
| closed | [#7021](https://github.com/jomcgi-org/homelab/issues/7021), [#7031](https://github.com/jomcgi-org/homelab/issues/7031), [#7032](https://github.com/jomcgi-org/homelab/issues/7032), [#7033](https://github.com/jomcgi-org/homelab/issues/7033), [#7035](https://github.com/jomcgi-org/homelab/issues/7035), [#7044](https://github.com/jomcgi-org/homelab/issues/7044), [#7045](https://github.com/jomcgi-org/homelab/issues/7045), [#7046](https://github.com/jomcgi-org/homelab/issues/7046), [#7047](https://github.com/jomcgi-org/homelab/issues/7047), [#7049](https://github.com/jomcgi-org/homelab/issues/7049), [#7052](https://github.com/jomcgi-org/homelab/issues/7052), [#7054](https://github.com/jomcgi-org/homelab/issues/7054), [#7055](https://github.com/jomcgi-org/homelab/issues/7055), [#7063](https://github.com/jomcgi-org/homelab/issues/7063), [#7064](https://github.com/jomcgi-org/homelab/issues/7064), [#7066](https://github.com/jomcgi-org/homelab/issues/7066), [#7067](https://github.com/jomcgi-org/homelab/issues/7067), [#7069](https://github.com/jomcgi-org/homelab/issues/7069), [#7070](https://github.com/jomcgi-org/homelab/issues/7070), [#7073](https://github.com/jomcgi-org/homelab/issues/7073), [#7074](https://github.com/jomcgi-org/homelab/issues/7074), [#7077](https://github.com/jomcgi-org/homelab/issues/7077), [#7078](https://github.com/jomcgi-org/homelab/issues/7078), [#7079](https://github.com/jomcgi-org/homelab/issues/7079), [#7080](https://github.com/jomcgi-org/homelab/issues/7080), [#7081](https://github.com/jomcgi-org/homelab/issues/7081), [#7082](https://github.com/jomcgi-org/homelab/issues/7082), [#7084](https://github.com/jomcgi-org/homelab/issues/7084), [#7086](https://github.com/jomcgi-org/homelab/issues/7086), [#7089](https://github.com/jomcgi-org/homelab/issues/7089), [#7092](https://github.com/jomcgi-org/homelab/issues/7092), [#7093](https://github.com/jomcgi-org/homelab/issues/7093), [#7094](https://github.com/jomcgi-org/homelab/issues/7094), [#7095](https://github.com/jomcgi-org/homelab/issues/7095), [#7096](https://github.com/jomcgi-org/homelab/issues/7096) |

## Changed-file manifest

Complete sorted intersection. `P` is an existing Python input, `G` an existing non-generated Go input, `X` generated and excluded from final delta findings, `D` absent at the source pin, and `O` other present content outside the four tools’ Python/Go function scope. Every path belongs to exactly one category.

```text
O .bazelrc
O .claude/settings.json
O .github/workflows/factory-mobile-preview.yml
O .github/workflows/fixture-preview-build.yml
O .github/workflows/fixture-preview-publish.yml
O .gitignore
O .impeccable.md
O .prettierignore
O AGENTS.md
O BUILD
O MODULE.bazel
O README.md
O bazel/ARCHITECTURE.md
D bazel/ci.bazelrc
O bazel/helm/BUILD
O bazel/helm/README.md
O bazel/helm/app.bzl
D bazel/helm/argocd-live-diff.sh
D bazel/helm/argocd-live-diff_test.sh
O bazel/helm/chart-admissibility-test.sh
O bazel/helm/defs.bzl
O bazel/helm/gazelle/BUILD
G bazel/helm/gazelle/config.go
G bazel/helm/gazelle/config_test.go
G bazel/helm/gazelle/generate.go
G bazel/helm/gazelle/generate_layout_test.go
G bazel/helm/gazelle/generate_test.go
G bazel/helm/gazelle/language.go
G bazel/helm/gazelle/language_test.go
O bazel/helm/generation_contract_test.bzl
D bazel/helm/helm-assert-annotations.sh
D bazel/helm/render.bzl
O bazel/helm/test.bzl
O bazel/images/BUILD
O bazel/images/README.md
O bazel/images/digests/BUILD
O bazel/images/generate-push-all.sh
O bazel/images/push/push-changed.sh
O bazel/images/validate-generate-scripts.sh
O bazel/requirements/README.md
O bazel/requirements/all.in
O bazel/requirements/all.txt
O bazel/requirements/overrides.txt
O bazel/requirements/runtime.txt
D bazel/requirements/tools.in
D bazel/requirements/tools.txt
O bazel/tools/BUILD
O bazel/tools/README.md
D bazel/tools/agent-run/BUILD
D bazel/tools/agent-run/main.go
D bazel/tools/agent-run/main_test.go
O bazel/tools/ci/ci
O bazel/tools/ci/ci_test.sh
O bazel/tools/cluster/BUILD
D bazel/tools/cluster/scripts/argocd.sh
D bazel/tools/cluster/scripts/events.sh
D bazel/tools/cluster/scripts/pods.sh
D bazel/tools/cluster/scripts/status.sh
O bazel/tools/format/BUILD
D bazel/tools/format/fast-format.sh
O bazel/tools/format/format-code_test.sh
O bazel/tools/format/helm_deps/BUILD
P bazel/tools/format/helm_deps/check_helm_deps.py
P bazel/tools/format/helm_deps/check_helm_deps_test.py
O bazel/tools/format/prettier.config.cjs
O bazel/tools/format/run-generators.sh
O bazel/tools/format/sync-helm-deps.sh
O bazel/tools/format/update-python-requirements.sh
D bazel/tools/format/validate-apko-configs.sh
G bazel/tools/hf2oci/pkg/ociref/ociref.go
D bazel/tools/hf_lock.sh
O bazel/tools/hooks/prefer-bb-remote.sh
O bazel/tools/hooks/prefer-bb-remote_test.sh
O bazel/tools/http/stage_layer_tar_test.sh
O bazel/tools/image/BUILD
O bazel/tools/image/README.md
P bazel/tools/image/tools_image_test.py
O bazel/tools/js/exec_filegroup.bzl
D bazel/tools/lint/.shellcheckrc
O bazel/tools/lint/BUILD
D bazel/tools/lint/eslint.config.mjs
D bazel/tools/lint/linters.bzl
O bazel/tools/oci/README.md
O bazel/tools/oci/go_image.bzl
O bazel/tools/oci/py3_image.bzl
O bazel/tools/oci/verify-apko-lock.sh
D bazel/tools/package.json
O bazel/tools/previews/BUILD
P bazel/tools/previews/capture.py
P bazel/tools/previews/compare.py
P bazel/tools/previews/compare_test.py
O bazel/tools/previews/comparison.html
P bazel/tools/previews/publish.py
P bazel/tools/previews/publish_test.py
P bazel/tools/previews/suite.py
P bazel/tools/previews/suite_test.py
O bazel/tools/tools.lock.json
O bootstrap.sh
O bootstrap_test.sh
O buildbuddy.yaml
O docs/THREAT-MODEL.md
O docs/agents/cloud-sessions.md
O docs/agents/drain-queue.md
O docs/agents/local-tests.md
O docs/agents/review.md
O docs/agents/ship.md
O docs/observability.md
O docs/runbooks/fixture-previews.md
O docs/runbooks/improve-ambient/runbook.md
O docs/runbooks/private-action-synthetics.md
O docs/runbooks/refresh-structure-docs.md
O docs/security.md
O go.mod
O go.sum
O package.json
O pnpm-lock.yaml
O pnpm-workspace.yaml
O projects/BUILD
O projects/design-system/README.md
O projects/design-system/tokens/contract.css
O projects/embervm/ARCHITECTURE.md
O projects/embervm/STPA.md
O projects/embervm/chart/BUILD
O projects/embervm/chart/Chart.yaml
P projects/embervm/chart/chart_control_env_gate_test.py
P projects/embervm/chart/chart_env_identity_test.py
O projects/embervm/chart/crds/workload-crd.yaml
O projects/embervm/chart/templates/_noded-pod.tpl
O projects/embervm/chart/templates/conformance-deployment.yaml
O projects/embervm/chart/templates/deployment.yaml
O projects/embervm/chart/templates/noded-rootfs-builder-configmap.yaml
O projects/embervm/chart/templates/workload-claude-runtime.yaml
O projects/embervm/chart/templates/workload-pi-runtime.yaml
O projects/embervm/chart/values.yaml
O projects/embervm/conformance/BUILD
G projects/embervm/conformance/main.go
G projects/embervm/conformance/s7_jailer.go
G projects/embervm/conformance/s7_jailer_test.go
G projects/embervm/conformance/scenarios.go
O projects/embervm/control/config/runtime.exs
O projects/embervm/control/lib/embervm/application.ex
O projects/embervm/control/lib/embervm/base_builder.ex
O projects/embervm/control/lib/embervm/brick.ex
O projects/embervm/control/lib/embervm/brick_controller.ex
O projects/embervm/control/lib/embervm/brick_portfolio.ex
O projects/embervm/control/lib/embervm/capacity_report.ex
O projects/embervm/control/lib/embervm/capture_evidence_retention.ex
O projects/embervm/control/lib/embervm/dispatcher.ex
O projects/embervm/control/lib/embervm/drain_coordinator.ex
O projects/embervm/control/lib/embervm/group_manager.ex
O projects/embervm/control/lib/embervm/group_manager/supervisor.ex
O projects/embervm/control/lib/embervm/group_sweeper.ex
O projects/embervm/control/lib/embervm/group_wake_manager.ex
O projects/embervm/control/lib/embervm/log_formatter.ex
O projects/embervm/control/lib/embervm/node_registry.ex
O projects/embervm/control/lib/embervm/op_log.ex
O projects/embervm/control/lib/embervm/op_log/postgres.ex
O projects/embervm/control/lib/embervm/op_log/sqlite.ex
O projects/embervm/control/lib/embervm/pool_manager.ex
O projects/embervm/control/lib/embervm/residency_intent.ex
O projects/embervm/control/lib/embervm/retry.ex
O projects/embervm/control/lib/embervm/router.ex
O projects/embervm/control/lib/embervm/s3_warmth_gc.ex
O projects/embervm/control/lib/embervm/scheduler.ex
O projects/embervm/control/lib/embervm/scheduler/inflight.ex
O projects/embervm/control/lib/embervm/scheduler/request.ex
O projects/embervm/control/lib/embervm/scheduler/retry.ex
O projects/embervm/control/lib/embervm/scheduler/score.ex
O projects/embervm/control/lib/embervm/serving_manager.ex
O projects/embervm/control/lib/embervm/serving_sweeper.ex
O projects/embervm/control/lib/embervm/session.ex
O projects/embervm/control/lib/embervm/session_manager.ex
O projects/embervm/control/lib/embervm/session_store.ex
O projects/embervm/control/lib/embervm/stateful_manager.ex
O projects/embervm/control/lib/embervm/stateful_sweeper.ex
O projects/embervm/control/lib/embervm/task_store.ex
O projects/embervm/control/lib/embervm/wake_instance.ex
O projects/embervm/control/test/embervm/application_test.exs
O projects/embervm/control/test/embervm/base_builder_test.exs
O projects/embervm/control/test/embervm/brick_controller_decision_span_test.exs
O projects/embervm/control/test/embervm/brick_controller_test.exs
O projects/embervm/control/test/embervm/brick_portfolio_test.exs
O projects/embervm/control/test/embervm/brick_test.exs
O projects/embervm/control/test/embervm/capacity_report_test.exs
O projects/embervm/control/test/embervm/capture_evidence_retention_test.exs
O projects/embervm/control/test/embervm/dispatcher_test.exs
O projects/embervm/control/test/embervm/drain_coordinator_test.exs
O projects/embervm/control/test/embervm/group_manager_test.exs
O projects/embervm/control/test/embervm/group_sweeper_test.exs
O projects/embervm/control/test/embervm/group_wake_manager_test.exs
O projects/embervm/control/test/embervm/log_formatter_metadata_test.exs
O projects/embervm/control/test/embervm/log_formatter_test.exs
O projects/embervm/control/test/embervm/multi_brick_scheduling_sim_test.exs
O projects/embervm/control/test/embervm/node_registry_test.exs
O projects/embervm/control/test/embervm/pool_manager_test.exs
O projects/embervm/control/test/embervm/residency_intent_test.exs
O projects/embervm/control/test/embervm/residency_manager_test.exs
O projects/embervm/control/test/embervm/residency_postgres_test.exs
O projects/embervm/control/test/embervm/retry_test.exs
O projects/embervm/control/test/embervm/router_residency_test.exs
O projects/embervm/control/test/embervm/router_test.exs
O projects/embervm/control/test/embervm/s3_warmth_gc_test.exs
O projects/embervm/control/test/embervm/scheduler/inflight_test.exs
O projects/embervm/control/test/embervm/scheduler/score_test.exs
O projects/embervm/control/test/embervm/scheduler_test.exs
O projects/embervm/control/test/embervm/serving_manager_test.exs
O projects/embervm/control/test/embervm/serving_sweeper_test.exs
O projects/embervm/control/test/embervm/session_invoke_channel_test.exs
O projects/embervm/control/test/embervm/session_manager_test.exs
O projects/embervm/control/test/embervm/session_store_test.exs
O projects/embervm/control/test/embervm/stateful_manager_store_truth_test.exs
O projects/embervm/control/test/embervm/stateful_manager_test.exs
O projects/embervm/control/test/embervm/stateful_sweeper_test.exs
O projects/embervm/control/test/embervm/support/multi_brick_sim.exs
O projects/embervm/control/test/embervm/task_store_test.exs
O projects/embervm/control/test/embervm/wake_instance_test.exs
O projects/embervm/control/test/embervm/workload_watcher_test.exs
O projects/embervm/deploy/values-gke.yaml
O projects/embervm/deploy/values.yaml
O projects/embervm/dev/deploy/values-gke.yaml
O projects/embervm/dev/deploy/values-recovery-gke.yaml
O projects/embervm/dev/deploy/values.yaml
O projects/embervm/noded/cmd/BUILD
G projects/embervm/noded/cmd/launcher_config_test.go
G projects/embervm/noded/cmd/main.go
G projects/embervm/noded/cmd/main_test.go
G projects/embervm/noded/cmd/rootfs-store/main.go
G projects/embervm/noded/cmd/rootfs-store/main_test.go
G projects/embervm/noded/cmd/startup_gate_test.go
G projects/embervm/noded/config/config.go
G projects/embervm/noded/config/config_test.go
O projects/embervm/noded/egress/BUILD
G projects/embervm/noded/egress/forwarder.go
G projects/embervm/noded/egress/forwarder_linux_test.go
G projects/embervm/noded/egress/forwarder_test.go
O projects/embervm/noded/fcvm/driver/BUILD
G projects/embervm/noded/fcvm/driver/cgroup_delegation_linux_test.go
G projects/embervm/noded/fcvm/driver/cgroup_linux.go
G projects/embervm/noded/fcvm/driver/cgroup_linux_test.go
G projects/embervm/noded/fcvm/driver/cgroup_other.go
G projects/embervm/noded/fcvm/driver/cgroup_path.go
G projects/embervm/noded/fcvm/driver/cgroup_path_test.go
G projects/embervm/noded/fcvm/driver/driver.go
G projects/embervm/noded/fcvm/driver/driver_exit_accounting_test.go
G projects/embervm/noded/fcvm/driver/driver_test.go
G projects/embervm/noded/fcvm/driver/jail.go
G projects/embervm/noded/fcvm/driver/jail_linux_test.go
G projects/embervm/noded/fcvm/driver/jail_sweep.go
G projects/embervm/noded/fcvm/driver/jail_sweep_linux_test.go
G projects/embervm/noded/fcvm/driver/jail_test.go
G projects/embervm/noded/fcvm/driver/launcher.go
G projects/embervm/noded/fcvm/driver/launcher_containment_linux_test.go
G projects/embervm/noded/fcvm/driver/launcher_linux_test.go
G projects/embervm/noded/fcvm/driver/pidns_linux.go
G projects/embervm/noded/fcvm/driver/pidns_other.go
G projects/embervm/noded/fcvm/driver/preflight.go
G projects/embervm/noded/fcvm/driver/preflight_test.go
G projects/embervm/noded/fcvm/driver/serial.go
G projects/embervm/noded/fcvm/driver/uidlease_linux.go
G projects/embervm/noded/fcvm/driver/uidlease_linux_test.go
G projects/embervm/noded/fcvm/driver/uidlease_other.go
O projects/embervm/noded/fcvm/fcclient/BUILD
G projects/embervm/noded/fcvm/fcclient/client.go
O projects/embervm/noded/groupclock/BUILD
G projects/embervm/noded/groupclock/vsock.go
O projects/embervm/noded/guestmem/BUILD
G projects/embervm/noded/guestmem/client.go
O projects/embervm/noded/server/BUILD
G projects/embervm/noded/server/activator_test.go
G projects/embervm/noded/server/budget.go
G projects/embervm/noded/server/budget_delegation_test.go
G projects/embervm/noded/server/budget_test.go
G projects/embervm/noded/server/drain_test.go
G projects/embervm/noded/server/group_member.go
G projects/embervm/noded/server/group_member_test.go
G projects/embervm/noded/server/jail_preflight.go
G projects/embervm/noded/server/jail_preflight_test.go
G projects/embervm/noded/server/node_contract_test.go
G projects/embervm/noded/server/preemption.go
G projects/embervm/noded/server/pressure.go
G projects/embervm/noded/server/pressure_test.go
G projects/embervm/noded/server/registry_workload.go
G projects/embervm/noded/server/registry_workload_test.go
G projects/embervm/noded/server/server.go
G projects/embervm/noded/server/server_test.go
G projects/embervm/noded/server/serving.go
G projects/embervm/noded/server/serving_test.go
G projects/embervm/noded/server/stateful.go
G projects/embervm/noded/server/stateful_activator.go
G projects/embervm/noded/server/stateful_activator_test.go
G projects/embervm/noded/server/stateful_test.go
G projects/embervm/noded/substrate/substrate.go
O projects/embervm/noded/unixsock/BUILD
G projects/embervm/noded/unixsock/unixsock.go
G projects/embervm/noded/unixsock/unixsock_linux_test.go
O projects/embervm/noded/vsockhttp/BUILD
G projects/embervm/noded/vsockhttp/transport.go
G projects/embervm/noded/vsockhttp/transport_test.go
O projects/embervm/proto/embervm/node/v1/BUILD
G projects/embervm/proto/embervm/node/v1/fakenode/main.go
X projects/embervm/proto/embervm/node/v1/node.pb.go
O projects/embervm/proto/embervm/node/v1/node.proto
X projects/embervm/proto/embervm/node/v1/node_grpc.pb.go
O projects/embervm/proto/embervm/node/v1/roundtrip/node_roundtrip_test.exs
O projects/embervm/proto/embervm/node/v1/sync-go-stubs.sh
O projects/embervm/proto/embervm/node/v1/sync-go-stubs_test.sh
O projects/embervm/runtimes/claude/README.md
O projects/embervm/runtimes/claude/apko.lock.json
O projects/embervm/runtimes/claude/apko.yaml
P projects/embervm/runtimes/claude/rootfs_reclaim_guard_test.py
P projects/embervm/runtimes/claude/shim.py
P projects/embervm/runtimes/claude/shim_test.py
P projects/embervm/runtimes/claude/toolchain_guard_test.py
O projects/embervm/runtimes/pi/apko.lock.json
O projects/embervm/runtimes/pi/apko.yaml
P projects/embervm/runtimes/pi/package_parity_test.py
O projects/embervm/runtimes/postgres/apko.lock.json
O projects/embervm/runtimes/postgres/apko.yaml
O projects/embervm/runtimes/python/testdata/echo/README.md
O projects/embervm/specs/BUILD
O projects/embervm/specs/README.md
O projects/embervm/specs/admission.cfg
O projects/embervm/specs/admission.tla
O projects/embervm/specs/admission_check_then_act.cfg
O projects/embervm/specs/admission_liveness.cfg
O projects/embervm/specs/admission_liveness_no_walk.cfg
O projects/embervm/specs/admission_observed.cfg
O projects/embervm/specs/admission_single_attempt.cfg
O projects/embervm/specs/vocabulary.exs
O projects/embervm/stpa/security.json
O projects/embervm/tokenbroker/README.md
G projects/embervm/tokenbroker/cmd/tokenbroker/codex_usage.go
G projects/embervm/tokenbroker/cmd/tokenbroker/codex_usage_test.go
G projects/embervm/tokenbroker/cmd/tokenbroker/main_test.go
G projects/embervm/tokenbroker/internal/broker/broker.go
G projects/embervm/tokenbroker/internal/broker/broker_test.go
G projects/embervm/tokenbroker/internal/quota/quota.go
G projects/embervm/tokenbroker/internal/quota/quota_test.go
G projects/firecracker/substrate/egress-proxy/cmd/quota.go
G projects/firecracker/substrate/egress-proxy/cmd/quota_test.go
G projects/firecracker/substrate/egress-proxy/cmd/swap.go
G projects/firecracker/substrate/egress-proxy/cmd/swap_test.go
O projects/gke-apps/kustomization.yaml
O projects/gke-apps/monolith-public/application.yaml
O projects/gke-apps/monolith/application.yaml
O projects/gke-apps/namespace-embervm.yaml
O projects/gke-apps/scotland-connector/application.yaml
O projects/gke-apps/scotland-connector/kustomization.yaml
O projects/gke-cluster/root/root-application.yaml
O projects/inference/deploy/BUILD
O projects/inference/deploy/values-gke.yaml
O projects/inference/deploy/values.yaml
O projects/loom/deploy/BUILD
O projects/loom/deploy/README.md
P projects/loom/deploy/application_test.py
P projects/loom/deploy/chart_render_test.py
O projects/loom/deploy/cnpg-cluster.yaml
D projects/loom/deploy/cnpg-metrics-service.yaml
P projects/loom/deploy/deploy_manifests_test.py
O projects/loom/deploy/httproute.yaml
O projects/loom/deploy/kustomization.yaml
O projects/mcp/context-forge-gateway/chart/BUILD
O projects/mcp/context-forge-gateway/chart/Chart.lock
O projects/mcp/context-forge-gateway/chart/Chart.yaml
D projects/mcp/context-forge-gateway/chart/charts/homelab-library-0.6.0.tgz
O projects/mcp/context-forge-gateway/chart/charts/homelab-library-0.7.0.tgz
O projects/mcp/context-forge-gateway/chart/values.yaml
O projects/mcp/context-forge-gateway/deploy/BUILD
P projects/model-bench/bench/cli_test.py
O projects/model-bench/bench/verifiers/BUILD
P projects/model-bench/bench/verifiers/__init__.py
D projects/model-bench/bench/verifiers/compile.py
D projects/model-bench/bench/verifiers/lint.py
P projects/model-bench/bench/verifiers/verifiers_test.py
O projects/model-bench/requirements-venv.txt
O projects/model-bench/tasks/stars-grid-speedup-01/task.yaml
O projects/monolith-agents/chart/BUILD
O projects/monolith-agents/chart/Chart.lock
O projects/monolith-agents/chart/Chart.yaml
P projects/monolith-agents/chart/chart_render_test.py
D projects/monolith-agents/chart/charts/homelab-library-0.6.0.tgz
O projects/monolith-agents/chart/charts/homelab-library-0.7.0.tgz
O projects/monolith-agents/chart/values.yaml
O projects/monolith-public/chart/BUILD
O projects/monolith-public/chart/Chart.lock
O projects/monolith-public/chart/Chart.yaml
D projects/monolith-public/chart/charts/homelab-library-0.6.0.tgz
O projects/monolith-public/chart/charts/homelab-library-0.7.0.tgz
O projects/monolith-public/chart/values.yaml
O projects/monolith-public/deploy/application.yaml
O projects/monolith-public/deploy/values-gke.yaml
O projects/monolith-public/deploy/values.yaml
O projects/monolith/AGENTS.md
O projects/monolith/ARCHITECTURE.md
O projects/monolith/BUILD
O projects/monolith/README.md
P projects/monolith/agent/api.py
P projects/monolith/agent/checks.py
P projects/monolith/agent/config.py
P projects/monolith/agent/config_test.py
P projects/monolith/agent/mcp.py
P projects/monolith/agent/mcp_test.py
P projects/monolith/agent/routine_reconciliation.py
P projects/monolith/agent/routine_reconciliation_test.py
P projects/monolith/agent/schema_retirement.py
P projects/monolith/agent/schema_retirement_test.py
P projects/monolith/agent/tests/bdd_checks_test.py
P projects/monolith/agent_kubernetes/client.py
P projects/monolith/app/bdd_completeness_test.py
P projects/monolith/app/integration_test.py
P projects/monolith/app/jobs_main.py
P projects/monolith/app/jobs_main_test.py
P projects/monolith/app/main_agents_imports_test.py
P projects/monolith/app/main_app_state_test.py
P projects/monolith/app/main_extra_test.py
P projects/monolith/app/main_lifespan_discord_test.py
P projects/monolith/app/main_lock_sweep_test.py
P projects/monolith/app/main_public_imports_test.py
P projects/monolith/app/main_sidecar_test.py
P projects/monolith/app/main_summary_edge_test.py
P projects/monolith/app/main_summary_test.py
P projects/monolith/app/main_test.py
P projects/monolith/artifact/s3.py
P projects/monolith/campsites/__init__.py
P projects/monolith/campsites/router.py
O projects/monolith/chart/BUILD
O projects/monolith/chart/Chart.yaml
P projects/monolith/chart/backup_advisory_test.py
P projects/monolith/chart/chart_env_identity_test.py
P projects/monolith/chart/chart_env_vars_test.py
P projects/monolith/chart/chart_tokenbroker_spiffe_test.py
P projects/monolith/chart/grimoire_play_test.py
P projects/monolith/chart/knowledge_review_jobs_test.py
D projects/monolith/chart/migrations/20261011010000_grimoire_session_initiative.sql
O projects/monolith/chart/migrations/20261018000000_knowledge_review_batches.sql
O projects/monolith/chart/migrations/20261018034100_grimoire_session_initiative.sql
O projects/monolith/chart/migrations/20261018034200_grimoire_transcripts.sql
O projects/monolith/chart/migrations/20261018123000_retire_unused_feature_schema.sql
O projects/monolith/chart/migrations/20261019021500_factory_daily_admission_limit.sql
O projects/monolith/chart/migrations/20261019043000_factory_residency_wait.sql
D projects/monolith/chart/migrations/20261019090000_knowledge_source_coverage.sql
O projects/monolith/chart/migrations/20261019093000_factory_implement_sol_first.sql
O projects/monolith/chart/migrations/20261021030000_hikes_walk_hours_full_forecast.sql
O projects/monolith/chart/migrations/20261022040000_knowledge_source_coverage.sql
O projects/monolith/chart/migrations/20261023000000_entity_forecast_freshness.sql
O projects/monolith/chart/migrations/20261024010000_agent_workspace_handoff.sql
O projects/monolith/chart/migrations/atlas.sum
P projects/monolith/chart/native_egress_allowlist_test.py
P projects/monolith/chart/otel_endpoint_test.py
P projects/monolith/chart/stars_aurora_job_test.py
D projects/monolith/chart/templates/cnpg-metrics-service.yaml
O projects/monolith/chart/templates/cronworkflows.yaml
O projects/monolith/chart/templates/deployment.yaml
O projects/monolith/chart/templates/networkpolicy-egress.yaml
O projects/monolith/chart/templates/onepassworditem-r2-s3.yaml
P projects/monolith/chart/tests/retired_transport_migration_test.py
O projects/monolith/chart/values.yaml
P projects/monolith/chat/acl.py
P projects/monolith/chat/acl_test.py
P projects/monolith/chat/ambient_analysis.py
P projects/monolith/chat/api.py
P projects/monolith/chat/attention.py
P projects/monolith/chat/autopilot_job.py
P projects/monolith/chat/bot.py
P projects/monolith/chat/changelog.py
P projects/monolith/chat/changelog_test.py
P projects/monolith/chat/explore_router_test.py
P projects/monolith/chat/explorer.py
P projects/monolith/chat/explorer_test.py
P projects/monolith/chat/jobs.py
P projects/monolith/chat/leader.py
P projects/monolith/chat/leader_scheduled_tasks_test.py
P projects/monolith/chat/models.py
P projects/monolith/chat/observer.py
P projects/monolith/chat/observer_job.py
P projects/monolith/chat/observer_job_test.py
O projects/monolith/chat/orchestrator_bundle.md
P projects/monolith/chat/outbox.py
P projects/monolith/chat/outbox_test.py
P projects/monolith/chat/reminders.py
P projects/monolith/chat/router.py
P projects/monolith/chat/safeguards.py
P projects/monolith/chat/safeguards_train_job.py
P projects/monolith/chat/safeguards_train_job_test.py
P projects/monolith/chat/sse.py
P projects/monolith/chat/sse_test.py
P projects/monolith/chat/startup_test.py
P projects/monolith/chat/store.py
P projects/monolith/chat/store_channel_summary_test.py
P projects/monolith/chat/store_summary_test.py
P projects/monolith/chat/store_test.py
P projects/monolith/chat/store_upsert_test.py
P projects/monolith/chat/streaming_router_test.py
P projects/monolith/chat/summarizer.py
D projects/monolith/chat/summarizer_agent_reply_test.py
D projects/monolith/chat/summarizer_startup_test.py
P projects/monolith/chat/summarizer_test.py
P projects/monolith/chat/triggers.py
P projects/monolith/cluster/cd_health.py
P projects/monolith/cluster/cd_health_test.py
P projects/monolith/core/clock.py
P projects/monolith/core/clock_test.py
P projects/monolith/core/db.py
O projects/monolith/deploy/application.yaml
O projects/monolith/deploy/values-gke.yaml
O projects/monolith/deploy/values.yaml
O projects/monolith/dev/deploy/values-recovery-gke.yaml
O projects/monolith/dev/deploy/values.yaml
P projects/monolith/dev/grimoire/rehearse.py
P projects/monolith/dr_jobs/__init__.py
P projects/monolith/dr_jobs/jobs.py
P projects/monolith/dr_jobs/router.py
P projects/monolith/dr_jobs/router_test.py
P projects/monolith/e2e/e2e_http_test.py
P projects/monolith/e2e/e2e_test.py
P projects/monolith/ember_public/synthetic_router.py
P projects/monolith/ember_public/synthetic_router_test.py
P projects/monolith/faas/__init__.py
D projects/monolith/faas/functions/__init__.py
P projects/monolith/faas/models_test.py
P projects/monolith/faas/repository.py
P projects/monolith/faas/storage.py
P projects/monolith/faas/storage_test.py
O projects/monolith/factory/MCP.md
P projects/monolith/factory/api.py
P projects/monolith/factory/backup_health_test.py
P projects/monolith/factory/dependency_gate.py
P projects/monolith/factory/dependency_gate_test.py
P projects/monolith/factory/execution/admission.py
P projects/monolith/factory/execution/admission_diagnostics.py
P projects/monolith/factory/execution/admission_test.py
P projects/monolith/factory/execution/api.py
P projects/monolith/factory/execution/api_test.py
P projects/monolith/factory/execution/capacity_wait.py
P projects/monolith/factory/execution/capacity_wait_test.py
P projects/monolith/factory/execution/execution_api.py
P projects/monolith/factory/execution/mcp.py
P projects/monolith/factory/execution/mcp_test.py
P projects/monolith/factory/execution/models.py
P projects/monolith/factory/execution/permit_supervision.py
P projects/monolith/factory/execution/permit_supervision_test.py
P projects/monolith/factory/execution/provider_quota.py
P projects/monolith/factory/execution/public_agent_activity_views_test.py
P projects/monolith/factory/execution/reconciliation.py
P projects/monolith/factory/execution/response_lost_test.py
P projects/monolith/factory/execution/result_receipts.py
P projects/monolith/factory/execution/result_receipts_router.py
P projects/monolith/factory/execution/result_receipts_test.py
P projects/monolith/factory/execution/router.py
P projects/monolith/factory/execution/runtime_meter_evidence.py
P projects/monolith/factory/execution/store.py
P projects/monolith/factory/execution/transport.py
P projects/monolith/factory/execution/transport_test.py
D projects/monolith/factory/execution/workspace_handoff.py
O projects/monolith/factory/execution/workspace_handoff/BUILD
P projects/monolith/factory/execution/workspace_handoff/__init__.py
P projects/monolith/factory/health_alerts.py
P projects/monolith/factory/health_alerts_test.py
P projects/monolith/factory/module_test.py
P projects/monolith/factory/ops_health.py
P projects/monolith/factory/ops_health_test.py
O projects/monolith/factory/orchestration/FACTORY.md
O projects/monolith/factory/orchestration/IMPROVEMENT_PREVIEW.md
P projects/monolith/factory/orchestration/api.py
P projects/monolith/factory/orchestration/browser_evidence.py
P projects/monolith/factory/orchestration/browser_evidence_test.py
P projects/monolith/factory/orchestration/browser_review_dispatch.py
P projects/monolith/factory/orchestration/browser_review_dispatch_test.py
P projects/monolith/factory/orchestration/browser_review_handoff.py
P projects/monolith/factory/orchestration/browser_review_handoff_test.py
P projects/monolith/factory/orchestration/conductor_context.py
P projects/monolith/factory/orchestration/dependency_prs.py
P projects/monolith/factory/orchestration/drain_console.py
P projects/monolith/factory/orchestration/drain_console_router.py
P projects/monolith/factory/orchestration/drain_console_router_test.py
P projects/monolith/factory/orchestration/drainer.py
P projects/monolith/factory/orchestration/drainer_router.py
P projects/monolith/factory/orchestration/drainer_router_test.py
P projects/monolith/factory/orchestration/drainer_test.py
P projects/monolith/factory/orchestration/factory_allocation_recovery.py
P projects/monolith/factory/orchestration/factory_attempt_stop_test.py
P projects/monolith/factory/orchestration/factory_conductor.py
P projects/monolith/factory/orchestration/factory_conductor_test.py
P projects/monolith/factory/orchestration/factory_continuations.py
P projects/monolith/factory/orchestration/factory_controls.py
P projects/monolith/factory/orchestration/factory_controls_test.py
P projects/monolith/factory/orchestration/factory_daily_admission_limit_postgres_test.py
P projects/monolith/factory/orchestration/factory_decisions.py
P projects/monolith/factory/orchestration/factory_decisions_test.py
P projects/monolith/factory/orchestration/factory_escalation_test.py
P projects/monolith/factory/orchestration/factory_feedback.py
P projects/monolith/factory/orchestration/factory_fence_test.py
P projects/monolith/factory/orchestration/factory_funding.py
P projects/monolith/factory/orchestration/factory_funding_limits.py
P projects/monolith/factory/orchestration/factory_gates.py
P projects/monolith/factory/orchestration/factory_implement_sol_first_postgres_test.py
P projects/monolith/factory/orchestration/factory_improvement_fence.py
P projects/monolith/factory/orchestration/factory_improvement_preview.py
P projects/monolith/factory/orchestration/factory_improvement_preview_test.py
P projects/monolith/factory/orchestration/factory_improvement_scout.py
P projects/monolith/factory/orchestration/factory_improvement_scout_test.py
P projects/monolith/factory/orchestration/factory_improvements.py
P projects/monolith/factory/orchestration/factory_intake.py
P projects/monolith/factory/orchestration/factory_intake_loop.py
P projects/monolith/factory/orchestration/factory_intake_loop_test.py
P projects/monolith/factory/orchestration/factory_intake_test.py
P projects/monolith/factory/orchestration/factory_integration_test.py
P projects/monolith/factory/orchestration/factory_judge.py
P projects/monolith/factory/orchestration/factory_landing.py
P projects/monolith/factory/orchestration/factory_landing_release.py
P projects/monolith/factory/orchestration/factory_landing_test.py
P projects/monolith/factory/orchestration/factory_merged_delivery.py
P projects/monolith/factory/orchestration/factory_notify_policy.py
P projects/monolith/factory/orchestration/factory_pause.py
P projects/monolith/factory/orchestration/factory_phase_diagnostics_test.py
P projects/monolith/factory/orchestration/factory_policy_amendment.py
P projects/monolith/factory/orchestration/factory_pr_lifecycle.py
P projects/monolith/factory/orchestration/factory_pr_lifecycle_test.py
P projects/monolith/factory/orchestration/factory_problem_issues.py
P projects/monolith/factory/orchestration/factory_problem_issues_test.py
P projects/monolith/factory/orchestration/factory_progress_shadow.py
P projects/monolith/factory/orchestration/factory_progress_shadow_test.py
P projects/monolith/factory/orchestration/factory_progress_watchdog.py
P projects/monolith/factory/orchestration/factory_progress_watchdog_test.py
P projects/monolith/factory/orchestration/factory_refine.py
P projects/monolith/factory/orchestration/factory_refine_test.py
P projects/monolith/factory/orchestration/factory_refinement_batch.py
P projects/monolith/factory/orchestration/factory_refinement_delivery.py
P projects/monolith/factory/orchestration/factory_reverts.py
P projects/monolith/factory/orchestration/factory_rollout.py
P projects/monolith/factory/orchestration/factory_rollout_test.py
P projects/monolith/factory/orchestration/factory_router.py
P projects/monolith/factory/orchestration/factory_router_test.py
P projects/monolith/factory/orchestration/factory_runtime_meter.py
P projects/monolith/factory/orchestration/factory_runtime_meter_test.py
P projects/monolith/factory/orchestration/factory_supervision_test.py
P projects/monolith/factory/orchestration/factory_task_recovery.py
P projects/monolith/factory/orchestration/factory_validation.py
P projects/monolith/factory/orchestration/github_read_cooldown.py
P projects/monolith/factory/orchestration/graph.py
P projects/monolith/factory/orchestration/graph_test.py
P projects/monolith/factory/orchestration/mcp.py
P projects/monolith/factory/orchestration/mcp_test.py
P projects/monolith/factory/orchestration/model_pool.py
P projects/monolith/factory/orchestration/model_pool_test.py
P projects/monolith/factory/orchestration/node_workflows.py
P projects/monolith/factory/orchestration/node_workflows_test.py
P projects/monolith/factory/orchestration/planner_knowledge_test.py
P projects/monolith/factory/orchestration/review_recovery.py
P projects/monolith/factory/orchestration/router.py
P projects/monolith/factory/orchestration/router_test.py
P projects/monolith/factory/orchestration/runtime.py
P projects/monolith/factory/orchestration/synthetic_actions.py
P projects/monolith/factory/orchestration/synthetic_actions_postgres_test.py
P projects/monolith/factory/orchestration/turn_artifact.py
P projects/monolith/factory/orchestration/turn_artifact_test.py
P projects/monolith/factory/orchestration/uncertain_tasks.py
P projects/monolith/factory/orchestration/work_item_pointer.py
P projects/monolith/factory/private_view.py
P projects/monolith/factory/private_view_test.py
P projects/monolith/factory/public_view.py
P projects/monolith/factory/publication.py
P projects/monolith/factory/publication_test.py
P projects/monolith/factory/publication_writer_test.py
P projects/monolith/factory/quota_probe.py
P projects/monolith/factory/quota_probe_test.py
P projects/monolith/framework/core.py
O projects/monolith/frontend/BUILD
O projects/monolith/frontend/docs/reference-mockups/agentstory/reference-mockup.html
O projects/monolith/frontend/docs/reference-mockups/fcstory/reference-mockup.html
O projects/monolith/frontend/docs/reference-mockups/grimoire/scrollstory/reference-mockup-mobile.html
O projects/monolith/frontend/docs/reference-mockups/grimoire/scrollstory/reference-mockup.html
O projects/monolith/frontend/factory-preview/README.md
P projects/monolith/frontend/factory-preview/browser_check.py
P projects/monolith/frontend/factory-preview/browser_handoff_check.py
P projects/monolith/frontend/factory-preview/browser_handoff_prepare.py
P projects/monolith/frontend/factory-preview/browser_handoff_prepare_test.py
O projects/monolith/frontend/factory-preview/contract.test.js
O projects/monolith/frontend/factory-preview/fixtures.js
O projects/monolith/frontend/factory-preview/main.js
O projects/monolith/frontend/package.json
O projects/monolith/frontend/profile-preview/App.svelte
O projects/monolith/frontend/profile-preview/README.md
O projects/monolith/frontend/profile-preview/browser-check.mjs
O projects/monolith/frontend/profile-preview/fixtures.js
O projects/monolith/frontend/profile-preview/index.html
O projects/monolith/frontend/profile-preview/main.js
O projects/monolith/frontend/profile-preview/vite.config.js
O projects/monolith/frontend/review-preview/App.svelte
O projects/monolith/frontend/review-preview/fixtures.js
O projects/monolith/frontend/review-preview/index.html
O projects/monolith/frontend/review-preview/main.js
O projects/monolith/frontend/review-preview/vite.config.js
D projects/monolith/frontend/scripts/capture-qwen-replay.mjs
D projects/monolith/frontend/scripts/capture-qwen-timing.mjs
D projects/monolith/frontend/scripts/capture-qwen-timing.test.mjs
D projects/monolith/frontend/scripts/qwen-replay-metrics.mjs
D projects/monolith/frontend/scripts/qwen-replay-metrics.test.mjs
O projects/monolith/frontend/src/hooks.js
O projects/monolith/frontend/src/hooks.server.js
O projects/monolith/frontend/src/hooks.test.js
O projects/monolith/frontend/src/lib/cache-headers.js
O projects/monolith/frontend/src/lib/cache-headers.test.js
O projects/monolith/frontend/src/lib/components/notes/NotePanel.svelte
O projects/monolith/frontend/src/lib/components/notes/NoteReviewHistory.dom.test.js
O projects/monolith/frontend/src/lib/components/notes/NoteReviewHistory.ssr.test.js
O projects/monolith/frontend/src/lib/components/notes/NoteReviewHistory.svelte
O projects/monolith/frontend/src/lib/components/notes/clusters.js
O projects/monolith/frontend/src/lib/design-system/data-display.ssr.test.js
O projects/monolith/frontend/src/lib/grimoire/InitiativeEditor.dom.test.js
O projects/monolith/frontend/src/lib/grimoire/InitiativeEditor.svelte
O projects/monolith/frontend/src/lib/grimoire/TurnStrip.dom.test.js
O projects/monolith/frontend/src/lib/grimoire/TurnStrip.svelte
O projects/monolith/frontend/src/lib/grimoire/YourTurnBanner.dom.test.js
O projects/monolith/frontend/src/lib/grimoire/YourTurnBanner.svelte
O projects/monolith/frontend/src/lib/grimoire/fixtures/dm-only-routes.json
O projects/monolith/frontend/src/lib/private/components/ReviewCard.svelte
O projects/monolith/frontend/src/lib/private/launcher.js
O projects/monolith/frontend/src/lib/private/launcher.test.js
O projects/monolith/frontend/src/lib/public/agentstory/timeline.js
O projects/monolith/frontend/src/lib/public/agentstory/timeline.test.js
O projects/monolith/frontend/src/lib/public/apps.js
O projects/monolith/frontend/src/lib/public/components/Footer.svelte
D projects/monolith/frontend/src/lib/public/components/Marquee.svelte
O projects/monolith/frontend/src/lib/public/components/ProfileSheet.svelte
O projects/monolith/frontend/src/lib/public/components/SchemeToggle.svelte
D projects/monolith/frontend/src/lib/public/components/ScrollCta.svelte
O projects/monolith/frontend/src/lib/public/components/SiteIndex.svelte
D projects/monolith/frontend/src/lib/public/components/Stamp.svelte
D projects/monolith/frontend/src/lib/public/components/Sticker.svelte
O projects/monolith/frontend/src/lib/public/components/TechnicalDrawingChrome.svelte
O projects/monolith/frontend/src/lib/public/components/Trail.svelte
O projects/monolith/frontend/src/lib/public/components/diagrams/DArrow.svelte
O projects/monolith/frontend/src/lib/public/components/diagrams/DBox.svelte
O projects/monolith/frontend/src/lib/public/components/diagrams/DGroup.svelte
O projects/monolith/frontend/src/lib/public/components/diagrams/Diagram.svelte
O projects/monolith/frontend/src/lib/public/components/index.js
O projects/monolith/frontend/src/lib/public/components/scheme-toggle.dom.test.js
O projects/monolith/frontend/src/lib/public/components/trail.dom.test.js
O projects/monolith/frontend/src/lib/public/components/trips/PhotoViewer.svelte
O projects/monolith/frontend/src/lib/public/factory/charts.js
O projects/monolith/frontend/src/lib/public/factory/charts.test.js
O projects/monolith/frontend/src/lib/public/factory/factory.css
O projects/monolith/frontend/src/lib/public/factory/model.js
O projects/monolith/frontend/src/lib/public/factory/model.test.js
O projects/monolith/frontend/src/lib/public/fcstory/FcScrollStory.svelte
O projects/monolith/frontend/src/lib/public/fcstory/ReplayWidget.svelte
O projects/monolith/frontend/src/lib/public/fcstory/data/trace.js
O projects/monolith/frontend/src/lib/public/fcstory/fcstory.css
O projects/monolith/frontend/src/lib/public/fcstory/metrics.js
O projects/monolith/frontend/src/lib/public/fcstory/metrics.test.js
O projects/monolith/frontend/src/lib/public/fcstory/timeline.js
O projects/monolith/frontend/src/lib/public/grimoire/api.js
O projects/monolith/frontend/src/lib/public/grimoire/scrollstory/ScrollStory.svelte
O projects/monolith/frontend/src/lib/public/grimoire/scrollstory/timeline.js
O projects/monolith/frontend/src/lib/public/grimoire/scrollstory/timeline.test.js
O projects/monolith/frontend/src/lib/public/hikes/filters.js
O projects/monolith/frontend/src/lib/public/hikes/filters.test.js
O projects/monolith/frontend/src/lib/public/notes/GraphView.svelte
O projects/monolith/frontend/src/lib/public/notes/GraphView.test.js
O projects/monolith/frontend/src/lib/public/notes/graph-cache.js
O projects/monolith/frontend/src/lib/public/notes/graph-cache.test.js
O projects/monolith/frontend/src/lib/public/posts/ix/data-copy-queue.js
O projects/monolith/frontend/src/lib/public/posts/ix/data-precision.js
O projects/monolith/frontend/src/lib/public/posts/ix/precision.test.js
O projects/monolith/frontend/src/lib/public/stars/heat.js
O projects/monolith/frontend/src/lib/public/stars/heat.test.js
O projects/monolith/frontend/src/lib/public/styles/technical-drawing.css
O projects/monolith/frontend/src/lib/public/styles/technical-drawing.test.js
O projects/monolith/frontend/src/lib/server/grimoire-session.js
O projects/monolith/frontend/src/lib/server/grimoire-session.test.js
O projects/monolith/frontend/src/lib/server/public-snapshots.js
O projects/monolith/frontend/src/lib/server/public-snapshots.test.js
O projects/monolith/frontend/src/lib/server/trips-img.js
O projects/monolith/frontend/src/lib/server/trips-img.test.js
O projects/monolith/frontend/src/lib/styles/shared/tokens.css
O projects/monolith/frontend/src/routes/+error.svelte
O projects/monolith/frontend/src/routes/+layout.svelte
O projects/monolith/frontend/src/routes/+page.ts
O projects/monolith/frontend/src/routes/friends/grimoire/campaigns/[id]/session/+page.svelte
O projects/monolith/frontend/src/routes/friends/grimoire/campaigns/[id]/session/page.dom.test.js
O projects/monolith/frontend/src/routes/friends/grimoire/campaigns/[id]/session/page.server.test.js
O projects/monolith/frontend/src/routes/friends/grimoire/campaigns/[id]/session/reveal.dom.test.js
O projects/monolith/frontend/src/routes/friends/grimoire/campaigns/[id]/session/state/+server.js
O projects/monolith/frontend/src/routes/friends/grimoire/campaigns/[id]/session/state/server.test.js
O projects/monolith/frontend/src/routes/friends/moving/moving-theme.css
O projects/monolith/frontend/src/routes/private/factory/execution/run-history.js
O projects/monolith/frontend/src/routes/private/factory/execution/run-history.test.js
D projects/monolith/frontend/src/routes/private/factory/execution/run-load.js
D projects/monolith/frontend/src/routes/private/factory/execution/run-load.test.js
O projects/monolith/frontend/src/routes/private/factory/execution/url-state.js
O projects/monolith/frontend/src/routes/private/factory/execution/url-state.test.js
O projects/monolith/frontend/src/routes/private/factory/execution/workspace-recovery.js
O projects/monolith/frontend/src/routes/private/factory/execution/workspace-recovery.test.js
O projects/monolith/frontend/src/routes/private/factory/work-items/[id]/+page.svelte
O projects/monolith/frontend/src/routes/private/factory/work-items/[id]/page.dom.test.js
O projects/monolith/frontend/src/routes/private/notes/+page.svelte
O projects/monolith/frontend/src/routes/public/+page.server.js
O projects/monolith/frontend/src/routes/public/+page.svelte
D projects/monolith/frontend/src/routes/public/HomepageRack.svelte
O projects/monolith/frontend/src/routes/public/app/notes/graph/+server.js
O projects/monolith/frontend/src/routes/public/app/notes/graph/server.test.js
O projects/monolith/frontend/src/routes/public/app/notes/stats/+server.js
O projects/monolith/frontend/src/routes/public/app/notes/stats/server.test.js
O projects/monolith/frontend/src/routes/public/app/trips/[slug]/timeline/+page.svelte
O projects/monolith/frontend/src/routes/public/app/trips/[slug]/timeline/page.dom.test.js
O projects/monolith/frontend/src/routes/public/blog/Trail.svelte
O projects/monolith/frontend/src/routes/public/cv/+page.svelte
O projects/monolith/frontend/src/routes/public/docs/+page.svelte
O projects/monolith/frontend/src/routes/public/docs/DocsSearch.svelte
O projects/monolith/frontend/src/routes/public/docs/DocsShell.svelte
O projects/monolith/frontend/src/routes/public/docs/DocsTabs.svelte
O projects/monolith/frontend/src/routes/public/docs/[...slug]/+page.svelte
O projects/monolith/frontend/src/routes/public/docs/shell.dom.test.js
O projects/monolith/frontend/src/routes/public/engineering/+page.svelte
O projects/monolith/frontend/src/routes/public/engineering/page.dom.test.js
O projects/monolith/frontend/src/routes/public/home-portrait.dom.test.js
O projects/monolith/frontend/src/routes/public/home-portrait.js
O projects/monolith/frontend/src/routes/public/home-portrait.test.js
O projects/monolith/frontend/src/routes/public/homepage-stats.js
O projects/monolith/frontend/src/routes/public/homepage-stats.test.js
O projects/monolith/frontend/src/routes/public/homepage.dom.test.js
O projects/monolith/frontend/src/routes/public/llms.txt/+server.js
O projects/monolith/frontend/src/routes/public/llms.txt/server.test.js
O projects/monolith/frontend/src/routes/public/page.server.test.js
O projects/monolith/frontend/src/routes/public/profile-shell.dom.test.js
O projects/monolith/frontend/src/routes/public/scotland/+page.svelte
O projects/monolith/frontend/src/routes/public/scotland/PolicyPage.svelte
O projects/monolith/frontend/src/routes/public/scotland/marketplace.json/+server.js
O projects/monolith/frontend/src/routes/public/scotland/marketplace.json/server.test.js
O projects/monolith/frontend/src/routes/public/scotland/page.dom.test.js
O projects/monolith/frontend/src/routes/public/scotland/policy-data.js
O projects/monolith/frontend/src/routes/public/scotland/policy.dom.test.js
O projects/monolith/frontend/src/routes/public/scotland/privacy/+page.svelte
O projects/monolith/frontend/src/routes/public/scotland/scotland-data.js
O projects/monolith/frontend/src/routes/public/scotland/support/+page.svelte
O projects/monolith/frontend/src/routes/public/scotland/terms/+page.svelte
O projects/monolith/frontend/src/routes/public/sitemap.xml/+server.js
O projects/monolith/frontend/src/routes/public/sitemap.xml/server.test.js
O projects/monolith/frontend/src/routes/public/slop/Trail.svelte
O projects/monolith/frontend/src/routes/public/slop/factory/+page.server.js
O projects/monolith/frontend/src/routes/public/slop/factory/+page.svelte
O projects/monolith/frontend/src/routes/public/slop/factory/activity/server.test.js
O projects/monolith/frontend/src/routes/public/slop/factory/data/activity/+server.js
O projects/monolith/frontend/src/routes/public/slop/factory/data/activity/server.test.js
O projects/monolith/frontend/src/routes/public/slop/factory/data/board/+server.js
O projects/monolith/frontend/src/routes/public/slop/factory/data/board/server.test.js
O projects/monolith/frontend/src/routes/public/slop/factory/facts/+server.js
O projects/monolith/frontend/src/routes/public/slop/factory/facts/server.test.js
O projects/monolith/frontend/src/routes/public/slop/factory/goals/+server.js
O projects/monolith/frontend/src/routes/public/slop/factory/goals/server.test.js
O projects/monolith/frontend/src/routes/public/slop/factory/merges/+server.js
O projects/monolith/frontend/src/routes/public/slop/factory/merges/server.test.js
O projects/monolith/frontend/src/routes/public/slop/factory/page.server.test.js
O projects/monolith/frontend/src/routes/public/theme-scope.dom.test.js
O projects/monolith/frontend/src/routes/public/theme-scope.test.js
O projects/monolith/frontend/static/joe-grey.png
O projects/monolith/frontend/svelte.config.js
O projects/monolith/frontend/test/ProfileRoute.svelte
O projects/monolith/frontend/test/data-display/server-render.js
O projects/monolith/frontend/test/profile-page.svelte.js
O projects/monolith/frontend/test/profile-stores.js
P projects/monolith/grimoire/__init__.py
O projects/monolith/grimoire/architecture.md
P projects/monolith/grimoire/audience.py
P projects/monolith/grimoire/audience_test.py
P projects/monolith/grimoire/character_facts.py
P projects/monolith/grimoire/character_facts_test.py
P projects/monolith/grimoire/explore.py
P projects/monolith/grimoire/extract.py
P projects/monolith/grimoire/extract_test.py
P projects/monolith/grimoire/handouts.py
P projects/monolith/grimoire/images.py
P projects/monolith/grimoire/images_test.py
P projects/monolith/grimoire/initiative.py
P projects/monolith/grimoire/initiative_test.py
P projects/monolith/grimoire/jobs.py
P projects/monolith/grimoire/join_links.py
P projects/monolith/grimoire/journal_router_test.py
P projects/monolith/grimoire/library.py
P projects/monolith/grimoire/models.py
P projects/monolith/grimoire/play_embeddings.py
P projects/monolith/grimoire/public.py
P projects/monolith/grimoire/route_inventory_test.py
P projects/monolith/grimoire/router.py
P projects/monolith/grimoire/router_public.py
P projects/monolith/grimoire/router_public_test.py
P projects/monolith/grimoire/session_events.py
P projects/monolith/grimoire/session_events_postgres_test.py
P projects/monolith/grimoire/session_events_test.py
P projects/monolith/grimoire/testing/inventory_matrix.py
P projects/monolith/grimoire/testing/leak_harness.py
O projects/monolith/grimoire/tools/README.md
O projects/monolith/grimoire/tools/upload-book.sh
P projects/monolith/grimoire/tools/upload_book_test.py
P projects/monolith/grimoire/transcripts_test.py
P projects/monolith/grimoire/visibility_test.py
P projects/monolith/grimoire_chat/cache.py
P projects/monolith/grimoire_chat/limits.py
P projects/monolith/grimoire_chat/router.py
P projects/monolith/hikes/__init__.py
P projects/monolith/hikes/__init___test.py
P projects/monolith/hikes/forecast.py
P projects/monolith/hikes/forecast_test.py
P projects/monolith/hikes/jobs.py
P projects/monolith/hikes/jobs_test.py
P projects/monolith/hikes/materialize.py
P projects/monolith/hikes/materialize_test.py
P projects/monolith/hikes/models.py
P projects/monolith/hikes/models_test.py
P projects/monolith/hikes/router.py
P projects/monolith/hikes/router_test.py
P projects/monolith/home/__init__.py
D projects/monolith/home/api.py
P projects/monolith/home/observability/rollup.py
P projects/monolith/home/observability/rollup_test.py
P projects/monolith/home/observability/router.py
P projects/monolith/home/observability/router_test.py
P projects/monolith/home/observability/stats.py
P projects/monolith/home/observability/stats_test.py
P projects/monolith/home/schedule_calendar_handler_test.py
D projects/monolith/home/schedule_router.py
D projects/monolith/home/schedule_router_test.py
P projects/monolith/home/tests/bdd_api_test.py
D projects/monolith/home/tests/bdd_public_test.py
O projects/monolith/knowledge/README.md
P projects/monolith/knowledge/__init__.py
P projects/monolith/knowledge/api.py
P projects/monolith/knowledge/audit.py
P projects/monolith/knowledge/audit_feedback.py
P projects/monolith/knowledge/audit_test.py
P projects/monolith/knowledge/board.py
P projects/monolith/knowledge/dead_letter_test.py
P projects/monolith/knowledge/deployment_observations.py
P projects/monolith/knowledge/deployment_observations_test.py
P projects/monolith/knowledge/extraction.py
P projects/monolith/knowledge/fact_review.py
P projects/monolith/knowledge/fact_review_batch.py
P projects/monolith/knowledge/fact_review_batch_postgres_test.py
P projects/monolith/knowledge/fact_review_batch_test.py
P projects/monolith/knowledge/fact_review_test.py
P projects/monolith/knowledge/freshness.py
P projects/monolith/knowledge/freshness_test.py
P projects/monolith/knowledge/frontmatter_scoped_test.py
P projects/monolith/knowledge/gap_answer_fileless_test.py
P projects/monolith/knowledge/gap_api_test.py
P projects/monolith/knowledge/gap_review_endpoints_test.py
P projects/monolith/knowledge/gap_set_class_test.py
P projects/monolith/knowledge/gaps.py
P projects/monolith/knowledge/health.py
P projects/monolith/knowledge/http_cache.py
P projects/monolith/knowledge/http_cache_test.py
P projects/monolith/knowledge/indexing.py
P projects/monolith/knowledge/indexing_test.py
P projects/monolith/knowledge/interventions_test.py
P projects/monolith/knowledge/knowledge_fixes_coverage_test.py
P projects/monolith/knowledge/mcp.py
P projects/monolith/knowledge/models.py
P projects/monolith/knowledge/notes.py
P projects/monolith/knowledge/notes_test.py
D projects/monolith/knowledge/profile.py
D projects/monolith/knowledge/profile_test.py
P projects/monolith/knowledge/public_fact_counts.py
P projects/monolith/knowledge/public_router.py
P projects/monolith/knowledge/public_router_test.py
P projects/monolith/knowledge/raw_paths.py
P projects/monolith/knowledge/raw_paths_test.py
P projects/monolith/knowledge/review_batch_sources.py
P projects/monolith/knowledge/review_coverage.py
P projects/monolith/knowledge/review_source_blocks.py
P projects/monolith/knowledge/review_sources.py
P projects/monolith/knowledge/review_sources_test.py
P projects/monolith/knowledge/router.py
P projects/monolith/knowledge/router_test.py
P projects/monolith/knowledge/store.py
P projects/monolith/knowledge/store_extra_test.py
P projects/monolith/knowledge/store_test.py
P projects/monolith/knowledge/store_zero_chunk_test.py
P projects/monolith/knowledge/tasks_router.py
P projects/monolith/knowledge/tasks_router_test.py
P projects/monolith/knowledge/tests/bdd_api_test.py
P projects/monolith/knowledge/visibility.py
P projects/monolith/knowledge/visibility_test.py
P projects/monolith/knowledge/wikilinks.py
P projects/monolith/knowledge/wikilinks_test.py
P projects/monolith/moving/gcal_sync.py
P projects/monolith/moving/tests/gcal_sync_test.py
P projects/monolith/observability/factory_goals.py
P projects/monolith/observability/public_router.py
P projects/monolith/observability_snapshot_grants_test.py
P projects/monolith/public_faas_rate_limit_test.py
P projects/monolith/public_faas_token_projection_test.py
P projects/monolith/public_knowledge_views_test.py
P projects/monolith/public_library_image_digest_guard_test.py
P projects/monolith/public_turnstile_secret_isolation_test.py
P projects/monolith/scheduler/api.py
P projects/monolith/scheduler/argo_handled_test.py
P projects/monolith/scheduler/router_test.py
P projects/monolith/scheduler/scheduler_test.py
P projects/monolith/scheduler/service.py
P projects/monolith/scheduler/service_test.py
P projects/monolith/scheduler/views.py
P projects/monolith/scheduler/views_test.py
P projects/monolith/shared/embedding.py
P projects/monolith/shared/embedding_retryable_test.py
P projects/monolith/shared/env.py
P projects/monolith/shared/env_test.py
P projects/monolith/shared/inference.py
P projects/monolith/shared/inference_test.py
P projects/monolith/shared/pricing.py
P projects/monolith/shared/pricing_test.py
P projects/monolith/shared/testing/markers.py
P projects/monolith/shared/testing/markers_test.py
P projects/monolith/shared/testing/plugin.py
P projects/monolith/shared/testing/server_readiness_test.py
P projects/monolith/ships/__init__.py
P projects/monolith/ships/router.py
P projects/monolith/ships/store.py
P projects/monolith/shotter/__init__.py
P projects/monolith/shotter/mcp.py
P projects/monolith/shotter/s3.py
P projects/monolith/shotter/tests/bdd_mcp_tool_test.py
O projects/monolith/stars/AURORA.md
P projects/monolith/stars/__init__.py
P projects/monolith/stars/__init___test.py
P projects/monolith/stars/aurora_job.py
P projects/monolith/stars/aurora_job_test.py
P projects/monolith/stars/forecast_test.py
P projects/monolith/stars/grid.py
P projects/monolith/stars/grid_gen/backfill_cerra.py
D projects/monolith/stars/grid_gen/backfill_climatology.py
D projects/monolith/stars/grid_gen/backfill_climatology_test.py
P projects/monolith/stars/grid_gen/build_config_test.py
D projects/monolith/stars/grid_gen/generate_grid.py
D projects/monolith/stars/grid_gen/generate_grid_test.py
P projects/monolith/stars/grid_gen/generate_grid_v2.py
P projects/monolith/stars/grid_gen/prepare_aurora_terrain.py
P projects/monolith/stars/grid_ingest.py
P projects/monolith/stars/grid_test.py
P projects/monolith/stars/jobs.py
P projects/monolith/stars/jobs_test.py
P projects/monolith/stars/models.py
P projects/monolith/stars/models_test.py
P projects/monolith/stars/router.py
P projects/monolith/stars/router_test.py
P projects/monolith/stars/scoring.py
P projects/monolith/trips/backfill/__init__.py
P projects/monolith/trips/backfill/main.py
P projects/monolith/trips/backfill/main_test.py
P projects/monolith/trips/s3.py
P projects/monolith/worldcup/__init__.py
P projects/monolith/worldcup/router.py
O projects/oom-inference/Cargo.lock
O projects/oom-inference/README.md
O projects/oom-inference/crates/oominf-convert/src/adapters/mod.rs
O projects/oom-inference/crates/oominf-convert/src/adapters/qwen35.rs
O projects/oom-inference/crates/oominf-convert/src/lib.rs
O projects/oom-inference/crates/oominf-convert/src/safetensors.rs
O projects/oom-inference/crates/oominf-convert/src/tests.rs
O projects/oom-inference/crates/oominf-metal/Cargo.toml
O projects/oom-inference/crates/oominf-metal/src/decoder.rs
O projects/oom-inference/crates/oominf-metal/src/elementwise.rs
O projects/oom-inference/crates/oominf-metal/src/kernels.metal
O projects/oom-inference/crates/oominf-metal/src/lib.rs
O projects/oom-inference/crates/oominf-metal/src/linear.rs
O projects/oom-inference/crates/oominf-metal/src/memory.rs
O projects/oom-inference/crates/oominf-metal/src/norm.rs
O projects/oom-inference/crates/oominf-metal/src/routed.rs
O projects/oom-inference/crates/oominf-metal/src/tests.rs
O projects/oom-inference/crates/oominf-metal/src/transfer.rs
O projects/oom-inference/crates/oominf-models-qwen/Cargo.toml
O projects/oom-inference/crates/oominf-models-qwen/src/model.rs
O projects/oom-inference/crates/oominf-models-qwen/src/ple.rs
O projects/oom-inference/crates/oominf-models-qwen35/Cargo.toml
O projects/oom-inference/crates/oominf-models-qwen35/examples/logits.rs
O projects/oom-inference/crates/oominf-models-qwen35/src/lib.rs
O projects/oom-inference/crates/oominf-server/src/parse.rs
O projects/oom-inference/crates/oominf-tiers/Cargo.toml
O projects/oom-inference/crates/oominf-tiers/src/host.rs
O projects/oom-inference/crates/oominf-tiers/src/resources.rs
O projects/oom-inference/crates/oominf-tiers/tests/read_throughput.rs
O projects/oom-inference/crates/oominf/Cargo.toml
O projects/oom-inference/crates/oominf/src/chat.rs
O projects/oom-inference/crates/oominf/src/doctor_metal.rs
O projects/oom-inference/crates/oominf/src/generate.rs
O projects/oom-inference/crates/oominf/src/load.rs
O projects/oom-inference/crates/oominf/src/main.rs
O projects/oom-inference/crates/oominf/src/profile.rs
O projects/oom-inference/crates/oominf/src/profile_args.rs
D projects/oom-inference/docs/HARDWARE.md
D projects/oom-inference/docs/TESTING.md
O projects/oom-inference/docs/dev/README.md
O projects/oom-inference/docs/dev/architecture.md
O projects/oom-inference/docs/dev/measurements.md
O projects/oom-inference/docs/dev/testing.md
O projects/oom-inference/docs/guide/install.md
O projects/oom-inference/evals/pyproject.toml
O projects/oom-inference/evals/uv.lock
P projects/oom-inference/reference/check_qwen35_metal.py
O projects/oom-inference/reference/pyproject.toml
O projects/oom-inference/reference/uv.lock
O projects/oom-inference/rustfmt.toml
O projects/operators/README.md
O projects/operators/oci-model-cache/cmd/BUILD
G projects/operators/oci-model-cache/cmd/chart_startup_test.go
G projects/operators/oci-model-cache/cmd/main_test.go
O projects/operators/oci-model-cache/helm/oci-model-cache-operator/BUILD
O projects/operators/oci-model-cache/helm/oci-model-cache-operator/Chart.yaml
O projects/operators/oci-model-cache/helm/oci-model-cache-operator/templates/deployment.yaml
O projects/operators/oci-model-cache/helm/oci-model-cache-operator/values.yaml
G projects/operators/oci-model-cache/internal/config/config.go
G projects/operators/oci-model-cache/internal/config/config_test.go
G projects/operators/oci-model-cache/internal/naming/naming.go
D projects/platform-gke/argo-workflows/BUILD
D projects/platform-gke/argocd/BUILD
D projects/platform-gke/authentik/BUILD
D projects/platform-gke/cert-manager/BUILD
O projects/platform-gke/cert-manager/application.yaml
D projects/platform-gke/cloudflare-gateway/BUILD
O projects/platform-gke/cloudnative-pg/application.yaml
D projects/platform-gke/kargo/BUILD
O projects/platform-gke/kustomization.yaml
D projects/platform-gke/kyverno/BUILD
O projects/platform-gke/kyverno/application.yaml
D projects/platform-gke/onepassword/BUILD
D projects/platform-gke/otel-collector/BUILD
O projects/platform-gke/otel-collector/application.yaml
D projects/platform-gke/polylane/BUILD
D projects/platform-gke/priority-classes/BUILD
O projects/platform-gke/renovate/BUILD
D projects/platform-gke/spire/BUILD
O projects/platform-gke/tailscale/BUILD
O projects/platform-gke/tailscale/values.yaml
O projects/platform/ARCHITECTURE.md
O projects/platform/README.md
O projects/platform/argo-workflows/BUILD
O projects/platform/argo-workflows/values.yaml
O projects/platform/argocd/BUILD
O projects/platform/authentik/BUILD
O projects/platform/authentik/README.md
O projects/platform/authentik/blueprints/kargo-auth.yaml
O projects/platform/authentik/values-gke.yaml
O projects/platform/authentik/values.yaml
O projects/platform/cert-manager/BUILD
O projects/platform/cert-manager/values.yaml
D projects/platform/cf-ingress-library/BUILD
O projects/platform/cloudflare-gateway/BUILD
O projects/platform/cloudflare-gateway/templates/public-rate-limit-patches.yaml
O projects/platform/cloudflare-gateway/values.yaml
O projects/platform/kargo/BUILD
O projects/platform/kargo/README.md
O projects/platform/kargo/templates/promotion.yaml
O projects/platform/kargo/values.yaml
D projects/platform/kustomization.yaml
O projects/platform/kyverno/BUILD
D projects/platform/kyverno/values-prod.yaml
O projects/platform/kyverno/values.yaml
O projects/platform/onepassword/BUILD
O projects/platform/otel-collector/BUILD
P projects/platform/otel-collector/backup_check_test.py
P projects/platform/otel-collector/collector_optin_test.py
P projects/platform/otel-collector/files/backup_check.py
O projects/platform/otel-collector/templates/backup-check.yaml
D projects/platform/otel-collector/templates/loom-backup-check.yaml
O projects/platform/otel-collector/values-gke.yaml
O projects/platform/otel-collector/values-prod.yaml
O projects/platform/otel-collector/values.yaml
O projects/platform/polylane/BUILD
O projects/platform/polylane/values.yaml
O projects/platform/priority-classes/BUILD
O projects/platform/priority-classes/templates/priorityclasses.yaml
O projects/platform/renovate/BUILD
O projects/platform/renovate/COVERAGE.md
O projects/platform/renovate/README.md
D projects/platform/renovate/application.yaml
P projects/platform/renovate/dependency_policy_test.py
D projects/platform/renovate/files/custom-pins.py
O projects/platform/renovate/files/custom-pins.py.tpl
O projects/platform/renovate/files/pull-request.mjs
P projects/platform/renovate/hub_deployment_test.py
D projects/platform/renovate/kustomization.yaml
D projects/platform/renovate/migration_staging_test.py
D projects/platform/renovate/scripts/bump-renovated-chart.sh
O projects/platform/renovate/templates/apko-lock-cronworkflow.yaml
O projects/platform/renovate/templates/apko-lock-script.yaml
O projects/platform/renovate/templates/cronworkflow.yaml
O projects/platform/renovate/values-gke.yaml
D projects/platform/renovate/values-home-suspend.yaml
O projects/platform/renovate/values.yaml
O projects/platform/spire/BUILD
O projects/scotland-connector/ARCHITECTURE.md
O projects/scotland-connector/chart/BUILD
O projects/scotland-connector/chart/Chart.yaml
P projects/scotland-connector/chart/chart_posture_test.py
P projects/scotland-connector/chart/rate_limit_acceptance_test.py
O projects/scotland-connector/chart/templates/_helpers.tpl
O projects/scotland-connector/chart/templates/deployment.yaml
O projects/scotland-connector/chart/templates/hpa.yaml
O projects/scotland-connector/chart/templates/httproute.yaml
O projects/scotland-connector/chart/templates/namespace.yaml
O projects/scotland-connector/chart/templates/networkpolicy.yaml
O projects/scotland-connector/chart/templates/onepassworditem.yaml
O projects/scotland-connector/chart/templates/pdb.yaml
O projects/scotland-connector/chart/templates/service.yaml
O projects/scotland-connector/chart/templates/serviceaccount.yaml
O projects/scotland-connector/chart/values.yaml
O projects/scotland-connector/cmd/connector/BUILD
G projects/scotland-connector/cmd/connector/main.go
G projects/scotland-connector/cmd/connector/main_test.go
O projects/scotland-connector/cmd/stars-aurora/BUILD
G projects/scotland-connector/cmd/stars-aurora/main.go
G projects/scotland-connector/cmd/stars-aurora/main_test.go
O projects/scotland-connector/deploy/BUILD
O projects/scotland-connector/deploy/application.yaml
O projects/scotland-connector/deploy/kustomization.yaml
O projects/scotland-connector/deploy/values.yaml
O projects/scotland-connector/image/BUILD
O projects/scotland-connector/image/apko.lock.json
O projects/scotland-connector/image/apko.yaml
O projects/scotland-connector/internal/astro/BUILD
G projects/scotland-connector/internal/astro/moon.go
G projects/scotland-connector/internal/astro/moon_test.go
G projects/scotland-connector/internal/astro/sun.go
G projects/scotland-connector/internal/astro/sun_test.go
O projects/scotland-connector/internal/aurora/BUILD
G projects/scotland-connector/internal/aurora/aurora.go
G projects/scotland-connector/internal/aurora/aurora_test.go
G projects/scotland-connector/internal/aurora/integration_test.go
G projects/scotland-connector/internal/aurora/producer.go
G projects/scotland-connector/internal/aurora/terrain.go
G projects/scotland-connector/internal/aurora/terrain_test.go
O projects/scotland-connector/internal/connector/BUILD
G projects/scotland-connector/internal/connector/aurora.go
G projects/scotland-connector/internal/connector/bench_test.go
G projects/scotland-connector/internal/connector/check.go
G projects/scotland-connector/internal/connector/connector.go
G projects/scotland-connector/internal/connector/connector_test.go
G projects/scotland-connector/internal/connector/diversity_test.go
G projects/scotland-connector/internal/connector/plan.go
D projects/scotland-connector/internal/connector/shape_test.go
G projects/scotland-connector/internal/connector/trip.go
D projects/scotland-connector/internal/connector/walks.go
D projects/scotland-connector/internal/connector/walks_test.go
O projects/scotland-connector/internal/fixture/BUILD
G projects/scotland-connector/internal/fixture/aurora.go
G projects/scotland-connector/internal/fixture/fixture.go
O projects/scotland-connector/internal/httpapi/BUILD
G projects/scotland-connector/internal/httpapi/aurora.go
G projects/scotland-connector/internal/httpapi/httpapi.go
G projects/scotland-connector/internal/httpapi/httpapi_test.go
O projects/scotland-connector/internal/httpapi/method.md
O projects/scotland-connector/internal/httpapi/openapi.json
O projects/scotland-connector/internal/mcp/BUILD
G projects/scotland-connector/internal/mcp/mcp.go
G projects/scotland-connector/internal/mcp/mcp_test.go
G projects/scotland-connector/internal/mcp/tools.go
O projects/scotland-connector/internal/meter/BUILD
G projects/scotland-connector/internal/meter/meter.go
G projects/scotland-connector/internal/meter/meter_test.go
D projects/scotland-connector/internal/places/BUILD
D projects/scotland-connector/internal/places/places.go
D projects/scotland-connector/internal/places/places_test.go
D projects/scotland-connector/internal/places/seed.json
O projects/scotland-connector/internal/r2/BUILD
G projects/scotland-connector/internal/r2/client.go
G projects/scotland-connector/internal/r2/client_test.go
O projects/scotland-connector/internal/stars/BUILD
G projects/scotland-connector/internal/stars/dataset.go
G projects/scotland-connector/internal/stars/geo.go
G projects/scotland-connector/internal/stars/stars_test.go
G projects/scotland-connector/internal/stars/verdict.go
G projects/scotland-connector/internal/stars/verdict_test.go
G projects/scotland-connector/internal/stars/windows.go
O projects/scotland-connector/internal/store/BUILD
G projects/scotland-connector/internal/store/aurora.go
G projects/scotland-connector/internal/store/store.go
G projects/scotland-connector/internal/store/store_test.go
G projects/scotland-connector/internal/store/walks.go
G projects/scotland-connector/internal/store/walks_test.go
O projects/scotland-connector/internal/verdict/BUILD
G projects/scotland-connector/internal/verdict/verdict.go
G projects/scotland-connector/internal/verdict/verdict_test.go
O projects/scotland-connector/internal/walks/BUILD
G projects/scotland-connector/internal/walks/verdict.go
G projects/scotland-connector/internal/walks/verdict_test.go
G projects/scotland-connector/internal/walks/walks.go
G projects/scotland-connector/internal/walks/walks_test.go
D projects/scotland-connector/plugin/.claude-plugin/marketplace.json
O projects/scotland-connector/plugin/.claude-plugin/plugin.json
O projects/scotland-connector/plugin/.mcp.json
O projects/scotland-connector/plugin/BUILD
O projects/scotland-connector/plugin/LICENSE
O projects/scotland-connector/plugin/README.md
O projects/scotland-connector/plugin/assets/icon-dark.png
O projects/scotland-connector/plugin/assets/icon.png
O projects/scotland-connector/plugin/mcp.json
O projects/scotland-connector/plugin/plugin.json
P projects/scotland-connector/plugin/plugin_test.py
O projects/scotland-connector/plugin/skills/scotland-trips/SKILL.md
O projects/sextant/cmd/sextant/BUILD
O projects/sextant/cmd/sextant/cmd/BUILD
O projects/sextant/pkg/codegen/BUILD
O projects/shared/README.md
D projects/shared/helm/homelab-library/chart/BUILD
O projects/shared/helm/homelab-library/chart/Chart.yaml
D projects/shared/helm/homelab-library/chart/templates/_imagepullsecret.tpl
D projects/shared/helm/homelab-library/chart/templates/_statefulset.tpl
O pyproject.toml
O renovate.json
P tools/cli/http_error_coverage_test.py
P tools/cli/main.py
P tools/cli/output.py
P tools/cli/scheduler_cmd.py
P tools/cli/scheduler_test.py
P tools/cli/tasks_cmd.py
P tools/cli/tasks_test.py
O tools/setup-cloud-env.sh
O tools/setup-local-tests.sh
```
