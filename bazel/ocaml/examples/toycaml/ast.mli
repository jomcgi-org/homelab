(* A deliberately tiny "generic AST": integer literals, variables, and calls.
   This mirrors the shape of a real engine's generic AST node type -- the thing
   patterns are matched against -- with none of the language-specific detail. *)

type expr =
  | Int of int
  | Var of string
  | Call of string * expr list

(* Render an expression back to source-like text (for demo output / tests). *)
val to_string : expr -> string
