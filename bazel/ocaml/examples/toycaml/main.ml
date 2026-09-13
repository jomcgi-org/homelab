(* tOyCaml command line. Cmdliner comes from a hand-written override because
   the pinned opam package uses b0/topkg Makefiles rather than dune. *)

open Cmdliner

let run pattern_src target_src =
  let pattern = Tc_parse.parse pattern_src in
  let target = Tc_parse.parse target_src in
  Printf.printf "pattern: %s\n" (Tc_ast.to_string pattern);
  Printf.printf "target:  %s\n" (Tc_ast.to_string target);
  match Tc_matcher.match_expr ~pattern ~target with
  | None -> print_endline "no match"
  | Some bindings ->
      print_endline "match!";
      List.iter
        (fun (name, value) ->
          Printf.printf "  %s = %s\n" name (Tc_ast.to_string value))
        (List.rev bindings)

let pattern =
  let doc = "Pattern expression, with optional dollar-prefixed metavariables." in
  Arg.(value & pos 0 string "foo($X, 2)" & info [] ~doc ~docv:"PATTERN")

let target =
  let doc = "Target in compact expression syntax or the generated JSON wire format." in
  Arg.(value & pos 1 string "foo(bar(7), 2)" & info [] ~doc ~docv:"TARGET")

let command =
  let info =
    Cmd.info "toycaml" ~doc:"match a small structural pattern against an expression"
  in
  Cmd.v info Term.(const run $ pattern $ target)

let () = exit (Cmd.eval command)
