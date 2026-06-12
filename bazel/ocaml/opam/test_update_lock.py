"""Tests for bazel/ocaml/opam/update_lock.py.

Covers parse_url_section() and guess_fields() -- the two pure functions
that transform opam file text and URL strings into lock-file fields.
Network-fetching helpers (fetch, resolve) require live opam-repository
access and are tested manually via --verify; only the pure parsing logic
is covered here.
"""

from __future__ import annotations

import pytest

from bazel.ocaml.opam.update_lock import guess_fields, parse_url_section


# ---------------------------------------------------------------------------
# parse_url_section
# ---------------------------------------------------------------------------


class TestParseUrlSection:
    # -- sha256-only ---------------------------------------------------------

    def test_sha256_only(self):
        opam = """\
opam-version: "2.0"
maintainer: "someone@example.com"
url {
  src: "https://example.com/pkg-1.0.tar.gz"
  checksum: "sha256=aabbccdd00112233aabbccdd00112233aabbccdd00112233aabbccdd00112233"
}
"""
        src, checksums = parse_url_section(opam)
        assert src == "https://example.com/pkg-1.0.tar.gz"
        assert checksums == {
            "sha256": "aabbccdd00112233aabbccdd00112233aabbccdd00112233aabbccdd00112233"
        }

    def test_sha256_only_returns_no_sha512(self):
        opam = """\
url {
  src: "https://example.com/pkg-2.0.tar.gz"
  checksum: "sha256=deadbeef00000000deadbeef00000000deadbeef00000000deadbeef00000000"
}
"""
        _, checksums = parse_url_section(opam)
        assert "sha512" not in checksums
        assert "md5" not in checksums

    # -- sha512-only ---------------------------------------------------------

    def test_sha512_only(self):
        opam = """\
url {
  src: "https://example.com/lib-3.1.tar.bz2"
  checksum: "sha512=ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
}
"""
        src, checksums = parse_url_section(opam)
        assert src == "https://example.com/lib-3.1.tar.bz2"
        assert "sha512" in checksums
        assert "sha256" not in checksums

    def test_sha512_value_captured_correctly(self):
        digest = "a" * 128
        opam = f"""\
url {{
  src: "https://example.com/pkg.tar.gz"
  checksum: "sha512={digest}"
}}
"""
        _, checksums = parse_url_section(opam)
        assert checksums["sha512"] == digest

    # -- both sha256 + sha512 ------------------------------------------------

    def test_both_sha256_and_sha512(self):
        opam = """\
url {
  src: "https://example.com/both-1.2.tar.gz"
  checksum: [
    "sha256=1111111111111111111111111111111111111111111111111111111111111111"
    "sha512=2222222222222222222222222222222222222222222222222222222222222222222222222222222222222222222222222222222222222222222222222222222222"
  ]
}
"""
        src, checksums = parse_url_section(opam)
        assert src == "https://example.com/both-1.2.tar.gz"
        assert checksums["sha256"] == "1111111111111111111111111111111111111111111111111111111111111111"
        assert checksums["sha512"] == "2222222222222222222222222222222222222222222222222222222222222222222222222222222222222222222222222222222222222222222222222222222222"

    def test_both_checksums_md5_ignored(self):
        opam = """\
url {
  src: "https://example.com/pkg.tar.gz"
  checksum: [
    "md5=d41d8cd98f00b204e9800998ecf8427e"
    "sha256=e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
  ]
}
"""
        _, checksums = parse_url_section(opam)
        # md5 is captured but sha256 is also present
        assert "sha256" in checksums
        assert checksums.get("md5") == "d41d8cd98f00b204e9800998ecf8427e"

    # -- brace on same line as checksum (rresult/bos style) ------------------

    def test_closing_brace_on_checksum_line(self):
        # Some packages (rresult, bos) write the closing brace of the url
        # section on the same line as the last checksum field.
        opam = """\
opam-version: "2.0"
url { src: "https://example.com/rresult-0.7.0.tbz"
  checksum: "sha256=cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc" }
"""
        src, checksums = parse_url_section(opam)
        assert src == "https://example.com/rresult-0.7.0.tbz"
        assert checksums["sha256"] == "cccccccccccccccccccccccccccccccccccccccccccccccccccc" + "cc" * 6

    def test_src_and_checksum_all_on_one_line(self):
        opam = 'url { src: "https://example.com/pkg.tar.gz" checksum: "sha256=abcd1234abcd1234abcd1234abcd1234abcd1234abcd1234abcd1234abcd1234" }\n'
        src, checksums = parse_url_section(opam)
        assert src == "https://example.com/pkg.tar.gz"
        assert "sha256" in checksums

    # -- missing url section -------------------------------------------------

    def test_missing_url_section_exits(self):
        opam = """\
opam-version: "2.0"
maintainer: "dev@example.com"
synopsis: "A package without a url section"
"""
        with pytest.raises(SystemExit):
            parse_url_section(opam)

    def test_empty_string_exits(self):
        with pytest.raises(SystemExit):
            parse_url_section("")

    def test_url_keyword_in_field_not_section_exits(self):
        # A line containing "url" as a field value but no "url { ... }" block.
        opam = 'homepage: "https://example.com"\nbug-reports: "https://example.com/issues"\n'
        with pytest.raises(SystemExit):
            parse_url_section(opam)

    # -- missing src field ---------------------------------------------------

    def test_missing_src_exits(self):
        opam = """\
url {
  checksum: "sha256=aabbccddaabbccddaabbccddaabbccddaabbccddaabbccddaabbccddaabbccdd"
}
"""
        with pytest.raises(SystemExit):
            parse_url_section(opam)

    def test_url_section_entirely_empty_exits(self):
        opam = "url {\n}\n"
        with pytest.raises(SystemExit):
            parse_url_section(opam)

    # -- realistic opam file snippets ----------------------------------------

    def test_realistic_github_archive(self):
        # Modelled on the actual fmt opam file.
        opam = """\
opam-version: "2.0"
name: "fmt"
version: "0.9.0"
synopsis: "OCaml Format pretty-printer combinators"
maintainer: "Daniel Bunzli <daniel.bunzl@erratique.ch>"
license: "ISC"
homepage: "https://erratique.ch/software/fmt"
doc: "https://erratique.ch/software/fmt/doc"
bug-reports: "https://github.com/dbuenzli/fmt/issues"
depends: [
  "ocaml" {>= "4.08.0"}
  "ocamlfind" {build}
  "ocamlbuild" {build}
]
build: [
  ["ocaml" "pkg/pkg.ml" "build" "--dev-pkg" "%{dev}%"]
]
url {
  src: "https://erratique.ch/software/fmt/releases/fmt-0.9.0.tbz"
  checksum: "sha256=5119d2babf3e3b41900c7f1e26b8e53e87f7c4c4e13dba26e4f15e97c75caecb"
}
"""
        src, checksums = parse_url_section(opam)
        assert src == "https://erratique.ch/software/fmt/releases/fmt-0.9.0.tbz"
        assert checksums["sha256"] == "5119d2babf3e3b41900c7f1e26b8e53e87f7c4c4e13dba26e4f15e97c75caecb"

    def test_realistic_github_release(self):
        # Modelled on a package using a GitHub releases tarball.
        opam = """\
opam-version: "2.0"
name: "cmdliner"
version: "1.3.0"
url {
  src:
    "https://github.com/dbuenzli/cmdliner/archive/refs/tags/v1.3.0.tar.gz"
  checksum: [
    "sha256=4c462b0f4d41a11b3680a09a6d85abc8b0e4d80aa81a3bc866f62fea1af84ea1"
    "sha512=e5e789e2e6dfe96c527be3eedabc5d4d940abc5b65e4e1e8ea2da2d64f2e0c3da82d7a7a66b61a3e3e6e2a0f5b6a0e5b27a73e37d2a9c4b3c1b2d0e4a7f1c1e1"
  ]
}
"""
        src, checksums = parse_url_section(opam)
        assert "refs/tags/v1.3.0" in src
        assert "sha256" in checksums
        assert "sha512" in checksums


# ---------------------------------------------------------------------------
# guess_fields
# ---------------------------------------------------------------------------


class TestGuessFields:
    # -- repo name derivation (hyphen to underscore) -------------------------

    def test_repo_name_plain(self):
        repo, _, _ = guess_fields("fmt", "0.9.0", "https://example.com/fmt-0.9.0.tar.gz")
        assert repo == "ocaml_fmt"

    def test_repo_name_hyphen_converted(self):
        repo, _, _ = guess_fields("ocaml-re", "1.11.0", "https://example.com/ocaml-re-1.11.0.tar.gz")
        assert repo == "ocaml_ocaml_re"

    def test_repo_name_multiple_hyphens(self):
        repo, _, _ = guess_fields("ambient-context-eio", "0.1.0", "https://example.com/a.tar.gz")
        assert repo == "ocaml_ambient_context_eio"

    def test_repo_name_already_underscored(self):
        repo, _, _ = guess_fields("ocaml_intrinsics_kernel", "0.2.1", "https://example.com/a.tar.gz")
        assert repo == "ocaml_ocaml_intrinsics_kernel"

    # -- archive type --------------------------------------------------------

    def test_tar_gz_type(self):
        _, _, archive_type = guess_fields("pkg", "1.0", "https://example.com/pkg-1.0.tar.gz")
        assert archive_type == "tar.gz"

    def test_tgz_type(self):
        _, _, archive_type = guess_fields("pkg", "1.0", "https://example.com/pkg-1.0.tgz")
        assert archive_type == "tar.gz"

    def test_tar_bz2_type(self):
        _, _, archive_type = guess_fields("pkg", "1.0", "https://example.com/pkg-1.0.tar.bz2")
        assert archive_type == "tar.bz2"

    def test_tbz_type(self):
        # .tbz is not tar.gz so falls through to tar.bz2
        _, _, archive_type = guess_fields("fmt", "0.9.0", "https://erratique.ch/fmt-0.9.0.tbz")
        assert archive_type == "tar.bz2"

    # -- strip_prefix for plain (non-GitHub) URLs ----------------------------

    def test_strip_prefix_tar_gz(self):
        _, strip, _ = guess_fields("re", "1.11.0", "https://example.com/re-1.11.0.tar.gz")
        assert strip == "re-1.11.0"

    def test_strip_prefix_tar_bz2(self):
        _, strip, _ = guess_fields("fmt", "0.9.0", "https://erratique.ch/fmt-0.9.0.tar.bz2")
        assert strip == "fmt-0.9.0"

    def test_strip_prefix_tgz(self):
        _, strip, _ = guess_fields("pkg", "2.0", "https://example.com/pkg-2.0.tgz")
        assert strip == "pkg-2.0"

    def test_strip_prefix_tbz(self):
        _, strip, _ = guess_fields("fmt", "0.9.0", "https://erratique.ch/fmt-0.9.0.tbz")
        assert strip == "fmt-0.9.0"

    # -- GitHub archive URLs (strip v prefix from tag) -----------------------

    def test_github_archive_strips_v_prefix(self):
        url = "https://github.com/dbuenzli/cmdliner/archive/v1.3.0.tar.gz"
        _, strip, _ = guess_fields("cmdliner", "1.3.0", url)
        assert strip == "cmdliner-1.3.0"

    def test_github_archive_refs_tags_strips_v_prefix(self):
        url = "https://github.com/dbuenzli/fmt/archive/refs/tags/v0.9.0.tar.gz"
        _, strip, _ = guess_fields("fmt", "0.9.0", url)
        assert strip == "fmt-0.9.0"

    def test_github_archive_no_v_prefix_unchanged(self):
        url = "https://github.com/dbuenzli/uutf/archive/1.0.3.tar.gz"
        _, strip, _ = guess_fields("uutf", "1.0.3", url)
        assert strip == "uutf-1.0.3"

    def test_github_archive_repo_name_in_strip(self):
        # strip_prefix uses the GitHub repo name (second path component), not
        # the opam package name.
        url = "https://github.com/mirage/ocaml-base64/archive/v3.5.1.tar.gz"
        _, strip, _ = guess_fields("base64", "3.5.1", url)
        assert strip == "ocaml-base64-3.5.1"

    def test_github_archive_refs_tags_repo_name_in_strip(self):
        url = "https://github.com/ocaml/ocaml-re/archive/refs/tags/v1.11.0.tar.gz"
        _, strip, _ = guess_fields("re", "1.11.0", url)
        assert strip == "ocaml-re-1.11.0"

    def test_github_archive_type_is_tar_gz(self):
        url = "https://github.com/dbuenzli/cmdliner/archive/v1.3.0.tar.gz"
        _, _, archive_type = guess_fields("cmdliner", "1.3.0", url)
        assert archive_type == "tar.gz"

    # -- non-GitHub URL with path components ---------------------------------

    def test_erratique_url_strip_prefix(self):
        url = "https://erratique.ch/software/re/releases/re-1.11.0.tar.gz"
        _, strip, _ = guess_fields("re", "1.11.0", url)
        assert strip == "re-1.11.0"

    def test_url_with_deep_path_uses_only_basename(self):
        url = "https://cdn.example.com/v2/packages/ocaml/lib-1.0.0/lib-1.0.0.tar.bz2"
        _, strip, _ = guess_fields("lib", "1.0.0", url)
        assert strip == "lib-1.0.0"

    # -- combined field values -----------------------------------------------

    def test_returns_three_tuple(self):
        result = guess_fields("re", "1.0.0", "https://example.com/re-1.0.0.tar.gz")
        assert len(result) == 3

    def test_all_fields_consistent_github(self):
        url = "https://github.com/ocaml/ocaml-re/archive/refs/tags/v1.11.0.tar.gz"
        repo, strip, archive_type = guess_fields("ocaml-re", "1.11.0", url)
        assert repo == "ocaml_ocaml_re"
        assert strip == "ocaml-re-1.11.0"
        assert archive_type == "tar.gz"

    def test_all_fields_consistent_erratique(self):
        url = "https://erratique.ch/software/fmt/releases/fmt-0.9.0.tbz"
        repo, strip, archive_type = guess_fields("fmt", "0.9.0", url)
        assert repo == "ocaml_fmt"
        assert strip == "fmt-0.9.0"
        assert archive_type == "tar.bz2"
