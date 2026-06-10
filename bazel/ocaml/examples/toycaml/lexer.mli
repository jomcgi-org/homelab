(* Minimal hand-written tokenizer for the toy expression language: calls,
   integer literals, identifiers, and '$'-prefixed metavariables. A real engine
   lexes with ocamllex (see ADR 005 roadmap); this hand-rolled scanner keeps the
   demonstrator buildable on today's ruleset while standing in for that stage.
   The identifier shape is validated with the `re` opam library, so the fetched-
   from-source dependency path is exercised by the matching engine itself. *)

type token =
  | LPAREN
  | RPAREN
  | COMMA
  | INT of int
  | IDENT of string

val tokenize : string -> token list
