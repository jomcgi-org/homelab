/* Validate JSON through the fetched tree-sitter runtime and JSON grammar. */
#include <caml/alloc.h>
#include <caml/fail.h>
#include <caml/memory.h>
#include <caml/mlvalues.h>
#include <tree_sitter/api.h>

const TSLanguage *tree_sitter_json(void);

CAMLprim value ml_toycaml_parse_json_root(value src) {
  CAMLparam1(src);
  CAMLlocal1(out);
  TSParser *parser = ts_parser_new();
  if (parser == NULL || !ts_parser_set_language(parser, tree_sitter_json())) {
    if (parser != NULL) {
      ts_parser_delete(parser);
    }
    caml_failwith("toycaml: could not initialize fetched JSON grammar");
  }
  TSTree *tree = ts_parser_parse_string(parser, NULL, String_val(src),
                                        caml_string_length(src));
  if (tree == NULL) {
    ts_parser_delete(parser);
    caml_failwith("toycaml: fetched JSON grammar did not produce a tree");
  }
  TSNode root = ts_tree_root_node(tree);
  out = caml_alloc_tuple(2);
  Store_field(out, 0, caml_copy_string(ts_node_type(root)));
  Store_field(out, 1, Val_bool(ts_node_has_error(root)));
  ts_tree_delete(tree);
  ts_parser_delete(parser);
  CAMLreturn(out);
}
