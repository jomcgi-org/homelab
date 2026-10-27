# Dependency coverage

`renovate.json` describes the proposed intake and merge policy. Activation is
held by the prerequisites in [README.md](README.md). A configured extractor
proves discovery; registry lookup, artifact regeneration, independent review
and CI must also pass before an update can merge.

## Inputs and generated outputs

| Input | Extraction | Outputs and constraints |
| --- | --- | --- |
| `MODULE.bazel` registry dependencies | `bazel-module` | Module lock maintenance uses Renovate's Bazel support and the existing `bazelModDeps` execution grant. Patched `rules_apko` still has to pass CI. |
| `MODULE.bazel` OCI pulls | `bazel-module` | Existing image digests stay pinned; native extraction covers the three OCI bases. |
| Bazel `http_archive` / `http_file` declarations | `bazel` | Reads `MODULE.bazel` and `.bzl` files; updates supported GitHub release URLs and checksums. Variable-built URLs and raw GitHub file URLs are gaps below. |
| `pyproject.toml` | `pep621` | Only this root manifest invokes the existing Bazel requirement compiler, updating `bazel/requirements/*.txt`. Generated locks are not independent version inputs. |
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
| `.github/workflows/*` | `github-actions` | Actions and supported tool versions remain covered, including SHA-pinned actions. Inline download commands are a separate gap. |
| `.bazelversion` | two custom regex extractors | The BuildBuddy fork selector and the upstream Bazel version are separate dependencies. The native one-line manager is disabled. |
| Bazel Node and pnpm toolchains | custom regex | pnpm changes group with `packageManager` in `package.json`. The documented pnpm 10 lock format stays below 11 pending migration. |
| GitHub binaries in `bazel/tools/tools.lock.json` | custom regex, `github-release-attachments` | Updates URL versions, versioned archive member paths and SHA256 together, for every platform. A missing checksum is not extracted. |
| Multiarch GitHub binaries in `MODULE.bazel` | custom regex, `github-release-attachments` | Updates paired URL/checksum fields together. Codex's `rust-v` tag format is explicit. |
| `apko.yaml` and `apko.lock.json` | existing weekly Argo job | Regenerates APK resolutions with pinned `rules_apko` and runs existing generators. It remains suspended on the hub and does not arm auto-merge. |

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
- `pydantic-ai-slim <2`, `genai-prices <0.2`, pnpm `<11` and npm override ranges
  preserve explicit API or lock-format constraints. Removing a compatibility
  bound is separate work.
- First-party chart identities, the manual `mcp-stack` mirror, the repository's
  own Git sources and first-party `projects/` and `bazel/` images remain owned
  by their publishers. Same-name upstream charts outside those exact identities
  remain eligible.
- Existing draft, assigned, human-owned or explicitly held PRs are not approved
  or queued by this configuration change. The held MCP experiment #7355 remains
  outside dependency maintenance. Cloudflared #7357 has its own owner.

## Remaining gaps

These pins must not be reported as automated:

- `tools.lock.json` downloads from `release.ariga.io` (Atlas), `get.helm.sh`
  (Helm), `cache.agilebits.com` (1Password CLI) and npm's architecture-specific
  Claude tarballs need a checksum-aware update adapter. GitHub release
  attachment discovery covers the other 13 tool families.
- The Python standalone interpreter override couples the release date, Python
  version and four checksums in `MODULE.bazel`. Generic version-only replacement
  would break this pin. Python minor/major migration also changes `pip.parse`.
- Raw-file Atlas/CNPG CRD snapshots, Bazel demo distdir URL/checksum arrays,
  variable-built Erlang/Hex dependencies, and the Meta Muse channel binary need
  their own coordinated adapters. Their generated outputs stay intact.
- The compiled root Python locks are regenerated after manifest updates, but
  do not yet have a standalone periodic transitive refresh. Security floors in
  `bazel/requirements/overrides.txt` require the same compiler after changes.
- Inline workflow downloads (currently the checksum-pinned Helm download in
  `factory-mobile-preview.yml`) are not GitHub Actions dependencies.
- GitHub dependency-graph comparison does not represent every ecosystem. The
  current evidence gate refuses an empty comparison, so a Helm, Bazel, image or
  apko-only PR can remain blocked even when independently reviewed and green.
  An attested alternative must preserve complete changed-file blobs, independent
  assessments and fresh vulnerability evidence; disabling the refusal would
  bypass an existing control.
- apko PRs remain review-only because the job does not request auto-merge and
  factory dependency landing is still disabled. Writer restoration reuses
  #7186, stacked on #7168, under #7001/#7068.

Registry access and checksum discovery failures remain failed updates. No
credentials, token scopes, installed Apps, rulesets, review publication flags or
factory author allowlists are provisioned by this change.
