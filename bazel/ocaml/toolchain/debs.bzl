"""Pinned Debian bullseye .deb packages for the hermetic OCaml compiler sysroot.

Bullseye (oldstable) is chosen for its glibc 2.31 / "needs glibc >= 2.29"
binaries: old enough to run forward-compatibly on the BuildBuddy RBE executor
*and* the workflow runner, so the compiler relocates with nothing but an
OCAMLLIB override. archive.debian.org is the permanent mirror for EOL releases,
so these URLs + checksums stay stable. The C toolchain (gcc/as/ld) is NOT
fetched: native linking uses the executor's, exactly like the repo's C/C++
builds. ocaml 4.11.1 satisfies fmt 0.11.0's `ocaml >= 4.08` lower bound.
"""

DEB_BASE_URL = "https://archive.debian.org/debian/"

# Only the OCaml bits: compiler, native stdlib, the bytecode runtime (for
# ocamlfind) and findlib. System libs (libc6, zlib1g, binutils, gcc) come from
# the action's execution host.
DEBS = [
    {
        "name": "ocaml-base-nox",
        "filename": "pool/main/o/ocaml/ocaml-base-nox_4.11.1-4_amd64.deb",
        "sha256": "aa7c337e7cc99eee59c5b8e4ceb02dc0076a708b4a795397f95e7437396e5410",
    },
    {
        "name": "ocaml-interp",
        "filename": "pool/main/o/ocaml/ocaml-interp_4.11.1-4_amd64.deb",
        "sha256": "8d8c066165444c5c1dbe3f3af3e115a2683b2270f308a01b5197e54c3bf2fca4",
    },
    {
        "name": "ocaml-nox",
        "filename": "pool/main/o/ocaml/ocaml-nox_4.11.1-4_amd64.deb",
        "sha256": "4e4532a43b8edd3476d606bcfc572c6827de396ba431363c285d1d7e13bc9290",
    },
    {
        "name": "libfindlib-ocaml",
        "filename": "pool/main/f/findlib/libfindlib-ocaml_1.8.1-2_amd64.deb",
        "sha256": "5b436abd2180689a666059a793aadebafaeee041c51c5a31d04f9ca1333c5f74",
    },
    {
        "name": "ocaml-findlib",
        "filename": "pool/main/f/findlib/ocaml-findlib_1.8.1-2_amd64.deb",
        "sha256": "2ceec9225eef1b3a72d651aef47523dc0fe332c3a088ece4221cba8698223944",
    },
]
