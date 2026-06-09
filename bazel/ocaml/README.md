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

- **`ocaml_library(name, srcs=[.ml/.mli], c_srcs=[.c], deps=[...], opam_deps=[...])`** —
  compiles a set of modules into a native `.cmxa` archive. `c_srcs` are C stubs
  (dune's `foreign_stubs`/`c_names`): each `.c` is compiled by `ocamlopt` (which
  supplies the `caml/*.h` headers, using the execution host's C compiler) and
  folded into the library's `.a`, so binaries linking the library resolve the
  `external` primitives with no extra wiring. See `examples/c_stubs`.
- **`ocaml_binary(name, srcs, deps, opam_deps)`** — compiles + links a runnable
  native executable.
- **`ocaml_test(name, srcs, deps, opam_deps)`** — a native test executable that
  exits 0 on success, non-zero on failure (the same convention as Dune's
  `(test)` stanza). The binary *is* the test runner, so `bazel test //...` runs
  it directly — no wrapper script — and it joins the global test-all with no
  extra wiring.
- **`OcamlInfo`** provider carrying the compiled output dir (`cmi`/`cmx`/`.o`),
  the `.cmxa` archive (+ its `.a`), transitive include dirs, and transitive
  opam/findlib package names in link order.

`deps` are other `ocaml_library` targets. `opam_deps` are findlib package names
(e.g. `unix`, `str`) — in this toy they resolve to stdlib-shipped archives.

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

This was the hard design question, and the answer changed once contact with the
infrastructure was made.

CI runs build actions on BuildBuddy RBE, whose stock executor has no OCaml. The
**first attempt** attached a digest-pinned `ocaml/opam` image to each action via
the BuildBuddy `container-image` execution property. That **did not work**: a
probe in the driver proved the actions kept landing on the default executor
(`os=ubuntu-24.04`, no `ocamlopt`) — this RBE does not honor a per-action custom
container image. No target in the repo had ever used one; there was no precedent
because it isn't supported here.

So the compiler has to **travel with the action as hermetic inputs**:

1. `toolchain/repositories.bzl` is a module extension that downloads pinned
   **Debian bullseye** OCaml `.deb`s (`debs.bzl`, from the permanent
   `archive.debian.org` mirror) and extracts them — with a stdlib-only Python
   `ar`/`tar` extractor, no `dpkg` needed — into `@ocaml_sysroot//:sysroot`.
2. Every ocaml action stages that sysroot as inputs. The driver relocates the
   compiler with a single `OCAMLLIB` override and calls `ocamlopt.opt` /
   `ocamldep.opt` from the sysroot.
3. **Native code generation and the final link use the execution host's
   `as`/`gcc`/`ld`** — the same C toolchain the repo's C/C++ builds already rely
   on. So no C toolchain is bundled.

Two properties make this robust regardless of where BuildBuddy schedules the
action:

- **Old glibc.** Bullseye binaries need `glibc >= 2.29`, so they run
  forward-compatibly on both the RBE executor and the workflow runner.
- **Host-linked output.** Binaries link the host's glibc, so they run wherever
  the build action ran (the `hello_run_test` executes the binary directly).

`OcamlToolchainInfo` (the Bazel toolchain) carries the sysroot files plus tool
configuration (whether to prefer `ocamlfind`, extra flags). `ocamlfind` is
present in the sysroot; it's left off by default because the toy's `opam_deps`
are stdlib libraries that link by archive name without it.

### Productionization path

The cleanest long-term shape is a **custom RBE executor image** (or a
self-hosted executor) with the compiler and the project's real `opam_deps`
preinstalled, so the sysroot need not be staged per action. That requires
control over the executor image this repo's BuildBuddy plan doesn't currently
expose. A Gazelle/`ocamldep` BUILD generator (per-module targets, real
incrementality) is the other obvious next step.

## The external opam dependency

The toy's external dependency is [`fmt`](https://erratique.ch/software/fmt)
0.11.0, **vendored from source** under `third_party/fmt/` and compiled by our own
`ocaml_library`. `fmt`'s core module is pure OCaml (no C stubs) and only needs
the stdlib, so it builds standalone — no `opam install` step anywhere.
`examples/hello` also wires the `unix` library through `opam_deps`.

The `opentelemetry` SDK span demo (the stretch goal) is intentionally dropped:
its transitive deps (protobuf etc.) make it heavy for a from-source toy.

## Real opam deps, built from their own dune file

The next step past a hand-vendored dep: take a *real* opam library and build it
from source **driven by its own `dune` metadata** — because an opam package
*is* a dune project, translating its dune file is how you resolve it.

[`re`](https://github.com/ocaml/ocaml-re) (ocaml-re) 1.11.0 is wired in this
way (`examples/regex` depends on it):

1. **Fetch.** `opam/packages.bzl` pins re's checksum-stable dune-release tarball
   (`re-1.11.0.tbz` + sha256). The `opam/extension.bzl` module extension
   downloads and extracts it into `@ocaml_re`.
2. **Translate.** The repository rule runs `opam/dune2bazel.py` over the
   package's real `lib/dune` (`(library (name re) (libraries seq))`) and emits
   the `ocaml_library` BUILD — no hand-written target. The generator maps
   `(libraries …)` to `opam_deps`, dropping stdlib-shipped packages (`seq` lives
   in `stdlib.cmxa`), and **rejects loudly** any dune feature we don't model yet
   (ppx `preprocess`, C `foreign_stubs`, module filtering, multiple stanzas) —
   that rejection marks exactly where real opam resolution would have to begin.
3. **Build.** Our existing `ocaml_library` compiles the whole library flat.
   re's modules already reference each other by flat names (`Cset.`, `Automata.`)
   and `re.ml` is itself the namespace module (`include Core; module Pcre = Pcre`
   …), so the flat compile reproduces the public `Re.*` API without replicating
   dune's `Re__`-prefixed wrapping.

**Version ceiling.** re 1.11.0 is the newest tag that compiles on the toolchain's
bullseye OCaml 4.11.1; 1.12.0+ call `List.equal` (OCaml ≥ 4.12). Bumping the
sysroot compiler lifts this.

**Module-name caveat.** re ships internal `Fmt` and `Str` modules. Because the
flat compile doesn't namespace-prefix them, a binary must not link both `re` and
the vendored `fmt` (duplicate `Fmt`) — `examples/regex` depends on re alone.
This is the collision that dune's library wrapping exists to prevent, and the
natural pressure toward the per-module/wrapped Gazelle generator below.

## Layout

```
bazel/ocaml/
  defs.bzl                 # public: ocaml_library, ocaml_binary, OcamlInfo
  rules.bzl                # rule impls + OcamlInfo provider
  toolchain.bzl            # OcamlToolchainInfo, ocaml_toolchain rule
  toolchain/
    debs.bzl               # pinned Debian OCaml .deb URLs + checksums
    repositories.bzl       # module extension: fetch + extract -> @ocaml_sysroot
    extract_debs.py        # stdlib-only .deb (ar + tar.xz) extractor
  driver/ocaml_compile.sh  # ocamldep -sort + per-module compile + archive/link
  opam/                    # fetch + dune->BUILD generation for real opam deps
    packages.bzl           # pinned opam package tarballs (URL + sha256)
    extension.bzl          # module extension: fetch + generate -> @ocaml_<pkg>
    dune2bazel.py          # stdlib-only dune (library) -> ocaml_library generator
  third_party/fmt/         # vendored fmt 0.11.0 (ISC)
  third_party/re/          # alias -> @ocaml_re (re 1.11.0, fetched + dune-built)
  examples/hello/          # message -> greeting -> main; greeting_test (ocaml_test)
  examples/regex/          # depends on the fetched `re` opam lib; regex_test
  examples/c_stubs/        # ocaml_library with a C stub (c_srcs); counter_test
```

## Verify

```bash
bazel test //bazel/ocaml/...        # build_test + run test, on BuildBuddy RBE
```

There is no local test loop in this repo — push the branch and watch CI.
