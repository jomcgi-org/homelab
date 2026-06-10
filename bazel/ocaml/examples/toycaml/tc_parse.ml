(* See tc_parse.mli. Tokenize with Tc_lexer, then consume tokens left to right. *)

let parse (s : string) : Tc_ast.expr =
  let toks = ref (Tc_lexer.tokenize s) in
  let peek () = match !toks with t :: _ -> Some t | [] -> None in
  let advance () =
    match !toks with
    | _ :: rest -> toks := rest
    | [] -> failwith "toycaml: unexpected end of input"
  in
  let expect tok =
    match !toks with
    | t :: rest when t = tok -> toks := rest
    | _ -> failwith "toycaml: unexpected token"
  in
  let rec expr () =
    match peek () with
    | Some (Tc_lexer.INT n) ->
        advance ();
        Tc_ast.Int n
    | Some (Tc_lexer.IDENT name) -> (
        advance ();
        match peek () with
        | Some Tc_lexer.LPAREN ->
            advance ();
            let a = args () in
            expect Tc_lexer.RPAREN;
            Tc_ast.Call (name, a)
        | _ -> Tc_ast.Var name)
    | _ -> failwith "toycaml: expected an expression"
  and args () =
    match peek () with
    | Some Tc_lexer.RPAREN -> []
    | _ ->
        let first = expr () in
        let rec rest acc =
          match peek () with
          | Some Tc_lexer.COMMA ->
              advance ();
              let e = expr () in
              rest (e :: acc)
          | _ -> List.rev acc
        in
        first :: rest []
  in
  let e = expr () in
  (match !toks with
  | [] -> ()
  | _ -> failwith "toycaml: trailing tokens after expression");
  e
