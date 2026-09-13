let check name condition =
  if not condition then (
    Printf.eprintf "FAIL: %s\n" name;
    exit 1)

let raises f =
  try
    f ();
    false
  with _ -> true

let () =
  let wire =
    {|["Call",["foo",[["Call",["bar",[["Int",7]]]], ["Int",2]]]]|}
  in
  let ast = Tc_parse.parse wire in
  check "fetched grammar plus atdgen decode"
    (Tc_ast.to_string ast = "foo(bar(7), 2)");
  check "fetched grammar rejects invalid JSON"
    (raises (fun () -> ignore (Tc_parse.parse {|["Int",]|})));
  check "visitors traversal" (Tc_ast.node_count ast = 4);
  check "composed ppx_deriving"
    (String.length (Tc_ast.debug_string ast) > String.length "Call");
  let unicode_identifier = Pcre2.regexp {|^caml_[[:alpha:]]+$|} in
  check "vendored PCRE2 through non-dune override"
    (Pcre2.pmatch ~rex:unicode_identifier "caml_engine");
  print_endline
    "toycaml capabilities: atdgen grammar visitors ppx pcre2 override ok"
