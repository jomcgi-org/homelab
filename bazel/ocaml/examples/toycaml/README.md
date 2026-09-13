# tOyCaml: a representative demonstrator for the OCaml ruleset

A deliberately small "grep for code": parse a pattern and a target expression,
then structurally match the pattern, including metavariables, against the
target. It mirrors the build shape of a larger OCaml analysis engine while
remaining a focused acceptance target for `bazel/ocaml`.

The architecture decisions are recorded in `bazel/ARCHITECTURE.md`, Decision
history entries tooling/005 (this demonstrator), tooling/006 (architectures),
and tooling/007 (BUILD generation).

## Delivered components

| Component | Behavior | Capability accepted |
| --- | --- | --- |
| `tc_ast.ml/.mli` | generic AST plus generated node traversal and debug rendering | a composed `visitors` and `ppx_deriving.show` driver |
| `tc_wire.atd` | generates recursive wire types and JSON codec source in a Bazel action | real atdgen code generation |
| `tc_wire_codec.ml` + `tc_wire_stubs.c` | validates JSON with tree-sitter before typed decoding | pinned fetched grammar in the tOyCaml parse path |
| `tc_lexer.ml/.mli` | validates identifiers with pcre2-ocaml | vendored PCRE2 system library plus the hand-written override for its non-dune C source |
| `tc_parse.ml/.mli` | accepts compact expressions or atdgen's JSON variant representation | hand-written parsing preserved alongside the fetched grammar path |
| `tc_matcher.ml/.mli` | structural matching and metavariable binding | multi-library native compilation |
| `tc_intern.ml/.mli` + `intern_stubs.c` | FNV-1a string hash in C | first-party C foreign stub |
| `main.ml` | Cmdliner command-line entry point | non-dune opam override, flambda compiler, `-O3`, and a fully static final link |

Every tOyCaml OCaml target sets `require_flambda = True` and
`ocamlopt_flags = ["-O3"]`. The rule driver checks the selected compiler's
configuration before compiling, and the compiler sysroot build independently
checks that `--enable-flambda` took effect.

The final `:toycaml` executable sets `static_link = True`.
`:toycaml_static_link_test` runs it and rejects an ELF `PT_INTERP` segment, so
a dynamic fallback cannot pass. `:toycaml_capabilities_test` executes focused
runtime checks for the atdgen decoder, fetched grammar rejection, generated
visitor traversal, composed show deriver, and the vendored PCRE2 override. The
static test also checks Cmdliner's generated help, proving the non-dune opam
package is present at runtime. The original `:toycaml_test` and
`:toycaml_build_test` remain in place.

```bash
# CI is the test loop for this repository. The required Linux pr-checks action
# runs the focused tests explicitly on the native arm64 OCaml shard.
bazel test //bazel/ocaml/examples/toycaml/...

# Compact syntax remains supported.
bazel run //bazel/ocaml/examples/toycaml:toycaml -- \
  'foo($X, 2)' 'foo(bar(7), 2)'

# A JSON target takes the fetched grammar plus atdgen path.
bazel run //bazel/ocaml/examples/toycaml:toycaml -- \
  'foo($X, 2)' '["Call",["foo",[["Call",["bar",[["Int",7]]]],["Int",2]]]]'
```

Per-architecture builds remain owned by tooling/006. They are intentionally
outside issue #3924 even though required CI also exercises this demonstrator on
the registered arm64 toolchain.
