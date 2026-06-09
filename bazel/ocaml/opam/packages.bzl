"""Pinned opam packages built from source via their own dune metadata.

Each entry is a release-asset tarball (URL + sha256) --
the dune-release `.tbz` assets are checksum-stable, unlike GitHub's
auto-generated `/archive/` tarballs. The @ocaml_<repo> repository rule fetches
and extracts the tarball, then runs dune2bazel.py over `<src_dir>/dune` to
generate the ocaml_library BUILD.

re is pinned at 1.11.0: pure OCaml, depends only on `seq`, no ppx or C stubs.
The pin originally came from the bullseye 4.11.1 toolchain (1.12.0+ call
`List.equal`, OCaml >= 4.12); the toolchain now builds OCaml 5.3.0, so a bump to
a modern tag is a trivial follow-up.
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
