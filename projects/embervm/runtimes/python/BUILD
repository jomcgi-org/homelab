# gazelle:ignore
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# The bootstrap shim as an importable library so the unit test can `import shim`
# without depending on the tar-packaging target above.
py_library(
    name = "shim_lib",
    srcs = ["shim.py"],
    imports = ["."],
)

# EmberVM python runtime base image (R1 zip lane, ADR embervm/002). A Wolfi apko
# base (python 3.12 + the baked dependency subset, see apko.yaml) whose PID 1 is
# the bootstrap shim (shim.py). On boot the shim unpacks the archive attached as
# a read-only block device (Task 6), imports the adopter's handler, and serves
# the frozen HTTP-over-vsock guest contract on port 1027. Zip-lane adopters
# bring only a zip; no toolchain.

# The bootstrap shim, layered at /usr/local/bin/ember-runtime-shim (the apko
# entrypoint runs `python3` against it). Platform-independent python source, so
# a single tar serves both arches (no per-arch transition needed).
tar(
    name = "shim_tar",
    srcs = ["shim.py"],
    mtree = [
        "./usr/local/bin/ember-runtime-shim type=file content=$(execpath shim.py) mode=0755 uid=0 gid=0",
    ],
)

apko_image(
    name = "image",
    config = "apko.yaml",
    contents = "@embervm_runtime_python_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/projects/embervm/runtimes/python",
    tars = [
        ":shim_tar",
    ],
)

# The shim is plain stdlib python, unit-tested without a microVM or vsock (a
# loopback TCP server stands in for the vsock listener). Hand-registered:
# gazelle does not generate py_test targets in this repo.
py_test(
    name = "shim_test",
    srcs = ["shim_test.py"],
    imports = ["."],
    deps = [
        ":shim_lib",
        "@pip//pytest",
    ],
)
