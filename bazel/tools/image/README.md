# bazel/tools/image

Builds the multi-arch developer tools OCI image pushed to
`ghcr.io/jomcgi/homelab/bazel/tools/image`.

This is a single-purpose package. It does not expose reusable macros to the
rest of the repo. The three `.bzl` files here are internal helpers for
assembling per-platform tar layers; they are not loaded by any other package.
If you are looking for macros to build application service images (Go services,
Python services, nginx frontends), those live in [`//bazel/tools/oci`](../oci/README.md).

## What the image contains

| Tool / group                           | Source                                                     |
| -------------------------------------- | ---------------------------------------------------------- |
| `argocd`, `buildozer`, `crane`, `gazelle`, `gh`, `gofumpt`, `helm`, `kind`, `op`, `ruff`, `shfmt` | multitool lockfile (per-platform binary repos) |
| `node`                                 | `@nodejs_{platform}//:node_bin` from rules_nodejs          |
| Python runtime + stdlib + pip packages | `py_image_layer` from `@aspect_rules_py`, with platform transitions |
| `pnpm`                                 | `@pnpm//:pkg` (aspect_rules_js)                            |
| `prettier` (+ `prettier-plugin-svelte`, peer `svelte`) | `//:node_modules/*` (pnpm lockfile); wrapper sets `NODE_PATH` |
| `homelab` CLI                          | Source from `//tools/cli:*`, wrapped in a bash exec script |

All binaries land under `/usr/bin/`; Python's stdlib is in the runfiles tree
alongside the interpreter. pnpm and prettier (with the Svelte plugin) are
installed under `/usr/local/lib/node_modules/`; `/usr/bin/prettier` is a
wrapper that sets `NODE_PATH` so `require("prettier-plugin-svelte")` from
`bazel/tools/format/prettier.config.cjs` resolves without a workspace
`node_modules` tree.

## Build targets

| Target                    | Use                                                                         |
| ------------------------- | --------------------------------------------------------------------------- |
| `:image`                  | `oci_image_index` covering linux/amd64, linux/arm64, darwin/arm64           |
| `:image.push`             | Push the index to GHCR (CI stamps branch + timestamp tags)                  |
| `:image_linux_amd64`      | Single-arch `oci_image` for linux/amd64                                     |
| `:image_linux_arm64`      | Single-arch `oci_image` for linux/arm64                                     |
| `:image_darwin_arm64`     | Single-arch `oci_image` for darwin/arm64 (experimental)                     |
| `:python_deps_test`       | `py_test` verifying that pip deps (`httpx`, `typer`) are importable         |
| `:python_deps_semgrep_test` | SCA scan of the `python_deps` requirements against `//bazel/semgrep/rules:sca_python_rules` |

`:image.push` is included in `//bazel/images:push_all` and runs on merge to main
via BuildBuddy CI, but only when this image's content actually changed: the
`deploy` action publishes through `bazel/images/push/push-changed.sh`, which
compares each image's digest against the registry first. `:image.info` exists to
feed it that digest. This is the repo's one raw `oci_push`, so unlike the macro
built images it needs its `oci_image_info` written out by hand; without one the
digest manifest fails to analyse. Tags are stamped at build time; never set them
by hand.

## Local development

After first clone, run `./bootstrap.sh`. It uses `crane export` to extract the
full image filesystem into `.tools/`:

```bash
crane export ghcr.io/jomcgi/homelab/bazel/tools/image:latest - | tar -xf - -C .tools/
```

`direnv` then adds `.tools/usr/bin` to `$PATH`. Full extraction (not a subtree)
is intentional: Python and other tools depend on their stdlib being at a
relative path inside the same root.

## Internal macros

These three macros are used only within this package's own `BUILD` file. Do not
load them elsewhere.

### `multitool_tar`

```python
load("//bazel/tools/image:multitool_tar.bzl", "multitool_tar")

multitool_tar(
    name = "tools_tar",
    tools = ["helm", "crane", "gh", ...],
)
# Creates :tools_tar_linux_amd64, :tools_tar_linux_arm64, :tools_tar_darwin_arm64
```

For each platform, generates a `genrule` that copies the named tool binaries
out of their per-platform multitool repos
(`@multitool.<tool>.<os>_<cpu>//tools/<tool>:<os>_<cpu>_executable`) into a
tar at `package_dir` (default `/usr/bin`). Avoids platform transitions by
referencing the platform-specific repos that `rules_multitool` creates
directly.

### `node_tar`

```python
load("//bazel/tools/image:node_tar.bzl", "node_tar")

node_tar(name = "node_tar")
# Creates :node_tar_linux_amd64, :node_tar_linux_arm64, :node_tar_darwin_arm64
```

Packages the Node.js binary from `@nodejs_{platform}//:node_bin` into a tar at
`/usr/bin/node` for each platform.

### `python_tar`

```python
load("//bazel/tools/image:python_tar.bzl", "python_tar")

python_tar(
    name = "python_tar",
    binary = ":python_deps",
)
# Creates :python_tar_linux_amd64, :python_tar_linux_arm64, :python_tar_darwin_arm64
```

Calls `py_image_layer` from `@aspect_rules_py` for each platform, using that
macro's `platform` parameter to trigger the correct Python toolchain and pip
wheel selection. The `binary` argument is a `py_venv_binary` shim
(`:python_deps`) whose only job is to declare the full pip dependency tree;
the binary does nothing at runtime.

## Python deps shim

`python_deps.py` imports `httpx` and `typer` (the pip packages needed by the
homelab CLI) so `py_image_layer` discovers and packages their transitive
dependency trees. `python_deps_test.py` verifies these imports resolve at
test time, catching cases where a package is dropped or a `BUILD` declaration
drifts.

## homelab CLI

The `homelab_cli_tar` genrule copies `//tools/cli:*` source files into the
Python runfiles tree and installs a wrapper at `/usr/bin/homelab` that execs:

```bash
exec env PYTHONPATH="$RUNFILES" "$PYTHON" -m tools.cli.main "$@"
```

The wrapper uses the vendored venv Python packaged by `python_tar`, so all pip
packages resolve without any host Python installation.

## Relationship to bazel/tools/oci

`bazel/tools/oci/` provides the macros the rest of the repo uses to build
application service images:

- `apko_image` / `apko_nginx_frontend` for apko-based images (Wolfi packages,
  non-root uid 65532)
- `go_image` for Go service images
- `py3_image` for Python service images
- `OciImageInfo` provider consumed by `helm_images_values` in
  `//bazel/helm:defs.bzl`

This package (`bazel/tools/image/`) builds one specific image using
`oci_image` / `oci_image_index` from `@rules_oci` directly, layering custom
tars rather than going through apko. The tools image is infrastructure for
developers and CI agents, not a deployed service.

## Conventions

- This package's `.bzl` files are private. Load them only from within this
  package.
- The image push target is `//bazel/tools/image:image.push`. Do not run it
  locally; CI handles pushes on merge to main.
- darwin/arm64 is built for local bootstrap parity but is experimental:
  `py_image_layer` was designed for Linux OCI images.
- Add new pip packages to the `py_venv_binary` deps in `BUILD` (with a `# keep`
  comment to prevent gazelle pruning) and import them in `python_deps.py`.
