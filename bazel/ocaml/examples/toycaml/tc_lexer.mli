(* Minimal hand-written tokenizer for the toy expression language: calls,
   integer literals, identifiers, and '$'-prefixed metavariables. This compact
   syntax remains alongside the fetched-grammar JSON input path.
   The identifier shape is validated by pcre2-ocaml against the vendored PCRE2
   C archive, exercising both system-library vendoring and the hand-written
   package override path in the matching engine itself. *)

type token =
  | LPAREN
  | RPAREN
  | COMMA
  | INT of int
  | IDENT of string

val tokenize : string -> token list
