(* Recursive-descent parser: source text -> AST. Grammar:
     expr := INT | IDENT | IDENT '(' [ expr (',' expr)* ] ')'
   A real engine parses with menhir over a tree-sitter CST (see ADR 005
   roadmap); this hand-rolled parser keeps the demonstrator buildable today. *)

val parse : string -> Tc_ast.expr
