"""Tests for bazel/ocaml/opam/dune2bazel.py.

Covers tokenize(), parse(), _field(), _resolve_libraries(), gen_library(),
gen_dune_dir(), and main() -- including error/edge-case paths that call
sys.exit().
"""

from __future__ import annotations

import pytest

from bazel.ocaml.opam.dune2bazel import (
    _field,
    _resolve_libraries,
    gen_dune_dir,
    gen_library,
    main,
    parse,
    tokenize,
)

# A representative lib_map (lock.json libs tables composed into labels).
LIB_MAP = {
    "re": "@ocaml_re//:re",
    "sexplib0": "@ocaml_sexplib0//:sexplib0",
    "stdlib-shims": "@ocaml_stdlib_shims//:stdlib_shims",
    "ppxlib": "@ocaml_ppxlib//:ppxlib",
    "ppxlib.metaquot": "@ocaml_ppxlib//:ppxlib_metaquot",
}


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
        toks = [
            "(",
            ("atom", "library"),
            "(",
            ("atom", "name"),
            ("atom", "re"),
            ")",
            ")",
        ]
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
        assert _resolve_libraries([], LIB_MAP) == ([], [])

    def test_stdlib_no_archive_seq_skipped(self):
        assert _resolve_libraries(self._atoms("seq"), LIB_MAP) == ([], [])

    def test_stdlib_no_archive_bytes_skipped(self):
        assert _resolve_libraries(self._atoms("bytes"), LIB_MAP) == ([], [])

    def test_stdlib_archive_unix(self):
        assert _resolve_libraries(self._atoms("unix"), LIB_MAP) == (["unix"], [])

    def test_stdlib_archive_str(self):
        assert _resolve_libraries(self._atoms("str"), LIB_MAP) == (["str"], [])

    def test_compiler_libs_common(self):
        opam, deps = _resolve_libraries(self._atoms("compiler-libs.common"), LIB_MAP)
        assert opam == ["compiler-libs.common"]
        assert deps == []

    def test_lock_package_resolves_to_label(self):
        opam, deps = _resolve_libraries(self._atoms("sexplib0"), LIB_MAP)
        assert opam == []
        assert deps == ["@ocaml_sexplib0//:sexplib0"]

    def test_dotted_sublibrary_resolves(self):
        _, deps = _resolve_libraries(self._atoms("ppxlib.metaquot"), LIB_MAP)
        assert deps == ["@ocaml_ppxlib//:ppxlib_metaquot"]

    def test_mix_everything(self):
        libs = self._atoms("seq", "unix", "sexplib0", "compiler-libs.common")
        opam, deps = _resolve_libraries(libs, LIB_MAP)
        assert opam == ["unix", "compiler-libs.common"]
        assert deps == ["@ocaml_sexplib0//:sexplib0"]

    def test_re_export_collapses_to_dep(self):
        libs = parse(tokenize("((re_export ppxlib) sexplib0)"))[0]
        opam, deps = _resolve_libraries(libs, LIB_MAP)
        assert deps == ["@ocaml_ppxlib//:ppxlib", "@ocaml_sexplib0//:sexplib0"]

    def test_select_form_exits(self):
        libs = parse(tokenize("((select x from (a -> b)))"))[0]
        with pytest.raises(SystemExit):
            _resolve_libraries(libs, LIB_MAP)

    def test_unknown_package_exits(self):
        with pytest.raises(SystemExit):
            _resolve_libraries(self._atoms("zarith"), LIB_MAP)

    def test_unknown_package_after_stdlib_exits(self):
        with pytest.raises(SystemExit):
            _resolve_libraries(self._atoms("unix", "zarith"), LIB_MAP)


# ---------------------------------------------------------------------------
# gen_library
# ---------------------------------------------------------------------------


class TestGenLibrary:
    def _stanza(self, text):
        return parse(tokenize(text))[0]

    def test_minimal_no_opam_deps(self):
        stanza = self._stanza("(library (name mylib))")
        out = gen_library(stanza, "src", LIB_MAP)
        assert 'name = "mylib"' in out
        assert '"src/*.ml"' in out and '"src/*.mli"' in out
        assert 'visibility = ["//visibility:public"]' in out
        assert "opam_deps" not in out
        assert "ocaml_library(" in out

    def test_with_opam_deps(self):
        stanza = self._stanza("(library (name re) (libraries unix str))")
        out = gen_library(stanza, "lib", LIB_MAP)
        assert 'opam_deps = ["unix", "str"]' in out

    def test_with_seq_only_no_opam_deps(self):
        stanza = self._stanza("(library (name mylib) (libraries seq bytes))")
        out = gen_library(stanza, "src", LIB_MAP)
        assert "opam_deps" not in out

    def test_lock_dep_emitted_as_label(self):
        stanza = self._stanza("(library (name mylib) (libraries sexplib0))")
        out = gen_library(stanza, "src", LIB_MAP)
        assert 'deps = ["@ocaml_sexplib0//:sexplib0"]' in out

    def test_src_dir_in_glob(self):
        stanza = self._stanza("(library (name mylib))")
        out = gen_library(stanza, "mysrc", LIB_MAP)
        assert '"mysrc/*.ml"' in out
        assert '"mysrc/*.mli"' in out

    def test_ends_with_newline(self):
        stanza = self._stanza("(library (name mylib))")
        out = gen_library(stanza, "src", LIB_MAP)
        assert out.endswith("\n")

    def test_no_name_field_exits(self):
        stanza = self._stanza('(library (synopsis "A lib"))')
        with pytest.raises(SystemExit):
            gen_library(stanza, "src", LIB_MAP)

    def test_name_falls_back_to_public_name(self):
        # Dune derives the internal name from public_name when name is omitted.
        stanza = self._stanza("(library (public_name glob))")
        out = gen_library(stanza, "src", LIB_MAP)
        assert 'name = "glob"' in out

    def test_dotted_public_name_without_name_exits(self):
        stanza = self._stanza("(library (public_name my.lib))")
        with pytest.raises(SystemExit):
            gen_library(stanza, "src", LIB_MAP)

    def test_unsupported_field_exits(self):
        # 'foreign_stubs' is not in _SUPPORTED_LIBRARY_FIELDS
        stanza = self._stanza("(library (name mylib) (foreign_stubs (language c)))")
        with pytest.raises(SystemExit):
            gen_library(stanza, "src", LIB_MAP)

    def test_public_name_field_accepted(self):
        stanza = self._stanza("(library (name mylib) (public_name my.lib))")
        out = gen_library(stanza, "src", LIB_MAP)
        assert 'name = "mylib"' in out

    def test_synopsis_field_accepted(self):
        stanza = self._stanza('(library (name mylib) (synopsis "A nice lib"))')
        out = gen_library(stanza, "src", LIB_MAP)
        assert 'name = "mylib"' in out

    def test_single_opam_dep(self):
        stanza = self._stanza("(library (name mylib) (libraries unix))")
        out = gen_library(stanza, "src", LIB_MAP)
        assert 'opam_deps = ["unix"]' in out

    def test_multiple_opam_deps_order_preserved(self):
        stanza = self._stanza("(library (name mylib) (libraries unix str dynlink))")
        out = gen_library(stanza, "src", LIB_MAP)
        assert '"unix", "str", "dynlink"' in out

    def test_wrapped_default_true(self):
        # Dune's default is wrapped; no (wrapped ...) field means wrapped.
        stanza = self._stanza("(library (name mylib))")
        out = gen_library(stanza, "src", LIB_MAP)
        assert "wrapped = True," in out

    def test_wrapped_false_omitted(self):
        stanza = self._stanza("(library (name mylib) (wrapped false))")
        out = gen_library(stanza, "src", LIB_MAP)
        assert "wrapped" not in out

    def test_wrapped_true_explicit(self):
        stanza = self._stanza("(library (name mylib) (wrapped true))")
        out = gen_library(stanza, "src", LIB_MAP)
        assert "wrapped = True," in out

    def test_instrumentation_inert(self):
        # (instrumentation (backend bisect_ppx)) is a no-op unless dune runs
        # with --instrument-with; we never instrument, so it must be dropped
        # without rejecting the stanza (lwt's core library carries it).
        stanza = self._stanza(
            "(library (name mylib) (instrumentation (backend bisect_ppx)))"
        )
        out = gen_library(stanza, "src", LIB_MAP)
        assert "bisect" not in out

    def test_no_preprocessing_accepted(self):
        stanza = self._stanza("(library (name mylib) (preprocess no_preprocessing))")
        out = gen_library(stanza, "src", LIB_MAP)
        assert "preprocess" not in out

    def test_future_syntax_accepted_as_noop(self):
        # dune's built-in compat shim is the identity on a 5.3 sysroot
        # (angstrom carries it).
        stanza = self._stanza("(library (name mylib) (preprocess future_syntax))")
        out = gen_library(stanza, "src", LIB_MAP)
        assert "preprocess" not in out

    def test_modules_without_implementation_inert(self):
        # mli-only modules compile naturally (ocamlgraph carries the field).
        stanza = self._stanza(
            "(library (name mylib) (modules_without_implementation sig sig_pack))"
        )
        out = gen_library(stanza, "src", LIB_MAP)
        assert 'name = "mylib"' in out
        assert "sig_pack" not in out

    def test_recursive_glob_for_include_subdirs(self):
        stanza = self._stanza("(library (name mylib))")
        out = gen_library(stanza, "src", LIB_MAP, recursive=True)
        assert '"src/**/*.ml"' in out
        assert '"src/**/*.mly"' in out

    def test_ppx_runtime_libraries_validated_and_dropped(self):
        # The field validates against the lock (runtime linkage stays on the
        # consumer) and emits nothing on the declaring library itself.
        stanza = self._stanza("(library (name mylib) (ppx_runtime_libraries sexplib0))")
        out = gen_library(stanza, "src", LIB_MAP)
        assert "sexplib0" not in out

    def test_ppx_runtime_libraries_unknown_exits(self):
        stanza = self._stanza("(library (name mylib) (ppx_runtime_libraries nope))")
        with pytest.raises(SystemExit):
            gen_library(stanza, "src", LIB_MAP)

    def test_pps_emits_preprocess_runtime_deps(self):
        # Dune adds a rewriter's ppx_runtime_libraries to the consumer; the
        # translator reproduces that from the lock's ppx_runtime tables.
        stanza = self._stanza(
            "(library (name mylib) (preprocess (pps ppxlib.metaquot)))"
        )
        out = gen_library(
            stanza,
            "src",
            LIB_MAP,
            ppx_runtime_map={"ppxlib.metaquot": ["@ocaml_sexplib0//:sexplib0"]},
        )
        assert 'preprocess_runtime_deps = ["@ocaml_sexplib0//:sexplib0"],' in out

    def test_pps_generates_ppx_target(self):
        stanza = self._stanza(
            "(library (name mylib) (preprocess (pps ppxlib.metaquot)))"
        )
        out = gen_library(stanza, "src", LIB_MAP)
        assert 'name = "mylib_ppx"' in out
        assert "ocaml_ppx(" in out
        assert '"@ocaml_ppxlib//:ppxlib_metaquot",' in out
        assert 'preprocess = ":mylib_ppx",' in out

    def test_pps_unknown_rewriter_exits(self):
        stanza = self._stanza("(library (name mylib) (preprocess (pps nope)))")
        with pytest.raises(SystemExit):
            gen_library(stanza, "src", LIB_MAP)

    def test_pps_with_flag_args_exits(self):
        stanza = self._stanza(
            "(library (name mylib) (preprocess (pps ppxlib.metaquot -keep-w32)))"
        )
        with pytest.raises(SystemExit):
            gen_library(stanza, "src", LIB_MAP)

    def test_preprocess_action_form_exits(self):
        stanza = self._stanza(
            "(library (name mylib) (preprocess (action (run pp.exe %{input-file}))))"
        )
        with pytest.raises(SystemExit):
            gen_library(stanza, "src", LIB_MAP)

    def test_preprocessor_deps_single_file(self):
        # (preprocessor_deps (file X)) becomes a preprocess_data attr listing X
        # src_dir-prefixed (the srcs convention) so the ppx pass can read it.
        stanza = self._stanza(
            "(library (name mylib) (preprocessor_deps (file default.semgrepignore)))"
        )
        out = gen_library(stanza, "src", LIB_MAP)
        assert 'preprocess_data = ["src/default.semgrepignore"],' in out

    def test_preprocessor_deps_multiple_files(self):
        stanza = self._stanza(
            "(library (name mylib) (preprocessor_deps (file a.txt) (file b.txt)))"
        )
        out = gen_library(stanza, "d", LIB_MAP)
        assert 'preprocess_data = ["d/a.txt", "d/b.txt"],' in out

    def test_preprocessor_deps_absent_omits_attr(self):
        stanza = self._stanza("(library (name mylib))")
        out = gen_library(stanza, "src", LIB_MAP)
        assert "preprocess_data" not in out

    def test_preprocessor_deps_glob_files_exits(self):
        # Only (file X) is modeled; (glob_files ...) would silently drop a
        # preprocess input, so it rejects loudly.
        stanza = self._stanza(
            "(library (name mylib) (preprocessor_deps (glob_files *.inc)))"
        )
        with pytest.raises(SystemExit):
            gen_library(stanza, "src", LIB_MAP)

    def test_preprocessor_deps_bare_atom_exits(self):
        stanza = self._stanza(
            "(library (name mylib) (preprocessor_deps foo.txt))"
        )
        with pytest.raises(SystemExit):
            gen_library(stanza, "src", LIB_MAP)

    def test_preprocessor_deps_parent_escape_exits(self):
        # A parent-dir escape would land outside the package; reject it.
        stanza = self._stanza(
            "(library (name mylib) (preprocessor_deps (file ../x.txt)))"
        )
        with pytest.raises(SystemExit):
            gen_library(stanza, "src", LIB_MAP)

    def test_kind_ppx_deriver_accepted(self):
        stanza = self._stanza("(library (name mylib) (kind ppx_deriver))")
        out = gen_library(stanza, "src", LIB_MAP)
        assert 'name = "mylib"' in out

    def test_kind_unknown_exits(self):
        stanza = self._stanza("(library (name mylib) (kind c_stubs))")
        with pytest.raises(SystemExit):
            gen_library(stanza, "src", LIB_MAP)

    def test_flags_standard_dropped(self):
        stanza = self._stanza("(library (name mylib) (flags :standard -safe-string))")
        out = gen_library(stanza, "src", LIB_MAP)
        assert 'ocamlopt_flags = ["-safe-string"]' in out

    def test_ocamlopt_flags(self):
        stanza = self._stanza("(library (name mylib) (ocamlopt_flags :standard -O3))")
        out = gen_library(stanza, "src", LIB_MAP)
        assert 'ocamlopt_flags = ["-O3"]' in out

    def test_flags_standard_list_additive(self):
        # (flags (:standard -open Foo)) is additive, same as the flat form.
        stanza = self._stanza("(library (name mylib) (flags (:standard -open Foo)))")
        out = gen_library(stanza, "src", LIB_MAP)
        assert 'ocamlopt_flags = ["-open", "Foo"]' in out

    def test_flags_subtraction_form_exits(self):
        stanza = self._stanza("(library (name mylib) (flags (:standard \\ -w)))")
        with pytest.raises(SystemExit):
            gen_library(stanza, "src", LIB_MAP)


# ---------------------------------------------------------------------------
# gen_dune_dir
# ---------------------------------------------------------------------------


class TestGenDuneDir:
    def _write(self, tmp_path, text):
        d = tmp_path / "src"
        d.mkdir()
        (d / "dune").write_text(text)
        return str(d / "dune")

    def test_single_library(self, tmp_path):
        path = self._write(tmp_path, "(library (name a))\n")
        out = gen_dune_dir(path, "src", LIB_MAP)
        assert 'name = "a"' in out

    def test_multiple_libraries(self, tmp_path):
        path = self._write(tmp_path, "(library (name a))\n(library (name b))\n")
        out = gen_dune_dir(path, "src", LIB_MAP)
        assert 'name = "a"' in out
        assert 'name = "b"' in out

    def test_inert_stanza_skipped(self, tmp_path):
        path = self._write(tmp_path, "(env (dev (flags -w +a)))\n(library (name a))\n")
        out = gen_dune_dir(path, "src", LIB_MAP)
        assert 'name = "a"' in out

    def test_rule_stanza_exits(self, tmp_path):
        path = self._write(
            tmp_path, "(library (name a))\n(rule (targets x.ml) (action (run gen)))\n"
        )
        with pytest.raises(SystemExit):
            gen_dune_dir(path, "src", LIB_MAP)

    # The atdgen codegen rule pair (the one (rule ...) shape modeled; the
    # exact stanzas src/configuring carries, recurring in src/rule and
    # src/parsing).
    ATDGEN_RULES = (
        "(rule\n"
        " (targets Rule_options_j.ml Rule_options_j.mli)\n"
        " (deps    Rule_options.atd)\n"
        " (action  (run atdgen -j -j-strict-fields %{deps})))\n"
        "(rule\n"
        " (targets Rule_options_t.ml Rule_options_t.mli)\n"
        " (deps    Rule_options.atd)\n"
        " (action  (run atdgen -deriving-conv show -t %{deps})))\n"
    )

    def test_atdgen_rule_pair_translates(self, tmp_path):
        path = self._write(tmp_path, "(library (name a))\n" + self.ATDGEN_RULES)
        out = gen_dune_dir(path, "src", LIB_MAP)
        assert 'name = "atdgen_Rule_options_j"' in out
        assert 'name = "atdgen_Rule_options_t"' in out
        assert 'srcs = ["src/Rule_options.atd"]' in out
        assert 'tools = ["@ocaml_atd//:atdgen"]' in out
        # The dune flags pass through verbatim, in a workdir cd'ed run.
        assert "$$AG -j -j-strict-fields Rule_options.atd" in out
        assert "$$AG -deriving-conv show -t Rule_options.atd" in out

    def test_atdgen_outputs_join_library_srcs(self, tmp_path):
        path = self._write(tmp_path, "(library (name a))\n" + self.ATDGEN_RULES)
        out = gen_dune_dir(path, "src", LIB_MAP)
        assert (
            '+ ["Rule_options_j.ml", "Rule_options_j.mli", '
            '"Rule_options_t.ml", "Rule_options_t.mli"],' in out
        )

    def test_atdgen_rule_non_atdgen_tool_exits(self, tmp_path):
        path = self._write(
            tmp_path,
            "(library (name a))\n"
            "(rule (targets X_t.ml) (deps X.atd) (action (run protoc %{deps})))\n",
        )
        with pytest.raises(SystemExit):
            gen_dune_dir(path, "src", LIB_MAP)

    def test_atdgen_rule_unknown_field_exits(self, tmp_path):
        # (mode fallback) etc. change rule semantics; reject loudly.
        path = self._write(
            tmp_path,
            "(library (name a))\n"
            "(rule (targets X_t.ml) (deps X.atd) (mode fallback)\n"
            " (action (run atdgen -t %{deps})))\n",
        )
        with pytest.raises(SystemExit):
            gen_dune_dir(path, "src", LIB_MAP)

    def test_atdgen_rule_non_atd_dep_exits(self, tmp_path):
        path = self._write(
            tmp_path,
            "(library (name a))\n"
            "(rule (targets X_t.ml) (deps X.proto) (action (run atdgen -t %{deps})))\n",
        )
        with pytest.raises(SystemExit):
            gen_dune_dir(path, "src", LIB_MAP)

    def test_atdgen_rule_multiple_deps_exits(self, tmp_path):
        path = self._write(
            tmp_path,
            "(library (name a))\n"
            "(rule (targets X_t.ml) (deps X.atd Y.atd)\n"
            " (action (run atdgen -t %{deps})))\n",
        )
        with pytest.raises(SystemExit):
            gen_dune_dir(path, "src", LIB_MAP)

    def test_atdgen_rule_target_not_from_atd_stem_exits(self, tmp_path):
        # atdgen derives output names from the input basename; a mismatched
        # target means the copy step could never succeed.
        path = self._write(
            tmp_path,
            "(library (name a))\n"
            "(rule (targets Other_t.ml) (deps X.atd) (action (run atdgen -t %{deps})))\n",
        )
        with pytest.raises(SystemExit):
            gen_dune_dir(path, "src", LIB_MAP)

    def test_atdgen_rule_deps_not_last_arg_exits(self, tmp_path):
        path = self._write(
            tmp_path,
            "(library (name a))\n"
            "(rule (targets X_t.ml) (deps X.atd) (action (run atdgen %{deps} -t)))\n",
        )
        with pytest.raises(SystemExit):
            gen_dune_dir(path, "src", LIB_MAP)

    def test_atdgen_rule_unsafe_characters_exit(self, tmp_path):
        # Names and flags embed verbatim in a Starlark string holding a shell
        # command; dune CAN carry e.g. spaces via quoted atoms, and rather
        # than escape for both layers the translator rejects.
        path = self._write(
            tmp_path,
            "(library (name a))\n"
            '(rule (targets "X y_t.ml") (deps "X y.atd")\n'
            " (action (run atdgen -t %{deps})))\n",
        )
        with pytest.raises(SystemExit):
            gen_dune_dir(path, "src", LIB_MAP)

    def test_atdgen_rule_other_dune_variable_exits(self, tmp_path):
        path = self._write(
            tmp_path,
            "(library (name a))\n"
            "(rule (targets X_t.ml) (deps X.atd)\n"
            " (action (run atdgen -o %{targets} %{deps})))\n",
        )
        with pytest.raises(SystemExit):
            gen_dune_dir(path, "src", LIB_MAP)

    def test_atdgen_rule_with_multiple_libraries_exits(self, tmp_path):
        # Without (modules ...) there is no way to know which library the
        # generated sources belong to.
        path = self._write(
            tmp_path,
            "(library (name a))\n(library (name b))\n"
            "(rule (targets X_t.ml) (deps X.atd) (action (run atdgen -t %{deps})))\n",
        )
        with pytest.raises(SystemExit):
            gen_dune_dir(path, "src", LIB_MAP)

    def test_atdgen_rule_without_action_exits(self, tmp_path):
        path = self._write(
            tmp_path,
            "(library (name a))\n(rule (targets X_t.ml) (deps X.atd))\n",
        )
        with pytest.raises(SystemExit):
            gen_dune_dir(path, "src", LIB_MAP)

    def test_executable_stanza_exits(self, tmp_path):
        path = self._write(tmp_path, "(executable (name main))\n")
        with pytest.raises(SystemExit):
            gen_dune_dir(path, "src", LIB_MAP)

    def test_no_library_stanza_exits(self, tmp_path):
        path = self._write(tmp_path, "(env (dev))\n")
        with pytest.raises(SystemExit):
            gen_dune_dir(path, "src", LIB_MAP)

    def test_ocamllex_stanza_inert(self, tmp_path):
        # (ocamllex M) is informational: the .mll is globbed into the library.
        path = self._write(tmp_path, "(library (name a))\n(ocamllex Lexer)\n")
        out = gen_dune_dir(path, "src", LIB_MAP)
        assert 'name = "a"' in out
        assert "menhir" not in out

    def test_ocamlyacc_stanza_inert(self, tmp_path):
        # (ocamlyacc M) is informational like ocamllex: the .mly is globbed in
        # and the driver runs ocamlyacc on non-menhir grammars (ocamlgraph).
        path = self._write(tmp_path, "(library (name a))\n(ocamlyacc Parser)\n")
        out = gen_dune_dir(path, "src", LIB_MAP)
        assert 'name = "a"' in out
        assert "menhir" not in out

    def test_include_subdirs_unqualified_recursive_glob(self, tmp_path):
        path = self._write(
            tmp_path, "(include_subdirs unqualified)\n(library (name a))\n"
        )
        out = gen_dune_dir(path, "src", LIB_MAP)
        assert '"src/**/*.ml"' in out

    def test_include_subdirs_qualified_exits(self, tmp_path):
        # qualified gives each subdir its own module namespace, which the
        # driver's flat stage-by-basename cannot express.
        path = self._write(
            tmp_path, "(include_subdirs qualified)\n(library (name a))\n"
        )
        with pytest.raises(SystemExit):
            gen_dune_dir(path, "src", LIB_MAP)

    def test_menhir_stanza_attaches_to_library(self, tmp_path):
        path = self._write(
            tmp_path,
            "(library (name a))\n"
            "(ocamllex Lexer)\n"
            "(menhir (modules Parser) (flags --unused-tokens --explain))\n",
        )
        out = gen_dune_dir(path, "src", LIB_MAP)
        assert 'menhir = ["Parser"]' in out
        assert 'menhir_flags = ["--unused-tokens", "--explain"]' in out
        assert 'menhir_tool = "@ocaml_menhir//:menhir"' in out

    def test_menhir_without_library_exits(self, tmp_path):
        path = self._write(tmp_path, "(menhir (modules Parser))\n")
        with pytest.raises(SystemExit):
            gen_dune_dir(path, "src", LIB_MAP)

    def test_menhir_with_multiple_libraries_exits(self, tmp_path):
        path = self._write(
            tmp_path,
            "(library (name a))\n(library (name b))\n(menhir (modules Parser))\n",
        )
        with pytest.raises(SystemExit):
            gen_dune_dir(path, "src", LIB_MAP)


# ---------------------------------------------------------------------------
# (modules ...) validate-and-drop
# ---------------------------------------------------------------------------


class TestModulesValidateAndDrop:
    """A (modules ...) list equal to the dir's module set is inert and drops;
    anything else is real module filtering and rejects loudly (base64's
    filtered (modules unsafe base64) stays an opam/overrides/ BUILD)."""

    def _write(self, tmp_path, dune_text, sources):
        d = tmp_path / "src"
        d.mkdir()
        (d / "dune").write_text(dune_text)
        for s in sources:
            (d / s).write_text("")
        return str(d / "dune")

    def test_complete_list_validates_and_drops(self, tmp_path):
        path = self._write(
            tmp_path,
            "(library (name a) (modules Foo Bar))\n",
            ["Foo.ml", "Bar.ml"],
        )
        out = gen_dune_dir(path, "src", LIB_MAP)
        assert 'name = "a"' in out
        assert "modules" not in out
        assert '"src/*.ml"' in out

    def test_mli_only_module_counts(self, tmp_path):
        # An interface-only module is part of the module set (the driver
        # compiles it naturally), so listing it must validate.
        path = self._write(
            tmp_path,
            "(library (name a) (modules Foo Sig))\n",
            ["Foo.ml", "Sig.mli"],
        )
        out = gen_dune_dir(path, "src", LIB_MAP)
        assert 'name = "a"' in out

    def test_lowercase_entries_match_capitalized_files(self, tmp_path):
        # dune normalizes module names; (modules foo) selects Foo.ml and a
        # lowercase source file foo.ml is module Foo.
        path = self._write(
            tmp_path,
            "(library (name a) (modules foo Bar))\n",
            ["Foo.ml", "bar.ml"],
        )
        out = gen_dune_dir(path, "src", LIB_MAP)
        assert 'name = "a"' in out

    def test_mll_module_counts(self, tmp_path):
        # An ocamllex module is globbed into srcs, so it is part of the set.
        path = self._write(
            tmp_path,
            "(library (name a) (modules Foo Lexer))\n(ocamllex Lexer)\n",
            ["Foo.ml", "Lexer.mll"],
        )
        out = gen_dune_dir(path, "src", LIB_MAP)
        assert 'name = "a"' in out

    def test_incomplete_list_exits(self, tmp_path):
        # An unlisted source in the dir means real filtering: reject.
        path = self._write(
            tmp_path,
            "(library (name a) (modules Foo))\n",
            ["Foo.ml", "Hidden.ml"],
        )
        with pytest.raises(SystemExit):
            gen_dune_dir(path, "src", LIB_MAP)

    def test_listed_module_without_source_exits(self, tmp_path):
        # A listed module with no source would be generated by machinery we
        # do not model: reject.
        path = self._write(
            tmp_path,
            "(library (name a) (modules Foo Generated))\n",
            ["Foo.ml"],
        )
        with pytest.raises(SystemExit):
            gen_dune_dir(path, "src", LIB_MAP)

    def test_standard_set_expression_exits(self, tmp_path):
        path = self._write(
            tmp_path,
            "(library (name a) (modules :standard))\n",
            ["Foo.ml"],
        )
        with pytest.raises(SystemExit):
            gen_dune_dir(path, "src", LIB_MAP)

    def test_subtraction_set_expression_exits(self, tmp_path):
        path = self._write(
            tmp_path,
            "(library (name a) (modules (:standard \\ Foo)))\n",
            ["Foo.ml", "Bar.ml"],
        )
        with pytest.raises(SystemExit):
            gen_dune_dir(path, "src", LIB_MAP)

    def test_atdgen_generated_modules_count(self, tmp_path):
        # Sources generated by a translated (rule ...) stanza join the
        # library's srcs, so the listed set must include them to be complete.
        path = self._write(
            tmp_path,
            "(library (name a) (modules Foo X_t))\n"
            "(rule (targets X_t.ml X_t.mli) (deps X.atd)\n"
            " (action (run atdgen -t %{deps})))\n",
            ["Foo.ml", "X.atd"],
        )
        out = gen_dune_dir(path, "src", LIB_MAP)
        assert 'name = "a"' in out

    def test_subdir_modules_count_when_recursive(self, tmp_path):
        # (include_subdirs unqualified) pulls subdir sources into the same
        # namespace; the module set must be computed over the same recursive
        # glob the BUILD emits.
        d = tmp_path / "src"
        sub = d / "sub"
        sub.mkdir(parents=True)
        (d / "dune").write_text(
            "(include_subdirs unqualified)\n(library (name a) (modules Foo))\n"
        )
        (sub / "nested.ml").write_text("")
        (d / "Foo.ml").write_text("")
        with pytest.raises(SystemExit):
            gen_dune_dir(str(d / "dune"), "src", LIB_MAP)
        (d / "dune").write_text(
            "(include_subdirs unqualified)\n(library (name a) (modules Foo nested))\n"
        )
        out = gen_dune_dir(str(d / "dune"), "src", LIB_MAP)
        assert 'name = "a"' in out

    def test_non_source_files_ignored(self, tmp_path):
        # READMEs, .atd specs, scripts &c never join the glob, so they are
        # not part of the module set (semgrep_interfaces is full of them).
        path = self._write(
            tmp_path,
            "(library (name a) (modules Foo))\n",
            ["Foo.ml", "README.md", "spec.atd", "generate.py"],
        )
        out = gen_dune_dir(path, "src", LIB_MAP)
        assert 'name = "a"' in out

    def test_subdirectories_ignored_when_not_recursive(self, tmp_path):
        # A tests/ subdir with its own dune stays out of a non-recursive
        # glob, so its sources are not part of the module set.
        d = tmp_path / "src"
        tests = d / "tests"
        tests.mkdir(parents=True)
        (d / "dune").write_text("(library (name a) (modules Foo))\n")
        (d / "Foo.ml").write_text("")
        (tests / "Test_foo.ml").write_text("")
        out = gen_dune_dir(str(d / "dune"), "src", LIB_MAP)
        assert 'name = "a"' in out


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


class TestMain:
    def _pkg(self, tmp_path, text, sub="src"):
        d = tmp_path / sub
        d.mkdir()
        (d / "dune").write_text(text)
        return tmp_path

    def test_missing_dune_dir_arg_exits(self):
        with pytest.raises(SystemExit):
            main(["dune2bazel.py", "--root", "homelab"])

    def test_reads_dune_file_and_emits_build(self, tmp_path, capsys, monkeypatch):
        pkg = self._pkg(tmp_path, "(library (name re))\n")
        monkeypatch.chdir(pkg)
        main(["dune2bazel.py", "--root", "my_root", "--dune-dir", "src"])
        captured = capsys.readouterr()
        assert 'name = "re"' in captured.out
        assert (
            'load("@my_root//bazel/ocaml:defs.bzl", "ocaml_library", "ocaml_ppx")'
            in captured.out
        )
        assert "# GENERATED by bazel/ocaml/opam/dune2bazel.py" in captured.out

    def test_lib_map_resolution(self, tmp_path, capsys, monkeypatch):
        pkg = self._pkg(tmp_path, "(library (name mylib) (libraries sexplib0))\n")
        monkeypatch.chdir(pkg)
        main(
            [
                "dune2bazel.py",
                "--root",
                "homelab",
                "--lib-map-json",
                '{"sexplib0": "@ocaml_sexplib0//:sexplib0"}',
                "--dune-dir",
                "src",
            ]
        )
        captured = capsys.readouterr()
        assert 'deps = ["@ocaml_sexplib0//:sexplib0"]' in captured.out

    def test_multiple_dune_dirs(self, tmp_path, capsys, monkeypatch):
        a = tmp_path / "a"
        a.mkdir()
        (a / "dune").write_text("(library (name liba))\n")
        b = tmp_path / "b"
        b.mkdir()
        (b / "dune").write_text("(library (name libb))\n")
        monkeypatch.chdir(tmp_path)
        main(
            ["dune2bazel.py", "--root", "homelab", "--dune-dir", "a", "--dune-dir", "b"]
        )
        captured = capsys.readouterr()
        assert 'name = "liba"' in captured.out
        assert 'name = "libb"' in captured.out
        # One load header for the whole BUILD, not one per dune dir.
        assert captured.out.count("load(") == 1

    def test_unknown_library_exits(self, tmp_path, monkeypatch):
        pkg = self._pkg(tmp_path, "(library (name mylib) (libraries zarith))\n")
        monkeypatch.chdir(pkg)
        with pytest.raises(SystemExit):
            main(["dune2bazel.py", "--root", "homelab", "--dune-dir", "src"])
