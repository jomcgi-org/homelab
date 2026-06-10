"""Single source of truth for the architectures the OCaml toolchain targets.

Per docs/decisions/tooling/006-extensible-multiarch-ocaml-toolchains.md: adding
an architecture is one entry in OCAML_ARCHES (plus a BuildBuddy executor pool
that satisfies its constraints). linux x86_64 + aarch64 are declared today and
the design is open-ended, so a future production arch is the same one-line edit.

The compiler is built *on the executor it will run on* (see compiler.bzl), so a
per-arch toolchain needs no cross-compilation: each arch builds its own sysroot
on its own pool, selected by exec_compatible_with / target_compatible_with. This
generalizes the existing single-arch design without changing it.

`enabled` gates per-arch *toolchain registration* (the Phase-7 work in ADR 006,
not yet wired). Platform declarations are always emitted: a `platform` target is
inert until a build selects it via --platforms or a toolchain's *_compatible_with,
so declaring both arches now is free and gives the executor probe and future
toolchains stable labels to reference.
"""

# Fields:
#   name    : the `platform` target name under //bazel/ocaml/platforms
#   os, cpu : @platforms constraint labels
#   bb_arch : the BuildBuddy `Arch` execution property for pool routing. Carried
#             as an exec_property so it travels with the platform, but inert until
#             a per-arch toolchain selects the platform; the exact key is confirmed
#             against the executor via //bazel/ocaml/platforms:executor_arch_probe
#             before Phase 7 wires toolchain registration (ADR 006).
#   enabled : whether Phase 7 should register a toolchain for this arch yet.
OCAML_ARCHES = [
    struct(
        name = "linux_x86_64",
        os = "@platforms//os:linux",
        cpu = "@platforms//cpu:x86_64",
        bb_arch = "amd64",
        enabled = True,
    ),
    struct(
        name = "linux_aarch64",
        os = "@platforms//os:linux",
        cpu = "@platforms//cpu:aarch64",
        bb_arch = "arm64",
        enabled = False,  # flip on once the arm64 executor pool is verified
    ),
]

def declare_ocaml_platforms():
    """Declare one inert `platform` target per entry in OCAML_ARCHES."""
    for arch in OCAML_ARCHES:
        native.platform(
            name = arch.name,
            constraint_values = [arch.os, arch.cpu],
            exec_properties = {"Arch": arch.bb_arch},
            visibility = ["//visibility:public"],
        )
