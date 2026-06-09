"""Pinned opam packages built from source via their own dune metadata.

Each entry is a release-asset tarball (URL + sha256, like toolchain/debs.bzl) --
the dune-release `.tbz` assets are checksum-stable, unlike GitHub's
auto-generated `/archive/` tarballs. The @ocaml_<repo> repository rule fetches
and extracts the tarball, then runs dune2bazel.py over `<src_dir>/dune` to
generate the ocaml_library BUILD.

re 1.11.0 is the newest ocaml-re that compiles on our bullseye OCaml 4.11.1: it
is pure OCaml, depends only on `seq` (which lives in the 4.11 stdlib), and uses
no ppx or C stubs. re 1.12.0+ call `List.equal` (OCaml >= 4.12), so they are out
of reach until the toolchain's compiler is bumped.
"""

OPAM_PACKAGES = [
    {
        "repo": "ocaml_re",
        "url": "https://github.com/ocaml/ocaml-re/releases/download/1.11.0/re-1.11.0.tbz",
        "sha256": "01fc244780c0f6be72ae796b1fb750f367de18624fd75d07ee79782ed6df8d4f",
        "strip_prefix": "re-1.11.0",
        "src_dir": "lib",
    },
]
