(* Phase 8 acceptance: a real Go program flows source -> tree-sitter CST
   (the pinned semgrep-go grammar) -> ast_go (Parse_go_tree_sitter) ->
   Semgrep's generic AST (Go_to_generic), the first end-to-end
   source-to-generic-AST pipeline. *)

let contains hay needle =
  let nh = String.length hay and nn = String.length needle in
  let rec go i = i + nn <= nh && (String.sub hay i nn = needle || go (i + 1)) in
  go 0

let () =
  (* Bazel runs tests with CWD at the runfiles root (the hello example's
     data-file convention). *)
  let res =
    Parse_go_tree_sitter.parse
      (Fpath.v "bazel/ocaml/examples/go_generic/hello.go")
  in
  assert (res.Tree_sitter_run.Parsing_result.errors = []);
  let ast =
    match res.Tree_sitter_run.Parsing_result.program with
    | Some ast -> ast
    | None -> failwith "go parse produced no ast_go program"
  in
  let generic = Go_to_generic.program ast in
  assert (generic <> []);
  (* The generic AST's own deriving show (the visitors/deriving rewriter
     set exercised end to end): both function definitions and the call
     must survive the translation. *)
  let printed = AST_generic.show_program generic in
  List.iter
    (fun needle ->
      if not (contains printed needle) then
        failwith (Printf.sprintf "generic AST is missing %S" needle))
    [ "FuncDef"; "main"; "add"; "Println"; "hello" ];
  (* deriving eq on the full program type. *)
  assert (AST_generic.equal_program generic generic);
  print_endline "go -> generic AST e2e ok"
