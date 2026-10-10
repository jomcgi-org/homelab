---
name: apko
invoke: explicit
summary: apko.yaml, locks, and apko_image patterns (amd64 images)
---

> **Runbook (explicit-only).** Open only when Joe asks for this procedure, or a
> claude.ai routine prompt names this file. Do not auto-load from skill matching.

# Container Images with apko

All container images in this repo are built with apko + rules_apko via the custom `apko_image` macro. Read `bazel/tools/oci/apko_image.bzl` first to understand the macro before wiring a new image.

Builds and pushes happen in CI / via `ci test` remote execution (see
`docs/agents/ci-triage.md` and the Commands section of `AGENTS.md`).
Locally you edit `apko.yaml` and BUILD files, regenerate locks when they change, and push.

## Architecture

Images are amd64. `apko_image` defaults `arm64 = True` and 24 of its 25 callers
pass `arm64 = False`: the hub nodes and the RBE executor are amd64, no chart
pins an arch, and the aarch64 half of an index had no consumer. The one
exception is `projects/embervm/noded/image`, which still builds an amd64 plus
arm64 index through `multiarch_tars`. Leave `aarch64` in `archs`: the lock
checksums the whole config, and apko ignores a declared arch nobody builds, so
`arm64 = False` needs no `archs` edit and no lock regeneration.

## apko.yaml Structure

```yaml
contents:
  repositories:
    - https://packages.wolfi.dev/os
  keyring:
    - https://packages.wolfi.dev/os/wolfi-signing.rsa.pub
  packages:
    - ca-certificates-bundle # Always include for HTTPS
    - tzdata # If timezone handling needed

archs:
  - x86_64 # The nodes and the RBE executor
  - aarch64 # Declared in every config; built only when arm64 = True

entrypoint:
  command: /opt/app # Use for Go binaries

work-dir: /app

# Non-root user (uid 65532 standard, 1000 if writable home needed)
accounts:
  groups:
    - groupname: appuser
      gid: 65532
  users:
    - username: appuser
      uid: 65532
      gid: 65532
  run-as: 65532

paths:
  - path: /app
    type: directory
    uid: 65532
    gid: 65532
    permissions: 0o755

environment:
  HOME: /home/appuser
```

## Lock Files

After changing any `apko.yaml`, regenerate locks (pre-commit does this when
`apko.yaml` is staged, or run `bazel/tools/format/update-apko-locks.sh`). Commit
only the locks that actually changed.

## BUILD.bazel Patterns

This repo uses a custom `apko_image` macro from `//bazel/tools/oci:apko_image.bzl`:

```starlark
load("@rules_pkg//pkg:tar.bzl", "pkg_tar")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")

pkg_tar(
    name = "static_tar",
    srcs = ["//projects/myservice:static_files"],
    mode = "0644",
    owner = "65532.65532",
    package_dir = "/app/static",
)

apko_image(
    name = "image",
    arm64 = False,
    config = "apko.yaml",
    contents = "@myservice_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/projects/myservice",
    tars = [":static_tar"],
)
```

### Arch-specific Binary Pattern (Go)

```starlark
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")

platform_transition_filegroup(
    name = "binary_amd64",
    srcs = ["//projects/myservice/cmd"],
    target_platform = "@rules_go//go/toolchain:linux_amd64",
)

tar(
    name = "binary_tar_amd64",
    srcs = [":binary_amd64"],
    mtree = ["./opt/app type=file content=$(execpath :binary_amd64)"],
)

apko_image(
    name = "image",
    arm64 = False,
    config = "apko.yaml",
    contents = "@myservice_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/projects/myservice",
    tars = [":binary_tar_amd64"],  # per-arch tar, named explicitly
)
```

Re-adding arm64 is `arm64 = True`, a `binary_tar_arm64` built with the
`linux_arm64` target platform, and `multiarch_tars = [":binary_tar"]` in place
of `tars` (the macro appends `_amd64` and `_arm64`). `arm64 = False` with
`multiarch_tars` passes PR CI and fails to push a layer blob on main.

### MODULE.bazel Registration

New locks must be registered:

```starlark
apko = use_extension("@rules_apko//apko:extensions.bzl", "apko")
apko.translate_lock(
    name = "myservice_lock",
    lock = "//projects/myservice/image:apko.lock.json",
)
use_repo(apko, "myservice_lock")
```

## Common Package Categories

| Use Case        | Packages                                   |
| --------------- | ------------------------------------------ |
| HTTPS/TLS       | `ca-certificates-bundle`                   |
| Timezone        | `tzdata`                                   |
| Git operations  | `git`, `openssh-client`                    |
| Node.js runtime | `nodejs-22`, `npm`                         |
| Bun runtime     | `bun`                                      |
| Go binary       | (no packages needed, just entrypoint)      |
| Python runtime  | `python-3.12`                              |
| Native builds   | `build-base`, `python-3.12` (for node-gyp) |
| Debugging       | `busybox`, `curl` (remove for production)  |

## Common Mistakes to Avoid

1. **Not updating lock files**: regenerate locks after changing any apko.yaml
2. **`arm64 = False` with `multiarch_tars`**: fails only at push time on main; pair `arm64 = False` with per-arch `tars`
3. **Missing CA certificates**: HTTPS calls fail without `ca-certificates-bundle`
4. **Forgetting MODULE.bazel**: new locks must be registered with `apko.translate_lock`

## Debugging Published Images

`crane` is vendored and allowed locally:

```bash
crane manifest ghcr.io/jomcgi/homelab/projects/myservice:main | jq
crane export ghcr.io/jomcgi/homelab/projects/myservice:main - | tar -tvf - | head -50
jq '.contents.packages[] | {name, version}' projects/myservice/image/apko.lock.json
```

