---
name: apko
invoke: explicit
summary: apko.yaml, locks, and apko_image patterns for dual-arch images
---

> **Runbook (explicit-only).** Open only when Joe asks for this procedure, or a
> claude.ai routine prompt names this file. Do not auto-load from skill matching.

# Container Images with apko

All container images in this repo are built with apko + rules_apko via the custom `apko_image` macro. Read `bazel/tools/oci/apko_image.bzl` first to understand the macro before wiring a new image.

Builds and pushes happen in CI / via `ci test` remote execution (see [bazel.md](bazel.md)).
Locally you edit `apko.yaml` and BUILD files, regenerate locks when they change, and push.

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
  - x86_64 # Required: Intel/AMD
  - aarch64 # Required: ARM (M-series Mac, ARM nodes)

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
    config = "apko.yaml",
    contents = "@myservice_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/projects/myservice",
    tars = [":static_tar"],
    # multiarch_tars = [":binary_tar"],  # For arch-specific binaries
)
```

### Multi-arch Binary Pattern (Go)

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

# Repeat for arm64 with linux_arm64 target platform

apko_image(
    name = "image",
    config = "apko.yaml",
    contents = "@myservice_lock//:contents",
    multiarch_tars = [":binary_tar"],  # Macro uses _amd64/_arm64 suffixes
    repository = "ghcr.io/jomcgi/homelab/projects/myservice",
)
```

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
2. **Missing architectures**: always include both `x86_64` and `aarch64`
3. **Missing CA certificates**: HTTPS calls fail without `ca-certificates-bundle`
4. **Forgetting MODULE.bazel**: new locks must be registered with `apko.translate_lock`

## Debugging Published Images

`crane` is vendored and allowed locally:

```bash
crane manifest ghcr.io/jomcgi/homelab/projects/myservice:main | jq
crane export ghcr.io/jomcgi/homelab/projects/myservice:main - | tar -tvf - | head -50
jq '.contents.packages[] | {name, version}' projects/myservice/image/apko.lock.json
```

