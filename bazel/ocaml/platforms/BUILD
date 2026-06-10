load("@bazel_skylib//rules:build_test.bzl", "build_test")
load("//bazel/ocaml/toolchain:arches.bzl", "declare_ocaml_platforms")

# One `platform` per entry in OCAML_ARCHES (linux x86_64 + aarch64 today). Inert
# until selected via --platforms or a toolchain's *_compatible_with; see
# docs/decisions/tooling/006-extensible-multiarch-ocaml-toolchains.md.
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
