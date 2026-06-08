#!/usr/bin/env python3
"""Extract Debian .deb packages into a sysroot directory.

A .deb is an `ar` archive containing debian-binary, control.tar.* and
data.tar.*. We only want data.tar.* (the installed files). Implemented with the
Python stdlib so the repository rule needs nothing on the host but python3 — no
`ar`/`dpkg-deb`. Used by //bazel/ocaml/toolchain:repositories.bzl to assemble a
relocatable OCaml compiler sysroot.
"""
import gzip
import io
import lzma
import os
import sys
import tarfile


def _ar_members(path):
    with open(path, "rb") as f:
        if f.read(8) != b"!<arch>\n":
            raise SystemExit("not an ar archive: " + path)
        while True:
            hdr = f.read(60)
            if len(hdr) < 60:
                return
            name = hdr[0:16].decode("ascii", "replace").strip()
            size = int(hdr[48:58].decode("ascii").strip())
            data = f.read(size)
            if size % 2 == 1:
                f.read(1)  # ar members are 2-byte aligned
            yield name, data


def _extract(deb, dest):
    for name, data in _ar_members(deb):
        if not name.startswith("data.tar"):
            continue
        if name.endswith(".xz"):
            raw = lzma.decompress(data)
        elif name.endswith(".gz"):
            raw = gzip.decompress(data)
        else:
            raise SystemExit("unsupported data compression: " + name)
        tf = tarfile.open(fileobj=io.BytesIO(raw))
        try:
            tf.extractall(dest, filter="tar")  # python >= 3.12
        except TypeError:
            tf.extractall(dest)
        return
    raise SystemExit("no data.tar member in " + deb)


def main(argv):
    dest = argv[1]
    os.makedirs(dest, exist_ok=True)
    for deb in argv[2:]:
        _extract(deb, dest)


if __name__ == "__main__":
    main(sys.argv)
