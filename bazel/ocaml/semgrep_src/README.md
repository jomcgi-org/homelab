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
| `libs/telemetry`     | `:telemetry`      | dune + `Telemetry.ml` overlays swap the exporter clients for a loud runtime failure (dispatch below) |
| `libs/parallelism`   | `:parallelism`    | dune overlay drops unreferenced eio_main/eio.mock           |
| `libs/commons`       | `:commons`        | dune overlay = measured dep set; `Ord.ml` overlay strips the let%test blocks |
| `libs/process_limits`| `:process_limits` | translated as-is                                            |
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
| `telemetry`, `parallelism`              | internal (translated; closures dispatched below)                                                       |
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

`semgrep-interfaces` (semgrep fork, git pin) and the rest of semgrep.opam
stay out of scope until a translated dir actually names them.

## libs/telemetry + libs/parallelism rejection dispatch

Translating these two dirs deleted the four telemetry overlay stubs
(`Common_metrics.ml`, `Tracing.ml` in commons; `Logging.ml`,
`Process_limit_metrics.ml` in process_limits): the real sources compile, and
the commons dune overlay declares `telemetry`/`parallelism` while the
process_limits dune overlay is gone entirely (upstream translates as-is).
The dune stanzas decompose as (same legend as above):

| piece                                                  | dispatch                                                                                                |
| ------------------------------------------------------ | ------------------------------------------------------------------------------------------------------- |
| `opentelemetry`, `opentelemetry-logs`                   | lock, git pin: the semgrep fork (`semgrep/ocaml-opentelemetry`, commit from semgrep.opam's pin-depends) as one tarball entry with an override (bundled Atomic codegen, a `(select)` for the hmap key, inert promote-guarded proto rules). Pulls `pbrt` and `thread-local-storage` into the lock; the override also builds `opentelemetry.client`, `.proto`, `.atomic` and the `ambient-context{,.types,.eio}` chain |
| `opentelemetry-client-ocurl`                            | NOT built: curl C bindings (ocurl + ezcurl + a system libcurl). `Telemetry.ml` overlay fails loudly where the backend would be created |
| `opentelemetry-client-cohttp-eio`                       | NOT built: a full cohttp + tls-eio + mirage-crypto stack. Same overlay, same loud failure              |
| `opentelemetry.client` (Self_trace)                     | override target on the fork (cheap: mtime + pbrt); keeps the `Telemetry.ml` overlay delta to setup_otel only |
| `ambient-context`, `ambient-context-lwt`                | lock (one tarball, override: virtual library + default_implementation + select + vendored Atomic codegen; pinned sha deviates from opam metadata, see the override header) |
| `opentelemetry.ambient-context.eio`                     | override target on the fork (eio fiber-local storage; eio already locked)                              |
| `eio`, `eio.unix` (telemetry dune overlay adds)         | in the lock; `Telemetry.mli`'s eio_sw_base type names them, upstream reached them through the cohttp-eio client |
| `ptime.clock.os` (opentelemetry core dep)               | override+: ptime override grows the clock sublibrary (one C stub)                                       |
| `eio_main`, `eio.mock` (parallelism)                    | overlay: drop. No source references either (the one `Eio_main` mention is a doc-comment example in `Executor_pool.mli`); eio_main would pull the io_uring backend closure, still out of scope |
| `uri`, `yojson`, `logs`, `collections`, pps             | already in the lock / internal                                                                          |
