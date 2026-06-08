# rules_ocaml — thin native OCaml rules

The fifth custom Bazel ruleset in this repo (alongside `bazel/helm`,
`bazel/semgrep`, `bazel/wrangler`, `bazel/vitepress`). It builds OCaml the way
the homelab needs for one specific reason: **OCaml fluency to support Semgrep**,
whose engine is written in OCaml. The goal here is a working toy, not a Dune
competitor.

## What it provides

```starlark
load("//bazel/ocaml:defs.bzl", "ocaml_library", "ocaml_binary")
```

- **`ocaml_library(name, srcs=[.ml/.mli], deps=[...], opam_deps=[...])`** —
  compiles a set of modules into a native `.cmxa` archive.
- **`ocaml_binary(name, srcs, deps, opam_deps)`** — compiles + links a runnable
  native executable.
- **`OcamlInfo`** provider carrying the compiled output dir (`cmi`/`cmx`/`.o`),
  the `.cmxa` archive (+ its `.a`), transitive include dirs, and transitive
  opam/findlib package names in link order.

`deps` are other `ocaml_library` targets. `opam_deps` are findlib package names
(e.g. `unix`, `str`) resolved via `ocamlfind` when present.

## Dependency inference (the learning bit)

You do **not** hand-order modules. The compile action runs
[`ocamldep -sort`](https://ocaml.org/manual/ocamldep.html) over the library's
sources to recover compile order, compiles each module's `.mli` before its
`.ml`, then archives the `.cmx` in that order (`ocamlopt -a -o lib.cmxa`). The
toy example (`examples/hello`) has `greeting.ml -> message.ml` so the sort
actually does work.

This is **one whole-library compile action** (`driver/ocaml_compile.sh`). It
trades fine-grained per-module incrementality for simplicity — fine for a toy.
The "real" version is a Gazelle/`ocamldep` BUILD generator that emits one target
per module; that's the next step, intentionally out of scope here.

## Toolchain: how `ocamlopt` runs on RBE

The important design question for this repo. CI runs build actions on
BuildBuddy's **stock RBE executor**, which has no OCaml. Two ways to fix that
were considered:

- **(a)** a custom RBE executor image with `opam` + `ocaml` + the `opam_deps`
  preinstalled, or
- **(b)** a hermetic toolchain Bazel fetches and relocates (obazl territory).

We use a pragmatic variant of **(a)**: a **digest-pinned public OCaml image**
(`ocaml/opam`, pinned by `sha256` in `toolchain.bzl`) attached to every ocaml
target's actions via the BuildBuddy `container-image` execution property
(`EXEC_PROPERTIES`). The build action therefore executes *inside* an environment
that already has `ocamlopt`/`ocamldep`/`gcc` on `PATH`. This is hermetic
(reproducible by digest) and needs no custom image build or registry push — so
it goes green on a PR branch with no extra credentials.

`OcamlToolchainInfo` (the Bazel toolchain) carries the *tool configuration*
(opam root, whether to prefer `ocamlfind`, extra flags); swapping the image or
switch is a one-line change in `toolchain.bzl`.

Why not fetch + extract the compiler into the Bazel sandbox (option b)? Wolfi
ships `ocaml`/`ocamlfind` apks, but they're built against a newer glibc than the
stock executor — running them hermetically means bundling the whole C toolchain
(gcc + binutils + glibc + glibc-dev + closure) and running both compilation and
the produced binaries under the extracted loader. That's a large, fragile build
for no extra benefit over a digest-pinned image. It's the documented "fully
self-hosted" alternative if we ever want zero dependency on a public registry.

### Productionization path

The cleanest long-term shape is a **custom GHCR image** with the compiler and
the project's real `opam_deps` preinstalled (built + pushed by a workflow like
`update-semgrep-pro.yaml`), referenced by digest in `EXEC_PROPERTIES`. At that
point `opam_deps` resolve as genuine preinstalled findlib packages and the
vendoring below is no longer needed.

## The external opam dependency

The toy's external dependency is [`fmt`](https://erratique.ch/software/fmt)
0.11.0, **vendored from source** under `third_party/fmt/` and compiled by our own
`ocaml_library`. `fmt`'s core module is pure OCaml (no C stubs), so it builds
standalone against the stdlib — no `opam install` step anywhere. `examples/hello`
also wires the `unix` findlib package through `opam_deps` to exercise that path.

The `opentelemetry` SDK span demo (the stretch goal) is intentionally dropped:
its transitive deps (protobuf etc.) make it heavy for a from-source toy. Revisit
once the productionized preinstalled-image path above exists.

## Layout

```
bazel/ocaml/
  defs.bzl              # public: ocaml_library, ocaml_binary, OcamlInfo
  rules.bzl             # rule impls + OcamlInfo provider
  toolchain.bzl         # OcamlToolchainInfo, ocaml_toolchain, pinned image
  driver/ocaml_compile.sh  # ocamldep -sort + per-module compile + archive/link
  third_party/fmt/      # vendored fmt 0.11.0 (ISC)
  examples/hello/       # message -> greeting -> main, links fmt + unix
```

## Verify

```bash
bazel test //bazel/ocaml/...        # build_test + run test, on BuildBuddy RBE
```

There is no local test loop in this repo — push the branch and watch CI.
