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
    "libs/telemetry",
    "libs/parallelism",
    "libs/commons",
    "libs/process_limits",
    "libs/profiling",
    "libs/profiling/ppx",
    "libs/glob",
    "libs/commons2",
    "libs/paths",
    "libs/gitignore",
    "libs/lib_parsing",
    "libs/lib_parsing_tree_sitter",
]

SEMGREP_LIBS = {
    "collections": ":collections",
    "telemetry": ":telemetry",
    "parallelism": ":parallelism",
    "commons": ":commons",
    "process_limits": ":process_limits",
    "profiling": ":profiling",
    "profiling.ppx": ":ppx_profiling",
    "glob": ":glob",
    "commons2": ":commons2",
    "paths": ":paths",
    "gitignore": ":gitignore",
    "lib_parsing": ":lib_parsing",
    "lib_parsing_tree_sitter": ":lib_parsing_tree_sitter",
}

OVERLAYS = [
    "libs/collections/dune",
    "libs/telemetry/dune",
    "libs/telemetry/Telemetry.ml",
    "libs/parallelism/dune",
    "libs/commons/dune",
    "libs/commons/Ord.ml",
    "libs/lib_parsing/dune",
    "libs/lib_parsing/Pos.ml",
]
