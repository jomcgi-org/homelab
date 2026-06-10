(* tOyCaml command line: a tiny "grep for code". Parses a pattern and a target
   expression, then reports whether the pattern matches and what its
   metavariables bind to. The CLI is the thin entry point; the matching engine
   lives in the toycaml_lib library. *)

let run pattern_src target_src =
  let pattern = Parse.parse pattern_src in
  let target = Parse.parse target_src in
  Printf.printf "pattern: %s\n" (Ast.to_string pattern);
  Printf.printf "target:  %s\n" (Ast.to_string target);
  match Matcher.match_expr ~pattern ~target with
  | None -> print_endline "no match"
  | Some bindings ->
      print_endline "match!";
      List.iter
        (fun (k, v) -> Printf.printf "  %s = %s\n" k (Ast.to_string v))
        (List.rev bindings)

let () =
  (* Defaults make the binary self-demonstrating under `bazel run`; positional
     args (pattern, target) override them. *)
  match Array.to_list Sys.argv with
  | _ :: pat :: tgt :: _ -> run pat tgt
  | _ -> run "foo($X, 2)" "foo(bar(7), 2)"
