#!/usr/bin/env python3
"""Maintain bazel/ocaml/opam/lock.json from opam-repository metadata.

A manual workstation tool (network access required) -- never a build action.
For each package it fetches the opam file from ocaml/opam-repository at the
pinned version, reads the `url { src ... checksum ... }` section, and records
the source URL plus a sha256. When opam only publishes md5/sha512, the tarball
is downloaded, its sha512 verified against opam's, and the sha256 computed
locally -- so every lock entry is still pinned to exactly the artifact opam
would install.

Hand-maintained fields (repo, src_dirs, override, override_extra, libs, type,
strip_prefix when set) are preserved on regeneration: they describe
dune-project layout and our build strategy, not opam metadata.

Usage:
  update_lock.py --add NAME==VERSION   # add or re-pin one package
  update_lock.py --verify              # re-download + re-hash every entry
"""

import argparse
import hashlib
import json
import re
import sys
import urllib.request
from pathlib import Path

LOCK = Path(__file__).resolve().parent / "lock.json"
OPAM_RAW = (
    "https://raw.githubusercontent.com/ocaml/opam-repository/master/"
    "packages/{name}/{name}.{version}/opam"
)


def fetch(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read()


def parse_url_section(opam_text):
    """Extract (src, {algo: digest}) from an opam file's url section."""
    m = re.search(r"^url\s*\{(.*?)^\}", opam_text, re.S | re.M)
    if not m:
        sys.exit("update_lock: no url section in opam file")
    body = m.group(1)
    src = re.search(r'src:\s*"([^"]+)"', body)
    if not src:
        sys.exit("update_lock: no src in url section")
    checksums = dict(re.findall(r'"(md5|sha256|sha512)=([0-9a-f]+)"', body))
    return src.group(1), checksums


def resolve(name, version):
    """Return (url, sha256) for name.version, verifying against opam's pins."""
    opam_text = fetch(OPAM_RAW.format(name=name, version=version)).decode()
    url, checksums = parse_url_section(opam_text)
    data = fetch(url)
    sha256 = hashlib.sha256(data).hexdigest()
    if "sha256" in checksums and checksums["sha256"] != sha256:
        sys.exit(f"update_lock: sha256 mismatch for {name}.{version}")
    if "sha512" in checksums:
        if hashlib.sha512(data).hexdigest() != checksums["sha512"]:
            sys.exit(f"update_lock: sha512 mismatch for {name}.{version}")
    elif "sha256" not in checksums:
        sys.exit(f"update_lock: opam pins neither sha256 nor sha512 for {name}")
    return url, sha256


def guess_fields(name, version, url):
    """Default repo/strip_prefix/type for a fresh entry (editable afterwards)."""
    repo = "ocaml_" + name.replace("-", "_")
    archive_type = "tar.gz" if url.endswith((".tar.gz", ".tgz")) else "tar.bz2"
    tail = url.rsplit("/", 1)[-1]
    strip = re.sub(r"\.(tar\.gz|tar\.bz2|tbz|tgz)$", "", tail)
    # GitHub /archive/ tarballs unpack as <repo>-<tag-without-v>.
    m = re.match(
        r"https://github\.com/[^/]+/([^/]+)/archive/(?:refs/tags/)?(.+?)\.tar", url
    )
    if m:
        strip = f"{m.group(1)}-{m.group(2).lstrip('v')}"
    return repo, strip, archive_type


def main():
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--add", metavar="NAME==VERSION")
    mode.add_argument("--verify", action="store_true")
    args = ap.parse_args()

    lock = json.loads(LOCK.read_text())
    packages = lock["packages"]

    if args.verify:
        for pkg in packages:
            # Vendored non-opam sources (e.g. the PCRE2 C library) have no
            # opam-repository entry to resolve against; their pin is the
            # upstream release tarball, checked into the lock directly.
            if pkg.get("opam", True) is False:
                print(f"skip {pkg['name']} (vendored, not an opam package)")
                continue
            url, sha256 = resolve(pkg["name"], pkg["version"])
            for field, got, want in (
                ("url", pkg["url"], url),
                ("sha256", pkg["sha256"], sha256),
            ):
                if got != want:
                    sys.exit(
                        f"update_lock: {pkg['name']} {field} drift:\n"
                        f"  locked: {got}\n  opam:   {want}"
                    )
            print(f"ok {pkg['name']}.{pkg['version']}")
        return

    name, _, version = args.add.partition("==")
    if not version:
        sys.exit("update_lock: --add expects NAME==VERSION")
    url, sha256 = resolve(name, version)
    existing = next((p for p in packages if p["name"] == name), None)
    if existing:
        existing.update(version=version, url=url, sha256=sha256)
        # strip_prefix tracks the artifact; recompute unless overridden later.
        _, strip, archive_type = guess_fields(name, version, url)
        existing["strip_prefix"] = strip
        existing["type"] = archive_type
    else:
        repo, strip, archive_type = guess_fields(name, version, url)
        packages.append(
            {
                "name": name,
                "version": version,
                "repo": repo,
                "url": url,
                "sha256": sha256,
                "strip_prefix": strip,
                "type": archive_type,
                "src_dirs": ["src"],
                "libs": {name: name.replace("-", "_")},
            }
        )
    LOCK.write_text(json.dumps(lock, indent=2) + "\n")
    print(f"pinned {name}.{version}")


if __name__ == "__main__":
    main()
