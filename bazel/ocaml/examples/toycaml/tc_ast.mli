(* A deliberately tiny "generic AST": integer literals, variables, and calls.
   This mirrors the shape of a real engine's generic AST node type -- the thing
   patterns are matched against -- with none of the language-specific detail.

   Module name is tc_-prefixed (not ast) deliberately: the ruleset has no
   library wrapping yet (ADR 004 Phase 2), and `re` -- built unwrapped -- ships
   a flat `Ast` unit, so an unprefixed `Ast` here would collide at link. The
   prefix is the manual stand-in for dune-style wrapping. *)

type expr =
  | Int of int
  | Var of string
  | Call of string * expr list

(* Render an expression back to source-like text (for demo output / tests). *)
val to_string : expr -> string
