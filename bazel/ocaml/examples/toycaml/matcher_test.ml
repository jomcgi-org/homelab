(* Native ocaml_test: non-zero exit on the first failed check (the Dune (test)
   convention). Exercises the full demonstrator stack end to end -- lexer,
   parser, matcher, and the C stub (via Tc_intern.hash inside the matcher). *)

let check name cond =
  if not cond then (
    Printf.eprintf "FAIL: %s\n" name;
    exit 1)

let matches pat tgt =
  match
    Tc_matcher.match_expr ~pattern:(Tc_parse.parse pat)
      ~target:(Tc_parse.parse tgt)
  with
  | Some _ -> true
  | None -> false

let binding pat tgt name =
  match
    Tc_matcher.match_expr ~pattern:(Tc_parse.parse pat)
      ~target:(Tc_parse.parse tgt)
  with
  | Some b -> List.assoc_opt name b |> Option.map Tc_ast.to_string
  | None -> None

let () =
  (* literal match / mismatch *)
  check "literal match" (matches "foo(1)" "foo(1)");
  check "literal mismatch" (not (matches "foo(1)" "foo(2)"));
  (* arity matters *)
  check "arity mismatch" (not (matches "foo(1)" "foo(1, 2)"));
  (* a metavariable binds to any sub-expression *)
  check "metavar binds call" (matches "foo($X, 2)" "foo(bar(7), 2)");
  check "metavar value"
    (binding "foo($X, 2)" "foo(bar(7), 2)" "$X" = Some "bar(7)");
  (* metavariable consistency on integer bindings *)
  check "metavar consistent int" (matches "eq($X, $X)" "eq(1, 1)");
  check "metavar inconsistent int" (not (matches "eq($X, $X)" "eq(1, 2)"));
  (* metavariable consistency on Call-subtree bindings (structural compare) *)
  check "metavar consistent call" (matches "f($X, $X)" "f(g(1), g(1))");
  check "metavar inconsistent call" (not (matches "f($X, $X)" "f(g(1), g(2))"));
  (* the C stub is deterministic and discriminating *)
  check "intern stable" (Tc_intern.hash "alpha" = Tc_intern.hash "alpha");
  check "intern distinct" (Tc_intern.hash "alpha" <> Tc_intern.hash "beta");
  print_endline "all toycaml tests passed"
