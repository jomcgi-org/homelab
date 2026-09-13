(* Parse compact source text or a JSON variant array. Compact grammar:
     expr := INT | IDENT | IDENT '(' [ expr (',' expr)* ] ')'
   JSON first passes through the fetched tree-sitter grammar, then through the
   atdgen-generated typed decoder. *)

val parse : string -> Tc_ast.expr
