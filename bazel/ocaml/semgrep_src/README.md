# @semgrep_src: the pinned Semgrep CE tree (Phase 8, wave D)

`repositories.bzl` clones the commit pinned in `source.bzl`, applies the
`overlays/` files, and runs `opam/dune2bazel.py` over the dune dirs in
`SEMGREP_SRC_DIRS`, resolving `(libraries ...)` against the opam lock plus
the internal libraries translated so far. The translated frontier grows
bottom-up, exactly like the opam universe; anything the translator does not
model rejects loudly at fetch time.

## Translated today

| dir                  | target            | notes                                                       |
| -------------------- | ----------------- | ----------------------------------------------------------- |
| `libs/collections`   | `:collections`    | dune overlay drops inert `(inline_tests)`/pps               |
| `libs/commons`       | `:commons`        | dune overlay = measured dep set; `Ord.ml` overlay strips the let%test blocks |
| `libs/process_limits`| `:process_limits` | dune overlay drops unreferenced telemetry/parallelism       |
| `libs/profiling`     | `:profiling`      | translated as-is                                            |
| `libs/profiling/ppx` | `:ppx_profiling`  | the first internal ppx rewriter (kind ppx_deriver)          |
| `libs/glob`          | `:glob`           | translated as-is (menhir Parser + ocamllex Lexer)           |

## libs/commons rejection dispatch

Running dune2bazel over `libs/commons` at the pinned commit rejects on
`(inline_tests)` first; the full dune stanza decomposes into the following
named decisions (the wave A4 shopping list). Legend: **lock** = add the opam
package to lock.json (translated unless noted), **override+** = extend an
existing override, **overlay** = source patch via `overlays/`, **internal** =
another `SEMGREP_SRC_DIRS` entry.

| piece                                  | dispatch                                                                                              |
| -------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| `(inline_tests)`                        | overlay: drop the field AND strip the 6 `let%test` blocks in `Ord.ml` (without ppx_inline_test the syntax does not parse; decided in the plan, do not re-litigate) |
| `ppx_inline_test` (pps)                 | not built (Jane Street ladder is the shallow half only); covered by the same overlay                   |
| `ppx_deriving.show`                     | in the lock                                                                                            |
| `ppx_deriving.eq` / `.ord`              | override+: add the eq/ord plugin targets to the ppx_deriving override (same tarball as show)          |
| `ppx_deriving_yojson`                   | lock (dune project; runtime needs yojson, already locked)                                              |
| `ppx_hash`, `ppx_sexp_conv`             | in the lock (wave B)                                                                                   |
| `lwt_ppx`                               | lock (sublibrary of the locked lwt tarball; ppxlib is in the lock)                                     |
| `pyro-caml-ppx`                         | overlay: drop. Not an opam release; semgrep pins `git+https://github.com/semgrep/pyro-caml.git` (a Pyroscope profiling ppx). Out of the phase's scope; revisit when profiling matters |
| `collections`                           | internal (translated, this directory)                                                                  |
| `telemetry`, `parallelism`              | internal (next `SEMGREP_SRC_DIRS` candidates; have their own closures)                                 |
| `fpath`, `hex`, `uuidm`, `semver`       | lock (small pure-OCaml)                                                                                |
| `fmt`                                   | lock (the real opam fmt; retires the vendored `third_party/fmt` eventually)                            |
| `ocolor`, `ANSITerminal`                | lock                                                                                                   |
| `logs.threaded`                         | override+: add the threaded target to the logs override (needs threads opam_dep)                       |
| `alcotest`, `alcotest-lwt`              | lock (test-only; needed because commons links them, not to run their tests)                            |
| `testo`                                 | lock with a git pin (semgrep pins `git+https://github.com/semgrep/testo.git`); the lock supports url+sha pins |
| `timedesc`                              | lock (+ its deps: timedesc-tzdb, timedesc-tzlocal)                                                      |
| `bos`                                   | lock (topkg/Bünzli, override like cmdliner/logs; + rresult/astring/fpath/logs deps)                     |
| `pcre` (in addition to pcre2)           | lock (pcre-ocaml against a vendored PCRE1 C lib, the pcre2 pattern)                                     |
| `digestif.ocaml`                        | lock (the pure-OCaml implementation half; avoids the C variant's stubs)                                 |
| `sexplib`                               | lock (sexplib0 is in; sexplib adds num? no, modern sexplib is pure + parsexp)                           |
| `memtrace`                              | lock (small, pure)                                                                                      |
| `eio_main`                              | lock, the heavyweight item (eio + eio_linux/eio_posix: uring on linux). Needs its own scoping pass     |
| `str`, `unix` (stdlib)                  | already modeled by the driver                                                                           |
| `re`, `yojson`, `atdgen-runtime`, `cmdliner`, `logs`, `uri`, `lwt`, `parmap`, `pcre2` | in the lock |

`semgrep-interfaces`, `opentelemetry` (semgrep forks, git pins) and the rest
of semgrep.opam stay out of scope until a translated dir actually names them.
