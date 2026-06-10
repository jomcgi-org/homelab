"""Tests for bazel/ocaml/opam/dune2bazel.py.

Covers tokenize(), parse(), _field(), _resolve_libraries(), gen_library(),
and main() -- including error/edge-case paths that call sys.exit().
"""

from __future__ import annotations

import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

from bazel.ocaml.opam.dune2bazel import (
    _field,
    _resolve_libraries,
    gen_library,
    main,
    parse,
    tokenize,
)


# ---------------------------------------------------------------------------
# tokenize
# ---------------------------------------------------------------------------


class TestTokenize:
    def test_empty_string(self):
        assert tokenize("") == []

    def test_open_paren(self):
        assert tokenize("(") == ["("]

    def test_close_paren(self):
        assert tokenize(")") == [")"]

    def test_parens_pair(self):
        assert tokenize("()") == ["(", ")"]

    def test_atom(self):
        assert tokenize("library") == [("atom", "library")]

    def test_multiple_atoms(self):
        assert tokenize("name foo") == [("atom", "name"), ("atom", "foo")]

    def test_simple_sexp(self):
        result = tokenize("(library)")
        assert result == ["(", ("atom", "library"), ")"]

    def test_nested_sexp(self):
        result = tokenize("(library (name re))")
        assert result == [
            "(",
            ("atom", "library"),
            "(",
            ("atom", "name"),
            ("atom", "re"),
            ")",
            ")",
        ]

    def test_quoted_string_basic(self):
        result = tokenize('"hello"')
        assert result == [("str", "hello")]

    def test_quoted_string_with_spaces(self):
        result = tokenize('"hello world"')
        assert result == [("str", "hello world")]

    def test_quoted_string_escape_backslash(self):
        # \\ in source -> \ in value
        result = tokenize(r'"a\\b"')
        assert result == [("str", "a\\b")]

    def test_quoted_string_escape_quote(self):
        # \" in source -> " in value
        result = tokenize(r'"say \"hi\""')
        assert result == [("str", 'say "hi"')]

    def test_quoted_string_escape_other(self):
        # \n in source -> n in value (the char after the backslash)
        result = tokenize(r'"foo\nbar"')
        assert result == [("str", "foonbar")]

    def test_line_comment_skipped(self):
        result = tokenize("; this is a comment\n(name foo)")
        assert result == ["(", ("atom", "name"), ("atom", "foo"), ")"]

    def test_comment_mid_line(self):
        result = tokenize("(name ; inline comment\nfoo)")
        assert result == ["(", ("atom", "name"), ("atom", "foo"), ")"]

    def test_whitespace_only(self):
        assert tokenize("   \n\t  ") == []

    def test_atom_stops_at_paren(self):
        result = tokenize("foo(bar)")
        assert result == [("atom", "foo"), "(", ("atom", "bar"), ")"]

    def test_atom_stops_at_double_quote(self):
        result = tokenize('foo"bar"')
        assert result == [("atom", "foo"), ("str", "bar")]

    def test_multiple_top_level_stanzas(self):
        result = tokenize("(a b) (c d)")
        assert result == [
            "(",
            ("atom", "a"),
            ("atom", "b"),
            ")",
            "(",
            ("atom", "c"),
            ("atom", "d"),
            ")",
        ]


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------


class TestParse:
    def test_empty_token_list(self):
        assert parse([]) == []

    def test_single_atom(self):
        result = parse([("atom", "foo")])
        assert result == [("atom", "foo")]

    def test_simple_list(self):
        toks = ["(", ("atom", "a"), ("atom", "b"), ")"]
        assert parse(toks) == [[("atom", "a"), ("atom", "b")]]

    def test_nested_list(self):
        toks = ["(", ("atom", "library"), "(", ("atom", "name"), ("atom", "re"), ")", ")"]
        result = parse(toks)
        assert result == [[("atom", "library"), [("atom", "name"), ("atom", "re")]]]

    def test_multiple_top_level_sexprs(self):
        toks = [
            "(",
            ("atom", "a"),
            ")",
            "(",
            ("atom", "b"),
            ")",
        ]
        result = parse(toks)
        assert result == [[("atom", "a")], [("atom", "b")]]

    def test_string_token_in_list(self):
        toks = ["(", ("str", "hello world"), ")"]
        result = parse(toks)
        assert result == [[("str", "hello world")]]

    def test_unbalanced_close_paren_exits(self):
        with pytest.raises(SystemExit):
            parse([")"])

    def test_roundtrip_via_tokenize(self):
        src = "(library (name re) (libraries seq unix))"
        result = parse(tokenize(src))
        assert result == [
            [
                ("atom", "library"),
                [("atom", "name"), ("atom", "re")],
                [("atom", "libraries"), ("atom", "seq"), ("atom", "unix")],
            ]
        ]


# ---------------------------------------------------------------------------
# _field
# ---------------------------------------------------------------------------


class TestField:
    def _make_stanza(self, text):
        """Parse a single stanza from text, returning the first list."""
        return parse(tokenize(text))[0]

    def test_name_field_present(self):
        stanza = self._make_stanza("(library (name re))")
        result = _field(stanza, "name")
        assert result == [("atom", "re")]

    def test_libraries_field_present(self):
        stanza = self._make_stanza("(library (libraries seq unix))")
        result = _field(stanza, "libraries")
        assert result == [("atom", "seq"), ("atom", "unix")]

    def test_field_absent_returns_none(self):
        stanza = self._make_stanza("(library (name re))")
        assert _field(stanza, "libraries") is None

    def test_synopsis_field(self):
        stanza = self._make_stanza('(library (name re) (synopsis "A lib"))')
        result = _field(stanza, "synopsis")
        assert result == [("str", "A lib")]

    def test_multiple_fields_returns_first_match(self):
        # Only the first matching key is returned.
        stanza = self._make_stanza("(library (name foo) (name bar))")
        result = _field(stanza, "name")
        assert result == [("atom", "foo")]

    def test_empty_stanza_body(self):
        # A stanza with no sub-fields beyond the keyword.
        stanza = self._make_stanza("(library)")
        assert _field(stanza, "name") is None


# ---------------------------------------------------------------------------
# _resolve_libraries
# ---------------------------------------------------------------------------


class TestResolveLibraries:
    def _atoms(self, *names):
        return [("atom", n) for n in names]

    def test_empty_list(self):
        assert _resolve_libraries([]) == []

    def test_stdlib_no_archive_seq_skipped(self):
        assert _resolve_libraries(self._atoms("seq")) == []

    def test_stdlib_no_archive_bytes_skipped(self):
        assert _resolve_libraries(self._atoms("bytes")) == []

    def test_stdlib_no_archive_result_skipped(self):
        assert _resolve_libraries(self._atoms("result")) == []

    def test_stdlib_no_archive_stdlib_skipped(self):
        assert _resolve_libraries(self._atoms("stdlib")) == []

    def test_stdlib_archive_unix(self):
        assert _resolve_libraries(self._atoms("unix")) == ["unix"]

    def test_stdlib_archive_str(self):
        assert _resolve_libraries(self._atoms("str")) == ["str"]

    def test_stdlib_archive_threads(self):
        assert _resolve_libraries(self._atoms("threads")) == ["threads"]

    def test_stdlib_archive_dynlink(self):
        assert _resolve_libraries(self._atoms("dynlink")) == ["dynlink"]

    def test_stdlib_archive_bigarray(self):
        assert _resolve_libraries(self._atoms("bigarray")) == ["bigarray"]

    def test_mix_no_archive_and_archive(self):
        libs = self._atoms("seq", "unix", "bytes", "str")
        assert _resolve_libraries(libs) == ["unix", "str"]

    def test_unsupported_package_exits(self):
        with pytest.raises(SystemExit):
            _resolve_libraries(self._atoms("re"))

    def test_unsupported_package_after_stdlib_exits(self):
        with pytest.raises(SystemExit):
            _resolve_libraries(self._atoms("unix", "zarith"))


# ---------------------------------------------------------------------------
# gen_library
# ---------------------------------------------------------------------------


class TestGenLibrary:
    def _stanza(self, text):
        return parse(tokenize(text))[0]

    def test_minimal_no_opam_deps(self):
        stanza = self._stanza("(library (name mylib))")
        out = gen_library(stanza, "src", "my_root")
        assert 'name = "mylib"' in out
        assert 'srcs = glob(["src/*.ml", "src/*.mli"])' in out
        assert 'load("@my_root//bazel/ocaml:defs.bzl", "ocaml_library")' in out
        assert 'visibility = ["//visibility:public"]' in out
        assert "opam_deps" not in out
        assert "ocaml_library(" in out

    def test_header_comment_present(self):
        stanza = self._stanza("(library (name mylib))")
        out = gen_library(stanza, "src", "root")
        assert "# GENERATED by bazel/ocaml/opam/dune2bazel.py" in out
        assert "do not edit" in out

    def test_with_opam_deps(self):
        stanza = self._stanza("(library (name re) (libraries unix str))")
        out = gen_library(stanza, "lib", "my_root")
        assert 'opam_deps = ["unix", "str"]' in out

    def test_with_seq_only_no_opam_deps(self):
        stanza = self._stanza("(library (name mylib) (libraries seq bytes))")
        out = gen_library(stanza, "src", "root")
        assert "opam_deps" not in out

    def test_src_dir_in_glob(self):
        stanza = self._stanza("(library (name mylib))")
        out = gen_library(stanza, "mysrc", "root")
        assert '"mysrc/*.ml"' in out
        assert '"mysrc/*.mli"' in out

    def test_root_module_in_load(self):
        stanza = self._stanza("(library (name mylib))")
        out = gen_library(stanza, "src", "custom_root")
        assert '"@custom_root//bazel/ocaml:defs.bzl"' in out

    def test_ends_with_newline(self):
        stanza = self._stanza("(library (name mylib))")
        out = gen_library(stanza, "src", "root")
        assert out.endswith("\n")

    def test_no_name_field_exits(self):
        stanza = self._stanza("(library (synopsis \"A lib\"))")
        with pytest.raises(SystemExit):
            gen_library(stanza, "src", "root")

    def test_unsupported_field_exits(self):
        # 'preprocess' is not in _SUPPORTED_LIBRARY_FIELDS
        stanza = self._stanza("(library (name mylib) (preprocess (pps ppx_sexp_conv)))")
        with pytest.raises(SystemExit):
            gen_library(stanza, "src", "root")

    def test_public_name_field_accepted(self):
        stanza = self._stanza("(library (name mylib) (public_name my.lib))")
        out = gen_library(stanza, "src", "root")
        # Should not exit; public_name is in _SUPPORTED_LIBRARY_FIELDS
        assert 'name = "mylib"' in out

    def test_synopsis_field_accepted(self):
        stanza = self._stanza('(library (name mylib) (synopsis "A nice lib"))')
        out = gen_library(stanza, "src", "root")
        assert 'name = "mylib"' in out

    def test_single_opam_dep(self):
        stanza = self._stanza("(library (name mylib) (libraries unix))")
        out = gen_library(stanza, "src", "root")
        assert 'opam_deps = ["unix"]' in out

    def test_multiple_opam_deps_order_preserved(self):
        stanza = self._stanza("(library (name mylib) (libraries unix str dynlink))")
        out = gen_library(stanza, "src", "root")
        assert '"unix", "str", "dynlink"' in out


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


class TestMain:
    def test_wrong_argc_too_few(self):
        with pytest.raises(SystemExit):
            main(["dune2bazel.py"])

    def test_wrong_argc_too_many(self):
        with pytest.raises(SystemExit):
            main(["dune2bazel.py", "a", "b", "c", "d"])

    def test_wrong_argc_exactly_two_args(self):
        with pytest.raises(SystemExit):
            main(["dune2bazel.py", "file", "src"])

    def test_reads_dune_file_and_emits_build(self, tmp_path, capsys):
        dune = tmp_path / "dune"
        dune.write_text("(library (name re))\n")
        main(["dune2bazel.py", str(dune), "lib", "my_root"])
        captured = capsys.readouterr()
        assert 'name = "re"' in captured.out
        assert 'load("@my_root//bazel/ocaml:defs.bzl", "ocaml_library")' in captured.out

    def test_dune_file_with_opam_deps(self, tmp_path, capsys):
        dune = tmp_path / "dune"
        dune.write_text("(library (name mylib) (libraries unix))\n")
        main(["dune2bazel.py", str(dune), "src", "root"])
        captured = capsys.readouterr()
        assert 'opam_deps = ["unix"]' in captured.out

    def test_no_library_stanza_exits(self, tmp_path):
        dune = tmp_path / "dune"
        dune.write_text("(executable (name main))\n")
        with pytest.raises(SystemExit):
            main(["dune2bazel.py", str(dune), "src", "root"])

    def test_multiple_library_stanzas_exits(self, tmp_path):
        dune = tmp_path / "dune"
        dune.write_text("(library (name a))\n(library (name b))\n")
        with pytest.raises(SystemExit):
            main(["dune2bazel.py", str(dune), "src", "root"])

    def test_src_dir_passed_through(self, tmp_path, capsys):
        dune = tmp_path / "dune"
        dune.write_text("(library (name mylib))\n")
        main(["dune2bazel.py", str(dune), "custom_src", "root"])
        captured = capsys.readouterr()
        assert '"custom_src/*.ml"' in captured.out
