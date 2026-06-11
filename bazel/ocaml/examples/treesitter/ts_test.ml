let () =
  let typ, has_err = Ts_json.parse_root {|{"a": [1, 2, 3], "b": null}|} in
  assert (typ = "document");
  assert (not has_err);
  let _, bad = Ts_json.parse_root "{nope" in
  assert bad;
  print_endline "tree-sitter: ok"
