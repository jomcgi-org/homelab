# tOyCaml -- a representative demonstrator for the OCaml ruleset

A deliberately tiny "grep for code": parse a pattern and a target expression,
then structurally match the pattern (with metavariables) against the target. It
builds in seconds, but it is wired to mirror the *shape* of how a large OCaml
analysis engine uses the ecosystem, so `bazel/ocaml` can be grown against a
representative target instead of a generic hello-world.

Why it exists, what public build it mirrors, and the features it is meant to
drive out are recorded in the ADRs:

- `bazel/ARCHITECTURE.md`, ADR map entry tooling/005 -- this demonstrator
- `bazel/ARCHITECTURE.md`, ADR map entry tooling/006 -- arches
- `bazel/ARCHITECTURE.md`, ADR map entry tooling/007 -- BUILD gen

## What it is today (builds on the current ruleset)

| File | Role | Build feature exercised |
|------|------|-------------------------|
| `tc_ast.ml/.mli` | generic AST node type | multi-module library, `.mli` interfaces |
| `tc_pattern.ml/.mli` | a pattern is code with metavariables | intra-library dep on `Tc_ast` |
| `tc_lexer.ml/.mli` | hand-written tokenizer | uses the fetched-from-source `re` opam lib |
| `tc_parse.ml/.mli` | recursive-descent parser | inter-module compile ordering (`ocamldep -sort`) |
| `tc_matcher.ml/.mli` | structural match + metavar binding | inter-library dep on `:toycaml_intern` |
| `tc_intern.ml/.mli` + `intern_stubs.c` | FNV-1a string hash in C | `c_srcs` (C foreign stub) |
| `main.ml` | CLI entry point | `ocaml_binary` + `build_test` |
| `matcher_test.ml` | end-to-end checks | `ocaml_test` (exit 0 = pass) |

```bash
# No local test loop in this repo -- push the branch and watch BuildBuddy CI.
bazel test //bazel/ocaml/examples/toycaml/...
bazel run  //bazel/ocaml/examples/toycaml:toycaml -- 'foo($X, 2)' 'foo(bar(7), 2)'
```

## What it is meant to grow into

The demonstrator is intentionally missing the load-bearing build features a real
engine needs. Each is a planned ruleset capability that will land as its own
component here (ADR 005 maps every item to the public engine's build):

- a compiler built with flambda, and `-O3` on every compile;
- a real codegen tool run as a build action (e.g. `atdgen`), not only
  `ocamllex`/`menhir`;
- a `visitors`-style ppx over the AST, plus the wider ppx set;
- vendored system libraries (gmp, pcre, ...) and an escape hatch for opam
  packages that are not dune projects;
- a statically linked final binary;
- per-architecture builds (ADR 006).

As each lands, the matching toy component above is upgraded to use it, so the
ruleset always has a representative, green target to build against.
