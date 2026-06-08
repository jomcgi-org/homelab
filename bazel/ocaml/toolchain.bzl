"""OCaml toolchain — Bazel toolchain mechanism for the bazel/ocaml ruleset.

The compiler itself (ocamlopt / ocamldep / ocamlfind / gcc) is NOT fetched into
the Bazel sandbox. Instead the build actions execute *inside a digest-pinned
public OCaml container image* on BuildBuddy RBE, via the `container-image`
execution property (see EXEC_PROPERTIES). This keeps the toolchain hermetic and
reproducible (pinned by digest) without standing up a custom RBE executor image
or shipping a non-relocatable opam switch through Bazel's repository cache.

Two things are wired through here:

  1. The container image (EXEC_PROPERTIES) — attached to every ocaml target's
     actions by the ocaml_library / ocaml_binary macros so they run in an
     environment that has ocamlopt + ocamldep + gcc on PATH.

  2. OcamlToolchainInfo — the *tool configuration* (opam root, whether to prefer
     ocamlfind, extra compile flags) consumed by the rule implementations to
     build the driver command line. Swapping the image/switch is a one-line
     change here.

The "real" production shape is option (a) from the design discussion: a custom
RBE executor image with the compiler + opam_deps preinstalled. The pinned public
image is the same idea with far less moving infrastructure — documented in
bazel/ocaml/README.md.
"""

# Digest-pinned public OCaml image (debian-12-ocaml-5.3, multi-arch index).
# Pinned by digest so the toolchain is reproducible; bump deliberately.
OCAML_IMAGE = "docker://ocaml/opam@sha256:97a21a819648875667315f235f68652766b7839290a274bfaa1305127c58e5ae"

# Execution properties attached to every ocaml build/test action. BuildBuddy RBE
# runs the action inside OCAML_IMAGE. dockerUser=root sidesteps exec-root
# permission friction; the driver sets OPAMROOTISOK so opam tolerates root.
EXEC_PROPERTIES = {
    "container-image": OCAML_IMAGE,
    "OSFamily": "linux",
    "dockerUser": "root",
}

# Opam root inside OCAML_IMAGE (the ocaml/opam images create the switch here).
DEFAULT_OPAM_ROOT = "/home/opam/.opam"

OcamlToolchainInfo = provider(
    doc = "Tool configuration for driving ocamlopt inside the OCaml container.",
    fields = {
        "opam_root": "Absolute OPAMROOT inside the container image.",
        "use_ocamlfind": "If True, drive compilation via `ocamlfind ocamlopt` and resolve opam_deps as findlib packages; otherwise fall back to raw ocamlopt with a small stdlib package map.",
        "extra_compile_flags": "Extra flags passed to every ocamlopt -c invocation.",
        "container_image": "Informational: the pinned image actions run in.",
    },
)

def _ocaml_toolchain_impl(ctx):
    return [platform_common.ToolchainInfo(
        ocaml = OcamlToolchainInfo(
            opam_root = ctx.attr.opam_root,
            use_ocamlfind = ctx.attr.use_ocamlfind,
            extra_compile_flags = ctx.attr.extra_compile_flags,
            container_image = OCAML_IMAGE,
        ),
    )]

ocaml_toolchain = rule(
    implementation = _ocaml_toolchain_impl,
    attrs = {
        "opam_root": attr.string(default = DEFAULT_OPAM_ROOT),
        "use_ocamlfind": attr.bool(default = True),
        "extra_compile_flags": attr.string_list(default = []),
    },
    doc = "Declares an OCaml tool configuration. Register an instance with register_toolchains().",
)
