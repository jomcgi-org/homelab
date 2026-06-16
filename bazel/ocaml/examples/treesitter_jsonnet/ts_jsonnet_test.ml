(* Phase 9 wave 7 acceptance: parse jsonnet end to end through the pinned
   semgrep-jsonnet grammar (generated parser.c PLUS the external scanner.c),
   the ocaml-tree-sitter-core runtime, and the generated typed CST. *)
let () =
  let res = Tree_sitter_jsonnet.Parse.string "{ x: 1 }" in
  (match res.Tree_sitter_run.Parsing_result.program with
  | Some _cst -> ()
  | None -> failwith "jsonnet parse produced no CST");
  assert (res.Tree_sitter_run.Parsing_result.errors = []);
  print_endline "tree-sitter jsonnet e2e ok"
