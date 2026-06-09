"""Pinned source for the hermetic OCaml compiler — Semgrep's OCaml fork.

The compiler source is cloned (repositories.bzl) and built from source by an RBE
action (compiler.bzl) rather than fetched as Debian debs, for two reasons:

  1. It matches **Semgrep CE**, which pins `ocaml >= 5.3.0` via the custom variant
     `git+https://github.com/semgrep/ocaml.git` branch `5.3.0-semgrep`. That
     branch is stock OCaml 5.3.0 plus a thin patch set (notably preserving
     `backtrace_enabled` between domains). Building Semgrep with this ruleset is
     the end goal, so the toolchain targets Semgrep's exact compiler.
  2. A from-source `make install` ships `compiler-libs` (ast_mapper, ocamlcommon),
     which the stripped Debian OCaml packages omitted — that is what unblocks ppx.

Pinned by commit for reproducibility. `OCAML_VERSION` is the human-readable
string `ocamlopt -version` reports for this commit, for messages/docs only.
"""

OCAML_GIT_URL = "https://github.com/semgrep/ocaml.git"

# Tip of branch `5.3.0-semgrep`.
OCAML_COMMIT = "8521d35f98c3bbd7fdcb6678bf120b334b4b6a9c"

OCAML_VERSION = "5.3.0+semgrep-fork"
