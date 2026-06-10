(* A pattern is just an AST expression in which a variable whose name begins
   with '$' is a metavariable that binds to any sub-expression. Mirrors the
   engine's "a pattern is code with metavariables" model. *)

type t = Ast.expr

val is_metavar : string -> bool
