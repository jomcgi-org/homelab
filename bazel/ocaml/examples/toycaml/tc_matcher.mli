(* Structural matching of a pattern against a target expression, collecting
   metavariable bindings. A metavariable ($X) binds to any sub-expression; if it
   appears more than once it must bind to equal sub-expressions (the engine's
   metavariable-consistency rule). This is the heart of the demonstrator -- the
   piece that most directly mirrors a matching engine. *)

type bindings = (string * Tc_ast.expr) list

val match_expr : pattern:Tc_ast.expr -> target:Tc_ast.expr -> bindings option
