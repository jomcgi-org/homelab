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
There is no filename-based exemption. Old artifacts require fresh review.

Fresh GitHub alerts cannot establish fresh upstream advisories for ecosystems
GitHub does not represent. Live acceptance must establish revalidation of that
supplemental evidence before enabling automated merges. Missing provenance or
vulnerability evidence keeps the particular update blocked. The known polling
window between check publication and merge also remains an activation check.

The following pins retain explicit boundaries; they are not claimed automated:

- Python standalone's release date, Python version and four checksums form one
  override. `python.toolchain` and `pip.parse` select Python 3.13. A runtime
  adapter must verify rules_python's version resolution and all four platforms;
  independent version-only replacements cannot safely update this bundle.
- Atlas/CNPG raw CRD snapshots must track the deployed operator version and
  matching offline test schemas, rather than the latest raw file independently.
- The demo distdir is a proven offline closure for a fixed Abseil/Bazel fixture.
  Its parallel URL/checksum arrays and transitive versions need a regenerated
  closure. The Erlang OTP pin must exactly match Wolfi's runtime; Hex tuples
  encode a manually selected transitive closure and Hex's installer URL is
  mutable. These require coordinated resolvers/fixture validation.
- Meta Muse has a reachable public channel and checksum manifest, verified
  read-only. Neither supplies a release timestamp. A channel adapter needs
  durable first-seen age handling to preserve the three-day release-age policy.
  The current `1.4.1-R4503.1` pin and recorded runtime fixtures remain intact.

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
