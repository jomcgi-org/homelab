"""Pinned source for the Semgrep CE tree (Phase 8, wave D).

Pinned by commit for reproducibility, like the compiler fork in
toolchain/source.bzl. Rationale for this pin: the `develop` tip on the pin
date (2026-06-12). The opam floors in semgrep.opam at this commit are what
bazel/ocaml/opam/lock.json mirrors (lwt 5.9.2, uri >= 4.4.0,
ocamlgraph >= 2.2.0, parmap >= 1.2.5, base >= v0.17.3, ppx_hash >= v0.17.0,
ppx_sexp_conv >= v0.17.1); bumping the pin means re-checking those floors.

The clone is shallow and does NOT init submodules: the `languages/`
tree-sitter grammars ride the opam lock as `"opam": false` entries instead
(wave C), pinned to the commits the submodules reference.

SEMGREP_SRC_DIRS is the translated frontier: the dune dirs dune2bazel runs
over at fetch time, growing bottom-up exactly like the opam universe.
SEMGREP_LIBS maps each translated library's dune name to its target so
later dirs can reference earlier ones. OVERLAYS lists tree paths replaced
by overlays/<path> before translation; every overlay documents what it
changes and why (the reject-loudly contract's "source patch" dispatch).
"""

SEMGREP_GIT_URL = "https://github.com/semgrep/semgrep.git"

# Tip of `develop` on 2026-06-12.
SEMGREP_COMMIT = "872766d4b93fc9d4b0e414c0afd9ed4e99171c6c"

SEMGREP_SRC_DIRS = [
    "libs/collections",
    "libs/commons",
    "libs/process_limits",
    "libs/profiling",
    "libs/profiling/ppx",
    "libs/glob",
]

SEMGREP_LIBS = {
    "collections": ":collections",
    "commons": ":commons",
    "process_limits": ":process_limits",
    "profiling": ":profiling",
    "profiling.ppx": ":ppx_profiling",
    "glob": ":glob",
}

OVERLAYS = [
    "libs/collections/dune",
    "libs/commons/dune",
    "libs/commons/Ord.ml",
    "libs/process_limits/dune",
]
