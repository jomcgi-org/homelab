"""Pinned opam packages built from source via their own dune metadata.

Each entry is a checksum-pinned release tarball: the URL and hash come from the
package's opam-repository metadata (the same pin `opam install` would use), so
prefer whatever artifact opam pins -- dune-release `.tbz` assets where upstream
publishes them, the tagged `/archive/` tarball where it does not (re moved to
the latter at 1.13.x). The @ocaml_<repo> repository rule fetches and extracts
the tarball, then runs dune2bazel.py over `<src_dir>/dune` to generate the
ocaml_library BUILD.

re is pinned at 1.13.2 (matches opam-repository's url/checksum section; the
sha512 there was cross-checked against this sha256 at pin time). The old 1.11.0
ceiling came from the bullseye 4.11.1 toolchain; the from-source 5.3.0 toolchain
lifted it.
"""

OPAM_PACKAGES = [
    {
        "repo": "ocaml_re",
        "url": "https://github.com/ocaml/ocaml-re/archive/refs/tags/1.13.2.tar.gz",
        "sha256": "2e37b01b9bda0e39f0fd3913c0ec81237ed2d04c6bbe23f48b102de83ba47454",
        "strip_prefix": "ocaml-re-1.13.2",
        "type": "tar.gz",
        "src_dir": "lib",
    },
]
