load("@bazel_skylib//rules:build_test.bzl", "build_test")
load("//bazel/ocaml/toolchain:arches.bzl", "declare_ocaml_platforms")

# One `platform` per entry in OCAML_ARCHES (linux x86_64 + aarch64 today). Inert
# until selected via --platforms or a toolchain's *_compatible_with; see
# bazel/ARCHITECTURE.md (OCaml section).
declare_ocaml_platforms()

# Executor-arch probe: records `uname -m` from whatever executor Bazel routes
# build actions to, then a build_test runs it so the value is visible in the
# BuildBuddy invocation. This de-risks the multi-arch work -- it confirms what
# the default pool reports before any arm64 toolchain is registered. It carries
# NO arch exec_property, so it never depends on a pool that may not exist yet.
genrule(
    name = "executor_arch_probe",
    outs = ["executor_arch.txt"],
    cmd = "uname -m > $@ && echo \"ocaml executor arch: $$(cat $@)\" >&2",
)

build_test(
    name = "executor_arch_probe_test",
    targets = [":executor_arch_probe"],
)

# Arm64-pool probe: the same `uname -m` probe, routed to BuildBuddy's cloud
# linux/arm64 executor pool (launched 2026-01-15, ADR 008) via the `Arch`
# execution property -- the bb_arch key held in arches.bzl. Unlike the neutral
# probe above, this one asserts the answer: a mis-route to the default amd64
# pool fails the build instead of silently building an x86_64 sysroot later.
# Green here is the ADR 006 gate for flipping `enabled` on linux_aarch64.
genrule(
    name = "executor_arch_probe_arm64",
    outs = ["executor_arch_arm64.txt"],
    cmd = 'arch="$$(uname -m)"; echo "arm64-pool probe ran on: $$arch" >&2; ' +
          'if [ "$$arch" != "aarch64" ]; then echo "expected aarch64: Arch=arm64 did not route to the arm64 pool" >&2; exit 1; fi; ' +
          'echo "$$arch" > $@',
    exec_properties = {"Arch": "arm64"},
)

build_test(
    name = "executor_arch_probe_arm64_test",
    targets = [":executor_arch_probe_arm64"],
)
