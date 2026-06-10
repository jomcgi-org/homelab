(* See parse.mli. Tokenize with Lexer, then consume tokens left to right. *)

let parse (s : string) : Ast.expr =
  let toks = ref (Lexer.tokenize s) in
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
    | Some (Lexer.INT n) ->
        advance ();
        Ast.Int n
    | Some (Lexer.IDENT name) -> (
        advance ();
        match peek () with
        | Some Lexer.LPAREN ->
            advance ();
            let a = args () in
            expect Lexer.RPAREN;
            Ast.Call (name, a)
        | _ -> Ast.Var name)
    | _ -> failwith "toycaml: expected an expression"
  and args () =
    match peek () with
    | Some Lexer.RPAREN -> []
    | _ ->
        let first = expr () in
        let rec rest acc =
          match peek () with
          | Some Lexer.COMMA ->
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
