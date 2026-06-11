# Override BUILD for yojson (installed by extension.bzl).
#
# Why an override: yojson builds its t/basic/safe/raw variants with a vendored
# tool, mucppo (a tiny #include/#if-OCAML_VERSION cppo subset), then exposes
# only a filtered module set; the other source files are mucppo *inputs*
# inlined into the variants, not library modules. We build mucppo, run it per
# variant (over-staging its inputs is harmless -- it reads only what each
# .cppo file #includes), generate read.ml via ocamllex, and compile the
# filtered module set as the library.
load("@homelab//bazel/ocaml:defs.bzl", "ocaml_binary", "ocaml_library")

_SYSROOT = "@homelab//bazel/ocaml/toolchain:ocaml_compiler"

# The mucppo inputs (raw module sources) shared by the variant rules.
filegroup(
    name = "mucppo_inputs",
    srcs = [
        "lib/monomorphic.ml",
        "lib/monomorphic.mli",
        "lib/prettyprint.ml",
        "lib/read.mli",
        "lib/safe_to_basic.ml",
        "lib/safe_to_basic.mli",
        "lib/type.ml",
        "lib/util.ml",
        "lib/util.mli",
        "lib/write.ml",
        "lib/write.mli",
        "lib/write2.ml",
        "lib/write2.mli",
        ":read_ml",
    ],
)

ocaml_binary(
    name = "mucppo",
    srcs = ["lib/mucppo/mucppo.ml"],
)

# read.ml is an mucppo #include input (not a library module); generate it from
# read.mll with the sysroot's ocamllex.
genrule(
    name = "read_ml",
    srcs = [
        "lib/read.mll",
        _SYSROOT,
    ],
    outs = ["read.ml"],
    cmd = "T=$$(mktemp -d) && tar -xf $(location %s) -C $$T && " % _SYSROOT +
          "OCAMLLIB=$$T/lib/ocaml $$T/bin/ocamllex -q -o $@ $(location lib/read.mll)",
)

# One mucppo invocation per generated module + extension. `safe` additionally
# inlines the generated `basic`, so its inputs include basic.{ml,mli}.
[
    genrule(
        name = "gen_%s_%s" % (variant, ext),
        srcs = [
            "lib/%s.cppo.%s" % (variant, ext),
            ":mucppo_inputs",
        ] + ([":gen_basic_%s" % ext] if variant == "safe" else []),
        outs = ["%s.%s" % (variant, ext)],
        # mucppo resolves #include relative to cwd, so stage every input by
        # basename into a scratch dir (over-staging is harmless) and run there.
        cmd = "W=$$(mktemp -d) && MU=$$(realpath $(location :mucppo)) && " +
              "for f in $(SRCS); do cp $$f $$W/; done && " +
              "(cd $$W && $$MU %s.cppo.%s -o %s.%s) && " % (variant, ext, variant, ext) +
              "cp $$W/%s.%s $@" % (variant, ext),
        tools = [":mucppo"],
    )
    for variant in [
        "t",
        "basic",
        "raw",
        "safe",
    ]
    for ext in [
        "ml",
        "mli",
    ]
]

ocaml_library(
    name = "yojson",
    srcs = [
        "lib/yojson.ml",
        "lib/yojson.mli",
        "lib/common.ml",
        "lib/common.mli",
        "lib/codec.ml",
        "lib/codec.mli",
        "lib/lexer_utils.mll",
        ":gen_t_ml",
        ":gen_t_mli",
        ":gen_basic_ml",
        ":gen_basic_mli",
        ":gen_safe_ml",
        ":gen_safe_mli",
        ":gen_raw_ml",
        ":gen_raw_mli",
    ],
    ocamlopt_flags = [
        "-w",
        "-27-32",
    ],
    wrapped = True,
    visibility = ["//visibility:public"],
)
