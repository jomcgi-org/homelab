# rules_oci (homelab wrappers)

Bazel macros for building dual-arch OCI images and the `OciImageInfo` provider
that carries repository and tag metadata downstream to Helm chart packaging.

All image rules produce a `{name}.info` target exposing `OciImageInfo`. That
target is what `helm_images_values` (and by extension `helm_chart`) consumes via
its `images` map to pin image tags at build time. See
[../../helm/README.md](../../helm/README.md) for the chart side of this
pipeline.

## Public API

Each macro lives in its own `.bzl` file; load from the file that matches the
image type.

```python
load("//bazel/tools/oci:go_image.bzl",           "go_image")
load("//bazel/tools/oci:apko_image.bzl",          "apko_image")
load("//bazel/tools/oci:py3_image.bzl",           "py3_image")
load("//bazel/tools/oci:apko_nginx_frontend.bzl", "apko_nginx_frontend")
load("//bazel/tools/oci:providers.bzl",           "OciImageInfo", "oci_image_info")
```

| Symbol                | Kind     | File                      | Use                                                               |
| --------------------- | -------- | ------------------------- | ----------------------------------------------------------------- |
| `go_image`            | macro    | `go_image.bzl`            | Multi-arch OCI image from a Go binary (distroless base)           |
| `apko_image`          | macro    | `apko_image.bzl`          | Multi-arch apko-based OCI image, optionally with extra tar layers |
| `py3_image`           | macro    | `py3_image.bzl`           | Multi-arch Python 3 image using `py_image_layer`                  |
| `apko_nginx_frontend` | macro    | `apko_nginx_frontend.bzl` | Vite/React build packaged into a nginx apko image                 |
| `OciImageInfo`        | provider | `providers.bzl`           | Provider carrying `repository` + `image_tags` files               |
| `oci_image_info`      | rule     | `providers.bzl`           | Low-level rule that writes an `OciImageInfo` from raw strings     |

## How it fits the pipeline

```
projects/<svc>/BUILD
  go_image / apko_image / py3_image
       |
       +-- {name}.push  -->  ghcr.io/jomcgi/homelab/<svc>  (CI only)
       |
       +-- {name}.info  -->  OciImageInfo { repository, image_tags }
                                    |
                     projects/<svc>/chart/BUILD
                       helm_chart(images = {"key": "{name}.info"})
                             |
                     helm_images_values  (yq deep-merge into values.yaml)
                             |
                     helm_package  -->  OCI chart on ghcr.io
```

Each image macro creates a `{name}.info` target at build time. `helm_chart`
passes a map of `{dotted.helm.path: label}` pointing at `.info` targets; the
Helm packaging machinery resolves the repository and primary tag and deep-merges
them into the chart's `values.yaml`. You never hand-write `@sha256:` digests in
values files: the pipeline manages pinning automatically.

Image pushes (`{name}.push`) run only in CI, and only on main, one target at a
time from `bazel/images/push/push-changed.sh`, and only for images whose content
digest is not already published. Do not run push targets locally.

## `OciImageInfo` provider

Defined in `providers.bzl`. Produced by every image macro as `{name}.info` and
by the low-level `oci_image_info` rule.

| Field        | Type | Description                                                                 |
| ------------ | ---- | --------------------------------------------------------------------------- |
| `repository` | File | Plain-text file containing the OCI repository URL (no trailing newline)     |
| `image_tags` | File | Text file with one tag per line; first line is the primary tag used by Helm |

The tag format is `YYYY.MM.DD.HH.MM.SS-shortsha` (the `STABLE_IMAGE_TAG` stamp
variable), and it is the only tag written. CI builds used to append a second
line carrying the branch name for ArgoCD Image Updater tag filtering; there is
no Image Updater in this cluster, and charts deploy by `repository@digest`, so
the tag is a registry-facing alias rather than the deployed reference.

## `go_image`

Wraps a `go_binary` in a distroless OCI image. Builds both `amd64` and `arm64`
slices and combines them into an `oci_image_index`.

```python
# projects/example/api/BUILD
load("//bazel/tools/oci:go_image.bzl", "go_image")

go_image(
    name = "image",
    binary = ":api",
    repository = "ghcr.io/jomcgi/homelab/projects/example/api",
)
```

| Arg              | Default                                 | Description                                                           |
| ---------------- | --------------------------------------- | --------------------------------------------------------------------- |
| `name`           | required                                | Target name prefix                                                    |
| `binary`         | required                                | `go_binary` target to package                                         |
| `base`           | `@distroless_base`                      | Base image                                                            |
| `repository`     | `ghcr.io/jomcgi/homelab/{package_name}` | OCI registry repository URL                                           |
| `extra_tars`     | `[]`                                    | Platform-independent tar layers added after the platform transition   |
| `visibility`     | `["//bazel/images:__pkg__"]`            | Visibility of the `.push` target                                      |
| `multi_platform` | `True`                                  | Build both amd64 and arm64; set `False` for legacy single-arch builds |

Created targets:

| Target         | Description                                  |
| -------------- | -------------------------------------------- |
| `{name}`       | `oci_image_index` (multi-arch manifest list) |
| `{name}_amd64` | AMD64 image slice                            |
| `{name}_arm64` | ARM64 image slice                            |
| `{name}.load`  | Load host-arch image into local Docker       |
| `{name}.push`  | Push index to the OCI registry (CI)          |
| `{name}.info`  | `OciImageInfo` provider for `helm_chart`     |

The binary runs as uid `65532` (distroless nonroot convention).

## `apko_image`

Builds a multi-platform image from an apko config and lock file, optionally
layering in additional tars. The apko config must list both `x86_64` and
`aarch64` in its `archs` field.

```python
# projects/monolith/obsidian-image/BUILD
load("//bazel/tools/oci:apko_image.bzl", "apko_image")

apko_image(
    name = "image",
    config = "apko.yaml",
    contents = "@monolith_obsidian_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/projects/monolith/obsidian",
)
```

With extra layers:

```python
apko_image(
    name = "image",
    config = "apko.yaml",
    contents = "@my_lock//:contents",
    tars = [":config_tar"],          # same tar used on both arches
    multiarch_tars = [":binary_tar"], # uses :binary_tar_amd64 / :binary_tar_arm64
)
```

| Arg                  | Default                                 | Description                                                      |
| -------------------- | --------------------------------------- | ---------------------------------------------------------------- |
| `name`               | required                                | Target name prefix                                               |
| `config`             | required                                | apko YAML config file                                            |
| `contents`           | required                                | apko lock file label (`@<lock>//:contents`)                      |
| `repository`         | `ghcr.io/jomcgi/homelab/{package_name}` | OCI registry repository URL                                      |
| `tars`               | `None`                                  | Tar layers added to both platforms                               |
| `multiarch_tars`     | `None`                                  | Tar base names; macro appends `_amd64` / `_arm64` for each entry |
| `multiplatform_tars` | `None`                                  | Deprecated. Use `tars` and `multiarch_tars` instead              |
| `visibility`         | `["//bazel/images:__pkg__"]`            | Visibility of the `.push` target                                 |

Created targets:

| Target             | Description                                                       |
| ------------------ | ----------------------------------------------------------------- |
| `{name}`           | `oci_image_index` (or native apko image when no tars provided)    |
| `{name}.push`      | Push to OCI registry (CI)                                         |
| `{name}.run`       | Load and run locally via podman or docker                         |
| `{name}_lock_test` | `sh_test` that verifies the lock file matches the config checksum |
| `{name}.info`      | `OciImageInfo` provider for `helm_chart`                          |

## `py3_image`

Builds a multi-platform Python 3 image using `py_image_layer` from
`@aspect_rules_py`. Handles runfiles layout, `PYTHONPATH`, and a `/bin/bash`
symlink layer automatically.

```python
# projects/my_app/BUILD
load("//bazel/tools/oci:py3_image.bzl", "py3_image")

py3_image(
    name = "image",
    binary = "//projects/my_app/backend:main",
    base = "@gdal_python_base",
    env = {
        "GDAL_DATA": "/usr/share/gdal",
        "DATA_DIR": "/data",
    },
    repository = "ghcr.io/jomcgi/homelab/projects/my_app",
)
```

| Arg              | Default                                 | Description                                                              |
| ---------------- | --------------------------------------- | ------------------------------------------------------------------------ |
| `name`           | required                                | Target name prefix                                                       |
| `binary`         | required                                | `py_venv_binary` or `py_binary` target                                   |
| `main`           | `{binary_name}.py` (auto)               | Main `.py` source; set explicitly for non-standard naming                |
| `root`           | `"/"`                                   | Container path prefix for the binary and runfiles                        |
| `layer_groups`   | `{}`                                    | `py_image_layer` layer group config                                      |
| `env`            | `{}`                                    | Additional environment variables (merged with auto-set `PYTHONPATH` etc) |
| `workdir`        | workspace root (auto)                   | Container working directory                                              |
| `base`           | `@python_base`                          | Base image                                                               |
| `tars`           | `[]`                                    | Additional tar layers (both platforms)                                   |
| `multiarch_tars` | `[]`                                    | Tar base names; macro appends `_amd64` / `_arm64` for each entry         |
| `bash_symlink`   | `True`                                  | Add `/bin/bash -> /usr/bin/bash` symlink layer; set `False` for Wolfi    |
| `repository`     | `ghcr.io/jomcgi/homelab/{package_name}` | OCI registry repository URL                                              |
| `visibility`     | `["//bazel/images:__pkg__"]`            | Visibility of the `.push` target                                         |
| `multi_platform` | `True`                                  | Build both amd64 and arm64                                               |

Created targets:

| Target               | Description                                       |
| -------------------- | ------------------------------------------------- |
| `{name}`             | `oci_image_index` (multi-arch manifest list)      |
| `{name}.load`        | Load host-arch image into local Docker            |
| `{name}.push`        | Push index to the OCI registry (CI)               |
| `{name}_config_test` | `sh_test` verifying Python runtime env is correct |
| `{name}.info`        | `OciImageInfo` provider for `helm_chart`          |

## `apko_nginx_frontend`

Convenience wrapper around `apko_image` for Vite or React frontends. Stages a
`js_run_binary` tree artifact into the nginx document root, working around
`pkg_tar strip_prefix` limitations with Bazel tree artifacts.

```python
# projects/example/frontend/BUILD
load("//bazel/tools/oci:apko_nginx_frontend.bzl", "apko_nginx_frontend")

apko_nginx_frontend(
    name = "image",
    dist = ":build",     # js_run_binary with out_dirs = ["dist"]
    config = "apko.yaml",
    contents = "@example_frontend_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/projects/example/frontend",
)
```

| Arg             | Default                      | Description                                              |
| --------------- | ---------------------------- | -------------------------------------------------------- |
| `name`          | required                     | Target name prefix                                       |
| `dist`          | required                     | `js_run_binary` tree artifact label (the `dist/` output) |
| `config`        | required                     | apko YAML config (must include nginx package)            |
| `contents`      | required                     | apko lock file label                                     |
| `repository`    | standard GHCR path           | OCI registry repository URL                              |
| `document_root` | `/usr/share/nginx/html`      | Nginx document root inside the container                 |
| `visibility`    | `["//bazel/images:__pkg__"]` | Visibility of the `.push` target                         |

Creates the same targets as `apko_image` (delegates to it internally).

## Connecting images to a Helm chart

In the chart's `BUILD`, pass a map of dotted Helm values paths to `.info`
labels:

```python
# projects/monolith/chart/BUILD
load("//bazel/helm:defs.bzl", "helm_chart")

helm_chart(
    name = "chart",
    images = {
        "backend.image":               "//projects/monolith:image.info",
        "frontend.image":              "//projects/monolith/frontend:image.info",
        "knowledge.headlessSync.image": "//projects/monolith/obsidian-image:image.info",
    },
    publish = True,
)
```

`helm_chart` passes these labels to `helm_images_values`, which reads the
`repository` and first-line `image_tags` from each `OciImageInfo` and
deep-merges `{dotted.path}.repository` and `{dotted.path}.tag` into the chart's
`values.yaml` before packaging. The pinned values are baked into the `.tgz`;
the deploy-time `values.yaml` never needs manual image entries.

## Conventions

- All image macros default to dual-arch builds (x86_64 + aarch64). Set
  `multi_platform = False` only for genuinely legacy targets.
- All containers run as uid `65532` (nonroot). The distroless base enforces
  this; apko images should declare a non-root user in their config.
- The `{name}.push` target is visible to `//bazel/images:__pkg__` by default,
  which is where the auto-generated `push_all` multirun target lives.
- Do not load `apko_push` or `oci_run` directly; they are implementation
  details used by the macros above.
- Repository URLs default to `ghcr.io/jomcgi/homelab/{package_name}`. Always
  override with an explicit `repository` arg when the Bazel package path does
  not match the desired registry path.
- Lock file drift (`{name}_lock_test` for apko images) is caught by `bazel test
//...` in CI. Regenerate with `bazel run @rules_apko//apko -- lock ...`.
