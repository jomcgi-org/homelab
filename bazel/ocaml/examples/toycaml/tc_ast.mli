(* A deliberately tiny "generic AST": integer literals, variables, and calls.
   This mirrors the shape of a real engine's generic AST node type -- the thing
   patterns are matched against -- with none of the language-specific detail.

   The tc_ prefix keeps this unwrapped first-party library's public module
   names stable and collision-free beside the wider opam universe. *)

type expr =
  | Int of int
  | Var of string
  | Call of string * expr list

(* Render an expression back to source-like text (for demo output / tests). *)
val to_string : expr -> string

(* Implemented by the visitors-derived traversal over [expr]. *)
val node_count : expr -> int

(* Implemented by ppx_deriving.show in the same composed ppx pass. *)
val debug_string : expr -> string
