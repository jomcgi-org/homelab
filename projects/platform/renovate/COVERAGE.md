# Dependency coverage

`renovate.json` describes the proposed intake and merge policy. Activation is
held by the prerequisites in [README.md](README.md). A configured extractor
proves discovery; registry lookup, artifact regeneration, independent review
and CI must also pass before an update can merge.

## Inputs and generated outputs

| Input | Extraction | Outputs and constraints |
| --- | --- | --- |
| `MODULE.bazel` registry dependencies | `bazel-module` | Updates source module versions. `MODULE.bazel.lock` is ignored and untracked, so Bazel regenerates it; Renovate skips module-lock artifact maintenance when the file is absent. Patched `rules_apko` still has to pass CI. |
| `MODULE.bazel` OCI pulls | `bazel-module` | Existing image digests stay pinned; native extraction covers the three OCI bases. |
| Bazel `http_archive` / `http_file` declarations | `bazel` | Reads `MODULE.bazel` and `.bzl` files; updates supported GitHub release URLs and checksums. Variable-built URLs and raw GitHub file URLs are gaps below. |
| `pyproject.toml` and `bazel/requirements/overrides.txt` | `pep621` / `pip_requirements` | Root manifest and security-floor changes invoke the existing Bazel requirement compiler. Weekly maintenance also runs both targets with `--upgrade` to refresh transitives within these bounds. Generated locks are outputs. |
| Both `projects/oom-inference/*/pyproject.toml` manifests | `pep621` | Native uv artifact support maintains their adjacent `uv.lock` files. The root Bazel post-upgrade command does not run for these projects. |
| `projects/model-bench/requirements-venv.txt` | `pip_requirements` | Pins remain explicit and are updated by the requirements manager. |
| `go.mod` | `gomod` | Updates `go.sum`; the Bazel Go SDK derives its version from this file. CI owns Gazelle BUILD regeneration. |
| Root and workspace `package.json` files | `npm` | Maintains the shared `pnpm-lock.yaml`; lifecycle scripts remain disabled. Security override ranges remain in range. |
| `Chart.yaml` dependencies | `helmv3` | Refreshes `Chart.lock` and committed subchart archives. Published wrapper versions and Application pins remain main-publish outputs. |
| ArgoCD `application.yaml` | `argocd` | Upstream chart versions are tracked. Exact first-party chart identities and self-source URLs are excluded. |
| `values.yaml` and `values-*.yaml` | `helm-values` | Includes cloudflare-gateway, polylane and hub overlays. Third-party digest updates remain available; CI/Kargo own first-party image versions. |
| Polylane cloudflared tag and separate digest | custom regex | The native values manager ignores the separate digest field, so only this custom extractor owns that dependency. Both cloudflared deployments group into one PR. |
| Literal helper images such as `iptablesImage` | custom regex | Tracks image tags and SHA256 digests that the structured values extractor misses. |
| Raw hub and deploy YAML | `kubernetes` | Explicit file patterns activate this otherwise inert manager. Templated Helm YAML is rendered and tested by CI, not treated as raw Kubernetes input. |
| `.github/workflows/*` | `github-actions` plus custom regex | SHA-pinned actions, supported tool versions and the browser workflow's pnpm, Playwright and PyYAML pins are extracted. Its Helm download reads the canonical tools lock. |
| `.bazelversion` | two custom regex extractors | The BuildBuddy fork selector and the upstream Bazel version are separate dependencies. The native one-line manager is disabled. |
| Bazel Node and pnpm toolchains | custom regex | pnpm changes group with `packageManager` in `package.json`. The documented pnpm 10 lock format stays below 11 pending migration. |
| GitHub binaries in `bazel/tools/tools.lock.json` | custom regex, `github-release-attachments` | Updates URL versions, versioned archive member paths and SHA256 together, for every platform. A missing checksum is not extracted. |
| Multiarch GitHub binaries in `MODULE.bazel` | custom regex, `github-release-attachments` | Updates paired URL/checksum fields together. Codex's `rust-v` tag format is explicit. |
| Non-GitHub Atlas, Helm, 1Password CLI and Claude binaries | weekly maintenance adapter | Separate PR per tool; updates every platform URL/SHA256 atomically. Stable releases age at least three days. Atlas/Helm verify published SHA256; Claude verifies npm SHA512 integrity. 1Password hashes its official HTTPS archive; this is not a signature verification. |
| `apko.yaml` and `apko.lock.json` | existing weekly Argo job | Regenerates APK resolutions with pinned `rules_apko` and runs existing generators. The job now handles apko, root Python and each non-GitHub tool in separate branches and requests platform auto-merge. Both hub schedules remain suspended. |

## Holds and exclusions

Ordinary major upgrades are eligible for the same independent review and CI as
minor and patch updates. They stay separate and carry `major-upgrade`.

- Envoy Gateway charts, Gateway and Proxy images, and the paired downloaded
  binaries stay disabled while the known header regression is resolved. The
  separate guard owner decides when the compatibility hold can be removed.
- Firecracker, Kata and k3s binary updates require dashboard approval and do not
  auto-merge. Firecracker changes invalidate snapshots and need the coordinated
  runtime/base-signature migration documented beside the pins.
- The pinned Loom fixture and Kubernetes schema archives require coordinated
  fixture changes and do not auto-merge.
- `pydantic-ai-slim <2`, `genai-prices <0.2`, `pytest-asyncio <2`, pnpm `<11` and npm override ranges
  preserve explicit API or lock-format constraints. Removing a compatibility
  bound is separate work.
- First-party chart identities, the manual `mcp-stack` mirror, the repository's
  own Git sources and first-party `projects/` and `bazel/` images remain owned
  by their publishers. Same-name upstream charts outside those exact identities
  remain eligible.
- Existing draft, assigned, human-owned or explicitly held PRs are not approved
  or queued by this configuration change. The held MCP experiment #7355 remains
  outside dependency maintenance. Cloudflared #7357 has its own owner.

## Review evidence and remaining boundaries

A successful empty GitHub dependency-graph comparison is accepted as a coverage
limitation, never as proof of safety. The gate still requires complete changed
file identities/blob SHAs, fresh successful graph and alert reads, two distinct
safe review sessions and an exact match at the queue head. Both artifacts must
list every changed filename in `reviewed_files` and supply `evidence_coverage`
with concrete dependency identities, immutable versions/digests, upstream
advisories and artifact/SBOM/scan evidence for unrepresented ecosystems.
Omissions, unavailable reads, changed blobs and unsafe assessments refuse.
Ownership/approval holds are rechecked at intake and both publication reads;
a new hold invalidates the old approval. Native Renovate stops updating PRs
labelled `human`.
There is no filename-based exemption. Old artifacts require fresh review.

Both reviewers must independently agree on complete base/head `advisory_sources`
inventories, bound to the reviewed commits and every corresponding changed file.
Each entry binds its stable occurrence, immutable artifact bytes, exposure
fingerprint, exact package/ecosystem/version or Git commit query, file associations
and complete normalized OSV response. The trusted server re-fetches both sides at
completion and publication. Unrelated baseline findings may remain only when all
those component fields are unchanged. Partial remediation can therefore pass;
added vulnerable versions, copies or exposure cannot, even if other findings are
removed. Changed advisory content requires fresh independent review. Residual
findings also require the current PR/queue base to equal the reviewed base;
otherwise fresh review must establish the new baseline. Candidate blob equality
alone cannot show that main has not already fixed a vulnerability or changed
exposure. Clean candidates retain equivalent-content queue comparison.

Incomplete inventories, truncation, errors and unsupported coverage refuse. No
GitHub credentials or caller-selected endpoint reach OSV. At most 100 components
per side fit an assessment. Reviewers establish query applicability, complete
artifact/SBOM closure and exposure derivation; agreement and hashes do not prove
those facts. An unknown package with an empty result proves nothing. Arbitrary
upstream advisory pages are not silently treated as covered. This implements the
existing non-regression policy without a suppression or new risk exception.

Publication waits for exact-head CI and revalidates each head immediately before
writing, with a 60-second collection bound and a final durable-approval read.
This verifies the exact queue commit at a point in time. GitHub check success
has no expiry: publisher/token loss after success or advisory changes before
merge remain possible. Canary that behavior and obtain explicit acceptance of
the residual race before activation. No atomic merge freshness or mandatory
expiry service is claimed.

Remaining coverage is split below. An adapter gap is code work under the
maintenance goal, not a request for Joe to approve each ordinary bump. A
compatibility hold remains binding until its migration or evidence is ready.

| Boundary | Classification | Work required |
| --- | --- | --- |
| Python standalone release/date/four-platform checksums | Adapter code gap; Python 3.13 selection is a separate compatibility constraint | Update the bundle atomically and test rules_python resolution on all four platforms. Patch/rebuild maintenance can preserve 3.13; a minor/major migration must also update `python.toolchain`, `pip.parse` and compatibility validation. |
| Atlas/CNPG raw CRD snapshots | Adapter code gap, not an intentional version freeze | Derive CRD versions from the operator update and refresh corresponding checksums/offline schema tests together. |
| Hex package tuples and mutable installer | Resolver/lock code gap | Regenerate and validate the complete transitive closure, including integrity, instead of changing independent tuple versions. |
| Muse stable channel | Adapter/state code gap | Its public channel and checksum manifest are readable but lack release timestamps. Add durable first-seen aging and checksum-paired updates. Runtime fixtures must validate compatibility. |
| Demo Abseil/Bazel distdir | Deliberate fixture compatibility hold | Regenerate its fixed offline closure and prove the zero-egress fixture before advancing the parallel URL/checksum arrays. |
| OTP and Wolfi Erlang runtime | Deliberate exact-version compatibility hold, with a future paired adapter possible | Choose a version published by both sources, update both together and validate the release/runtime pair. |
| Envoy family, Firecracker snapshots, Kata/k3s, Loom/schema fixtures and explicit API bounds | Existing compatibility holds listed above | Preserve the exact holds; only the corresponding regression fix, coordinated migration or fixture validation can remove them. Ordinary majors outside these explicit boundaries stay eligible. |

Unsupported supplemental advisory sources, CI credential isolation and any
missing identity renewal/wiring are engineering/evidence gaps, not compatibility
holds. The structured OSV path is implemented; live feed applicability and the
point-in-time queue canary remain unverified. The post-publication race requires
explicit acceptance, not an invented expiry-service requirement. #7005's isolation
design needs a verified concrete target and budget/access authorization before
implementation. Existing owners must close those gaps before the relevant
automatic merge path is accepted. Updates requiring Python backend execution
also need an isolated generation path; the writer refuses execution.

Root Python automatic compilers pass `--no-build`; Renovate passes
`UV_NO_BUILD=true` to native uv artifact commands. Packages requiring source
backend execution need an isolated generation path, not writer credentials.

Registry access, checksum discovery and integrity failures remain failed
updates. Existing held/assigned/draft or human-authored maintenance PRs are
left untouched. The workflow checks holds before generation and again before
push. Each tool failure is isolated and leaves that tool's lock unchanged;
other modes continue. CI validates generated output and remains required.

No credentials, token scopes, installed Apps, rulesets, review publication flags
or factory author allowlists are provisioned here. Writer restoration reuses
#7186, stacked on #7168, under #7001/#7068; the CI credential boundary stays
with #7005. See README for the exact owner handoff.
