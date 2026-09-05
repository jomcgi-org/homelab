"""Single source of truth for the architectures the OCaml toolchain targets.

Per bazel/ARCHITECTURE.md (OCaml section): adding
an architecture is one entry in OCAML_ARCHES (plus a BuildBuddy executor pool
that satisfies its constraints). linux x86_64 + aarch64 are live today and the
design is open-ended, so a future production arch is the same one-line edit.

The compiler is built *on the executor it will run on* (see compiler.bzl), so a
per-arch toolchain needs no cross-compilation: each arch builds its own sysroot
on its own pool, selected by exec_compatible_with / target_compatible_with. This
generalizes the existing single-arch design without changing it.

`enabled` gates everything live about an arch: its platform's BuildBuddy routing
property, its sysroot build target (compiler.bzl), and its toolchain pair
(toolchain.bzl). Flipping an arch on requires its executor pool to be verified
first -- the gate ADR 006 sets is the executor-arch probe in
//bazel/ocaml/platforms (see executor_arch_probe_arm64, which asserts the
`Arch: arm64` routing answers with an aarch64 executor on every CI run).
Platform declarations are emitted even for disabled arches (without the routing
property): a `platform` target is inert until a build selects it, so declaring
it early gives probes and future toolchains stable labels to reference.
"""

# Fields:
#   name    : the `platform` target name under //bazel/ocaml/platforms
#   os, cpu : @platforms constraint labels
#   bb_arch : the BuildBuddy `Arch` execution property for pool routing. Only
#             emitted on the platform once `enabled` is flipped, which in turn
#             requires the pool verified via the executor probe (ADR 006), so
#             an unverified property can never reach an executor.
#   enabled : whether this arch has a live platform routing property, sysroot
#             build, and registered toolchain. Registration lives in
#             MODULE.bazel (register_toolchains cannot be generated from a
#             list); keep that list in sync when flipping an arch.
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
        # Verified 2026-06-12: //bazel/ocaml/platforms:executor_arch_probe_arm64
        # confirmed `Arch: arm64` routes to BuildBuddy's cloud arm64 pool
        # (launched 2026-01-15, ADR 008).
        enabled = True,
    ),
]

def declare_ocaml_platforms():
    """Declare one `platform` target per entry in OCAML_ARCHES.

    Platforms carry constraint_values, plus -- for enabled arches only -- the
    BuildBuddy `Arch` exec property that routes actions to that arch's executor
    pool. Disabled arches stay property-free so an unverified routing key can
    never reach an executor (ADR 006); their platforms are inert labels until a
    probe verifies the pool and `enabled` is flipped.
    """
    for arch in OCAML_ARCHES:
        native.platform(
            name = arch.name,
            constraint_values = [arch.os, arch.cpu],
            exec_properties = {"Arch": arch.bb_arch} if arch.enabled else {},
            visibility = ["//visibility:public"],
        )
