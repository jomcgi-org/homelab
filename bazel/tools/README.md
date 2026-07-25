# bazel/tools

Shared Bazel build tooling: rules, wrappers, and helper binaries used across the repo. Each subdirectory covers a distinct concern, from container image construction and frontend builds to code formatting, git hooks, and cluster inspection.

| Subdir | What it provides | Load path / notes |
| --- | --- | --- |
| [agent-run](agent-run/) | Go CLI binary that creates `SandboxClaim` resources and exec's into Goose sandbox pods to run agent tasks against a given task description or GitHub issue | Go binary; no `.bzl` files |
| [cdk8s](cdk8s/) | Shared Python cdk8s constructs (resource requirements, standard labels) for generating Kubernetes manifests | Python library; no `.bzl` files |
| [cluster](cluster/) | Read-only `sh_binary` wrappers around kubectl for cluster inspection: `pods`, `events`, `status`, `argocd` | Script helpers; no `.bzl` files |
| [ci](ci/) | Unified local feedback loop: selective lint/regen + `bb remote` Linux test (matches Workflows Test flags) | `ci` on PATH via direnv; `bazel/tools/ci/ci` |
| [format](format/) | The top-level `format` multirun target plus helper scripts for updating apko locks, Python requirements, and Atlas migration checksums | No new `.bzl` rules; orchestrates aspect_rules_lint |
| [git](git/) | Git hook scripts: commit-message ASCII check, stale-PR guard, post-rewrite format trigger, and main-branch protection | Shell scripts; no `.bzl` files |
| [hf2oci](hf2oci/README.md) | Go binary that streams HuggingFace model weights directly into OCI layers without writing temporary files to disk | See linked README |
| [hooks](hooks/) | Minimal Claude PreToolUse hooks (prefer `ci`/`bb remote`, em-dash, public_reader grants, plans-retired) | Shell scripts + `sh_test` |
| [http](http/) | `multiarch_http_archive` and `multiarch_http_file` repository rules for downloading dual-arch binaries from HTTP archives and packaging them as platform-specific tars for apko images | `//bazel/tools/http:multiarch_http_archive.bzl` |
| [image](image/README.md) | Builds the developer tools OCI image (vendored CLI tools, Node.js, Python, pnpm, prettier, homelab CLI) and exposes `multitool_tar`, `node_tar`, and `python_tar` macros | See linked README |
| [js](js/README.md) | Bazel macros for frontend builds: `vite_build`, `tailwind_build`, `exec_filegroup`, and `node_modules_tar` | See linked README |
| [lint](lint/) | Configures aspect_rules_lint linter aspects for ESLint, Ruff, and Shellcheck | `//bazel/tools/lint:linters.bzl` |
| [oci](oci/README.md) | Bazel rules and macros for building and pushing container images: `apko_image`, `go_image`, `py3_image`, `apko_nginx_frontend`, and the `OciImageInfo` provider | See linked README |
| [platforms](platforms/) | Bazel `platform` targets for `linux_aarch64`, `linux_x86_64`, and `darwin_aarch64`, used by image build transitions and cross-compilation | No `.bzl` files |
| [postgres](postgres/) | Module extension registering `oci_postgres`, which extracts PostgreSQL 16 and pgvector binaries from an OCI image for use as test data dependencies | `//bazel/tools/postgres:extensions.bzl` |
| [pytest](pytest/) | Thin `py_test` wrapper macro that sets `pytest_main = True` by default across all Python test targets in the repo | `//bazel/tools/pytest:defs.bzl` |
| [python](python/) | Gazelle Python manifest configuration (`gazelle_python_manifest`, `modules_mapping`) for keeping import-to-package mappings current during BUILD file generation | Gazelle config; no `.bzl` rules |
| [semgrep](semgrep/) | Python helper script that uploads semgrep scan results to Semgrep App after CI scans; always exits 0 so upload failures never affect Bazel test outcomes | Python script; no `.bzl` files |
