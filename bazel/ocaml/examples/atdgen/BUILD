load("//bazel/ocaml:defs.bzl", "ocaml_library", "ocaml_test")

# The atd ladder end to end: run the from-source atdgen binary over an ATD spec
# to emit OCaml types (-t) and JSON serializers (-j), then compile and exercise
# them. This is the codegen shape Semgrep uses for its rule/config types.
genrule(
    name = "point_gen",
    srcs = ["point.atd"],
    outs = [
        "point_t.ml",
        "point_t.mli",
        "point_j.ml",
        "point_j.mli",
    ],
    cmd = "W=$$(mktemp -d) && AG=$$(realpath $(location @ocaml_atd//:atdgen)) && " +
          "cp $(location point.atd) $$W/point.atd && " +
          "(cd $$W && $$AG -t point.atd && $$AG -j -j-std point.atd) && " +
          "cp $$W/point_t.ml $(location point_t.ml) && " +
          "cp $$W/point_t.mli $(location point_t.mli) && " +
          "cp $$W/point_j.ml $(location point_j.ml) && " +
          "cp $$W/point_j.mli $(location point_j.mli)",
    tools = ["@ocaml_atd//:atdgen"],
)

ocaml_library(
    name = "point",
    srcs = [
        "point_t.ml",
        "point_t.mli",
        "point_j.ml",
        "point_j.mli",
    ],
    deps = [
        "@ocaml_atd//:atdgen_runtime",
        "@ocaml_biniou//:biniou",
        "@ocaml_yojson//:yojson",
    ],
)

ocaml_test(
    name = "atd_test",
    srcs = ["atd_test.ml"],
    deps = [":point"],
)
