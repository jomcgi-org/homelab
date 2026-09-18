# ocaml sandbox guest environment

Zero-egress OCaml execution sandbox (ADR agents/057). One-shot: each request
runs in a fresh microVM restore and nothing persists. No network access at all.
Code runs as uid 65532 with a hard wall-clock timeout; stdout, stderr, and files
created in the working directory are returned to the caller. Save files with a
plain relative filename (e.g. chart.png), never an absolute path or /tmp, or
they are not collected.

Your code is written to main.ml and run with `ocaml main.ml`, the bytecode
script path. Write top-level definitions ending in a `let () = ...` entry point;
print_endline and Printf.printf are how you return anything as stdout. A type
error comes back as a nonzero exit with the compiler's own message on stderr.

The OCaml standard library is all you get. There is no opam and no dune: the
guest has no network, so no Core, no Lwt, no Base. Use Stdlib (List, Array,
String, Hashtbl, Map, Set, Printf, Buffer, Float, Int64) and nothing else.
findlib is installed, so `#require` resolves the few libraries bundled with the
compiler, but nothing can be downloaded.

This is the bytecode interpreter, not ocamlopt, chosen so start-up stays fast
for a single snippet. Heavy numeric loops will run slower than native code
would.

## Installed packages (from the image lock; exact and exhaustive)

| Package | Version |
| ------- | ------- |
| busybox | 1.38.0-r1 |
| ca-certificates-bundle | 20260413-r1 |
| glibc-2.43 | 2.43-r15 |
| glibc-2.43-locale-posix | 2.43-r15 |
| ld-linux-2.43 | 2.43-r15 |
| libcrypt1-2.43 | 2.43-r15 |
| libgcc | 16.2.0-r0 |
| libxcrypt | 4.5.2-r4 |
| ocaml | 5.5.0-r4 |
| ocaml-findlib | 1.9.8-r1 |
| ocamlfind | 1.9.8-r1 |
| wolfi-baselayout | 20230201-r29 |
