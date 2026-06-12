(* Phase 8 acceptance: parse Go end to end through the pinned semgrep-go
   grammar (generated parser.c, no external scanner), the
   ocaml-tree-sitter-core runtime, and the generated typed CST. *)
let () =
  let res =
    Tree_sitter_go.Parse.string
      "package main\n\nimport \"fmt\"\n\nfunc main() {\n\tfmt.Println(\"hello\")\n}\n"
  in
  (match res.Tree_sitter_run.Parsing_result.program with
  | Some _cst -> ()
  | None -> failwith "go parse produced no CST");
  assert (res.Tree_sitter_run.Parsing_result.errors = []);
  print_endline "tree-sitter go e2e ok"
