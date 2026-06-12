(* Wave D acceptance: parse bash end to end through the pinned semgrep-bash
   grammar (parser.c + C++ scanner), the ocaml-tree-sitter-core runtime, and
   the generated typed CST. *)
let () =
  let res = Tree_sitter_bash.Parse.string "echo hello && ls | wc -l\n" in
  (match res.Tree_sitter_run.Parsing_result.program with
  | Some _cst -> ()
  | None -> failwith "bash parse produced no CST");
  assert (res.Tree_sitter_run.Parsing_result.errors = []);
  print_endline "tree-sitter bash e2e ok"
