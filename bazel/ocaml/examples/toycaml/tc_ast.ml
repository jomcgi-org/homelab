type expr =
  | Int of int
  | Var of string
  | Call of string * expr list

let rec to_string = function
  | Int n -> string_of_int n
  | Var v -> v
  | Call (f, args) -> f ^ "(" ^ String.concat ", " (List.map to_string args) ^ ")"
