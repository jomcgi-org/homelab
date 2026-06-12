# Override BUILD for yaml (installed by extension.bzl).
#
# Why an override: ocaml-yaml is a two-stage ctypes stubgen package -- the
# README's "yaml scoping" dispatch in bazel/ocaml/semgrep_src/README.md,
# measured against this tarball's dune files. The pieces:
#
#   1. vendor/: the vendored libyaml C sources as a cc_library (the pcre2-c
#      pattern), compiled -DHAVE_CONFIG_H against the checked-in config.h.
#      Upstream's per-object rules + ocamlmklib archive collapse into one
#      cc_library; dumper.c stays out exactly as upstream's dune leaves it.
#   2. config/discover.ml's two probe outputs are pinned (the parmap
#      pattern): `cflags` is provably ocamlopt_cflags on the linux sysroot
#      ("-O2 -fno-strict-aliasing -fwrapv", inlined into yaml_c's copts;
#      the ppc64/msvc branches are dead here) and `ctypes-cflags` is just
#      -I<installed ctypes headers>, which here is the staged
#      @ocaml_ctypes//:c_headers dir.
#   3. yaml.bindings.types / yaml.bindings: plain libraries over
#      ctypes.stubs + ctypes (no codegen).
#   4. stage one (compile-AND-RUN): run ffi_types_stubgen.exe to emit C,
#      compile that C with the executor's gcc against the staged ctypes
#      headers + vendor/yaml.h + the sysroot's lib/ocaml (dune's
#      %{ocaml_where}), run the result to emit g.ml for yaml.types. One
#      genrule, two process generations.
#   5. stage two: ffi_stubgen.exe -ml -> g.ml and -c -> yaml_stubs.c for
#      yaml.ffi (pure prints, no C compile inside the genrule).
#   6. yaml (lib/): plain library over yaml.ffi. yaml.unix and yaml-sexp
#      stay unbuilt (src/core names only `yaml`).
#
# Both stubgen binaries and the stage-one genrule run on the default pool
# (the yojson ocamllex precedent: the genrule stages the unconstrained
# sysroot). Their outputs are arch-independent for this ruleset's two
# platforms: linux x86_64 and aarch64 are both little-endian LP64, so
# libyaml's struct layouts and enum values agree. The cc_library keeps the
# whole package in CI's cc bucket (no-arm64) regardless.
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_binary", "ocaml_library")
load("@rules_cc//cc:defs.bzl", "cc_library")

_SYSROOT = "@homelab//bazel/ocaml/toolchain:ocaml_compiler"

_CTYPES_HEADERS = "@ocaml_ctypes//:c_headers"

# The root dune's (env ...) flags, applied by dune to every library here.
_ENV_WFLAGS = [
    "-w",
    "-9-27-32-34",
]

cc_library(
    name = "yaml_c",
    srcs = [
        "vendor/api.c",
        "vendor/config.h",
        "vendor/emitter.c",
        "vendor/loader.c",
        "vendor/parser.c",
        "vendor/reader.c",
        "vendor/scanner.c",
        "vendor/writer.c",
        "vendor/yaml_private.h",
    ],
    hdrs = ["vendor/yaml.h"],
    copts = [
        "-DHAVE_CONFIG_H",
        "-O2",
        "-fno-strict-aliasing",
        "-fwrapv",
        "-w",
    ],
    includes = ["vendor"],
)

ocaml_library(
    name = "yaml_bindings_types",
    srcs = [
        "types/bindings/yaml_bindings_types.ml",
        "types/bindings/yaml_bindings_types.mli",
    ],
    ocamlopt_flags = _ENV_WFLAGS,
    wrapped = True,
    deps = [
        "@ocaml_ctypes//:ctypes",
        "@ocaml_ctypes//:ctypes_stubs",
    ],
)

ocaml_binary(
    name = "ffi_types_stubgen",
    srcs = ["types/stubgen/ffi_types_stubgen.ml"],
    deps = [":yaml_bindings_types"],
)

# Stage one: the compile-AND-RUN genrule. ffi_types_stubgen.exe prints a C
# program; building THAT (against the ctypes headers, vendor/yaml.h, and the
# sysroot's caml/ headers) and running it prints g.ml for yaml.types.
genrule(
    name = "yaml_types_g_ml",
    srcs = [
        "vendor/yaml.h",
        _SYSROOT,
        _CTYPES_HEADERS,
    ],
    outs = ["types/lib/g.ml"],
    cmd = """
set -e
T=$$(mktemp -d) && tar -xf $(location %s) -C $$T
G=$$(realpath $(location :ffi_types_stubgen))
H=$$(mktemp -d)
cp $(location vendor/yaml.h) $$H/
for h in $(locations %s); do cp $$h $$H/; done
W=$$(mktemp -d)
"$$G" > $$W/ffi_ml_types_stubgen.c
gcc $$W/ffi_ml_types_stubgen.c -I $$H -I $$T/lib/ocaml -o $$W/gen
$$W/gen > $@
""" % (_SYSROOT, _CTYPES_HEADERS),
    tools = [":ffi_types_stubgen"],
)

ocaml_library(
    name = "yaml_types",
    srcs = [
        "types/lib/m.ml",
        ":yaml_types_g_ml",
    ],
    ocamlopt_flags = _ENV_WFLAGS,
    wrapped = True,
    deps = [
        ":yaml_bindings_types",
        "@ocaml_ctypes//:ctypes",
        "@ocaml_ctypes//:ctypes_stubs",
    ],
)

ocaml_library(
    name = "yaml_bindings",
    srcs = ["ffi/bindings/yaml_bindings.ml"],
    ocamlopt_flags = _ENV_WFLAGS,
    wrapped = True,
    deps = [
        ":yaml_types",
        "@ocaml_ctypes//:ctypes",
        "@ocaml_ctypes//:ctypes_stubs",
    ],
)

ocaml_binary(
    name = "ffi_stubgen",
    srcs = ["ffi/stubgen/ffi_stubgen.ml"],
    deps = [
        ":yaml_bindings",
        ":yaml_types",
    ],
)

# Stage two: plain prints (Cstubs.write_ml / write_c), no C compile here.
genrule(
    name = "yaml_ffi_g_ml",
    outs = ["ffi/lib/g.ml"],
    cmd = "$(location :ffi_stubgen) -ml > $@",
    tools = [":ffi_stubgen"],
)

genrule(
    name = "yaml_stubs_c",
    outs = ["yaml_stubs.c"],
    cmd = "$(location :ffi_stubgen) -c > $@",
    tools = [":ffi_stubgen"],
)

# The generated yaml_stubs.c quote-includes "ctypes_cstubs_internals.h" (the
# ctypes header set stages by basename next to it) and angle-includes
# <yaml.h>, which resolves through yaml_c's exported vendor/ include dir.
ocaml_library(
    name = "yaml_ffi",
    srcs = [
        "ffi/lib/m.ml",
        ":yaml_ffi_g_ml",
    ],
    c_headers = [_CTYPES_HEADERS],
    c_srcs = [":yaml_stubs_c"],
    cc_deps = [":yaml_c"],
    ocamlopt_flags = _ENV_WFLAGS,
    wrapped = True,
    deps = [
        ":yaml_bindings",
        ":yaml_types",
        "@ocaml_ctypes//:ctypes",
        "@ocaml_ctypes//:ctypes_stubs",
    ],
)

ocaml_library(
    name = "yaml",
    srcs = glob([
        "lib/*.ml",
        "lib/*.mli",
    ]),
    ocamlopt_flags = _ENV_WFLAGS,
    visibility = ["//visibility:public"],
    wrapped = True,
    deps = [":yaml_ffi"],
)
